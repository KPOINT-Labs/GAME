"""
Short-lived aggregates in the response cache (RESPONSE_CACHE_AGGREGATE_TTL_SECONDS).

Game/task points totals and users per game: cached per game (and task), not
refreshed by point awards, refreshed by game/task definition changes.
Custom strategy usage/metrics/compare: cached per realm and parameters.
Dashboard summaries: cached per date range and grouping.
"""

from collections import namedtuple
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import ForbiddenError, NotFoundError
from app.schema.games_schema import ListTasksWithUsers
from app.schema.user_points_schema import (
    AllPointsByGame,
    AllPointsByGameWithDetails,
    PointsAssignedToUser,
)
from app.services.dashboard_service import DashboardService
from app.services.redis_resilience import RedisCircuitBreaker
from app.services.response_cache import (
    TTL_AGGREGATE,
    NullResponseCache,
    RedisResponseCache,
    game_definition_entity,
    user_entity,
)
from app.services.strategy_definition_service import StrategyDefinitionService
from app.services.strategy_observability_service import StrategyObservabilityService
from app.services.task_service import TaskService
from app.services.user_points_service import UserPointsService

fakeredis = pytest.importorskip("fakeredis")

TTL = 600

# SQLAlchemy rows expose ``_asdict``; a namedtuple behaves the same way.
PointsRow = namedtuple("PointsRow", "externalUserId points timesAwarded")
DetailsRow = namedtuple("DetailsRow", "externalUserId points timesAwarded pointsData")


def _cache():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = RedisResponseCache(
        redis,
        circuit=RedisCircuitBreaker("response_cache", 5),
        ttls={TTL_AGGREGATE: TTL},
    )
    cache.redis = redis
    return cache


def _render(response_model, value):
    app = FastAPI()

    @app.get("/", response_model=response_model)
    async def endpoint():
        return value

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/")
        return response.status_code, response.json() if response.content else None


async def _entry_ttls(cache):
    return [
        await cache.redis.ttl(k) async for k in cache.redis.scan_iter("game:rc:e:*")
    ]


# --- game-level: points, points/details, users --------------------------------


class GameAggregates:
    def __init__(self, cache, game_id=None):
        self.cache = cache
        self.game_id = game_id or uuid4()
        self.game = SimpleNamespace(
            id=self.game_id,
            externalGameId="g",
            created_at="2026-01-01 00:00:00+00:00",
            apiKey_used="key-a",
            oauth_user_id=None,
        )
        self.task = SimpleNamespace(id=uuid4(), externalTaskId="t1")
        self.points = {"alice": 5, "bob": 2}

        self.game_repository = AsyncMock()
        self.game_repository.read_by_id.side_effect = self._game
        self.game_repository.read_by_column.side_effect = self._game_by_column
        self.task_repository = AsyncMock()
        self.task_repository.read_by_column.return_value = [self.task]
        self.users_repository = AsyncMock()
        self.users_repository.read_by_column.side_effect = (
            lambda col, ext, **k: SimpleNamespace(
                externalUserId=ext, created_at="2026-01-01 00:00:00+00:00"
            )
        )
        self.repo = AsyncMock()
        self.repo.get_points_and_users_by_taskIds.side_effect = self._rows
        self.repo.get_points_and_users_by_taskId.side_effect = lambda task_id: [
            r for r in self._sync_rows()
        ]
        self.repo.get_first_user_points_in_external_task_id_by_user_id.return_value = (
            SimpleNamespace(created_at="2026-01-02 00:00:00+00:00")
        )
        self.service = UserPointsService(
            user_points_repository=self.repo,
            users_repository=self.users_repository,
            users_game_config_repository=AsyncMock(),
            game_repository=self.game_repository,
            task_repository=self.task_repository,
            wallet_repository=AsyncMock(),
            wallet_transaction_repository=AsyncMock(),
            response_cache=cache,
        )

    async def _game(self, game_id, **kwargs):
        return self.game if str(game_id) == str(self.game_id) else None

    async def _game_by_column(self, column, value, **kwargs):
        game = await self._game(value)
        if game is None and kwargs.get("not_found_raise_exception", True):
            raise NotFoundError(detail="Game not found")
        return game

    def _sync_rows(self):
        return [
            SimpleNamespace(
                taskId=str(self.task.id),
                externalUserId=user,
                userId=user,
                points=points,
                timesAwarded=1,
                pointsData=[
                    {
                        "points": points,
                        "caseName": "c",
                        "created_at": "2026-01-02T00:00:00+00:00",
                    }
                ],
            )
            for user, points in self.points.items()
        ]

    async def _rows(self, task_ids):
        return self._sync_rows()

    def queries(self):
        return (
            self.repo.get_points_and_users_by_taskIds.await_count
            + self.repo.get_points_and_users_by_taskId.await_count
        )

    def call(self, method, **kwargs):
        kwargs.setdefault("api_key", "key-a")
        kwargs.setdefault("enforce_scope", True)
        return getattr(self.service, method)(self.game_id, **kwargs)


