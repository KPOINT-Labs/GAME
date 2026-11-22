"""
Response cache (app/services/response_cache.py) against fakeredis.

``FlakyRedis`` can be switched "down" so the bypass, lost-invalidation and
epoch-compensation paths run without a Redis server.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from app.core.exceptions import ForbiddenError, NotFoundError
from app.services.redis_resilience import RedisCircuitBreaker
from app.services.response_cache import (
    EPOCH,
    GAMES_ALL,
    GAMES_LIST,
    TTL_GAME_DEFINITION,
    GameAccess,
    NullResponseCache,
    RedisResponseCache,
    ResponseCache,
    _entity_type,
    build_response_cache,
    cached_game_read,
    cached_read,
    game_definition_entity,
    response_cache_requests_total,
)

redis_exceptions = pytest.importorskip("redis.exceptions")
fakeredis = pytest.importorskip("fakeredis")

GAME_ID = "4ce32be2-77f6-4ffc-8e07-78dc220f0520"


class FlakyRedis:
    """fakeredis with a switch: ``down`` makes every call fail."""

    def __init__(self):
        self.inner = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.down = False
        self.calls = 0

    def _check(self):
        self.calls += 1
        if self.down:
            raise redis_exceptions.ConnectionError("Connection refused")

    async def get(self, key):
        self._check()
        return await self.inner.get(key)

    async def mget(self, keys):
        self._check()
        return await self.inner.mget(keys)

    async def set(self, key, value, **kwargs):
        self._check()
        return await self.inner.set(key, value, **kwargs)

    def pipeline(self, transaction=True):
        outer = self
        pipe = self.inner.pipeline(transaction=transaction)

        class _Pipe:
            def set(self, *a, **k):
                pipe.set(*a, **k)

            async def execute(self):
                outer._check()
                return await pipe.execute()

        return _Pipe()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _cache(client=None, clock=None, **kwargs):
    client = client or FlakyRedis()
    circuit = RedisCircuitBreaker("response_cache", 5, clock=clock or Clock())
    kwargs.setdefault("ttls", {TTL_GAME_DEFINITION: 600, "list": 300})
    return RedisResponseCache(client, circuit=circuit, **kwargs), client


def _count(namespace, result):
    return response_cache_requests_total.labels(
        namespace=namespace, result=result
    )._value.get()


# --- RedisResponseCache -----------------------------------------------------


async def test_miss_store_hit_round_trip():
    cache, client = _cache()
    first = await cache.lookup("ns", ["a"], {"limit": 10})
    assert first.key is not None and not first.hit

    await cache.store(first, {"x": [1, 2]}, 60)
    second = await cache.lookup("ns", ["a"], {"limit": 10})

    assert second.hit
    assert second.key == first.key
    assert second.value == {"x": [1, 2]}
    assert await client.inner.ttl(first.key) == 60


async def test_params_namespace_and_versions_select_different_entries():
    cache, _ = _cache()
    base = await cache.lookup("ns", ["a"], {"limit": 10})
    other_params = await cache.lookup("ns", ["a"], {"limit": 20})
    other_ns = await cache.lookup("other", ["a"], {"limit": 10})
    other_entity = await cache.lookup("ns", ["b"], {"limit": 10})
    keys = {base.key, other_params.key, other_ns.key, other_entity.key}
    assert len(keys) == 4


async def test_params_key_is_independent_of_dict_order():
    cache, _ = _cache()
    a = await cache.lookup("ns", ["a"], {"x": 1, "y": 2})
    b = await cache.lookup("ns", ["a"], {"y": 2, "x": 1})
    assert a.key == b.key


async def test_invalidate_drops_only_dependent_entries():
    cache, _ = _cache()
    on_a = await cache.lookup("ns", ["a"], None)
    on_b = await cache.lookup("ns", ["b"], None)
    await cache.store(on_a, "A", 60)
    await cache.store(on_b, "B", 60)

    await cache.invalidate("a")

    assert not (await cache.lookup("ns", ["a"], None)).hit
    assert (await cache.lookup("ns", ["b"], None)).value == "B"


async def test_invalidate_refreshes_version_ttl():
    cache, client = _cache(version_ttl_seconds=123)
    await cache.invalidate("a", "a", "")
    assert await client.inner.ttl("game:rc:v:a") == 123
    assert not await client.inner.exists("game:rc:v:")


async def test_invalidate_without_entities_is_a_noop():
    cache, client = _cache()
    await cache.invalidate()
    assert client.calls == 0


async def test_evicted_version_never_matches_an_old_entry():
    tokens = iter(["t1", "t2", "t3", "t4"])
    cache, client = _cache(token_factory=lambda: next(tokens))
    first = await cache.lookup("ns", ["a"], None)  # epoch=t1, a=t2
    await cache.store(first, "old", 60)

    await client.inner.delete("game:rc:v:a")  # evicted / expired

    again = await cache.lookup("ns", ["a"], None)  # a=t3
    assert not again.hit
    assert again.key != first.key


async def test_concurrent_version_creation_agrees_on_one_token():
    tokens = iter(["epoch", "first", "second"])
    cache, client = _cache(token_factory=lambda: next(tokens))
    await client.inner.set("game:rc:v:epoch", "epoch")
    # Another worker creates the version between our MGET and SET NX.
    real_mget = client.mget

    async def racing_mget(keys):
        result = await real_mget(keys)
        await client.inner.set("game:rc:v:a", "theirs", nx=True)
        return result

    client.mget = racing_mget
    lookup = await cache.lookup("ns", ["a"], None)
    client.mget = real_mget
    assert await client.inner.get("game:rc:v:a") == "theirs"
    expected = (await cache.lookup("ns", ["a"], None)).key
    assert lookup.key == expected


async def test_store_skips_bypass_zero_ttl_and_oversize():
    cache, client = _cache(max_entry_bytes=20)
    lookup = await cache.lookup("big", ["a"], None)

    await cache.store(lookup, "x" * 50, 60)
    await cache.store(lookup, "ok", 0)
    assert not (await cache.lookup("big", ["a"], None)).hit

    await cache.store(lookup.__class__(namespace="big", key=None), "ok", 60)
    await cache.store(lookup, "ok", 60)
    assert (await cache.lookup("big", ["a"], None)).value == "ok"


async def test_max_entry_bytes_zero_means_unlimited():
    cache, _ = _cache(max_entry_bytes=0)
    lookup = await cache.lookup("ns", ["a"], None)
    await cache.store(lookup, "x" * 10000, 60)
    assert (await cache.lookup("ns", ["a"], None)).hit


async def test_ttl_groups():
    cache, _ = _cache(ttls={"g": 42})
    assert cache.ttl("g") == 42
    assert cache.ttl("unknown") == 0


async def test_redis_down_bypasses_and_counts():
    clock = Clock()
    cache, client = _cache(clock=clock)
    client.down = True
    before = _count("down_ns", "bypass")

    lookup = await cache.lookup("down_ns", ["a"], None)
    assert lookup.key is None and not lookup.hit

    # Circuit open: no Redis call at all for the cool-down.
    calls = client.calls
    assert (await cache.lookup("down_ns", ["a"], None)).key is None
    await cache.store(lookup, "x", 60)
    assert client.calls == calls
    assert _count("down_ns", "bypass") == before + 2


async def test_store_error_is_swallowed():
    cache, client = _cache()
    lookup = await cache.lookup("ns", ["a"], None)
    client.down = True
    await cache.store(lookup, "x", 60)
    assert not cache._circuit.allow()


async def test_store_skipped_while_circuit_open():
    clock = Clock()
    cache, client = _cache(clock=clock)
    lookup = await cache.lookup("ns", ["a"], None)
    cache._circuit.record_failure(RuntimeError("x"))
    await cache.store(lookup, "x", 60)
    clock.now += 10
    assert not (await cache.lookup("ns", ["a"], None)).hit


async def test_lost_invalidation_changes_epoch_on_recovery():
    clock = Clock()
    cache, client = _cache(clock=clock)
    unrelated = await cache.lookup("ns", ["other"], None)
    await cache.store(unrelated, "cached", 60)

    client.down = True
    await cache.invalidate("a")  # fails: lost
    await cache.invalidate("a")  # circuit open: lost without a call
    assert cache._lost_invalidation

    client.down = False
    clock.now += 10
    # Every entry is dropped, even one that does not depend on "a".
    assert not (await cache.lookup("ns", ["other"], None)).hit
    assert not cache._lost_invalidation


async def test_lost_invalidation_compensated_by_next_invalidate():
    clock = Clock()
    cache, client = _cache(clock=clock)
    entry = await cache.lookup("ns", ["other"], None)
    await cache.store(entry, "cached", 60)
    client.down = True
    await cache.invalidate("a")
    client.down = False
    clock.now += 10

    await cache.invalidate("b")

    assert not cache._lost_invalidation
    assert not (await cache.lookup("ns", ["other"], None)).hit


async def test_compensation_failure_keeps_flag_and_bypasses():
    clock = Clock()
    cache, client = _cache(clock=clock)
    cache._lost_invalidation = True
    client.down = True
    assert (await cache.lookup("ns", ["a"], None)).key is None
    assert cache._lost_invalidation


async def test_invalidating_runs_on_exception():
    cache, _ = _cache()
    entry = await cache.lookup("ns", ["a"], None)
    await cache.store(entry, "cached", 60)

    with pytest.raises(RuntimeError):
        async with cache.invalidating("a"):
            raise RuntimeError("write failed halfway")

    assert not (await cache.lookup("ns", ["a"], None)).hit


def test_entity_helpers():
    assert game_definition_entity(GAME_ID) == f"game:{GAME_ID}:def"
    assert game_definition_entity(GAME_ID.upper()) == f"game:{GAME_ID}:def"
    assert game_definition_entity("not-a-uuid") == "game:not-a-uuid:def"
    assert _entity_type(f"game:{GAME_ID}:def") == "game:def"
    assert _entity_type(GAMES_LIST) == GAMES_LIST
    assert _entity_type(EPOCH) == EPOCH


# --- Null cache and factory ---------------------------------------------------


async def test_null_cache_never_caches():
    cache = NullResponseCache()
    assert isinstance(cache, ResponseCache)
    lookup = await cache.lookup("ns", ["a"], None)
    assert lookup.key is None and not lookup.hit
    await cache.store(lookup, "x", 60)
    await cache.invalidate("a")
    async with cache.invalidating("a"):
        pass
    assert cache.ttl(TTL_GAME_DEFINITION) == 0


def test_build_disabled_or_without_url_is_null(caplog):
    assert isinstance(build_response_cache(False, "redis://x"), NullResponseCache)
    assert isinstance(build_response_cache(True, None), NullResponseCache)
    assert "REDIS_URL is empty" in caplog.text


def test_build_enabled_uses_redis():
    with patch(
        "app.services.rate_limit_counter_backend.build_redis_client_from_url",
        return_value=object(),
    ) as build_client:
        cache = build_response_cache(
            True,
            "redis://redis:6379/0",
            key_prefix="p:",
            socket_timeout_seconds=0.2,
            connect_timeout_seconds=0.3,
            ttls={"g": 5},
        )
    build_client.assert_called_once_with("redis://redis:6379/0", 0.2, 0.3)
    assert isinstance(cache, RedisResponseCache)
    assert cache.ttl("g") == 5
    assert cache._circuit.component == "response_cache"


# --- cached_read / cached_game_read ------------------------------------------


class Out(BaseModel):
    name: str
    n: int


async def test_cached_read_disabled_returns_compute_untouched():
    marker = object()
    params = AsyncMock()
    result = await cached_read(
        NullResponseCache(),
        namespace="ns",
        versions=["a"],
        params=params,  # never evaluated when disabled
        ttl_group="list",
        compute=AsyncMock(return_value=marker),
    )
    assert result is marker
    params.assert_not_called()


async def test_cached_read_hit_and_miss_return_the_same_shape():
    cache, _ = _cache()
    compute = AsyncMock(return_value=Out(name="a", n=1))
    kwargs = dict(
        namespace="ns",
        versions=["a"],
        params=lambda: {"p": 1},
        ttl_group="list",
        compute=compute,
        model=Out,
    )
    miss = await cached_read(cache, **kwargs)
    hit = await cached_read(cache, **kwargs)
    assert miss == hit == Out(name="a", n=1)
    compute.assert_awaited_once()


async def test_cached_read_without_model_returns_json_form():
    cache, _ = _cache()
    result = await cached_read(
        cache,
        namespace="ns",
        versions=["a"],
        params=None,
        ttl_group="list",
        compute=AsyncMock(return_value={"items": [Out(name="a", n=1)]}),
    )
    assert result == {"items": [{"name": "a", "n": 1}]}


def _game(api_key="key-a", oauth=None):
    return SimpleNamespace(id=GAME_ID, apiKey_used=api_key, oauth_user_id=oauth)


async def _game_read(cache, access, load_game, compute, **kwargs):
    return await cached_game_read(
        cache,
        namespace="game",
        game_id=GAME_ID,
        access=access,
        load_game=load_game,
        compute=compute,
        **kwargs,
    )


async def test_cached_game_read_disabled_loads_and_computes():
    load = AsyncMock(return_value=_game())
    compute = AsyncMock(return_value="result")
    result = await _game_read(NullResponseCache(), GameAccess(), load, compute)
    assert result == "result"
    compute.assert_awaited_once_with(load.return_value)


async def test_cached_game_read_hit_skips_database_and_checks_access():
    cache, _ = _cache()
    load = AsyncMock(return_value=_game(api_key="key-a"))
    compute = AsyncMock(return_value=Out(name="g", n=1))
    owner = GameAccess(api_key="key-a", enforce_scope=True)

    first = await _game_read(cache, owner, load, compute, model=Out)
    second = await _game_read(cache, owner, load, compute, model=Out)
    assert first == second == Out(name="g", n=1)
    load.assert_awaited_once()
    compute.assert_awaited_once()

    # Another API key: same 403 as get_authorized_game, without a query.
    with pytest.raises(ForbiddenError) as exc:
        await _game_read(
            cache, GameAccess(api_key="key-b", enforce_scope=True), load, compute
        )
    assert exc.value.detail == "You do not have permission to access this game"

    # Admins and unscoped internal calls share the entry.
    admin = GameAccess(oauth_user_id="admin", is_admin=True, enforce_scope=True)
    assert (await _game_read(cache, admin, load, compute, model=Out)).name == "g"
    unscoped = GameAccess(api_key="key-b")
    assert (await _game_read(cache, unscoped, load, compute, model=Out)).n == 1
    load.assert_awaited_once()


async def test_cached_game_read_oauth_owner():
    cache, _ = _cache()
    load = AsyncMock(return_value=_game(api_key=None, oauth="sub-1"))
    compute = AsyncMock(return_value={"ok": True})
    await _game_read(
        cache, GameAccess(oauth_user_id="sub-1", enforce_scope=True), load, compute
    )
    assert await _game_read(
        cache, GameAccess(oauth_user_id="sub-1", enforce_scope=True), load, compute
    ) == {"ok": True}
    with pytest.raises(ForbiddenError):
        await _game_read(
            cache, GameAccess(oauth_user_id="sub-2", enforce_scope=True), load, compute
        )


async def test_cached_game_read_errors_are_not_cached():
    cache, _ = _cache()
    load = AsyncMock(side_effect=NotFoundError(detail="Game not found"))
    compute = AsyncMock()
    for _ in range(2):
        with pytest.raises(NotFoundError):
            await _game_read(cache, GameAccess(), load, compute)
    assert load.await_count == 2
    compute.assert_not_awaited()


async def test_cached_game_read_invalidated_by_game_and_games_all():
    cache, _ = _cache()
    load = AsyncMock(return_value=_game())
    compute = AsyncMock(return_value={"v": 1})

    await _game_read(cache, GameAccess(), load, compute)
    await cache.invalidate(game_definition_entity(GAME_ID))
    await _game_read(cache, GameAccess(), load, compute)
    await cache.invalidate(GAMES_ALL)
    await _game_read(cache, GameAccess(), load, compute)
    await cache.invalidate(GAMES_LIST)  # unrelated to one game's definition
    await _game_read(cache, GameAccess(), load, compute)

    assert compute.await_count == 3


async def test_cached_game_read_params_select_entries():
    cache, _ = _cache()
    load = AsyncMock(return_value=_game())
    compute = AsyncMock(side_effect=lambda game: {"page": compute.await_count})
    page1 = await _game_read(cache, GameAccess(), load, compute, params={"page": 1})
    page2 = await _game_read(
        cache, GameAccess(), load, compute, params=lambda: {"page": 2}
    )
    assert page1 != page2
    assert (
        await _game_read(cache, GameAccess(), load, compute, params={"page": 1})
        == page1
    )


async def test_cached_game_read_with_redis_down_still_answers():
    cache, client = _cache()
    client.down = True
    load = AsyncMock(return_value=_game())
    compute = AsyncMock(return_value=Out(name="g", n=2))
    result = await _game_read(cache, GameAccess(), load, compute, model=Out)
    assert result == Out(name="g", n=2)


# --- coalescing concurrent misses ---------------------------------------------


async def test_concurrent_misses_share_one_computation():
    import asyncio

    from app.services.response_cache import response_cache_coalesced_total

    cache, _ = _cache()
    release = asyncio.Event()
    load = AsyncMock(return_value=_game())

    async def slow(game):
        await release.wait()
        return {"computed": True}

    compute = AsyncMock(side_effect=slow)
    before = response_cache_coalesced_total.labels(namespace="game")._value.get()
    readers = [
        asyncio.ensure_future(_game_read(cache, GameAccess(), load, compute))
        for _ in range(5)
    ]
    await asyncio.sleep(0.01)
    release.set()
    results = await asyncio.gather(*readers)

    assert results == [{"computed": True}] * 5
    compute.assert_awaited_once()
    assert load.await_count == 5  # every caller still passes its access check
    assert (
        response_cache_coalesced_total.labels(namespace="game")._value.get()
        == before + 4
    )
    assert cache._inflight == {}
    # Stored once, served from the cache afterwards.
    assert await _game_read(cache, GameAccess(), load, compute) == {"computed": True}
    compute.assert_awaited_once()


async def test_coalesced_failure_reaches_every_waiter_and_is_not_kept():
    import asyncio

    cache, _ = _cache()
    release = asyncio.Event()
    load = AsyncMock(return_value=_game())

    async def failing(game):
        await release.wait()
        raise NotFoundError(detail="Task not found")

    compute = AsyncMock(side_effect=failing)
    readers = [
        asyncio.ensure_future(_game_read(cache, GameAccess(), load, compute))
        for _ in range(3)
    ]
    await asyncio.sleep(0.01)
    release.set()
    results = await asyncio.gather(*readers, return_exceptions=True)

    assert all(isinstance(r, NotFoundError) for r in results)
    compute.assert_awaited_once()
    assert cache._inflight == {}
    compute.side_effect = None
    compute.return_value = {"ok": 1}
    assert await _game_read(cache, GameAccess(), load, compute) == {"ok": 1}


async def test_cancelled_waiter_does_not_cancel_shared_computation():
    import asyncio

    cache, _ = _cache()
    release = asyncio.Event()
    load = AsyncMock(return_value=_game())

    async def slow(game):
        await release.wait()
        return {"done": True}

    compute = AsyncMock(side_effect=slow)
    first = asyncio.ensure_future(_game_read(cache, GameAccess(), load, compute))
    second = asyncio.ensure_future(_game_read(cache, GameAccess(), load, compute))
    await asyncio.sleep(0.01)
    first.cancel()  # e.g. the first client disconnected
    await asyncio.sleep(0)
    release.set()

    assert await second == {"done": True}
    assert first.cancelled()
    compute.assert_awaited_once()


async def test_computation_failing_with_no_waiters_left_is_cleaned_up():
    import asyncio

    cache, _ = _cache()
    release = asyncio.Event()
    load = AsyncMock(return_value=_game())

    async def failing(game):
        await release.wait()
        raise RuntimeError("db down")

    reader = asyncio.ensure_future(
        _game_read(cache, GameAccess(), load, AsyncMock(side_effect=failing))
    )
    await asyncio.sleep(0.01)
    reader.cancel()
    await asyncio.sleep(0)
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert cache._inflight == {}


async def test_coalesce_without_key_or_cache_just_runs():
    from app.services.response_cache import CacheLookup

    produce = AsyncMock(return_value=1)
    cache, _ = _cache()
    assert await cache.coalesce(CacheLookup("ns", None), produce) == 1
    assert await NullResponseCache().coalesce(CacheLookup("ns", None), produce) == 1
    assert produce.await_count == 2
    assert cache._inflight == {}


async def test_late_miss_uses_entry_stored_since_its_lookup():
    """A miss whose lookup raced a finishing computation does not recompute."""
    cache, _ = _cache()
    load = AsyncMock(return_value=_game())
    compute = AsyncMock(return_value={"v": 1})
    stale_lookup = await cache.lookup("game", [GAMES_ALL, f"game:{GAME_ID}:def"])
    await _game_read(cache, GameAccess(), load, compute)  # computes and stores
    compute.assert_awaited_once()

    real_lookup = cache.lookup
    cache.lookup = AsyncMock(return_value=stale_lookup)  # looked up too early
    assert await _game_read(cache, GameAccess(), load, compute) == {"v": 1}
    cache.lookup = real_lookup
    compute.assert_awaited_once()


async def test_recheck_failure_still_computes():
    cache, client = _cache()
    load = AsyncMock(return_value=_game())
    compute = AsyncMock(return_value={"v": 2})
    lookup = await cache.lookup("game", [GAMES_ALL, f"game:{GAME_ID}:def"])
    client.down = True
    cache.lookup = AsyncMock(return_value=lookup)
    assert await _game_read(cache, GameAccess(), load, compute) == {"v": 2}
    compute.assert_awaited_once()


def test_encode_keeps_fields_of_subclass_instances():
    """A base-typed field holding a subclass keeps the subclass's fields."""
    from typing import List as _List

    from app.services.response_cache import _encode

    class Base(BaseModel):
        name: str

    class Detailed(Base):
        extra: _List[int]

    class Holder(BaseModel):
        items: _List[Base]

    value = {"holder": Holder(items=[Detailed(name="a", extra=[1, 2])]), "n": (1,)}
    assert _encode(value) == {
        "holder": {"items": [{"name": "a", "extra": [1, 2]}]},
        "n": [1],
    }
