"""
Redis failures degrade gracefully (app/services/redis_resilience.py).

A ``FlakyRedis`` wrapper around fakeredis can be switched "down" (every call
raises ``redis.exceptions.ConnectionError``) or "hung" (``TimeoutError``),
so the fallbacks, the circuit breaker and recovery are exercised end to end
without a Redis server.
"""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.container import Container
from app.services.apikey_cache_backend import (
    InMemoryApiKeyCacheBackend,
    RedisApiKeyCacheBackend,
    build_apikey_cache_backend,
)
from app.services.apikey_service import ApiKeyService, hash_api_key
from app.services.rate_limit_counter_backend import (
    RedisRateLimitCounterBackend,
    build_rate_limit_counter_backend,
)
from app.services.redis_resilience import (
    RedisCircuitBreaker,
    build_redis_client,
    redis_fallback_total,
    redis_unavailable_total,
)

redis_exceptions = pytest.importorskip("redis.exceptions")
fakeredis = pytest.importorskip("fakeredis")


class FlakyRedis:
    """fakeredis with a switch: ``mode`` None (healthy), "down" or "hung"."""

    def __init__(self):
        self.inner = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.mode = None
        self.calls = 0

    def _check(self):
        self.calls += 1
        if self.mode == "down":
            raise redis_exceptions.ConnectionError("Connection refused")
        if self.mode == "hung":
            raise redis_exceptions.TimeoutError("Timeout reading from socket")

    async def get(self, key):
        self._check()
        return await self.inner.get(key)

    async def set(self, key, value, **kwargs):
        self._check()
        return await self.inner.set(key, value, **kwargs)

    async def delete(self, key):
        self._check()
        return await self.inner.delete(key)

    async def scan_iter(self, match=None, count=None):
        self._check()
        async for key in self.inner.scan_iter(match=match, count=count):
            yield key

    def pipeline(self, transaction=True):
        outer = self
        pipe = self.inner.pipeline(transaction=transaction)

        class _Pipe:
            def set(self, *a, **k):
                pipe.set(*a, **k)

            def incr(self, *a, **k):
                pipe.incr(*a, **k)

            async def execute(self):
                outer._check()
                return await pipe.execute()

        return _Pipe()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _metric(counter, component):
    return counter.labels(component=component)._value.get()


def _window():
    return datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


async def _count(backend, scope_value="k-1"):
    return await backend.increment_and_get(
        scope_type="api_key",
        scope_value=scope_value,
        window_name="task_mutation_short_60s",
        window_start=_window(),
        ttl_seconds=65,
    )


# --- circuit breaker ---------------------------------------------------------


def test_circuit_opens_for_the_cooldown_and_logs_once_per_outage(caplog):
    clock = FakeClock()
    circuit = RedisCircuitBreaker("test_circuit", cooldown_seconds=5, clock=clock)
    before = _metric(redis_unavailable_total, "test_circuit")
    caplog.set_level(logging.INFO, logger="app.services.redis_resilience")

    assert circuit.allow() and not circuit.degraded
    circuit.record_failure(ConnectionError("refused"))
    assert not circuit.allow() and circuit.degraded

    clock.now += 4.9
    assert not circuit.allow()
    clock.now += 0.2
    assert circuit.allow()  # cool-down over: probe Redis again

    circuit.record_failure(ConnectionError("still refused"))  # same outage
    assert not circuit.allow()
    assert _metric(redis_unavailable_total, "test_circuit") == before + 1
    warnings = [r for r in caplog.records if "Redis unavailable" in r.message]
    assert len(warnings) == 1

    circuit.record_success()
    assert circuit.allow() and not circuit.degraded
    assert any("available again" in r.message for r in caplog.records)


# --- rate-limit counters -----------------------------------------------------


def _rate_limit_backend(client, clock, fallback_value=41):
    fallback = MagicMock()
    fallback.increment_and_get = AsyncMock(return_value=fallback_value)
    circuit = RedisCircuitBreaker("rate_limit_test", cooldown_seconds=5, clock=clock)
    backend = RedisRateLimitCounterBackend(
        client, key_prefix="t:rl:", fallback=fallback, circuit=circuit
    )
    return backend, fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["down", "hung"])