GAME_READS = [
    ("get_points_by_gameId", AllPointsByGame),
    ("get_points_by_gameId_with_details", AllPointsByGameWithDetails),
    ("get_users_by_gameId", ListTasksWithUsers),
]


@pytest.mark.parametrize("method, model", GAME_READS)
async def test_game_aggregates_match_uncached_and_hit_skips_database(method, model):
    plain = GameAggregates(NullResponseCache())
    cached = GameAggregates(_cache(), game_id=plain.game_id)
    cached.task = plain.task
    cached.task_repository.read_by_column.return_value = [plain.task]

    expected = _render(model, await plain.call(method))
    miss = _render(model, await cached.call(method))
    queries = cached.queries()
    hit = _render(model, await cached.call(method))

    assert expected[0] == 200
    assert miss == hit == expected
    assert cached.queries() == queries
    assert all(TTL - 5 <= t <= TTL for t in await _entry_ttls(cached.cache))


async def test_points_details_keep_points_data_through_the_cache():
    cached = GameAggregates(_cache())
    await cached.call("get_points_by_gameId_with_details")
    hit = await cached.call("get_points_by_gameId_with_details")
    status, body = _render(AllPointsByGameWithDetails, hit)
    assert status == 200
    points_data = body["task"][0]["points"][0]["pointsData"]
    assert [(d["points"], d["caseName"], d["created_at"]) for d in points_data] == [
        (5, "c", "2026-01-02T00:00:00+00:00")
    ]


@pytest.mark.parametrize("method, model", GAME_READS)
async def test_awards_do_not_refresh_game_aggregates_but_definitions_do(method, model):
    agg = GameAggregates(_cache())
    first = _render(model, await agg.call(method))
    agg.points["carol"] = 50  # an award to a new player, in the database
    await agg.cache.invalidate(user_entity("carol"))  # what an award invalidates

    assert _render(model, await agg.call(method)) == first

    await agg.cache.invalidate(game_definition_entity(agg.game_id))
    assert _render(model, await agg.call(method)) != first


@pytest.mark.parametrize("method, _", GAME_READS)
async def test_game_aggregates_check_scope_on_hits(method, _):
    agg = GameAggregates(_cache())
    await agg.call(method)
    with pytest.raises(ForbiddenError):
        await agg.call(method, api_key="key-b")


async def test_users_by_game_still_echoes_requested_game_id():
    agg = GameAggregates(_cache())
    result = await agg.call("get_users_by_gameId")
    assert str(result.gameId) == str(agg.game_id)


# --- task-level: points, points/details, per-user (not cached) ------------------


