"""
GREENCROWD calendar logic uses the game's reference timezone.

"Now" is frozen at 2026-11-18 14:00 UTC, which is 19:30 the same day in
Asia/Kolkata (UTC+05:30): the Afternoon slot in UTC but Evening in Kolkata.
"""

import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.engine.greencrowdStrategy import get_dynamic_values_from_tasks
from tests.unit_tests.engine import test_greencrowd_characterization as chars

frozen_now = chars.frozen_now  # fixture
BP = chars.BP
UTC = datetime.timezone.utc
KOLKATA = ZoneInfo("Asia/Kolkata")


def _record(created_at):
    return SimpleNamespace(taskId="task-a", userId="user-1", created_at=created_at)


def _dynamic(records, zone):
    return get_dynamic_values_from_tasks(
        task=SimpleNamespace(id="task-a", externalTaskId="poi_1"),
        list_ids_tasks=[{"id": "task-a", "externalTaskId": "poi_1"}],
        all_records=records,
        user=SimpleNamespace(id="user-1"),
        variable_basic_points=BP,
        variable_lbe_multiplier=chars.LBE_MULTIPLIER,
        zone=zone,
    )


def _utc(day, hour, minute=0):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def test_streak_days_follow_the_games_local_midnight(frozen_now):
    # 23:59 and 00:01 Kolkata time: two local days, but one UTC day (17th).
    records = [_record(_utc(17, 18, 29)), _record(_utc(17, 18, 31))]

    assert _dynamic(records, None)["DIM_S"] == 11  # UTC: 1-day streak
    assert _dynamic(records, UTC)["DIM_S"] == 11
    assert _dynamic(records, KOLKATA)["DIM_S"] == 13  # Kolkata: 2-day streak


def test_today_is_the_games_local_today(frozen_now):
    # 22:00 UTC on the 17th is 03:30 on the 18th in Kolkata, i.e. "today".
    records = [_record(_utc(17, 22)), _record(_utc(17, 2))]

    assert _dynamic(records, UTC)["DIM_S"] == 11  # UTC days {17}: 1 (grace)
    assert _dynamic(records, KOLKATA)["DIM_S"] == 13  # local {18, 17}: 2


def test_time_of_day_slots_use_local_hours(frozen_now):
    records = [
        _record(_utc(17, 16)),  # UTC Afternoon | Kolkata 21:30 Evening
        _record(_utc(17, 12)),  # UTC Afternoon | Kolkata 17:30 Afternoon
        _record(_utc(17, 8)),  # UTC Morning   | Kolkata 13:30 Afternoon
    ]

    # Current slot: UTC Afternoon (2 records, 1 other) -> (1 - 2/1) * 10.
    assert _dynamic(records, UTC)["DIM_TD"] == -10
    # Current slot: Kolkata Evening (1 record, 2 others) -> (1 - 1/2) * 10.
    assert _dynamic(records, KOLKATA)["DIM_TD"] == 5
