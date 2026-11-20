"""
GET /games/{gameId}/users/{externalUserId}/activity, through the real service
and repositories on SQLite.

"Now" is frozen at 2026-11-18 14:00 UTC = 19:30 on 2026-11-18 in Kolkata.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.model.games import Games
from app.model.tasks import Tasks
from app.model.user_points import UserPoints
from app.model.users import Users
from app.repository.game_repository import GameRepository
from app.repository.user_points_repository import UserPointsRepository
from app.repository.user_repository import UserRepository
from app.services.user_points_service import UserPointsService

UTC = datetime.timezone.utc
FIXED_NOW = datetime.datetime(2026, 11, 18, 14, 0, tzinfo=UTC)
D = datetime.date


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
    with patch("app.services.user_points.activity.datetime", fake):
        yield


@pytest.fixture
def service(session_factory):
    return UserPointsService(
        user_points_repository=UserPointsRepository(session_factory=session_factory),
        users_repository=UserRepository(session_factory=session_factory),
        users_game_config_repository=AsyncMock(),
        game_repository=GameRepository(session_factory=session_factory),
        task_repository=AsyncMock(),
        wallet_repository=AsyncMock(),
        wallet_transaction_repository=AsyncMock(),
    )


async def _add(db_session, obj):
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


async def _setup(db_session, timezone="Asia/Kolkata"):
    game = await _add(
        db_session,
        Games(
            externalGameId="g_act",
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
        db_session, Tasks(externalTaskId="t", gameId=game.id, strategyId="default")
    )
    other_task = await _add(
        db_session, Tasks(externalTaskId="t", gameId=other.id, strategyId="default")
    )
    alice = await _add(db_session, Users(externalUserId="alice"))
    bob = await _add(db_session, Users(externalUserId="bob"))
    return SimpleNamespace(
        game=game, task=task, other_task=other_task, alice=alice, bob=bob
    )


async def _award(db_session, user, task, when, points=10):
    await _add(
        db_session,
        UserPoints(userId=user.id, taskId=task.id, points=points, created_at=when),
    )


def _utc(month, day, hour, minute=0):
    return datetime.datetime(2026, month, day, hour, minute, tzinfo=UTC)


@pytest.mark.asyncio
async def test_days_are_local_days_with_points_and_award_counts(service, db_session):
    s = await _setup(db_session)
    await _award(
        db_session, s.alice, s.task, _utc(11, 10, 18, 29), points=5
    )  # 23:59 on the 10th (Kolkata)
    await _award(
        db_session, s.alice, s.task, _utc(11, 10, 18, 31), points=7
    )  # 00:01 on the 11th
    await _award(
        db_session, s.alice, s.task, _utc(11, 11, 3, 0), points=3
    )  # 08:30 on the 11th

    result = await service.get_user_activity(
        s.game.id, "alice", from_date=D(2026, 11, 1), to_date=D(2026, 11, 30)
    )

    assert result.timezone == "Asia/Kolkata"
    assert result.basis == "awards"
    assert [(d.date, d.points, d.awards) for d in result.activeDays] == [
        (D(2026, 11, 10), 5, 1),
        (D(2026, 11, 11), 10, 2),
    ]
    assert [(r.start, r.end, r.length) for r in result.streaks.runs] == [
        (D(2026, 11, 10), D(2026, 11, 11), 2)
    ]
    assert result.streaks.longest == 2


@pytest.mark.asyncio
async def test_default_period_is_30_local_days_ending_today(service, db_session):
    s = await _setup(db_session)

    result = await service.get_user_activity(s.game.id, "alice")

    assert (result.from_, result.to) == (D(2026, 10, 20), D(2026, 11, 18))
    dumped = result.model_dump(by_alias=True, mode="json")
    assert (dumped["from"], dumped["to"]) == ("2026-10-20", "2026-11-18")


@pytest.mark.asyncio
async def test_runs_are_cut_at_the_period_edges(service, db_session):
    s = await _setup(db_session)
    for day in range(1, 11):  # Nov 1..10, every day
        await _award(db_session, s.alice, s.task, _utc(11, day, 6))

    result = await service.get_user_activity(
        s.game.id, "alice", from_date=D(2026, 11, 4), to_date=D(2026, 11, 7)
    )

    assert [(r.start, r.end, r.length) for r in result.streaks.runs] == [
        (D(2026, 11, 4), D(2026, 11, 7), 4)
    ]
    assert result.streaks.longest == 4


@pytest.mark.asyncio
async def test_current_streak_spans_all_history_beyond_period_and_lookup_window(
    service, db_session
):
    s = await _setup(db_session)
    # 100 consecutive local days ending yesterday (no award yet today):
    # longer than the 30-day period and than the first 64-day lookup window.
    yesterday = D(2026, 11, 17)
    for back in range(100):
        day = yesterday - datetime.timedelta(days=back)
        await _award(
            db_session,
            s.alice,
            s.task,
            datetime.datetime.combine(day, datetime.time(6), UTC),
        )

    result = await service.get_user_activity(s.game.id, "alice")

    assert result.streaks.current == 100  # grace day: counts from yesterday
    assert result.streaks.longest == 29  # Oct 20 .. Nov 17 inside the period
    assert len(result.activeDays) == 29


@pytest.mark.asyncio
async def test_current_streak_counts_today_in_game_timezone(service, db_session):
    s = await _setup(db_session)
    # 18:31 UTC on the 17th is already the 18th (today) in Kolkata.
    await _award(db_session, s.alice, s.task, _utc(11, 17, 18, 31))
    await _award(db_session, s.alice, s.task, _utc(11, 17, 6, 0))  # the 17th, locally

    kolkata = await service.get_user_activity(s.game.id, "alice")

    assert kolkata.streaks.current == 2  # local 18th + 17th


@pytest.mark.asyncio
async def test_current_streak_is_zero_after_a_missed_day(service, db_session):
    s = await _setup(db_session, timezone="UTC")
    await _award(db_session, s.alice, s.task, _utc(11, 16, 6))  # two days ago

    result = await service.get_user_activity(s.game.id, "alice")

    assert result.streaks.current == 0
    assert [d.date for d in result.activeDays] == [D(2026, 11, 16)]


@pytest.mark.asyncio
async def test_only_this_users_awards_in_this_game_count(service, db_session):
    s = await _setup(db_session, timezone="UTC")
    await _award(db_session, s.bob, s.task, _utc(11, 18, 6))  # another user
    await _award(db_session, s.alice, s.other_task, _utc(11, 18, 6))  # another game
    await _award(db_session, s.alice, s.task, _utc(11, 12, 6))

    result = await service.get_user_activity(s.game.id, "alice")

    assert [d.date for d in result.activeDays] == [D(2026, 11, 12)]
    assert result.streaks.current == 0


@pytest.mark.asyncio
async def test_unknown_user_gets_an_empty_calendar(service, db_session):
    s = await _setup(db_session)

    result = await service.get_user_activity(s.game.id, "nobody")

    assert result.activeDays == []
    assert result.streaks.model_dump() == {"current": 0, "longest": 0, "runs": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "from_date,to_date",
    [(D(2026, 11, 10), D(2026, 11, 9)), (D(2025, 11, 1), D(2026, 11, 2))],
)
async def test_invalid_periods_are_rejected(service, db_session, from_date, to_date):
    s = await _setup(db_session)

    with pytest.raises(HTTPException) as exc:
        await service.get_user_activity(
            s.game.id, "alice", from_date=from_date, to_date=to_date
        )

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_a_full_366_day_period_is_allowed(service, db_session):
    s = await _setup(db_session)

    result = await service.get_user_activity(
        s.game.id, "alice", from_date=D(2025, 11, 18), to_date=D(2026, 11, 18)
    )

    assert (result.to - result.from_).days + 1 == 366


@pytest.mark.asyncio
async def test_scope_is_enforced(service, db_session):
    s = await _setup(db_session)
    with patch(
        "app.services.user_points.activity.get_authorized_game",
        new=AsyncMock(return_value=s.game),
    ) as authorized:
        await service.get_user_activity(
            s.game.id, "alice", api_key="key-1", enforce_scope=True
        )

    authorized.assert_awaited_once()
