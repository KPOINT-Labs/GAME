"""
Integration tests for ``CohortRepository`` against in-memory aiosqlite.
"""

import pytest

from app.model.games import Games
from app.model.tasks import Tasks
from app.model.user_points import UserPoints
from app.model.users import Users
from app.repository.cohort_repository import CohortRepository


@pytest.fixture
def repository(session_factory):
    return CohortRepository(session_factory=session_factory)


async def _add(db_session, obj):
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


async def _user_and_games(db_session):
    user = await _add(db_session, Users(externalUserId="cohort-user"))
    game = await _add(
        db_session, Games(externalGameId="g-c1", platform="web", strategyId="default")
    )
    other = await _add(
        db_session, Games(externalGameId="g-c2", platform="web", strategyId="default")
    )
    return user, game, other


@pytest.mark.asyncio
async def test_replace_then_get_returns_sorted_cohorts(repository, db_session):
    user, game, _ = await _user_and_games(db_session)

    await repository.replace_user_game_cohorts(user.id, game.id, ["b-team", "a-team"])

    assert await repository.get_user_game_cohorts(user.id, game.id) == [
        "a-team",
        "b-team",
    ]


@pytest.mark.asyncio
async def test_replace_swaps_the_whole_set(repository, db_session):
    user, game, _ = await _user_and_games(db_session)
    await repository.replace_user_game_cohorts(user.id, game.id, ["grade-7", "x"])

    await repository.replace_user_game_cohorts(user.id, game.id, ["grade-8"])

    assert await repository.get_user_game_cohorts(user.id, game.id) == ["grade-8"]


@pytest.mark.asyncio
async def test_replace_with_empty_list_clears(repository, db_session):
    user, game, _ = await _user_and_games(db_session)
    await repository.replace_user_game_cohorts(user.id, game.id, ["grade-7"])

    await repository.replace_user_game_cohorts(user.id, game.id, [])

    assert await repository.get_user_game_cohorts(user.id, game.id) == []


@pytest.mark.asyncio
async def test_cohorts_are_scoped_per_game(repository, db_session):
    user, game, other = await _user_and_games(db_session)
    await repository.replace_user_game_cohorts(user.id, game.id, ["in-game-1"])
    await repository.replace_user_game_cohorts(user.id, other.id, ["in-game-2"])

    await repository.replace_user_game_cohorts(user.id, game.id, [])

    assert await repository.get_user_game_cohorts(user.id, game.id) == []
    assert await repository.get_user_game_cohorts(user.id, other.id) == ["in-game-2"]


@pytest.mark.asyncio
async def test_add_points_cohorts_in_caller_transaction(
    repository, db_session, session_factory
):
    user, game, _ = await _user_and_games(db_session)
    task = await _add(
        db_session, Tasks(externalTaskId="t-c", gameId=game.id, strategyId="default")
    )
    points = await _add(
        db_session, UserPoints(userId=user.id, taskId=task.id, points=3)
    )

    async with session_factory() as session:
        await repository.add_points_cohorts(
            points.id, ["school-1", "grade-7"], session=session
        )
        await session.commit()

    assert await repository.get_points_cohorts(points.id) == ["grade-7", "school-1"]


@pytest.mark.asyncio
async def test_add_points_cohorts_rolls_back_with_caller(
    repository, db_session, session_factory
):
    user, game, _ = await _user_and_games(db_session)
    task = await _add(
        db_session, Tasks(externalTaskId="t-rb", gameId=game.id, strategyId="default")
    )
    points = await _add(
        db_session, UserPoints(userId=user.id, taskId=task.id, points=3)
    )

    async with session_factory() as session:
        await repository.add_points_cohorts(points.id, ["school-1"], session=session)
        await session.rollback()

    assert await repository.get_points_cohorts(points.id) == []


def test_cohort_foreign_keys_cascade_on_delete():
    """Deleting a game (or a points award) must remove its cohort rows, or
    the delete fails on Postgres. SQLite here does not enforce foreign keys,
    so assert the cascade is declared; it was verified on Postgres against
    GameRepository.delete_game_by_id."""
    from app.model.cohorts import UserGameCohort, UserPointsCohort

    def ondelete(model, column):
        (fk,) = model.__table__.c[column].foreign_keys
        return fk.ondelete

    assert ondelete(UserGameCohort, "gameId") == "CASCADE"
    assert ondelete(UserPointsCohort, "userPointsId") == "CASCADE"
