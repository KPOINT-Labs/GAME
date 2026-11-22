"""
Game-definition reads served from the response cache, and the writes that
invalidate them.

Services run with real model rows and a ``RedisResponseCache`` on fakeredis;
repositories are mocks, so "served from cache" means "no repository call".
Parity tests render each result through the endpoint's ``response_model``
in a throwaway FastAPI app, so the HTTP body is compared, not Python objects.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import ForbiddenError
from app.model.game_params import GamesParams
from app.model.games import Games
from app.model.task_params import TasksParams
from app.model.tasks import Tasks
from app.repository.game_params_repository import GameParamsRepository
from app.repository.game_repository import GameRepository
from app.repository.task_params_repository import TaskParamsRepository
from app.repository.task_repository import TaskRepository
from app.repository.user_points_repository import UserPointsRepository
from app.repository.user_repository import UserRepository
from app.schema.games_schema import (
    BaseGameResult,
    FindGameResult,
    PatchGame,
    PostCreateGame,
    PostFindGame,
)
from app.schema.strategy_schema import Strategy
from app.schema.task_schema import (
    CreateTaskPost,
    CreateTaskPostSuccesfullyCreated,
    FoundTasks,
    PostFindTask,
)
from app.services.game_service import GameService
from app.services.redis_resilience import RedisCircuitBreaker
from app.services.response_cache import (
    GAMES_ALL,
    GAMES_LIST,
    TTL_GAME_DEFINITION,
    TTL_GAMES_LIST,
    NullResponseCache,
    RedisResponseCache,
)
from app.services.strategy_definition_service import StrategyDefinitionService
from app.services.strategy_service import StrategyService
from app.services.task_service import TaskService

fakeredis = pytest.importorskip("fakeredis")

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
UPDATED = datetime(2026, 1, 2, 12, 30, tzinfo=timezone.utc)


def _redis_cache():
    return RedisResponseCache(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        circuit=RedisCircuitBreaker("response_cache", 5),
        ttls={TTL_GAME_DEFINITION: 600, TTL_GAMES_LIST: 300},
    )


def _render(response_model, value):
    """The JSON body FastAPI sends for ``value`` under ``response_model``."""
    app = FastAPI()

    @app.get("/", response_model=response_model)
    async def endpoint():
        return value

    with TestClient(app) as client:
        return client.get("/").json()


def _strategy():
    return {
        "id": "default",
        "name": "Default",
        "description": "desc",
        "version": "0.0.2",
        "variables": {"basicPoints": 1, "bonusPoints": 1.5},
        "hash_version": "hash",
    }


class World:
    """One game with params and two tasks, behind mock repositories."""

    def __init__(self, cache):
        self.game_id = uuid4()
        self.game = Games(
            id=self.game_id,
            externalGameId="game-1",
            strategyId="default",
            platform="web",
            timezone="Europe/Madrid",
            apiKey_used="key-a",
            created_at=CREATED,
            updated_at=UPDATED,
        )
        self.game_params = [
            GamesParams(
                id=uuid4(),
                key="basicPoints",
                value="5",
                gameId=self.game_id,
                created_at=CREATED,
                updated_at=UPDATED,
            )
        ]
        self.tasks = [
            Tasks(
                id=uuid4(),
                externalTaskId=f"task-{i}",
                gameId=self.game_id,
                strategyId="default",
                status="open",
                created_at=CREATED,
                updated_at=UPDATED,
            )
            for i in range(2)
        ]
        self.task_params = [
            TasksParams(
                id=uuid4(),
                key="bonusPoints",
                value="2.5",
                taskId=self.tasks[0].id,
                created_at=CREATED,
                updated_at=UPDATED,
            )
        ]

        self.game_repository = MagicMock(spec=GameRepository)
        self.game_repository.read_by_id = AsyncMock(side_effect=self._read_game)
        self.game_repository.read_by_column = AsyncMock(side_effect=self._read_game_by)
        self.game_repository.get_all_games = AsyncMock(
            side_effect=lambda *a, **k: FindGameResult(
                items=[
                    BaseGameResult(
                        gameId=self.game.id,
                        externalGameId=self.game.externalGameId,
                        strategyId=self.game.strategyId,
                        platform=self.game.platform,
                        timezone=self.game.timezone,
                        created_at=self.game.created_at,
                        updated_at=self.game.updated_at,
                        params=[
                            {"id": p.id, "key": p.key, "value": p.value}
                            for p in self.game_params
                        ],
                    )
                ],
                search_options={
                    "page": 1,
                    "page_size": 10,
                    "ordering": "-id",
                    "total_count": 1,
                },
            )
        )
        self.game_params_repository = MagicMock(spec=GameParamsRepository)
        self.game_params_repository.read_by_column = AsyncMock(
            side_effect=lambda *a, **k: [p.model_copy() for p in self.game_params]
        )
        self.task_repository = MagicMock(spec=TaskRepository)
        self.task_repository.read_by_gameId = AsyncMock(
            side_effect=lambda q: {
                "items": list(self.tasks),
                "search_options": {
                    "page": 1,
                    "page_size": 10,
                    "ordering": "-id",
                    "total_count": len(self.tasks),
                },
            }
        )
        self.task_repository.read_by_gameId_and_externalTaskId = AsyncMock(
            side_effect=lambda game_id, ext: next(
                (t for t in self.tasks if t.externalTaskId == ext), None
            )
        )
        self.task_params_repository = MagicMock(spec=TaskParamsRepository)
        self.task_params_repository.read_by_column = AsyncMock(
            side_effect=lambda col, task_id, **k: [
                p.model_copy() for p in self.task_params if p.taskId == task_id
            ]
        )
        self.strategy_service = MagicMock(spec=StrategyService)
        self.strategy_service.get_strategy_by_id.side_effect = lambda _id: _strategy()

        self.game_service = GameService(
            game_repository=self.game_repository,
            game_params_repository=self.game_params_repository,
            task_repository=self.task_repository,
            user_points_repository=MagicMock(spec=UserPointsRepository),
            strategy_service=self.strategy_service,
            task_params_repository=self.task_params_repository,
            response_cache=cache,
        )
        self.task_service = TaskService(
            strategy_service=self.strategy_service,
            task_repository=self.task_repository,
            game_repository=self.game_repository,
            user_repository=MagicMock(spec=UserRepository),
            user_points_repository=MagicMock(spec=UserPointsRepository),
            game_params_repository=self.game_params_repository,
            task_params_repository=self.task_params_repository,
            response_cache=cache,
        )

    async def _read_game(self, game_id, **kwargs):
        return self.game if str(game_id) == str(self.game_id) else None

    async def _read_game_by(self, column, value, **kwargs):
        if column in ("id", "externalGameId") and str(value) in (
            str(self.game_id),
            self.game.externalGameId,
        ):
            return self.game
        return None

    def db_calls(self):
        return (
            self.game_repository.read_by_id.await_count
            + self.game_repository.read_by_column.await_count
            + self.game_repository.get_all_games.await_count
            + self.game_params_repository.read_by_column.await_count
            + self.task_repository.read_by_gameId.await_count
            + self.task_repository.read_by_gameId_and_externalTaskId.await_count
            + self.task_params_repository.read_by_column.await_count
        )


SCOPE = {"api_key": "key-a", "enforce_scope": True}


def _reads(world):
    """(name, response_model, coroutine factory) for every cached read."""
    return [
        (
            "game",
            BaseGameResult,
            lambda: world.game_service.get_by_gameId(world.game_id, **SCOPE),
        ),
        (
            "game unscoped",
            BaseGameResult,
            lambda: world.game_service.get_by_gameId(world.game_id),
        ),
        (
            "strategy",
            Strategy,
            lambda: world.game_service.get_strategy_by_gameId(world.game_id, **SCOPE),
        ),
        (
            "games list",
            FindGameResult,
            lambda: world.game_service.get_all_games(PostFindGame(), api_key="key-a"),
        ),
        (
            "tasks",
            FoundTasks,
            lambda: world.task_service.get_tasks_list_by_gameId(
                world.game_id, PostFindTask(), **SCOPE
            ),
        ),
        (
            "task",
            CreateTaskPostSuccesfullyCreated,
            lambda: world.task_service.get_task_by_gameId_externalTaskId(
                world.game_id, "task-0", **SCOPE
            ),
        ),
    ]


@pytest.mark.parametrize("index", range(6))
async def test_cached_responses_match_uncached(index):
    plain = World(NullResponseCache())
    name, model, read = _reads(plain)[index]
    expected = _render(model, await read())

    cached = World(_redis_cache())
    cached.game_id = plain.game_id  # same ids, so the bodies are comparable
    cached.game = plain.game
    cached.game_params = plain.game_params
    cached.tasks = plain.tasks
    cached.task_params = plain.task_params
    _, _, cached_read = _reads(cached)[index]

    miss = _render(model, await cached_read())
    calls = cached.db_calls()
    hit = _render(model, await cached_read())

    assert miss == expected, name
    assert hit == expected, name
    assert cached.db_calls() == calls, f"{name}: hit queried the database"


async def test_hit_enforces_game_scope():
    world = World(_redis_cache())
    await world.game_service.get_by_gameId(world.game_id, **SCOPE)
    with pytest.raises(ForbiddenError):
        await world.game_service.get_by_gameId(
            world.game_id, api_key="key-b", enforce_scope=True
        )


async def test_games_list_is_cached_per_caller_scope():
    world = World(_redis_cache())
    svc = world.game_service
    await svc.get_all_games(PostFindGame(), api_key="key-a")
    await svc.get_all_games(PostFindGame(), api_key="key-a")
    await svc.get_all_games(PostFindGame(), api_key="key-b")
    await svc.get_all_games(PostFindGame(), oauth_user_id="admin", is_admin=True)
    await svc.get_all_games(PostFindGame(), oauth_user_id="admin-2", is_admin=True)
    await svc.get_all_games(PostFindGame(page=2), api_key="key-a")
    # key-a, key-b, admin (shared by admins), key-a page 2
    assert world.game_repository.get_all_games.await_count == 4


async def _assert_invalidated(world, read, write):
    await read()
    before = world.db_calls()
    await read()
    assert world.db_calls() == before  # cached
    await write()
    await read()
    assert world.db_calls() > before  # recomputed


async def test_patch_game_invalidates_game_reads_and_list():
    world = World(_redis_cache())
    svc = world.game_service
    world.game_repository.patch_game_by_id = AsyncMock(return_value=world.game)

    async def write():
        await svc.patch_game_by_id(world.game_id, PatchGame(platform="mobile"))

    for read in (
        lambda: svc.get_by_gameId(world.game_id),
        lambda: svc.get_strategy_by_gameId(world.game_id),
        lambda: svc.get_all_games(PostFindGame(), api_key="key-a"),
        lambda: world.task_service.get_tasks_list_by_gameId(
            world.game_id, PostFindTask()
        ),
    ):
        await _assert_invalidated(world, read, write)


async def test_failed_patch_write_still_invalidates():
    world = World(_redis_cache())
    svc = world.game_service
    world.game_repository.patch_game_by_id = AsyncMock(
        side_effect=RuntimeError("db error after params were written")
    )

    async def write():
        with pytest.raises(RuntimeError):
            await svc.patch_game_by_id(world.game_id, PatchGame(platform="mobile"))

    await _assert_invalidated(world, lambda: svc.get_by_gameId(world.game_id), write)


async def test_delete_game_invalidates_definition_and_list():
    world = World(_redis_cache())
    svc = world.game_service
    world.game_repository.delete_game_by_id = AsyncMock(return_value=True)

    async def write():
        await svc.delete_game_by_id(world.game_id)

    await _assert_invalidated(world, lambda: svc.get_by_gameId(world.game_id), write)
    await _assert_invalidated(
        world, lambda: svc.get_all_games(PostFindGame(), api_key="key-a"), write
    )


async def test_create_game_invalidates_list_only():
    world = World(_redis_cache())
    svc = world.game_service
    world.game_repository.read_by_column = AsyncMock(return_value=None)
    world.game_repository.create = AsyncMock(return_value=world.game)
    world.game_params_repository.create = AsyncMock(return_value=world.game_params[0])

    async def write():
        await svc.create(
            PostCreateGame(
                externalGameId="game_new",
                platform="web",
                params=[{"key": "basicPoints", "value": 3}],
            ),
            api_key="key-a",
        )

    await _assert_invalidated(
        world, lambda: svc.get_all_games(PostFindGame(), api_key="key-a"), write
    )
    # A game's own cached definition is unaffected by another game's creation.
    world.game_repository.read_by_id = AsyncMock(return_value=world.game)
    await svc.get_by_gameId(world.game_id, **SCOPE)
    await write()
    before = world.db_calls()
    await svc.get_by_gameId(world.game_id, **SCOPE)
    assert world.db_calls() == before


async def test_duplicate_game_invalidates_new_game_after_copying_tasks():
    world = World(_redis_cache())
    svc = world.game_service
    new_game = world.game.model_copy(update={"id": uuid4(), "externalGameId": "copy"})
    svc.create = AsyncMock(
        return_value=MagicMock(gameId=new_game.id, message="created")
    )
    world.task_repository.read_by_column = AsyncMock(return_value=world.tasks)
    world.task_repository.create = AsyncMock(
        side_effect=lambda t: Tasks(id=uuid4(), **t.model_dump(exclude={"apiKey_used"}))
    )
    world.task_params_repository.create = AsyncMock()
    svc.response_cache.invalidate = AsyncMock(wraps=svc.response_cache.invalidate)

    await svc.duplicate_game(world.game_id, "copy")

    svc.response_cache.invalidate.assert_awaited_once_with(f"game:{new_game.id}:def")
    assert world.task_repository.create.await_count == 2


async def test_task_writes_invalidate_the_games_task_reads():
    world = World(_redis_cache())
    tasks = world.task_service
    task = world.tasks[0]
    world.task_repository.read_by_id = AsyncMock(return_value=task)
    world.task_repository.patch_by_id = AsyncMock(return_value=task)
    world.task_repository.delete_task_by_id = AsyncMock()
    world.task_repository.create = AsyncMock(return_value=task)
    world.task_params_repository.create = AsyncMock(return_value=world.task_params[0])
    from app.schema.task_schema import PatchTask

    reads = (
        lambda: tasks.get_tasks_list_by_gameId(world.game_id, PostFindTask()),
        lambda: tasks.get_task_by_gameId_externalTaskId(world.game_id, "task-0"),
        lambda: world.game_service.get_by_gameId(world.game_id),
    )
    writes = (
        lambda: tasks.patch_task_by_id(
            world.game_id, task.id, PatchTask(status="closed")
        ),
        lambda: tasks.delete_task_by_id(world.game_id, task.id),
        lambda: tasks.create_task_by_game_id(
            world.game_id,
            CreateTaskPost(
                externalTaskId="task-new", params=[{"key": "bonusPoints", "value": 1}]
            ),
        ),
    )
    for write in writes:
        for read in reads:
            await _assert_invalidated(world, read, write)


async def test_task_write_in_another_game_keeps_cache():
    world = World(_redis_cache())
    other = World(world.task_service.response_cache)
    other.task_repository.read_by_id = AsyncMock(return_value=other.tasks[0])
    other.task_repository.delete_task_by_id = AsyncMock()

    await world.task_service.get_tasks_list_by_gameId(world.game_id, PostFindTask())
    before = world.db_calls()
    await other.task_service.delete_task_by_id(other.game_id, other.tasks[0].id)
    await world.task_service.get_tasks_list_by_gameId(world.game_id, PostFindTask())
    assert world.db_calls() == before


def _rollback_service(cache, displaced):
    repo = MagicMock()
    current = MagicMock(name="current")
    current.name = "s"
    target = MagicMock(id=uuid4())
    repo.get_for_realm = AsyncMock(return_value=current)
    repo.get_version = AsyncMock(return_value=target)
    repo.get_published = AsyncMock(return_value=displaced)
    repo.set_status = AsyncMock()
    game_repository = MagicMock()
    game_repository.bulk_update_strategy_id = AsyncMock(return_value=3)
    task_repository = MagicMock()
    task_repository.bulk_update_strategy_id = AsyncMock(return_value=4)
    service = StrategyDefinitionService(
        repo,
        game_repository=game_repository,
        task_repository=task_repository,
        response_cache=cache,
    )
    service.get_strategy = AsyncMock(return_value=MagicMock())
    return service


async def test_strategy_rollback_cascade_invalidates_every_game():
    cache = MagicMock()
    cache.invalidating = NullResponseCache().invalidating
    events = []

    def invalidating(*entities):
        events.append(entities)
        return NullResponseCache().invalidating(*entities)

    cache.invalidating = invalidating
    service = _rollback_service(cache, displaced=MagicMock(id=uuid4()))
    result = await service.rollback(id=str(uuid4()), target_version=1, realmId="r")
    assert events == [(GAMES_ALL, GAMES_LIST)]
    assert result.games_reassigned == 3


async def test_strategy_rollback_without_cascade_invalidates_nothing():
    cache = MagicMock()
    service = _rollback_service(cache, displaced=None)
    await service.rollback(id=str(uuid4()), target_version=1, realmId="r")
    cache.invalidating.assert_not_called()


async def test_strategy_rollback_cascade_drops_cached_game_definitions():
    world = World(_redis_cache())
    service = _rollback_service(
        world.game_service.response_cache, displaced=MagicMock(id=uuid4())
    )
    await _assert_invalidated(
        world,
        lambda: world.game_service.get_by_gameId(world.game_id),
        lambda: service.rollback(id=str(uuid4()), target_version=1, realmId="r"),
    )