class TaskAggregates:
    def __init__(self, cache):
        self.cache = cache
        self.game_id = uuid4()
        self.game = SimpleNamespace(
            id=self.game_id, apiKey_used="key-a", oauth_user_id=None
        )
        self.task = SimpleNamespace(id=uuid4(), externalTaskId="t1")
        self.rows = [PointsRow("alice", 5, 2), PointsRow("bob", 1, 1)]
        self.details = [
            DetailsRow("alice", 5, 2, [{"points": 3}, {"points": 2}]),
            DetailsRow("bob", 1, 1, [{"points": 1}]),
        ]
        self.game_repository = AsyncMock()
        self.game_repository.read_by_id.side_effect = lambda game_id, **k: self._game(
            game_id
        )
        self.game_repository.read_by_id.side_effect = self._game
        self.game_repository.read_by_column.side_effect = (
            lambda col, value, **k: self._game(value)
        )
        self.game_repository.read_by_column.side_effect = self._game_by_column
        self.task_repository = AsyncMock()
        self.task_repository.read_by_gameId_and_externalTaskId.side_effect = self._task
        self.repo = AsyncMock()
        self.repo.get_all_UserPoints_by_taskId.side_effect = lambda task_id: list(
            self.rows
        )
        self.repo.get_all_UserPoints_by_taskId_with_details.side_effect = (
            lambda task_id: list(self.details)
        )
        self.service = TaskService(
            strategy_service=MagicMock(),
            task_repository=self.task_repository,
            game_repository=self.game_repository,
            user_repository=AsyncMock(),
            user_points_repository=self.repo,
            game_params_repository=AsyncMock(),
            task_params_repository=AsyncMock(),
            response_cache=cache,
        )

    async def _game(self, game_id, **kwargs):
        return self.game if str(game_id) == str(self.game_id) else None

    async def _game_by_column(self, column, value, **kwargs):
        return await self._game(value)

    async def _task(self, game_id, ext):
        return self.task if ext == "t1" else None

    def queries(self):
        return (
            self.repo.get_all_UserPoints_by_taskId.await_count
            + self.repo.get_all_UserPoints_by_taskId_with_details.await_count
        )

    def call(self, method, *args, **kwargs):
        kwargs.setdefault("api_key", "key-a")
        kwargs.setdefault("enforce_scope", True)
        return getattr(self.service, method)(self.game_id, *args, **kwargs)


TASK_READS = [
    ("get_points_by_task_id", List[PointsAssignedToUser]),
    ("get_points_by_task_id_with_details", List[dict]),
]


@pytest.mark.parametrize("method, model", TASK_READS)
async def test_task_aggregates_render_and_are_cached(method, model):
    plain = TaskAggregates(NullResponseCache())
    cached = TaskAggregates(_cache())

    expected = _render(model, await plain.call(method, "t1"))
    miss = _render(model, await cached.call(method, "t1"))
    hit = _render(model, await cached.call(method, "t1"))

    # The details endpoint used to 500 on raw rows; dicts render.
    assert expected[0] == 200
    assert miss == hit == expected
    assert cached.queries() == 1
    assert all(TTL - 5 <= t <= TTL for t in await _entry_ttls(cached.cache))


async def test_task_details_keep_points_data():
    cached = TaskAggregates(_cache())
    await cached.call("get_points_by_task_id_with_details", "t1")
    hit = await cached.call("get_points_by_task_id_with_details", "t1")
    assert hit[0]["pointsData"] == [{"points": 3}, {"points": 2}]


@pytest.mark.parametrize("method, _", TASK_READS)
async def test_task_aggregates_scope_definition_and_missing_task(method, _):
    agg = TaskAggregates(_cache())
    await agg.call(method, "t1")
    with pytest.raises(ForbiddenError):
        await agg.call(method, "t1", api_key="key-b")
    agg.rows[0] = PointsRow("alice", 99, 3)
    await agg.cache.invalidate(game_definition_entity(agg.game_id))
    await agg.call(method, "t1")
    assert agg.queries() == 2
    for _ in range(2):
        with pytest.raises(NotFoundError):
            await agg.call(method, "nope")


async def test_unscoped_internal_calls_return_raw_rows_uncached():
    agg = TaskAggregates(_cache())
    rows = await agg.service.get_points_by_task_id(agg.game_id, "t1")
    details = await agg.service.get_points_by_task_id_with_details(agg.game_id, "t1")
    assert rows == agg.rows and details == agg.details
    await agg.service.get_points_by_task_id(agg.game_id, "t1")
    assert agg.queries() == 3


async def test_per_user_task_points_stay_uncached_and_fresh():
    agg = TaskAggregates(_cache())
    first = await agg.call("get_points_of_user_by_task_id", "t1", "alice")
    agg.details[0] = DetailsRow("alice", 9, 3, [])
    second = await agg.call("get_points_of_user_by_task_id", "t1", "alice")
    assert (first.points, second.points) == (5, 9)
    with pytest.raises(ForbiddenError):
        await agg.call("get_points_of_user_by_task_id", "t1", "alice", api_key="x")
    missing = await agg.call("get_points_of_user_by_task_id", "t1", "nobody")
    assert missing.points == 0


# --- realm-scoped strategy reads -----------------------------------------------


USAGE_ID, USAGE_GAME = uuid4(), uuid4()


