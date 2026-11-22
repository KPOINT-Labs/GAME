"""Fail-fast and 503 behaviour when the database is unreachable."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc

from app.core.db_errors import RETRY_AFTER_SECONDS, is_database_unavailable


def _operational():
    return sa_exc.OperationalError(
        "SELECT 1", {}, ConnectionRefusedError(111, "refused")
    )


@pytest.mark.parametrize(
    "exc",
    [
        _operational(),
        sa_exc.InterfaceError("SELECT 1", {}, Exception("connection is closed")),
        sa_exc.TimeoutError("QueuePool limit reached"),
    ],
)
def test_database_outage_errors_are_recognised(exc):
    assert is_database_unavailable(exc)


def test_outage_found_through_the_exception_chain():
    try:
        try:
            raise _operational()
        except Exception as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_database_unavailable(outer)


def test_asyncpg_connection_errors_are_recognised():
    import asyncpg.exceptions as apg

    assert is_database_unavailable(apg.ConnectionDoesNotExistError("closed"))
    assert is_database_unavailable(apg.CannotConnectNowError("starting up"))


def test_failover_errors_are_recognised():
    """An old primary shutting down or demoted to read-only during a
    CloudNativePG failover/switchover answers 503 and resets the pool."""
    import asyncpg.exceptions as apg

    assert is_database_unavailable(apg.AdminShutdownError("terminating connection"))
    assert is_database_unavailable(apg.CrashShutdownError("crash of another backend"))
    assert is_database_unavailable(
        apg.ReadOnlySQLTransactionError("cannot execute INSERT in a read-only txn")
    )
    wrapped = sa_exc.DBAPIError("INSERT", {}, Exception("adapter error"))
    wrapped.orig.__cause__ = apg.ReadOnlySQLTransactionError("read-only")
    assert is_database_unavailable(wrapped)


def test_query_cancellation_is_not_an_outage():
    import asyncpg.exceptions as apg

    assert not is_database_unavailable(apg.QueryCanceledError("statement timeout"))


@pytest.mark.parametrize(
    "exc",
    [
        sa_exc.IntegrityError("INSERT", {}, Exception("duplicate key")),
        ValueError("bad input"),
        TimeoutError("an HTTP call timed out"),  # not raised in the DB driver
        ConnectionRefusedError(111, "keycloak down"),  # not raised in the DB driver
    ],
)
def test_other_errors_are_not_outages(exc):
    assert not is_database_unavailable(exc)


def test_timeout_raised_inside_the_driver_is_an_outage():
    # Simulate a TimeoutError whose traceback passes through a driver module.
    namespace = {"__name__": "asyncpg.fake_protocol"}
    exec("def boom():\n    raise TimeoutError('query timed out')", namespace)
    try:
        namespace["boom"]()
    except TimeoutError as exc:
        assert is_database_unavailable(exc)


def _app_raising(exc):
    from app.middlewares.error_handler import CatchUnhandledErrorsMiddleware

    app = FastAPI()
    app.add_middleware(CatchUnhandledErrorsMiddleware)

    @app.get("/boom")
    async def boom():
        raise exc

    return TestClient(app, raise_server_exceptions=False)


def test_middleware_answers_503_with_retry_after_for_outages():
    response = _app_raising(_operational()).get("/boom")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    assert "temporarily unavailable" in response.json()["detail"]


def test_middleware_keeps_500_for_other_errors():
    response = _app_raising(ValueError("bug")).get("/boom")

    assert response.status_code == 500
    assert "Retry-After" not in response.headers


def test_write_path_maps_outage_to_503():
    from app.api.v1.endpoints.games_common import _map_write_exception

    mapped = _map_write_exception(_operational(), correlation_id="corr-1")

    assert mapped.status_code == 503
    assert mapped.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)


def _wrapped_in_internal_server_error():
    """An outage wrapped the way services do (``raise ... from`` / implicit)."""
    from app.core.exceptions import InternalServerError

    try:
        try:
            raise _operational()
        except Exception:
            raise InternalServerError("Error in calculate points for task t1")
    except InternalServerError as exc:
        return exc


def test_write_path_maps_wrapped_outage_5xx_to_503():
    from app.api.v1.endpoints.games_common import _map_write_exception

    mapped = _map_write_exception(
        _wrapped_in_internal_server_error(), correlation_id="corr-1"
    )

    assert mapped.status_code == 503
    assert mapped.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)


def test_write_path_keeps_client_errors_even_after_an_outage():
    from fastapi import HTTPException

    from app.api.v1.endpoints.games_common import _map_write_exception

    try:
        try:
            raise _operational()
        except Exception:
            raise HTTPException(status_code=404, detail="gone")
    except HTTPException as exc:
        mapped = _map_write_exception(exc, correlation_id="corr-1")

    assert mapped.status_code == 404


def _app_with_http_handler(exc):
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.middlewares.error_handler import http_exception_handler

    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)

    @app.get("/boom")
    async def boom():
        raise exc

    return TestClient(app, raise_server_exceptions=False)


def test_http_handler_turns_outage_5xx_into_503():
    response = _app_with_http_handler(_wrapped_in_internal_server_error()).get("/boom")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)


def test_http_handler_keeps_other_http_errors():
    from fastapi import HTTPException

    client = _app_with_http_handler(HTTPException(status_code=500, detail="bug"))
    assert client.get("/boom").status_code == 500
    assert client.get("/missing").status_code == 404


def test_postgres_engine_gets_connect_and_command_timeouts():
    from app.core import database

    with patch.object(database, "create_async_engine") as create:
        database.Database(
            "postgresql://u:p@db:5432/game",
            connect_timeout_seconds=5,
            command_timeout_seconds=30,
        )

    assert create.call_args.kwargs["connect_args"] == {
        "timeout": 5.0,
        "command_timeout": 30.0,
    }


def test_timeouts_can_be_disabled_and_are_not_used_for_sqlite():
    from app.core import database

    with patch.object(database, "create_async_engine") as create:
        database.Database(
            "postgresql://u:p@db:5432/game",
            connect_timeout_seconds=0,
            command_timeout_seconds=0,
        )
        database.Database("sqlite:///:memory:")

    assert all("connect_args" not in c.kwargs for c in create.call_args_list)


@pytest.mark.asyncio
async def test_ping_reports_reachability():
    from app.core.database import Database

    assert await Database("sqlite:///:memory:").ping() is True
    unreachable = Database(
        "postgresql://u:p@127.0.0.1:1/none", connect_timeout_seconds=1
    )
    assert await unreachable.ping(timeout_seconds=2) is False


@pytest.mark.parametrize("reachable,status", [(True, 200), (False, 503)])
def test_readiness_endpoint(reachable, status):
    from app.main import app, container

    fake_db = AsyncMock()
    fake_db.ping.return_value = reachable
    client = TestClient(app, raise_server_exceptions=False)
    with container.db.override(fake_db):
        response = client.get("/api/v1/kpi/ready")

    assert response.status_code == status
    assert response.json()["database"] == ("ok" if reachable else "unavailable")
    # Liveness never depends on the database.
    assert client.get("/api/v1/kpi/health_check").status_code == 200


@pytest.mark.asyncio
async def test_circuit_breaker_fails_fast_then_recovers(monkeypatch):
    from app.core import database
    from app.core.db_errors import DatabaseUnavailableError

    db = database.Database("sqlite:///:memory:", unavailable_cooldown_seconds=2)
    clock = [100.0]
    monkeypatch.setattr(database.time, "monotonic", lambda: clock[0])

    with pytest.raises(sa_exc.OperationalError):
        async with db.session():
            raise _operational()

    # Within the cool-down: rejected without touching the database.
    with pytest.raises(DatabaseUnavailableError):
        async with db.session():
            pass
    assert is_database_unavailable(DatabaseUnavailableError("x"))

    # After the cool-down the database is tried again; success closes it.
    clock[0] += 2.5
    async with db.session() as s:
        await s.execute(__import__("sqlalchemy").text("SELECT 1"))
    clock[0] += 0.1
    async with db.session():
        pass


@pytest.mark.asyncio
async def test_circuit_breaker_ignores_ordinary_errors_and_can_be_disabled():
    from app.core import database

    db = database.Database("sqlite:///:memory:", unavailable_cooldown_seconds=2)
    with pytest.raises(ValueError):
        async with db.session():
            raise ValueError("bug")
    async with db.session():  # not tripped by a non-outage error
        pass

    off = database.Database("sqlite:///:memory:", unavailable_cooldown_seconds=0)
    with pytest.raises(sa_exc.OperationalError):
        async with off.session():
            raise _operational()
    async with off.session():  # disabled: never rejects
        pass