async def test_rate_limit_counts_in_fallback_while_redis_fails(mode):
    client, clock = FlakyRedis(), FakeClock()
    backend, fallback = _rate_limit_backend(client, clock)
    fallbacks_before = _metric(redis_fallback_total, "rate_limit_test")

    assert await _count(backend) == 1  # healthy: Redis counts

    client.mode = mode
    assert await _count(backend) == 41  # error -> fallback
    calls = client.calls
    assert await _count(backend) == 41  # circuit open -> Redis not tried
    assert client.calls == calls
    assert fallback.increment_and_get.await_count == 2
    fallback.increment_and_get.assert_awaited_with(
        scope_type="api_key",
        scope_value="k-1",
        window_name="task_mutation_short_60s",
        window_start=_window(),
        ttl_seconds=65,
    )
    assert _metric(redis_fallback_total, "rate_limit_test") == fallbacks_before + 2

    client.mode = None  # Redis back; after the cool-down it is used again
    clock.now += 5
    assert await _count(backend) == 2
    assert fallback.increment_and_get.await_count == 2


@pytest.mark.asyncio
async def test_rate_limit_without_fallback_still_raises():
    client = FlakyRedis()
    client.mode = "down"
    backend = RedisRateLimitCounterBackend(client, key_prefix="t:rl:")

    with pytest.raises(redis_exceptions.ConnectionError):
        await _count(backend)


@pytest.mark.asyncio
async def test_rate_limit_programming_errors_are_not_swallowed():
    client = FlakyRedis()
    backend, fallback = _rate_limit_backend(client, FakeClock())
    client.pipeline = MagicMock(side_effect=TypeError("bug"))

    with pytest.raises(TypeError):
        await _count(backend)
    fallback.increment_and_get.assert_not_awaited()


def test_rate_limit_builder_wires_database_fallback_and_circuit():
    pytest.importorskip("redis")
    repository = MagicMock()

    backend = build_rate_limit_counter_backend(
        repository=repository,
        backend_name="redis",
        redis_url="redis://localhost:6379/0",
        redis_key_prefix="game:rl:",
        failure_cooldown_seconds=7,
    )

    assert isinstance(backend, RedisRateLimitCounterBackend)
    assert backend._fallback._repository is repository
    assert backend._circuit.component == "rate_limit"
    assert backend._circuit._cooldown == 7


# --- API key cache -----------------------------------------------------------


def _apikey_backend(client, clock):
    circuit = RedisCircuitBreaker("apikey_cache_test", cooldown_seconds=5, clock=clock)
    fallback = InMemoryApiKeyCacheBackend()
    backend = RedisApiKeyCacheBackend(
        client, key_prefix="t:apikey:", fallback=fallback, circuit=circuit
    )
    return backend, fallback


def _value(active=True):
    return SimpleNamespace(apiKey="gme_live_aaaaaaaa", active=active)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["down", "hung"])
async def test_apikey_cache_uses_process_cache_while_redis_fails(mode):
    client, clock = FlakyRedis(), FakeClock()
    backend, fallback = _apikey_backend(client, clock)

    await backend.set("h1", _value(), ttl_seconds=30)
    assert (await backend.get("h1")).apiKey == "gme_live_aaaaaaaa"
    assert await fallback.get("h1") is None  # healthy: Redis only

    client.mode = mode
    assert await backend.get("h1") is None  # miss -> caller reads the DB
    await backend.set("h1", _value(), ttl_seconds=30)  # stored in fallback
    calls = client.calls
    assert (await backend.get("h1")).active is True
    assert client.calls == calls  # circuit open: Redis not tried

    client.mode = None
    clock.now += 5
    assert (await backend.get("h1")).apiKey == "gme_live_aaaaaaaa"  # Redis again
    assert client.calls == calls + 1