def _usage_service(cache):
    repo = MagicMock()
    row = SimpleNamespace(id=USAGE_ID, name="s", version=1, status="PUBLISHED")
    repo.get_for_realm = AsyncMock(
        side_effect=lambda id, realmId: row if realmId == "r1" else None
    )
    games = MagicMock()
    games.list_by_strategy_id = AsyncMock(
        return_value=[SimpleNamespace(id=USAGE_GAME, externalGameId="g", platform="w")]
    )
    games.list_external_ids = AsyncMock(return_value={})
    tasks = MagicMock()
    tasks.list_by_strategy_id = AsyncMock(return_value=[])
    return (
        StrategyDefinitionService(
            repo, game_repository=games, task_repository=tasks, response_cache=cache
        ),
        games,
    )


async def test_strategy_usage_is_cached_per_realm():
    cache = _cache()
    service, games = _usage_service(cache)
    plain, _ = _usage_service(NullResponseCache())
    first = await service.get_usage(id="x", realmId="r1")
    again = await service.get_usage(id="x", realmId="r1")
    assert first == again
    assert first == await plain.get_usage(id="x", realmId="r1")
    assert games.list_by_strategy_id.await_count == 1
    for _ in range(2):  # another realm: its own lookup, and a 404 not cached
        with pytest.raises(NotFoundError):
            await service.get_usage(id="x", realmId="r2")
    assert service.strategy_definition_repository.get_for_realm.await_count == 3


async def test_strategy_metrics_and_compare_are_cached_per_realm_and_window():
    from datetime import datetime, timezone

    cache = _cache()
    service = StrategyObservabilityService(
        execution_log_repository=MagicMock(),
        strategy_definition_service=MagicMock(),
        response_cache=cache,
    )
    service._get_metrics_uncached = AsyncMock(return_value={"m": 1})
    service._compare_uncached = AsyncMock(return_value={"c": 1})
    from app.services import response_cache as rc

    rc_decode = rc._decode
    rc._decode = lambda body, model: body  # dicts stand in for the models
    try:
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for _ in range(2):
            await service.get_metrics(id="a", realmId="r1", sinceDt=since)
            await service.compare(idA="a", idB="b", realmId="r1")
        await service.get_metrics(id="a", realmId="r2", sinceDt=since)
        await service.get_metrics(id="a", realmId="r1")
    finally:
        rc._decode = rc_decode
    assert service._get_metrics_uncached.await_count == 3
    assert service._compare_uncached.await_count == 1


async def test_strategy_compare_uses_cached_metrics_for_each_side():
    service = StrategyObservabilityService(
        execution_log_repository=MagicMock(),
        strategy_definition_service=MagicMock(),
        response_cache=NullResponseCache(),
    )
    service.get_metrics = AsyncMock(
        side_effect=lambda **k: SimpleNamespace(
            totalRuns=0, avgPoints=None, errorRate=None, p95LatencyMs=None
        )
    )
    try:
        await service.compare(idA="a", idB="b", realmId="r1")
    except Exception:  # building the delta from fakes is not under test
        pass
    assert service.get_metrics.await_count == 2


# --- dashboard --------------------------------------------------------------------


def _dashboard(cache):
    repo = MagicMock()
    repo.get_dashboard_summary = AsyncMock(return_value={"games": 3})
    repo.get_dashboard_summary_logs = AsyncMock(return_value={"logs": 7})
    service = DashboardService(
        dashboard_repository=repo,
        game_repository=MagicMock(),
        task_repository=MagicMock(),
        user_repository=MagicMock(),
        logs_repository=MagicMock(),
        user_points_repository=MagicMock(),
        user_actions_repository=MagicMock(),
        response_cache=cache,
    )
    return service, repo


async def test_dashboard_summaries_are_cached_per_range_and_grouping():
    cache = _cache()
    service, repo = _dashboard(cache)
    for _ in range(2):
        assert await service.get_dashboard_summary("2026-01-01", None, "day") == {
            "games": 3
        }
        assert await service.get_dashboard_summary_logs(None, None, None) == {"logs": 7}
    await service.get_dashboard_summary("2026-01-01", None, "month")
    assert repo.get_dashboard_summary.await_count == 2
    assert repo.get_dashboard_summary_logs.await_count == 1
    assert all(TTL - 5 <= t <= TTL for t in await _entry_ttls(cache))


async def test_dashboard_without_cache_is_unchanged():
    service, repo = _dashboard(NullResponseCache())
    await service.get_dashboard_summary(None, None, None)
    await service.get_dashboard_summary(None, None, None)
    assert repo.get_dashboard_summary.await_count == 2
