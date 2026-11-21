import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import as_declarative, declared_attr

from app.core.db_errors import DatabaseUnavailableError, is_database_unavailable

logger = logging.getLogger(__name__)


def _consume_result(task: "asyncio.Future") -> None:
    """Retrieve an abandoned task's outcome so asyncio does not warn about it."""
    if not task.cancelled():
        task.exception()


@as_declarative()
class BaseModel:
    """
    Base model class for SQLAlchemy ORM models.

    Attributes:
        id (Any): The primary key of the model.
        __name__ (str): The name of the model class.
    """

    id: Any
    __name__: str

    @declared_attr
    def __tablename__(cls) -> str:
        return cls.__name__.lower()


def _to_async_url(db_url: str) -> str:
    """
    Coerce a sync DB URL into the async-driver form. Allows operators to keep
    the sync-style ``DATABASE_URI`` in env files while the engine itself runs
    on asyncpg / aiosqlite.
    """
    url = make_url(db_url)
    backend = url.get_backend_name()
    driver = url.get_driver_name() or ""
    if backend == "postgresql" and driver in ("", "psycopg2", "psycopg"):
        return url.set(drivername="postgresql+asyncpg").render_as_string(
            hide_password=False
        )
    if backend == "sqlite" and driver in ("", "pysqlite"):
        return url.set(drivername="sqlite+aiosqlite").render_as_string(
            hide_password=False
        )
    if backend == "mysql" and driver in ("", "pymysql", "mysqldb"):
        return url.set(drivername="mysql+aiomysql").render_as_string(
            hide_password=False
        )
    return db_url


class Database:
    """
    Async database wrapper around SQLAlchemy 2.0's ``AsyncEngine`` and
    ``async_sessionmaker``. Exposes an ``async with database.session() as s``
    context manager used by every repository.
    """

    def __init__(
        self,
        db_url: str,
        *,
        echo: bool = False,
        pool_pre_ping: bool = True,
        pool_size: int = 20,
        max_overflow: int = 40,
        pool_timeout_seconds: int = 30,
        pool_recycle_seconds: int = 1800,
        connect_timeout_seconds: float = 5,
        command_timeout_seconds: float = 30,
        unavailable_cooldown_seconds: float = 2,
    ) -> None:
        async_url = _to_async_url(db_url)
        engine_kwargs: dict = {"echo": echo}
        backend = make_url(async_url).get_backend_name()
        if backend != "sqlite":
            engine_kwargs.update(
                {
                    "pool_pre_ping": pool_pre_ping,
                    "pool_size": max(1, int(pool_size)),
                    "max_overflow": max(0, int(max_overflow)),
                    "pool_timeout": max(1, int(pool_timeout_seconds)),
                    "pool_recycle": max(30, int(pool_recycle_seconds)),
                }
            )
        if backend == "postgresql":
            # Fail fast instead of hanging when Postgres is down, frozen or
            # unreachable: without these, asyncpg waits up to 60s to connect
            # and forever for a query (requests then pile up). The timeouts
            # surface as "database unavailable" errors, answered with 503.
            # 0 disables either one.
            connect_args = {}
            if connect_timeout_seconds:
                connect_args["timeout"] = float(connect_timeout_seconds)
            if command_timeout_seconds:
                connect_args["command_timeout"] = float(command_timeout_seconds)
            if connect_args:
                engine_kwargs["connect_args"] = connect_args

        self._engine = create_async_engine(async_url, **engine_kwargs)
        # Circuit breaker: after an outage error, skip the database until this
        # monotonic time, so the rest of a request (and concurrent requests)
        # fail at once with 503 instead of each waiting out a timeout. The
        # first call after the cool-down tries the database again; a success
        # closes the breaker. 0 disables it.
        self._unavailable_cooldown = max(0.0, float(unavailable_cooldown_seconds))
        self._unavailable_until = 0.0
        self._session_factory: Callable[..., AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def ping(self, timeout_seconds: float = 2.0) -> bool:
        """
        Return whether the database answers ``SELECT 1`` within the timeout.

        Used by the readiness endpoint; never raises.

        Args:
            timeout_seconds (float): Upper bound for connecting and querying.

        Returns:
            bool: ``True`` if the database answered in time.
        """

        async def _select_one():
            conn = await self._engine.connect()
            try:
                await conn.execute(text("SELECT 1"))
            except BaseException:
                # Do not talk to a server that may be frozen: drop the
                # connection instead of resetting it.
                await conn.invalidate()
                raise
            finally:
                await conn.close()

        # asyncio.wait (not wait_for): on timeout we answer immediately and
        # let the abandoned check finish in the background, instead of
        # waiting for its cleanup against an unresponsive database.
        task = asyncio.ensure_future(_select_one())
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
        if task in done and task.exception() is None:
            return True
        if task in done:
            logger.warning("Database readiness check failed: %r", task.exception())
        else:
            logger.warning(
                "Database readiness check timed out after %ss", timeout_seconds
            )
            task.add_done_callback(_consume_result)
            task.cancel()
        return False

    async def create_database(self) -> None:
        """
        Create all tables declared on the metadata if they do not yet exist.

        Runs ``BaseModel.metadata.create_all`` inside a transactional
        connection. Intended for bootstrapping local/test databases;
        production schema changes go through migrations.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(BaseModel.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """
        Yield a managed ``AsyncSession`` for a unit of work.

        The session is rolled back automatically if the wrapped block raises,
        and is always closed on exit. Used as ``async with database.session()
        as s:`` by every repository.

        Yields:
            AsyncSession: An active database session bound to the engine.
        """
        if time.monotonic() < self._unavailable_until:
            raise DatabaseUnavailableError(
                "Database marked unavailable after a recent connection failure"
            )
        session: AsyncSession = self._session_factory()
        try:
            yield session
            self._unavailable_until = 0.0
        except Exception as exc:
            if is_database_unavailable(exc):
                if self._unavailable_cooldown:
                    self._unavailable_until = (
                        time.monotonic() + self._unavailable_cooldown
                    )
                # The connection is dead or the server is not answering: a
                # graceful rollback would wait on it (and so would the reset
                # when the connection goes back to the pool). Drop the
                # connection instead; the server discards the uncommitted
                # transaction when the connection goes away. Then replace the
                # pool without touching its other connections, which are
                # likely dead too, so later requests open fresh ones.
                await session.invalidate()
                await self._engine.dispose(close=False)
            else:
                await session.rollback()
            raise
        finally:
            await session.close()
