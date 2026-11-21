"""
Cohort flow against in-memory aiosqlite, through the real services and
repositories:

1. A user action with ``cohorts`` sets the user's current cohorts in the game.
2. Each points award snapshots those cohorts (or the award's own ``cohorts``).
3. Cohort leaderboards rank only the awards tagged with the cohort.

Two inserts are done here directly rather than through their services: the
action row and the points row. Both services pass ids as strings, which
Postgres accepts but the SQLite test engine's UUID binding does not. Nothing
cohort-related is faked; that the points write calls the cohort snapshot in
its transaction is covered by ``tests/unit_tests/services/test_cohorts.py``.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.model.games import Games
from app.model.tasks import Tasks
from app.model.user_points import UserPoints
from app.repository.cohort_repository import CohortRepository
from app.repository.game_repository import GameRepository
from app.repository.task_repository import TaskRepository
from app.repository.user_game_config_repository import UserGameConfigRepository
from app.repository.user_points_repository import UserPointsRepository
from app.repository.user_repository import UserRepository
from app.schema.task_schema import AddActionDidByUserInTask
from app.services.user_actions_service import UserActionsService
from app.services.user_points_service import UserPointsService


class _FakeActionsRepository:
    def __init__(self, session_factory):
        # The action + cohort change share one transaction on this factory.
        self.session_factory = session_factory

    async def create(self, action, session=None, auto_commit=True):
        stored = {
            **action.model_dump(),
            "id": str(uuid4()),
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        return SimpleNamespace(**stored, model_dump=lambda: stored)


@pytest.fixture
def repos(session_factory):
    return {
        "cohorts": CohortRepository(session_factory),
        "points": UserPointsRepository(session_factory=session_factory),
        "users": UserRepository(session_factory=session_factory),
        "games": GameRepository(session_factory=session_factory),
        "tasks": TaskRepository(session_factory=session_factory),
    }


@pytest.fixture
def actions_service(repos, session_factory):
    return UserActionsService(
        # Storing the action row itself is not under test, and its insert
        # passes the user id as a string, which only Postgres accepts.
        # Everything cohort-related below uses the real database.
        user_actions_repository=_FakeActionsRepository(session_factory),
        users_repository=repos["users"],
        game_repository=repos["games"],
        task_repository=repos["tasks"],
        cohort_repository=repos["cohorts"],
    )


@pytest.fixture
def points_service(repos, session_factory):
    return UserPointsService(
        user_points_repository=repos["points"],
        users_repository=repos["users"],
        users_game_config_repository=UserGameConfigRepository(session_factory),
        game_repository=repos["games"],
        task_repository=repos["tasks"],
        wallet_repository=None,
        wallet_transaction_repository=None,
        cohort_repository=repos["cohorts"],
    )


@pytest.fixture
async def game(db_session):
    game = Games(externalGameId="g-flow", platform="web", strategyId="default")
    db_session.add(game)
    await db_session.commit()
    await db_session.refresh(game)
    tasks = {}
    for ext in ("quiz", "walk"):
        task = Tasks(externalTaskId=ext, gameId=game.id, strategyId="default")
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        tasks[ext] = task
    return SimpleNamespace(id=game.id, tasks_by_ext=tasks)


async def _act(actions_service, game, user, cohorts):
    await actions_service.user_add_action_in_task(
        game.id,
        "quiz",
        AddActionDidByUserInTask(
            typeAction="JOIN",
            data={},
            description="joined",
            externalUserId=user,
            cohorts=cohorts,
        ),
    )


async def _award(
    points_service, repos, session_factory, game, user, task, points, cohorts=None
):
    """Write a points row and run the same cohort snapshot the award path uses,
    in one transaction."""
    user_row = await repos["users"].get_or_create_by_externalUserId(user)
    async with session_factory() as session:
        row = UserPoints(
            userId=user_row.id, taskId=game.tasks_by_ext[task].id, points=points
        )
        session.add(row)
        await session.flush()
        await points_service._attach_cohorts(
            session,
            user_points=row,
            user_id=user_row.id,
            game_id=game.id,
            cohorts=cohorts,
            api_key=None,
        )
        await session.commit()
    return row


async def _board(repos, game, cohort, task=None):
    rows, total = await repos["points"].get_leaderboard_by_gameId(
        game.id,
        limit=10,
        cohort=cohort,
        taskId=game.tasks_by_ext[task].id if task else None,
    )
    return [(r.rank, r.externalUserId, r.points) for r in rows], total


@pytest.mark.asyncio
async def test_action_cohorts_are_snapshotted_onto_awards(
    actions_service, points_service, repos, game, session_factory
):
    await _act(actions_service, game, "alice", ["school-a", "grade-7"])

    row = await _award(
        points_service, repos, session_factory, game, "alice", "quiz", 10
    )

    assert await repos["cohorts"].get_points_cohorts(row.id) == [
        "grade-7",
        "school-a",
    ]


@pytest.mark.asyncio
async def test_action_without_cohorts_leaves_them_unchanged(
    actions_service, points_service, repos, game, session_factory
):
    await _act(actions_service, game, "alice", ["school-a"])
    await _act(actions_service, game, "alice", None)

    row = await _award(
        points_service, repos, session_factory, game, "alice", "quiz", 10
    )

    assert await repos["cohorts"].get_points_cohorts(row.id) == ["school-a"]


@pytest.mark.asyncio
async def test_award_cohorts_override_and_empty_list_records_none(
    actions_service, points_service, repos, game, session_factory
):
    await _act(actions_service, game, "alice", ["school-a"])

    explicit = await _award(
        points_service,
        repos,
        session_factory,
        game,
        "alice",
        "quiz",
        5,
        cohorts=["event-x"],
    )
    none = await _award(
        points_service, repos, session_factory, game, "alice", "quiz", 5, cohorts=[]
    )

    assert await repos["cohorts"].get_points_cohorts(explicit.id) == ["event-x"]
    assert await repos["cohorts"].get_points_cohorts(none.id) == []


@pytest.mark.asyncio
async def test_cohort_leaderboard_keeps_points_in_cohort_earned(
    actions_service, points_service, repos, game, session_factory
):
    # alice earns 10 in school-a, then moves to school-b and earns 4 there.
    await _act(actions_service, game, "alice", ["school-a"])
    await _award(points_service, repos, session_factory, game, "alice", "quiz", 10)
    await _act(actions_service, game, "alice", ["school-b"])
    await _award(points_service, repos, session_factory, game, "alice", "walk", 4)
    # bob is in school-a throughout; carol has no cohorts.
    await _act(actions_service, game, "bob", ["school-a"])
    await _award(points_service, repos, session_factory, game, "bob", "quiz", 7)
    await _award(points_service, repos, session_factory, game, "carol", "quiz", 50)

    assert await _board(repos, game, "school-a") == (
        [(1, "alice", 10), (2, "bob", 7)],
        2,
    )
    assert await _board(repos, game, "school-b") == ([(1, "alice", 4)], 1)
    # Without a cohort, every award counts as before.
    assert await _board(repos, game, None) == (
        [(1, "carol", 50), (2, "alice", 14), (3, "bob", 7)],
        3,
    )


@pytest.mark.asyncio
async def test_cohort_and_task_filters_combine(
    actions_service, points_service, repos, game, session_factory
):
    await _act(actions_service, game, "alice", ["school-a"])
    await _award(points_service, repos, session_factory, game, "alice", "quiz", 10)
    await _award(points_service, repos, session_factory, game, "alice", "walk", 3)
    await _act(actions_service, game, "bob", ["school-a"])
    await _award(points_service, repos, session_factory, game, "bob", "walk", 6)

    assert await _board(repos, game, "school-a", task="walk") == (
        [(1, "bob", 6), (2, "alice", 3)],
        2,
    )


@pytest.mark.asyncio
async def test_user_rank_is_within_cohort(
    actions_service, points_service, repos, game, session_factory
):
    await _act(actions_service, game, "alice", ["school-a"])
    await _award(points_service, repos, session_factory, game, "alice", "quiz", 10)
    await _act(actions_service, game, "bob", ["school-b"])
    await _award(points_service, repos, session_factory, game, "bob", "quiz", 99)

    alice = await repos["points"].get_leaderboard_rank_of_user(
        game.id, "alice", cohort="school-a"
    )
    bob = await repos["points"].get_leaderboard_rank_of_user(
        game.id, "bob", cohort="school-a"
    )

    # bob outscores alice overall but is not in school-a.
    assert (alice.rank, alice.points) == (1, 10)
    assert bob is None
