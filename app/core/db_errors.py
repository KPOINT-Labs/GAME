"""Recognise "the database is unreachable" among exceptions.

When Postgres is down, frozen or unreachable the API answers ``503`` with
``Retry-After`` instead of a generic ``500``, so clients know the failure is
temporary and safe to retry (with the same ``Idempotency-Key`` for awards).
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy import exc as sa_exc

try:  # asyncpg is the production driver; absent in some test setups.
    import asyncpg.exceptions as _apg

    _ASYNCPG_UNAVAILABLE = (
        _apg.ConnectionDoesNotExistError,
        _apg.CannotConnectNowError,
        _apg.TooManyConnectionsError,
        _apg.ConnectionFailureError,
        _apg.InterfaceError,
    )
except ImportError:  # pragma: no cover
    _ASYNCPG_UNAVAILABLE = ()

RETRY_AFTER_SECONDS = 5


class DatabaseUnavailableError(Exception):
    """Raised without touching the database while it is known to be down.

    See ``Database`` in ``app.core.database``: after an outage error the
    database is skipped for a short cool-down, so the remaining database
    calls of a request (and concurrent requests) fail at once instead of
    each waiting out its own timeout.
    """


UNAVAILABLE_DETAIL = (
    "Service temporarily unavailable: the database cannot be reached. "
    "Retry shortly (reuse the same Idempotency-Key for awards)."
)
_DRIVER_MODULE_PREFIXES = ("asyncpg", "sqlalchemy")


def _raised_in_db_driver(tb: TracebackType | None) -> bool:
    """True if any frame of the traceback is in the database driver stack."""
    while tb is not None:
        module = tb.tb_frame.f_globals.get("__name__", "")
        if module.startswith(_DRIVER_MODULE_PREFIXES):
            return True
        tb = tb.tb_next
    return False


def _chain(exc: BaseException):
    """The exception, its causes/contexts and any wrapped DBAPI ``orig``."""
    seen: set[int] = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack.extend(
            [current.__cause__, current.__context__, getattr(current, "orig", None)]
        )


def is_database_unavailable(exc: BaseException) -> bool:
    """
    Whether ``exc`` (or anything in its chain) means the database cannot be
    reached: connection refused/closed/reset, pool exhausted, server not
    accepting connections, or a connect/query timeout raised by the driver.

    Timeouts and OS errors raised elsewhere (for example by an HTTP client)
    do not count, so unrelated failures still surface as ``500``.
    """
    for item in _chain(exc):
        if isinstance(item, DatabaseUnavailableError):
            return True
        if isinstance(item, (sa_exc.OperationalError, sa_exc.InterfaceError)):
            return True
        if isinstance(item, sa_exc.TimeoutError):  # connection pool exhausted
            return True
        if isinstance(item, sa_exc.DBAPIError) and item.connection_invalidated:
            return True
        if _ASYNCPG_UNAVAILABLE and isinstance(item, _ASYNCPG_UNAVAILABLE):
            return True
        if isinstance(item, (TimeoutError, ConnectionError)) and _raised_in_db_driver(
            item.__traceback__
        ):
            return True
    return False
