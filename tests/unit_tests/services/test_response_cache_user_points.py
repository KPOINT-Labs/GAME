"""
GET /games/{gameId}/users/{externalUserId}/points from the response cache.

Cached per game and user. Invalidated by every points award and every
action posted for that user, and by game-definition changes; other users'
entries stay cached.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import ForbiddenError, NotFoundError
from app.schema.task_schema import AddActionDidByUserInTask
from app.schema.user_actions_schema import CreateUserBodyActions
from app.schema.user_points_schema import PointsAssignedToUser
from app.services.redis_resilience import RedisCircuitBreaker
from app.services.response_cache import (
    TTL_USER_POINTS,
    NullResponseCache,
    RedisResponseCache,
    _entity_type,
    game_definition_entity,
    user_entity,
)
from app.services.user_actions_service import UserActionsService
from app.services.user_points_service import UserPointsService

fakeredis = pytest.importorskip("fakeredis")

TTL = 21600


def _render(value):
    app = FastAPI()

    @app.get("/", response_model=List[PointsAssignedToUser])
    async def endpoint():
        return value

    with TestClient(app) as client:
        return client.get("/").json()


class Points:
    """One game with two tasks and per-user award rows behind mocks."""

    def __init__(self, cache=None, game_id=None):
        self.game_id = game_id or uuid4()
        self.game = SimpleNamespace(
            id=self.game_id, externalGameId="g", apiKey_used="key-a", oauth_user_id=None
        )
        self.tasks = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]
        self.awards = {"alice": [5, 0], "bob": [1, 2]}  # points per task
        self.redis = None
        if cache is None:
            self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            cache = RedisResponseCache(
                self.redis,
                circuit=RedisCircuitBreaker("response_cache", 5),
                ttls={TTL_USER_POINTS: TTL},
            )
        self.cache = cache

        self.game_repository = AsyncMock()
        self.game_repository.read_by_id.side_effect = self._game
        self.game_repository.read_by_column.side_effect = self._game_by_column
        self.users_repository = AsyncMock()
        self.users_repository.read_by_column.side_effect = self._user
        self.task_repository = AsyncMock()
        self.task_repository.read_by_column.return_value = self.tasks
        self.user_points_repository = AsyncMock()
        self.user_points_repository.get_points_and_users_by_taskIds.side_effect = (
            self._rows
        )
        self.service = UserPointsService(
            user_points_repository=self.user_points_repository,
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
        return await self._game(value)

    async def _user(self, column, external_user_id, **kwargs):
        if external_user_id in self.awards:
            return SimpleNamespace(id=uuid4(), externalUserId=external_user_id)
        return None

    async def _rows(self, task_ids):
        return [
            SimpleNamespace(
                taskId=str(task.id),
                externalUserId=user,
                points=points[i],
                timesAwarded=1,
            )
            for i, task in enumerate(self.tasks)
            for user, points in self.awards.items()
            if points[i]
        ]

    def queries(self):
        return self.user_points_repository.get_points_and_users_by_taskIds.await_count

    def read(self, user="alice", **kwargs):
        kwargs.setdefault("api_key", "key-a")
        kwargs.setdefault("enforce_scope", True)
        return self.service.get_points_of_user_in_game(self.game_id, user, **kwargs)


async def test_group_points_by_task_is_keyed_by_task_id_string():
    # Sanity check on the fixture: the service groups rows by str(taskId).
    points = Points(NullResponseCache())
    result = await points.read("bob")
    assert [r.points for r in result] == [1, 2]


@pytest.mark.parametrize("user", ["alice", "bob", "nobody"])
async def test_cached_matches_uncached_and_hit_skips_database(user):
    plain = Points(NullResponseCache())
    cached = Points(game_id=plain.game_id)
    expected = _render(await plain.read(user))

    miss = _render(await cached.read(user))
    queries = cached.users_repository.read_by_column.await_count
    hit = _render(await cached.read(user))

    assert miss == hit == expected
    assert cached.users_repository.read_by_column.await_count == queries


async def test_entries_use_the_user_points_ttl():
    points = Points()
    await points.read("alice")
    keys = [k async for k in points.redis.scan_iter("game:rc:e:*")]
    assert len(keys) == 1
    assert TTL - 5 <= await points.redis.ttl(keys[0]) <= TTL


async def test_users_activity_invalidates_only_that_user():
    points = Points()
    await points.read("alice")
    await points.read("bob")
    before = points.queries()

    points.awards["alice"][1] = 7
    await points.cache.invalidate(user_entity("alice"))

    assert [r.points for r in await points.read("alice")] == [5, 7]
    assert points.queries() == before + 1
    await points.read("bob")
    assert points.queries() == before + 1  # bob still cached


async def test_game_definition_change_invalidates():
    points = Points()
    await points.read("alice")
    await points.cache.invalidate(game_definition_entity(points.game_id))
    await points.read("alice")
    assert points.queries() == 2


async def test_cache_is_per_game():
    cache = Points().cache
    one = Points(cache)
    two = Points(cache)
    await one.read("alice")
    await two.read("alice")
    assert one.queries() == 1 and two.queries() == 1


async def test_scope_on_hits_and_missing_game():
    points = Points()
    await points.read("alice")
    with pytest.raises(ForbiddenError):
        await points.read("alice", api_key="key-b")
    with pytest.raises(NotFoundError):
        await points.service.get_points_of_user_in_game(uuid4(), "alice")
    unscoped = await points.service.get_points_of_user_in_game(points.game_id, "bob")
    assert [r.points for r in unscoped] == [1, 2]


def test_user_entity_and_metric_label():
    assert user_entity("alice") == "user:alice:activity"
    # ids may contain ':'; the metric label must stay low-cardinality
    assert _entity_type(user_entity("tenant:42:alice")) == "user:activity"


# --- writes that invalidate -------------------------------------------------


def _persisting_service(cache):
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def session_factory():
        yield session

    repo = AsyncMock()
    repo.session_factory = session_factory
    repo.read_by_user_task_and_idempotency.return_value = None
    wallet_repository = AsyncMock()
    wallet_repository.upsert_points_balance.return_value = SimpleNamespace(id=uuid4())
    service = UserPointsService(
        user_points_repository=repo,
        users_repository=AsyncMock(),
        users_game_config_repository=AsyncMock(),
        game_repository=AsyncMock(),
        task_repository=AsyncMock(),
        wallet_repository=wallet_repository,
        wallet_transaction_repository=AsyncMock(),
        response_cache=cache,
    )
    service._attach_cohorts = AsyncMock()
    return service, repo, session


def _persist(service, **kwargs):
    args = dict(
        user_id=uuid4(),
        task_id=uuid4(),
        points=3,
        case_name="c",
        data_to_add={},
        description="d",
        api_key="key-a",
        external_user_id="alice",
        external_task_id="t1",
    )
    args.update(kwargs)
    return service._persist_points_wallet_and_transaction(**args)


def _spy_cache():
    cache = NullResponseCache()
    cache.invalidate = AsyncMock()
    return cache


async def test_points_award_invalidates_the_user_after_commit():
    cache = _spy_cache()
    service, _, session = _persisting_service(cache)
    order = []
    session.commit.side_effect = lambda: order.append("commit")
    cache.invalidate.side_effect = lambda *e: order.append(("invalidate", e))

    await _persist(service)

    assert order == ["commit", ("invalidate", ("user:alice:activity",))]


async def test_idempotent_replay_and_failed_award_do_not_invalidate():
    cache = _spy_cache()
    service, repo, _ = _persisting_service(cache)
    repo.read_by_user_task_and_idempotency.return_value = SimpleNamespace(id=1)
    await _persist(service, idempotency_key="k")

    repo.read_by_user_task_and_idempotency.return_value = None
    repo.create.side_effect = RuntimeError("db error")
    with pytest.raises(RuntimeError):
        await _persist(service)

    cache.invalidate.assert_not_awaited()


async def test_award_then_read_returns_fresh_points_end_to_end():
    points = Points()
    await points.read("alice")
    service, _, _ = _persisting_service(points.cache)
    points.awards["alice"][1] = 4
    await _persist(service, external_user_id="alice")
    assert [r.points for r in await points.read("alice")] == [5, 4]


def _actions_service(cache):
    repo = AsyncMock()
    repo.create.return_value = SimpleNamespace(
        typeAction="t", description="d", model_dump=lambda: {}
    )
    users = AsyncMock()
    users.get_or_create_by_externalUserId.return_value = SimpleNamespace(id=uuid4())
    users.read_by_column.return_value = SimpleNamespace(id=uuid4())
    tasks = AsyncMock()
    tasks.read_by_gameId_and_externalTaskId.return_value = SimpleNamespace(
        status="open", gameId=uuid4()
    )
    return UserActionsService(
        user_actions_repository=repo,
        users_repository=users,
        game_repository=AsyncMock(),
        task_repository=tasks,
        response_cache=cache,
    )


async def test_action_in_task_invalidates_the_user():
    cache = _spy_cache()
    service = _actions_service(cache)
    action = AddActionDidByUserInTask(
        typeAction="click", data={}, description="d", externalUserId="alice"
    )
    service.user_actions_repository.create.return_value = SimpleNamespace(
        model_dump=lambda: {
            "typeAction": "click",
            "data": {},
            "description": "d",
            "userId": str(uuid4()),
            "id": uuid4(),
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    result = await service.user_add_action_in_task(str(uuid4()), "t1", action)
    assert result.externalUserId == "alice"
    cache.invalidate.assert_awaited_once_with("user:alice:activity")


async def test_action_in_task_failure_before_write_does_not_invalidate():
    cache = _spy_cache()
    service = _actions_service(cache)
    service.task_repository.read_by_gameId_and_externalTaskId.return_value = None
    action = AddActionDidByUserInTask(
        typeAction="click", data={}, description="d", externalUserId="alice"
    )
    with pytest.raises(NotFoundError):
        await service.user_add_action_in_task(str(uuid4()), "t1", action)
    cache.invalidate.assert_not_awaited()


async def test_default_action_invalidates_the_user():
    cache = _spy_cache()
    service = _actions_service(cache)
    await service.user_add_action_default(
        "bob",
        CreateUserBodyActions(
            typeAction="login", data={}, description="d", apiKey_used="key-a"
        ),
    )
    cache.invalidate.assert_awaited_once_with("user:bob:activity")


# --- one user's writes never touch another user's cached entry ---------------


async def _post_award(points, user):
    service, _, _ = _persisting_service(points.cache)
    await _persist(service, external_user_id=user)


async def _post_action_in_task(points, user):
    service = _actions_service(points.cache)
    service.user_actions_repository.create.return_value = SimpleNamespace(
        model_dump=lambda: {
            "typeAction": "click",
            "data": {},
            "description": "d",
            "userId": str(uuid4()),
            "id": uuid4(),
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    await service.user_add_action_in_task(
        str(points.game_id),
        "t1",
        AddActionDidByUserInTask(
            typeAction="click", data={}, description="d", externalUserId=user
        ),
    )


async def _post_default_action(points, user):
    await _actions_service(points.cache).user_add_action_default(
        user,
        CreateUserBodyActions(
            typeAction="login", data={}, description="d", apiKey_used="key-a"
        ),
    )


async def _post_user_service_award(points, user):
    from app.services.user_service import UserService

    service = UserService(
        user_repository=AsyncMock(),
        user_points_repository=AsyncMock(),
        task_repository=AsyncMock(),
        wallet_repository=AsyncMock(),
        wallet_transaction_repository=AsyncMock(),
        response_cache=points.cache,
    )
    service.user_repository.read_by_id.return_value = SimpleNamespace(
        id=uuid4(), externalUserId=user
    )
    service.user_points_repository.get_user_measurement_count.return_value = 1
    service.user_points_repository.get_start_time_for_last_task.return_value = None
    service.user_points_repository.get_time_taken_for_last_task.return_value = None
    service.user_points_repository.get_individual_calculation.return_value = 1
    service.user_points_repository.get_global_calculation.return_value = 1
    # Everything after the points row is written is irrelevant here.
    service.wallet_repository.read_by_column.side_effect = RuntimeError("stop")
    from app.schema.user_points_schema import BaseUserPointsBaseModelWithCaseName

    with pytest.raises(RuntimeError, match="stop"):
        await service.assign_points_to_user(
            str(uuid4()),
            BaseUserPointsBaseModelWithCaseName(
                userId=str(uuid4()),
                taskId=str(uuid4()),
                caseName="c",
                points=0,
                description="d",
                data={},
            ),
        )


@pytest.mark.parametrize(
    "write",
    [
        _post_award,
        _post_action_in_task,
        _post_default_action,
        _post_user_service_award,
    ],
    ids=["award", "action_in_task", "default_action", "user_service_award"],
)
async def test_a_users_write_keeps_other_users_cached(write):
    points = Points()
    points.awards["carol"] = [2, 2]
    for user in ("alice", "bob", "carol"):
        await points.read(user)
    base = points.queries()

    await write(points, "bob")

    for user in ("alice", "carol"):
        await points.read(user)
    assert points.queries() == base, "another user's entry was invalidated"
    await points.read("bob")
    assert points.queries() == base + 1, "the writing user's entry was not"
