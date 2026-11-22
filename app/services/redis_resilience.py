"""
Keep Redis optional at runtime, not only at configuration time.

Redis backs two features (``APIKEY_CACHE_BACKEND=redis`` and
``ABUSE_PREVENTION_BACKEND=redis``). Neither may take the API down with it:
when Redis is unreachable, slow or erroring, each feature falls back to its
non-Redis behaviour (see the backends) and recovers on its own.

* Client calls are bounded by short socket/connect timeouts and are not
  retried (``build_redis_client_from_url``), so a hung Redis costs at most
  one timeout.
* ``RedisCircuitBreaker``: after a failure, Redis is skipped for
  ``REDIS_FAILURE_COOLDOWN_SECONDS`` so requests use the fallback at once
  instead of each waiting out a timeout; the first call after the cool-down
  probes Redis again.
* Metrics: ``redis_unavailable_total`` counts outages (circuit openings) and
  ``redis_fallback_total`` counts operations served by the fallback.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any, Callable, Tuple, Type

from prometheus_client import Counter

logger = logging.getLogger(__name__)

redis_unavailable_total = Counter(
    "redis_unavailable_total",
    "Times Redis became unavailable for a component (circuit opened).",
    labelnames=("component",),
)

redis_fallback_total = Counter(
    "redis_fallback_total",
    "Cache lookups and rate-limit counts served by the non-Redis fallback "
    "because Redis was unavailable.",
    labelnames=("component",),
)


def _redis_errors() -> Tuple[Type[BaseException], ...]:
    try:
        from redis.exceptions import RedisError
    except ImportError:  # pragma: no cover - redis is optional
        return ()
    return (RedisError,)


# Errors that mean "Redis is not usable right now": anything raised by the
# client (connection, timeout, response, read-only replica, auth, ...), plus
# OS-level socket errors and asyncio timeouts that may escape it. Programming
# errors (TypeError, ValueError from bad data) are not included.
REDIS_UNAVAILABLE_ERRORS: Tuple[Type[BaseException], ...] = _redis_errors() + (
    OSError,
    asyncio.TimeoutError,
)


class RedisCircuitBreaker:
    """
    Per-component on/off switch for Redis use.

    ``allow()`` is false while the circuit is open (within the cool-down after
    a failure). ``record_failure()`` opens it and logs once per outage;
    ``record_success()`` closes it and logs the recovery.
    """

    def __init__(
        self,
        component: str,
        cooldown_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.component = component
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._open_until = 0.0
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """True from a failure until the next successful Redis call."""
        return self._degraded

    def allow(self) -> bool:
        """Whether to try Redis now (closed, or cool-down elapsed)."""
        return self._clock() >= self._open_until

    def record_failure(self, exc: BaseException) -> None:
        """Open the circuit for the cool-down; log and count new outages."""
        self._open_until = self._clock() + self._cooldown
        if not self._degraded:
            self._degraded = True
            redis_unavailable_total.labels(component=self.component).inc()
            logger.warning(
                "Redis unavailable for %s (%s: %s); using the fallback, "
                "retrying Redis every %.0fs.",
                self.component,
                type(exc).__name__,
                exc,
                self._cooldown,
            )

    def record_success(self) -> None:
        """Close the circuit; log the recovery after an outage."""
        if self._degraded:
            self._degraded = False
            logger.info("Redis available again for %s.", self.component)
        self._open_until = 0.0

    def count_fallback(self) -> None:
        """Count one operation served by the fallback."""
        redis_fallback_total.labels(component=self.component).inc()


def build_redis_client(
    url: str,
    socket_timeout_seconds: float,
    connect_timeout_seconds: float,
) -> Any:
    """
    Async Redis client with bounded, non-retried calls.

    redis-py's own timeouts default to 5 s per call; a feature that has a
    fallback should give up quickly instead. Retries are disabled explicitly
    (``Retry(NoBackoff(), 0)``): the circuit breaker handles recovery.
    """
    from redis import asyncio as redis_asyncio
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff

    return redis_asyncio.from_url(
        url,
        decode_responses=True,
        socket_timeout=socket_timeout_seconds,
        socket_connect_timeout=connect_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
    )
