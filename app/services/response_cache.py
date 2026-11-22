"""
Shared response cache in Redis, invalidated through per-entity versions.

Read endpoints cache their result in Redis. Each cache key embeds the
current *version* of every entity the result depends on (a game's
definition, the list of games, ...), so a write invalidates every cached
result that depends on an entity by replacing that entity's version
(one ``SET``) - no key scans, no tag sets. Superseded entries are never
read again and expire on their own TTL.

Versions are random tokens, not counters: a version key that expires or is
evicted is re-created with a fresh token, which can never match an entry
cached under an earlier token (a counter restarting at 1 could).

Correctness relies on one ordering rule: **change versions after the write
is committed** (``invalidate`` / ``invalidating``). A reader that loads the
version before the change caches under the old version, which nobody reads
afterwards; a reader that loads it after the change also reads the
committed data.

Redis is optional at runtime (see ``app/services/redis_resilience.py``):
while it is unavailable every read bypasses the cache and is computed from
the database. There is deliberately no per-process fallback cache - other
workers could not invalidate it. Version changes that could not be written
while Redis was down are compensated by changing the global ``epoch`` on
the next successful Redis call, which drops every cached entry.

Disabled (``RESPONSE_CACHE_ENABLED=false``, the default) the services get a
:class:`NullResponseCache` and behave exactly as without caching.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import time_ns
from types import SimpleNamespace
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Type,
    TypeVar,
    Union,
    runtime_checkable,
)
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from prometheus_client import Counter
from pydantic import BaseModel

from app.core.exceptions import ForbiddenError
from app.services.game_access import GAME_FORBIDDEN_DETAIL, can_access_game
from app.services.redis_resilience import REDIS_UNAVAILABLE_ERRORS, RedisCircuitBreaker

logger = logging.getLogger(__name__)

response_cache_requests_total = Counter(
    "response_cache_requests_total",
    "Response cache lookups by result: hit, miss, or bypass (Redis "
    "unavailable, the read is computed from the database).",
    labelnames=("namespace", "result"),
)

response_cache_oversize_total = Counter(
    "response_cache_oversize_total",
    "Results not cached because they exceed RESPONSE_CACHE_MAX_ENTRY_BYTES.",
    labelnames=("namespace",),
)

response_cache_invalidations_total = Counter(
    "response_cache_invalidations_total",
    "Entity versions changed by writes, by entity type.",
    labelnames=("entity",),
)

response_cache_lost_invalidations_total = Counter(
    "response_cache_lost_invalidations_total",
    "Version changes that could not reach Redis; compensated by changing the "
    "global epoch once Redis is reachable again.",
)

# TTL groups; values come from RESPONSE_CACHE_*_TTL_SECONDS.
TTL_GAME_DEFINITION = "game_definition"
TTL_GAMES_LIST = "games_list"

# --- Entities ---------------------------------------------------------------
# An entity is a string naming something whose version cached results embed.

EPOCH = "epoch"
# Every game's definition at once; changed by bulk rewrites that span games
# (custom strategy rollback re-pointing Games/Tasks.strategyId).
GAMES_ALL = "games:all"
# The set of games and their top-level fields (GET /games).
GAMES_LIST = "games:list"


def game_definition_entity(game_id: Any) -> str:
    """
    Entity for one game's definition: the game row, its params, its strategy
    assignment, its tasks and their params.

    Args:
        game_id: Internal game id (UUID or its string form).

    Returns:
        str: ``game:<uuid>:def``.
    """
    return f"game:{_normalize_id(game_id)}:def"


def _normalize_id(value: Any) -> str:
    """Canonical string form of an id, so UUID and str callers agree."""
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return str(value)


def _entity_type(entity: str) -> str:
    """Low-cardinality metric label for an entity (``game:<id>:def`` -> ``game:def``)."""
    parts = entity.split(":")
    if len(parts) == 3:
        return f"{parts[0]}:{parts[2]}"
    return entity


def _new_token() -> str:
    """A version token that never repeats (time + randomness)."""
    return f"{time_ns():x}{secrets.token_hex(4)}"


_MISSING = object()


@dataclass(frozen=True)
class CacheLookup:
    """
    Result of :meth:`ResponseCache.lookup`.

    ``key`` is ``None`` when the cache was bypassed (disabled or Redis
    unavailable); ``store`` is then a no-op.
    """

    namespace: str
    key: Optional[str]
    value: Any = _MISSING

    @property
    def hit(self) -> bool:
        """Whether a cached value was found."""
        return self.value is not _MISSING


@runtime_checkable
class ResponseCache(Protocol):
    """Cache of JSON-compatible read results with version invalidation."""

    enabled: bool

    def ttl(self, group: str) -> int:
        """TTL in seconds for a TTL group (``TTL_*``)."""
        ...

    async def lookup(
        self,
        namespace: str,
        versions: Sequence[str],
        params: Optional[Mapping[str, Any]] = None,
    ) -> CacheLookup:
        """Find the entry for ``namespace`` + current entity versions + params."""
        ...

    async def store(self, lookup: CacheLookup, value: Any, ttl_seconds: int) -> None:
        """Store a JSON-compatible ``value`` under ``lookup.key``."""
        ...

    async def invalidate(self, *entities: str) -> None:
        """Change the versions of ``entities``. Call after the write commits."""
        ...

    def invalidating(self, *entities: str) -> Any:
        """Async context manager that invalidates ``entities`` on exit."""
        ...


class _InvalidatingMixin:
    """``invalidating()`` on top of ``invalidate()``."""

    async def invalidate(self, *entities: str) -> None:  # pragma: no cover
        raise NotImplementedError

    @asynccontextmanager
    async def invalidating(self, *entities: str) -> AsyncIterator[None]:
        """
        Invalidate ``entities`` when the block exits, also on an exception:
        a write that failed halfway may still have committed some rows, and
        an unneeded invalidation only costs a cache miss.
        """
        try:
            yield
        finally:
            await self.invalidate(*entities)


class NullResponseCache(_InvalidatingMixin):
    """Caching disabled: every lookup misses, nothing is stored."""

    enabled = False

    def ttl(self, group: str) -> int:
        """No caching, so no TTL."""
        return 0

    async def lookup(
        self,
        namespace: str,
        versions: Sequence[str],
        params: Optional[Mapping[str, Any]] = None,
    ) -> CacheLookup:
        """Always a bypass."""
        return CacheLookup(namespace=namespace, key=None)

    async def store(self, lookup: CacheLookup, value: Any, ttl_seconds: int) -> None:
        """No-op."""

    async def invalidate(self, *entities: str) -> None:
        """No-op."""


class RedisResponseCache(_InvalidatingMixin):
    """
    Response cache in Redis.

    Keys (all under ``key_prefix``):

    * ``v:<entity>``: the entity's current version token (TTL
      ``version_ttl_seconds``, refreshed when it changes).
    * ``e:<namespace>:<sha256>``: a cached result; the hash covers the
      namespace, the epoch and entity versions, and the request params.
    """

    enabled = True

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "game:rc:",
        circuit: Optional[RedisCircuitBreaker] = None,
        version_ttl_seconds: int = 172800,
        max_entry_bytes: int = 262144,
        ttls: Optional[Mapping[str, int]] = None,
        token_factory: Callable[[], str] = _new_token,
    ) -> None:
        self._client = client
        self._prefix = key_prefix
        self._circuit = circuit or RedisCircuitBreaker("response_cache", 5)
        self._version_ttl = max(1, int(version_ttl_seconds))
        self._max_entry_bytes = int(max_entry_bytes)
        self._ttls = dict(ttls or {})
        self._new_token = token_factory
        # Set when a version change could not be written; the next
        # successful Redis call changes the epoch to drop possibly stale
        # entries.
        self._lost_invalidation = False

    def ttl(self, group: str) -> int:
        """TTL in seconds for a TTL group; 0 (do not store) if unknown."""
        return int(self._ttls.get(group, 0))

    def _version_key(self, entity: str) -> str:
        return f"{self._prefix}v:{entity}"

    def _entry_key(self, namespace: str, tokens: Sequence[str], params: Any) -> str:
        material = json.dumps(
            [namespace, list(tokens), jsonable_encoder(params or {})],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"{self._prefix}e:{namespace}:{digest}"

    async def _versions(self, entities: Sequence[str]) -> list:
        """
        Current tokens for ``entities``, creating missing ones.

        A missing version (never written, expired or evicted) gets a fresh
        token with ``SET NX`` (so concurrent readers agree on one) and is
        then read back.
        """
        keys = [self._version_key(e) for e in entities]
        tokens = list(await self._client.mget(keys))
        missing = [i for i, token in enumerate(tokens) if token is None]
        if missing:
            for i in missing:
                await self._client.set(
                    keys[i], self._new_token(), nx=True, ex=self._version_ttl
                )
            reread = await self._client.mget([keys[i] for i in missing])
            for i, token in zip(missing, reread):
                tokens[i] = token
        return tokens

    def _failed(self, exc: BaseException) -> None:
        self._circuit.record_failure(exc)

    async def _compensate_lost_invalidations(self) -> None:
        """
        After version changes were lost (Redis unavailable), change the
        epoch before anything else is read, dropping every cached entry.
        Raises Redis errors to the caller, which treats them as an outage.
        """
        if not self._lost_invalidation:
            return
        await self._client.set(
            self._version_key(EPOCH), self._new_token(), ex=self._version_ttl
        )
        self._lost_invalidation = False
        response_cache_invalidations_total.labels(entity=EPOCH).inc()
        logger.warning(
            "Response cache: version changes were lost while Redis was "
            "unavailable; changed the epoch, so every cached response is "
            "recomputed."
        )

    def _bypass(self, namespace: str) -> CacheLookup:
        self._circuit.count_fallback()
        response_cache_requests_total.labels(namespace=namespace, result="bypass").inc()
        return CacheLookup(namespace=namespace, key=None)

    async def lookup(
        self,
        namespace: str,
        versions: Sequence[str],
        params: Optional[Mapping[str, Any]] = None,
    ) -> CacheLookup:
        """
        Find the cached entry for the current versions of ``versions``.

        Args:
            namespace (str): What is cached (one per read method).
            versions (Sequence[str]): Entities the result depends on.
            params (Optional[Mapping[str, Any]]): Request parameters that
                change the result (paging, filters, caller scope, ...).

        Returns:
            CacheLookup: ``hit`` with the decoded value, a miss with the key
            to ``store`` under, or a bypass (``key`` None) when Redis is
            unavailable.
        """
        if not self._circuit.allow():
            return self._bypass(namespace)
        try:
            await self._compensate_lost_invalidations()
            tokens = await self._versions([EPOCH, *versions])
            key = self._entry_key(namespace, tokens, params)
            raw = await self._client.get(key)
        except REDIS_UNAVAILABLE_ERRORS as exc:
            self._failed(exc)
            return self._bypass(namespace)
        self._circuit.record_success()
        if raw is None:
            response_cache_requests_total.labels(
                namespace=namespace, result="miss"
            ).inc()
            return CacheLookup(namespace=namespace, key=key)
        response_cache_requests_total.labels(namespace=namespace, result="hit").inc()
        return CacheLookup(namespace=namespace, key=key, value=json.loads(raw))

    async def store(self, lookup: CacheLookup, value: Any, ttl_seconds: int) -> None:
        """
        Store ``value`` (JSON-compatible) for ``ttl_seconds``.

        No-op for a bypassed lookup, a TTL ≤ 0, or a value larger than
        ``max_entry_bytes``. A Redis error is recorded and swallowed: the
        caller already has its result.
        """
        if lookup.key is None or ttl_seconds <= 0:
            return
        payload = json.dumps(value, separators=(",", ":"))
        if self._max_entry_bytes > 0 and len(payload) > self._max_entry_bytes:
            response_cache_oversize_total.labels(namespace=lookup.namespace).inc()
            return
        if not self._circuit.allow():
            return
        try:
            await self._client.set(lookup.key, payload, ex=int(ttl_seconds))
        except REDIS_UNAVAILABLE_ERRORS as exc:
            self._failed(exc)
            return
        self._circuit.record_success()

    async def invalidate(self, *entities: str) -> None:
        """
        Give each entity a new version, dropping every result cached on it.

        Call after the write is committed. When Redis is unavailable the
        change is lost; it is compensated by an epoch change on the next
        successful call, and in the meantime stale entries are bounded by
        their TTL.
        """
        unique = list(dict.fromkeys(e for e in entities if e))
        if not unique:
            return
        if not self._circuit.allow():
            self._lose()
            return
        try:
            await self._compensate_lost_invalidations()
            pipe = self._client.pipeline(transaction=False)
            for entity in unique:
                pipe.set(
                    self._version_key(entity),
                    self._new_token(),
                    ex=self._version_ttl,
                )
            await pipe.execute()
        except REDIS_UNAVAILABLE_ERRORS as exc:
            self._failed(exc)
            self._lose()
            return
        for entity in unique:
            response_cache_invalidations_total.labels(entity=_entity_type(entity)).inc()
        self._circuit.record_success()

    def _lose(self) -> None:
        self._lost_invalidation = True
        response_cache_lost_invalidations_total.inc()


def build_response_cache(
    enabled: bool,
    redis_url: Optional[str],
    key_prefix: str = "game:rc:",
    socket_timeout_seconds: float = 0.5,
    connect_timeout_seconds: float = 0.5,
    failure_cooldown_seconds: float = 5.0,
    version_ttl_seconds: int = 172800,
    max_entry_bytes: int = 262144,
    ttls: Optional[Mapping[str, int]] = None,
) -> ResponseCache:
    """
    Build the configured response cache.

    Disabled, or enabled without ``REDIS_URL`` (logged), gives a
    :class:`NullResponseCache`: the API then works exactly as without a
    cache.
    """
    if not enabled:
        return NullResponseCache()
    if not redis_url:
        logger.warning(
            "RESPONSE_CACHE_ENABLED=true but REDIS_URL is empty; response "
            "caching is disabled."
        )
        return NullResponseCache()
    from app.services.rate_limit_counter_backend import build_redis_client_from_url

    client = build_redis_client_from_url(
        redis_url, socket_timeout_seconds, connect_timeout_seconds
    )
    return RedisResponseCache(
        client,
        key_prefix=key_prefix,
        circuit=RedisCircuitBreaker("response_cache", failure_cooldown_seconds),
        version_ttl_seconds=version_ttl_seconds,
        max_entry_bytes=max_entry_bytes,
        ttls=ttls,
    )


# --- Read helpers -----------------------------------------------------------

M = TypeVar("M", bound=BaseModel)


Params = Optional[Mapping[str, Any]]


def _decode(body: Any, model: Optional[Type[M]]) -> Any:
    return model.model_validate(body) if model is not None else body


def _resolve_params(params: Union[Params, Callable[[], Params]]) -> Params:
    """Params may be given as a callable, evaluated only when caching."""
    return params() if callable(params) else params


async def cached_read(
    cache: ResponseCache,
    *,
    namespace: str,
    versions: Iterable[str],
    params: Union[Params, Callable[[], Params]],
    ttl_group: str,
    compute: Callable[[], Awaitable[Any]],
    model: Optional[Type[M]] = None,
) -> Any:
    """
    Return the cached result, or compute, cache and return it.

    With caching enabled, a hit and a miss return the same thing: the
    JSON-compatible form of the result (``jsonable_encoder``), rebuilt into
    ``model`` when given. Disabled, ``compute()`` is returned untouched.

    Only use this for results that do not depend on the caller beyond what
    ``params`` covers. ``params`` may be a callable, evaluated only when the
    cache is enabled.
    """
    if not cache.enabled:
        return await compute()
    lookup = await cache.lookup(namespace, list(versions), _resolve_params(params))
    if lookup.hit:
        return _decode(lookup.value, model)
    body = jsonable_encoder(await compute())
    await cache.store(lookup, body, cache.ttl(ttl_group))
    return _decode(body, model)


@dataclass(frozen=True)
class GameAccess:
    """The caller scope passed to game reads (see ``get_authorized_game``)."""

    api_key: Optional[str] = None
    oauth_user_id: Optional[str] = None
    is_admin: bool = False
    enforce_scope: bool = False


async def cached_game_read(
    cache: ResponseCache,
    *,
    namespace: str,
    game_id: Any,
    access: GameAccess,
    load_game: Callable[[], Awaitable[Any]],
    compute: Callable[[Any], Awaitable[Any]],
    params: Union[Params, Callable[[], Params]] = None,
    ttl_group: str = TTL_GAME_DEFINITION,
    model: Optional[Type[M]] = None,
) -> Any:
    """
    Cached read of data that depends on one game's definition.

    On a miss, ``load_game()`` loads the game and applies the caller's
    access check exactly as without caching (raising ``NotFoundError`` /
    ``ForbiddenError``), then ``compute(game)`` builds the result. The entry
    stores the game's owner fields with the result, so a hit applies the
    same access check without touching the database. A hit implies the game
    exists: deleting a game changes its definition version.

    One entry is shared by every caller allowed to see the game.
    """
    if not cache.enabled:
        return await compute(await load_game())
    lookup = await cache.lookup(
        namespace,
        [GAMES_ALL, game_definition_entity(game_id)],
        _resolve_params(params),
    )
    if lookup.hit:
        owner = lookup.value["owner"]
        if access.enforce_scope and not can_access_game(
            SimpleNamespace(**owner),
            api_key=access.api_key,
            oauth_user_id=access.oauth_user_id,
            is_admin=access.is_admin,
        ):
            raise ForbiddenError(detail=GAME_FORBIDDEN_DETAIL)
        return _decode(lookup.value["body"], model)
    game = await load_game()
    body = jsonable_encoder(await compute(game))
    entry = {
        "owner": {
            "apiKey_used": getattr(game, "apiKey_used", None),
            "oauth_user_id": getattr(game, "oauth_user_id", None),
        },
        "body": body,
    }
    await cache.store(lookup, entry, cache.ttl(ttl_group))
    return _decode(body, model)