@pytest.mark.asyncio
async def test_apikey_revoke_during_outage_evicts_process_cache_and_does_not_raise(
    caplog,
):
    client, clock = FlakyRedis(), FakeClock()
    backend, fallback = _apikey_backend(client, clock)
    await fallback.set("h1", _value(), ttl_seconds=30)
    client.mode = "down"

    await backend.delete("h1")  # must not raise

    assert await fallback.get("h1") is None
    assert any("Could not evict a revoked API key" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_apikey_delete_also_evicts_process_cache_when_redis_is_healthy():
    client, clock = FlakyRedis(), FakeClock()
    backend, fallback = _apikey_backend(client, clock)
    await fallback.set("h1", _value(), ttl_seconds=30)  # left from an outage
    await backend.set("h1", _value(), ttl_seconds=30)

    await backend.delete("h1")

    assert await fallback.get("h1") is None
    assert await client.inner.get("t:apikey:h1") is None


@pytest.mark.asyncio
async def test_apikey_clear_survives_redis_outage():
    client, clock = FlakyRedis(), FakeClock()
    backend, fallback = _apikey_backend(client, clock)
    await fallback.set("h1", _value(), ttl_seconds=30)
    client.mode = "down"

    await backend.clear()

    assert await fallback.get("h1") is None


@pytest.mark.asyncio
async def test_apikey_cache_without_fallback_still_raises():
    client = FlakyRedis()
    client.mode = "down"
    backend = RedisApiKeyCacheBackend(client, key_prefix="t:apikey:")

    with pytest.raises(redis_exceptions.ConnectionError):
        await backend.get("h1")


@pytest.mark.asyncio
async def test_api_key_authentication_works_while_redis_is_down(monkeypatch):
    """End to end through get_api_key_header: no 500, one DB read, then cached."""
    client, clock = FlakyRedis(), FakeClock()
    backend, _ = _apikey_backend(client, clock)
    client.mode = "down"
    repository = AsyncMock()
    repository.read_by_column.return_value = MagicMock(
        apiKey="gme_live_aaaaaaaa", active=True
    )
    monkeypatch.setattr(
        "app.services.apikey_service.configs.API_KEY_HEADER_CACHE_TTL_SECONDS", 5
    )

    with patch.object(
        Container, "apikey_cache_backend", MagicMock(return_value=backend)
    ), patch.object(Container, "apikey_repository", MagicMock(return_value=repository)):
        first = await ApiKeyService.get_api_key_header("plain-key")
        second = await ApiKeyService.get_api_key_header("plain-key")

    assert first.sucess and second.sucess
    assert second.data.apiKey == "gme_live_aaaaaaaa"
    repository.read_by_column.assert_awaited_once_with(
        "apiKeyHash", hash_api_key("plain-key"), not_found_raise_exception=False
    )


def test_apikey_builder_wires_process_cache_fallback_and_circuit():
    pytest.importorskip("redis")

    backend = build_apikey_cache_backend(
        backend_name="redis",
        redis_url="redis://localhost:6379/0",
        redis_key_prefix="game:apikey:",
    )

    assert isinstance(backend, RedisApiKeyCacheBackend)
    assert isinstance(backend._fallback, InMemoryApiKeyCacheBackend)
    assert backend._circuit.component == "apikey_cache"


# --- client ------------------------------------------------------------------


def test_client_has_short_timeouts_and_no_retries():
    client = build_redis_client("redis://localhost:6379/0", 0.25, 0.3)
    conn = client.connection_pool.make_connection()

    assert conn.socket_timeout == 0.25
    assert conn.socket_connect_timeout == 0.3
    assert conn.retry.get_retries() == 0


@pytest.mark.asyncio
async def test_hung_redis_server_fails_within_the_timeout():
    """A server that accepts but never answers: the call gives up quickly."""

    async def never_answer(reader, writer):
        await asyncio.sleep(3600)

    server = await asyncio.start_server(never_answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = build_redis_client(f"redis://127.0.0.1:{port}/0", 0.2, 0.2)
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(redis_exceptions.TimeoutError):
            await client.get("k")
        assert loop.time() - start < 1.0
    finally:
        await client.aclose()
        server.close()
