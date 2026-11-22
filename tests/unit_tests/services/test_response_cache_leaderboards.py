"""
Leaderboards served from the response cache.

Point awards do not invalidate a cached leaderboard: it is served until
``RESPONSE_CACHE_LEADERBOARD_TTL_SECONDS`` passes. Game/task definition
changes still invalidate it. ``UserPointsService`` runs with mock
repositories and a ``RedisResponseCache`` on fakeredis.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import ForbiddenError, NotFoundError
from app.schema.user_points_schema import GameLeaderboard, TaskLeaderboard
from app.services.redis_resilience import RedisCircuitBreaker
from app.services.response_cache import (
    TTL_LEADERBOARD,
    NullResponseCache,
    RedisResponseCache,
    game_definition_entity,
)
from app.services.user_points_service import UserPointsService

fakeredis = pytest.importorskip("fakeredis")

LEADERBOARD_TTL = 21600


def _render(response_model, value):
    app = FastAPI()

    @app.get("/", response_model=response_model)
    async def endpoint():
        return value

    with TestClient(app) as client:
        return client.get("/").json()


def _row(rank, user, points, times=1):
    return SimpleNamespace(
        rank=rank, externalUserId=user, points=points, timesAwarded=times
    )


class Board:
    """A game with one task and a leaderboard that changes when awarded."""

    def __init__(self, cache, game_id=None):
        self.game_id = game_id or uuid4()
        self.game = SimpleNamespace(
            id=self.game_id,
            externalGameId="game-1",
            apiKey_used="key-a",
            oauth_user_id=None,
        )
        self.task = SimpleNamespace(id=uuid4(), externalTaskId="task-1")
        self.rows = [_row(1, "u1", 30, 3), _row(2, "u2", 10)]
        self.redis = (
            fakeredis.aioredis.FakeRedis(decode_responses=True)
            if cache is None
            else None
        )
        if cache is None:
            cache = RedisResponseCache(
                self.redis,
                circuit=RedisCircuitBreaker("response_cache", 5),
                ttls={TTL_LEADERBOARD: LEADERBOARD_TTL},
            )
        self.cache = cache

        self.game_repository = AsyncMock()
        self.game_repository.read_by_id.side_effect = self._game
        self.game_repository.read_by_column.side_effect = self._game_by_column
        self.task_repository = AsyncMock()
        self.task_repository.read_by_gameId_and_externalTaskId.side_effect = (
            lambda game_id, ext: (self.task if ext == "task-1" else None)
        )
        self.user_points_repository = AsyncMock()
        self.user_points_repository.get_leaderboard.side_effect = self._leaderboard
        self.service = UserPointsService(
            user_points_repository=self.user_points_repository,
            users_repository=AsyncMock(),
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
        # Like BaseRepository.read_by_column: raises when nothing matches.
        game = await self._game(value)
        if game is None:
            raise NotFoundError(detail=kwargs.get("not_found_message", "Not found"))
        return game

    async def _leaderboard(self, game_id, *, externalUserId=None, **kwargs):
        user = next((r for r in self.rows if r.externalUserId == externalUserId), None)
        return list(self.rows), len(self.rows), user

    def award(self, user, points):
        """What a point award does to the data (it does not touch the cache)."""
        for row in self.rows:
            if row.externalUserId == user:
                row.points += points
        self.rows.sort(key=lambda r: -r.points)
        for i, row in enumerate(self.rows, 1):
            row.rank = i

    def queries(self):
        return self.user_points_repository.get_leaderboard.await_count

    def game_board(self, **kwargs):
        kwargs.setdefault("api_key", "key-a")
        kwargs.setdefault("enforce_scope", True)
        return self.service.get_leaderboard_by_gameId(self.game_id, **kwargs)

    def task_board(self, ext="task-1", **kwargs):
        kwargs.setdefault("api_key", "key-a")
        kwargs.setdefault("enforce_scope", True)
        return self.service.get_task_leaderboard(self.game_id, ext, **kwargs)


@pytest.mark.parametrize(
    "kind, model",
    [("game_board", GameLeaderboard), ("task_board", TaskLeaderboard)],
)
@pytest.mark.parametrize("user", [None, "u2", "nobody"])
async def test_cached_leaderboard_matches_uncached(kind, model, user):
    plain = Board(NullResponseCache())
    cached = Board(None, game_id=plain.game_id)
    expected = _render(model, await getattr(plain, kind)(externalUserId=user))

    miss = _render(model, await getattr(cached, kind)(externalUserId=user))
    hit = _render(model, await getattr(cached, kind)(externalUserId=user))

    assert miss == hit == expected
    assert cached.queries() == 1


async def test_point_awards_do_not_refresh_a_cached_leaderboard():
    board = Board(None)
    first = await board.game_board(externalUserId="u2")

    board.award("u2", 100)  # u2 now leads in the database
    again = await board.game_board(externalUserId="u2")

    assert again == first  # served stale, by design
    assert again.entries[0].externalUserId == "u1"
    assert again.user.points == 10
    assert board.queries() == 1


async def test_leaderboard_entries_expire_after_the_leaderboard_ttl():
    board = Board(None)
    await board.game_board()
    await board.task_board()
    keys = [k async for k in board.redis.scan_iter("game:rc:e:*")]
    assert len(keys) == 2
    for key in keys:
        ttl = await board.redis.ttl(key)
        assert LEADERBOARD_TTL - 5 <= ttl <= LEADERBOARD_TTL

    # Once expired, the next read recomputes and shows the awards.
    board.award("u2", 100)
    for key in keys:
        await board.redis.delete(key)  # what expiry does
    fresh = await board.game_board()
    assert fresh.entries[0].externalUserId == "u2"


async def test_definition_changes_refresh_leaderboards():
    board = Board(None)
    await board.game_board()
    await board.task_board()
    board.award("u2", 100)

    # e.g. a task deleted (its points go too) or the game patched
    await board.cache.invalidate(game_definition_entity(board.game_id))

    assert (await board.game_board()).entries[0].externalUserId == "u2"
    assert (await board.task_board()).entries[0].externalUserId == "u2"
    assert board.queries() == 4


async def test_each_page_cohort_task_and_user_is_its_own_entry():
    board = Board(None)
    variants = [
        dict(),
        dict(limit=5),
        dict(offset=10),
        dict(cohort="school-1"),
        dict(externalUserId="u1"),
        dict(externalUserId="u2"),
    ]
    for kwargs in variants:
        await board.game_board(**kwargs)
        await board.game_board(**kwargs)
    await board.task_board()
    await board.task_board()
    assert board.queries() == len(variants) + 1


async def test_scope_is_checked_on_cache_hits():
    board = Board(None)
    await board.game_board()
    await board.task_board()
    with pytest.raises(ForbiddenError):
        await board.game_board(api_key="key-b")
    with pytest.raises(ForbiddenError):
        await board.task_board(api_key="key-b")
    admin = await board.game_board(api_key=None, oauth_user_id="a", is_admin=True)
    assert admin.totalUsers == 2
    assert board.queries() == 2


async def test_missing_game_or_task_is_not_cached():
    board = Board(None)
    for _ in range(2):
        with pytest.raises(NotFoundError):
            await board.task_board(ext="no-such-task")
    assert board.task_repository.read_by_gameId_and_externalTaskId.await_count == 2

    other = uuid4()
    for _ in range(2):
        with pytest.raises(NotFoundError):
            await board.service.get_leaderboard_by_gameId(other)
    assert board.queries() == 0


async def test_unscoped_internal_call_uses_column_lookup():
    board = Board(None)
    result = await board.service.get_leaderboard_by_gameId(board.game_id)
    assert result.externalGameId == "game-1"
    board.game_repository.read_by_column.assert_awaited_once()
