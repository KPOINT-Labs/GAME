"""
Pluggable cache backends for the API key header lookup.

The in-memory backend preserves the original per-process dict (zero
dependency, fast, but one cache per gunicorn worker -- revocations only
reach the worker that handled the request and other workers serve the
cached value until TTL expires). The Redis backend stores entries in a
single shared keyspace so every worker observes revocations on the next
request.

Selection is driven by ``configs.APIKEY_CACHE_BACKEND`` ("memory" or
"redis") and wired in ``app/core/container.py``.

When Redis is unavailable at runtime the Redis backend uses a per-process
in-memory cache instead (exactly the "memory" backend's behaviour), so API
key authentication keeps working; see ``app/services/redis_resilience.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from types import SimpleNamespace
from typing import Optional, Protocol, runtime_checkable

from app.services.rate_limit_counter_backend import build_redis_client_from_url
from app.services.redis_resilience import REDIS_UNAVAILABLE_ERRORS, RedisCircuitBreaker

logger = logging.getLogger(__name__)


@runtime_checkable
class ApiKeyCacheBackend(Protocol):
    """Cache the resolved ``(apiKey, active)`` tuple keyed by sha256 hash."""

    async def get(self, cache_key: str) -> Optional[SimpleNamespace]:
        """Return the cached value for ``cache_key``, or ``None`` if absent."""
        ...

    async def set(
        self, cache_key: str, value: SimpleNamespace, ttl_seconds: int
    ) -> None:
        """Store ``value`` under ``cache_key`` with a ``ttl_seconds`` lifetime."""
        ...

    async def delete(self, cache_key: str) -> None:
        """Remove the entry for ``cache_key`` if present."""
        ...

    async def clear(self) -> None:
        """Remove every cached entry."""
        ...


class InMemoryApiKeyCacheBackend:
    """
    Per-process dict cache with monotonic TTL and lazy eviction.

    The asyncio lock is lazy-bound on first use and reset by ``sync_clear``
    so tests that swap event loops between cases don't trip "attached to a
    different loop". This mirrors the behavior the cache had when it lived
    on ``ApiKeyService`` directly.
    """

    def __init__(self) -> None:
        self._store: dict = {}
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        """
        Return the asyncio lock, binding it to the current loop on first use.

        Returns:
            asyncio.Lock: The lazily-created per-instance lock.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def get(self, cache_key: str) -> Optional[SimpleNamespace]:
        """
        Return the cached value for ``cache_key``, evicting it if expired.

        Args:
            cache_key (str): The lookup key (sha256 of the API key plaintext).

        Returns:
            Optional[SimpleNamespace]: The cached ``(apiKey, active)`` value,
            or ``None`` if absent or past its TTL.
        """
        now = monotonic()
        async with self._get_lock():
            entry = self._store.get(cache_key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._store.pop(cache_key, None)
                return None
            return value

    async def set(
        self, cache_key: str, value: SimpleNamespace, ttl_seconds: int
    ) -> None:
        """
        Cache ``value`` under ``cache_key`` for ``ttl_seconds`` (no-op if ≤ 0).

        Args:
            cache_key (str): The lookup key.
            value (SimpleNamespace): The ``(apiKey, active)`` value to store.
            ttl_seconds (int): Lifetime in seconds; values ≤ 0 are ignored.
        """
        if ttl_seconds <= 0:
            return
        expires_at = monotonic() + ttl_seconds
        async with self._get_lock():
            self._store[cache_key] = (expires_at, value)

    async def delete(self, cache_key: str) -> None:
        """
        Remove the cached entry for ``cache_key`` if present.

        Args:
            cache_key (str): The lookup key to evict.
        """
        async with self._get_lock():
            self._store.pop(cache_key, None)

    async def clear(self) -> None:
        """Remove every cached entry (async wrapper over ``sync_clear``)."""
        self.sync_clear()

    def sync_clear(self) -> None:
        """Clear the store synchronously and reset the lock for a new loop."""
        # dict.clear() is atomic under the GIL; resetting the lock lets the
        # next async caller bind it to the current event loop.
        self._store.clear()
        self._lock = None


class RedisApiKeyCacheBackend:
    """
    Redis-backed cache shared across workers. Values are stored as compact
    JSON (``{"apiKey": <prefix>, "active": <bool>}``) with TTL set via the
    ``EX`` argument on ``SET`` so Redis handles expiration server-side.

    While Redis is unavailable (an error, or an open ``circuit``) reads and
    writes go to ``fallback``, a per-process cache: authentication keeps
    working with the "memory" backend's consistency (a revocation reaches
    other workers within the TTL). Deletes always reach the fallback too, so
    a revoked key is never served from it.
    """

    def __init__(
        self,
        client,
        key_prefix: str = "game:apikey:",
        fallback: Optional["ApiKeyCacheBackend"] = None,
        circuit: Optional[RedisCircuitBreaker] = None,
    ) -> None:
        self._client = client
        self._key_prefix = key_prefix
        self._fallback = fallback
        self._circuit = circuit

    def _build_key(self, cache_key: str) -> str:
        """
        Namespace a cache key with the configured Redis prefix.

        Args:
            cache_key (str): The bare lookup key.

        Returns:
            str: The prefixed Redis key.
        """
        return f"{self._key_prefix}{cache_key}"

    def _redis_allowed(self) -> bool:
        """Whether to try Redis (always, without a fallback to use instead)."""
        return self._fallback is None or self._circuit is None or self._circuit.allow()

    def _failed(self, exc: BaseException) -> None:
        """Record a Redis failure; re-raise when there is no fallback."""
        if self._circuit:
            self._circuit.record_failure(exc)
        if self._fallback is None:
            raise exc

    def _succeeded(self) -> None:
        if self._circuit:
            self._circuit.record_success()

    def _count_fallback(self) -> None:
        if self._circuit:
            self._circuit.count_fallback()

    async def get(self, cache_key: str) -> Optional[SimpleNamespace]:
        """
        Read and deserialize a cached value from Redis.

        Args:
            cache_key (str): The lookup key.

        Returns:
            Optional[SimpleNamespace]: The decoded ``(apiKey, active)`` value,
            or ``None`` if the key is absent.
        """
        if self._redis_allowed():
            try:
                raw = await self._client.get(self._build_key(cache_key))
            except REDIS_UNAVAILABLE_ERRORS as exc:
                self._failed(exc)
            else:
                self._succeeded()
                if raw is None:
                    return None
                payload = json.loads(raw)
                return SimpleNamespace(
                    apiKey=payload.get("apiKey"), active=payload.get("active")
                )
        self._count_fallback()
        return await self._fallback.get(cache_key)

    async def set(
        self, cache_key: str, value: SimpleNamespace, ttl_seconds: int
    ) -> None:
        """
        Store ``value`` as JSON in Redis with a server-side TTL (no-op if ≤ 0).

        Args:
            cache_key (str): The lookup key.
            value (SimpleNamespace): The ``(apiKey, active)`` value to store.
            ttl_seconds (int): Expiry passed to Redis ``SET ... EX``.
        """
        if ttl_seconds <= 0:
            return
        if self._redis_allowed():
            payload = json.dumps({"apiKey": value.apiKey, "active": value.active})
            try:
                await self._client.set(
                    self._build_key(cache_key), payload, ex=max(1, int(ttl_seconds))
                )
            except REDIS_UNAVAILABLE_ERRORS as exc:
                self._failed(exc)
            else:
                self._succeeded()
                return
        await self._fallback.set(cache_key, value, ttl_seconds)

    async def delete(self, cache_key: str) -> None:
        """
        Delete a single cached entry from Redis (and from the fallback).

        If Redis is unavailable the Redis entry, if any, expires on its own
        within the TTL.

        Args:
            cache_key (str): The lookup key to evict.
        """
        if self._fallback is not None:
            await self._fallback.delete(cache_key)
        if self._redis_allowed():
            try:
                await self._client.delete(self._build_key(cache_key))
            except REDIS_UNAVAILABLE_ERRORS as exc:
                self._failed(exc)
                logger.warning(
                    "Could not evict a revoked API key from Redis; the cached "
                    "entry expires within API_KEY_HEADER_CACHE_TTL_SECONDS."
                )
            else:
                self._succeeded()

    async def clear(self) -> None:
        """Delete every entry under the key prefix via a bounded SCAN + DEL."""
        # SCAN + DEL is bounded and non-blocking for other clients; only
        # called from admin/maintenance paths, never the request hot path.
        if self._fallback is not None:
            await self._fallback.clear()
        pattern = f"{self._key_prefix}*"
        try:
            async for key in self._client.scan_iter(match=pattern, count=500):
                await self._client.delete(key)
        except REDIS_UNAVAILABLE_ERRORS as exc:
            self._failed(exc)


def build_apikey_cache_backend(
    backend_name: str,
    redis_url: Optional[str],
    redis_key_prefix: str,
    socket_timeout_seconds: float = 0.5,
    connect_timeout_seconds: float = 0.5,
    failure_cooldown_seconds: float = 5.0,
) -> ApiKeyCacheBackend:
    """
    Select the configured backend. Falls back to the in-memory backend with
    a warning when Redis is requested but ``REDIS_URL`` is missing -- so a
    misconfigured deploy still authenticates (just with the per-process
    consistency caveat) instead of failing requests outright. The Redis
    backend also uses a per-process cache while Redis is unavailable.
    """
    normalized = (backend_name or "memory").strip().lower()
    if normalized == "redis":
        if not redis_url:
            logger.warning(
                "APIKEY_CACHE_BACKEND=redis but REDIS_URL is empty; "
                "falling back to the in-process API key cache."
            )
            return InMemoryApiKeyCacheBackend()
        client = build_redis_client_from_url(
            redis_url, socket_timeout_seconds, connect_timeout_seconds
        )
        return RedisApiKeyCacheBackend(
            client,
            key_prefix=redis_key_prefix,
            fallback=InMemoryApiKeyCacheBackend(),
            circuit=RedisCircuitBreaker("apikey_cache", failure_cooldown_seconds),
        )
    return InMemoryApiKeyCacheBackend()
