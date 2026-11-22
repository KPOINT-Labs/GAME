"""
DSL field ``user.streak_days``: the analytics method on real repositories
(SQLite), and the field read by a DSL program.

"Now" is frozen at 2026-11-18 14:00 UTC = 19:30 on 2026-11-18 in Kolkata.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.engine.dsl_execution_context import ExecutionContext
from app.engine.dsl_interpreter import DslInterpreter
from app.engine.dsl_validator import validate_ast
from app.model.games import Games
from app.model.tasks import Tasks
from app.model.user_points import UserPoints
from app.model.users import Users
from app.repository.user_points_repository import UserPointsRepository
from app.services.user_points_analytics_service import UserPointsAnalyticsService

UTC = datetime.timezone.utc
FIXED_NOW = datetime.datetime(2026, 11, 18, 14, 0, tzinfo=UTC)
TODAY = datetime.date(2026, 11, 18)


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def frozen_now():
    fake = SimpleNamespace(
        datetime=_FrozenDateTime,
        date=datetime.date,
        time=datetime.time,
        timedelta=datetime.timedelta,
        timezone=datetime.timezone,
    )
    with patch("app.services.user_points_analytics_service.datetime", fake):
        yield


@pytest.fixture
def analytics(session_factory):
    return UserPointsAnalyticsService(
        user_points_repository=UserPointsRepository(session_factory=session_factory)
    )


async def _add(db_session, obj):
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


async def _setup(db_session, timezone="UTC"):
    game = await _add(
        db_session,
        Games(
            externalGameId="g_streak",
            platform="web",
            strategyId="default",
            timezone=timezone,
        ),
    )
    other = await _add(
        db_session,
        Games(externalGameId="g_other", platform="web", strategyId="default"),
    )
    task = await _add(
        db_session,
        Tasks(externalTaskId="video_view", gameId=game.id, strategyId="default"),
    )
    task2 = await _add(
        db_session, Tasks(externalTaskId="like", gameId=game.id, strategyId="default")
    )
    other_task = await _add(
        db_session,
        Tasks(externalTaskId="video_view", gameId=other.id, strategyId="default"),
    )
    alice = await _add(db_session, Users(externalUserId="alice"))
    bob = await _add(db_session, Users(externalUserId="bob"))
    return SimpleNamespace(
        task=task, task2=task2, other_task=other_task, alice=alice, bob=bob
    )


async def _award_on(db_session, user, task, day, hour=6, points=10):
    when = datetime.datetime.combine(day, datetime.time(hour), UTC)
    await _add(
        db_session,
        UserPoints(userId=user.id, taskId=task.id, points=points, created_at=when),
    )


def _back(n):
    return TODAY - datetime.timedelta(days=n)


async def _streak(analytics, user="alice", game="g_streak"):
    return await analytics.get_user_streak_days_including_today(game, user)


@pytest.mark.asyncio
async def test_first_ever_award_is_a_one_day_streak(analytics, db_session):
    await _setup(db_session)

    assert await _streak(analytics) == 1


@pytest.mark.asyncio
async def test_unknown_user_or_game_is_a_one_day_streak(analytics, db_session):
    await _setup(db_session)

    assert await _streak(analytics, user="nobody") == 1
    assert await _streak(analytics, game="no_such_game") == 1


@pytest.mark.asyncio
async def test_streak_through_yesterday_counts_today(analytics, db_session):
    s = await _setup(db_session)
    for n in (1, 2, 3):
        await _award_on(db_session, s.alice, s.task, _back(n))

    assert await _streak(analytics) == 4


@pytest.mark.asyncio
async def test_value_is_stable_across_awards_of_the_same_day(analytics, db_session):
    s = await _setup(db_session)
    for n in (1, 2):
        await _award_on(db_session, s.alice, s.task, _back(n))
    before_first_award_today = await _streak(analytics)
    await _award_on(db_session, s.alice, s.task, TODAY, hour=9)

    assert before_first_award_today == 3
    assert await _streak(analytics) == 3


@pytest.mark.asyncio
async def test_a_missed_day_resets_to_one(analytics, db_session):
    s = await _setup(db_session)
    for n in (2, 3, 4):
        await _award_on(db_session, s.alice, s.task, _back(n))

    assert await _streak(analytics) == 1


@pytest.mark.asyncio
async def test_any_task_of_the_game_counts_but_not_other_games_or_users(
    analytics, db_session
):
    s = await _setup(db_session)
    await _award_on(db_session, s.alice, s.task, _back(1))
    await _award_on(db_session, s.alice, s.task2, _back(2))  # other task, same game
    await _award_on(db_session, s.alice, s.other_task, _back(3))  # other game
    await _award_on(db_session, s.bob, s.task, _back(3))  # other user

    assert await _streak(analytics) == 3


@pytest.mark.asyncio
async def test_days_are_the_games_local_days(analytics, db_session):
    s = await _setup(db_session, timezone="Asia/Kolkata")
    # 2026-11-16 20:00 UTC is 01:30 on 2026-11-17 in Kolkata (yesterday there).
    await _add(
        db_session,
        UserPoints(
            userId=s.alice.id,
            taskId=s.task.id,
            points=1,
            created_at=datetime.datetime(2026, 11, 16, 20, 0, tzinfo=UTC),
        ),
    )

    assert await _streak(analytics) == 2


@pytest.mark.asyncio
async def test_dsl_program_reads_user_streak_days(analytics, db_session):
    s = await _setup(db_session)
    for n in range(1, 7):  # 6 days ending yesterday -> 7 with today
        await _award_on(db_session, s.alice, s.task, _back(n))
    ast = {
        "type": "program",
        "rules": [
            {
                "type": "rule",
                "when": {
                    "type": "compare",
                    "op": ">=",
                    "left": {"type": "field", "path": "user.streak_days"},
                    "right": {"type": "literal", "value": 7},
                },
                "then": [
                    {
                        "type": "assign_points",
                        "value": {"type": "field", "path": "user.streak_days"},
                        "case_name": "Streak7",
                    }
                ],
            }
        ],
        "default": {
            "type": "assign_points",
            "value": {"type": "literal", "value": 0},
            "case_name": "NoStreak",
        },
    }
    validate_ast(ast)
    ctx = await ExecutionContext.build_for_ast(
        ast,
        externalGameId="g_streak",
        externalTaskId="daily_streak",
        externalUserId="alice",
        data={},
        analytics_service=analytics,
    )

    result = await DslInterpreter(max_nodes=1000, max_depth=32).execute(ast, ctx)

    assert ctx.resolved_fields["user.streak_days"] == 7
    assert result["points"] == 7
    assert result["case_name"] == "Streak7"
