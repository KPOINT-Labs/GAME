"""
Characterization tests for GREENCROWD scoring.

These pin the CURRENT numeric behaviour of ``get_dynamic_values_from_tasks``
and ``calculate_points`` so that upcoming refactors (persisting a per-row
points breakdown, fixing the streak logic) cannot change scores silently.

Tests marked "KNOWN BUG" assert today's incorrect output on purpose. When the
streak calculation is fixed, update those expectations deliberately.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.engine.greencrowdStrategy import get_dynamic_values_from_tasks
from tests.unit_tests.engine.test_greencrowdStrategy import (
    QueryRecords,
    _build_sim_task_dict,
    _build_strategy_with_mocked_container,
)

UTC = datetime.timezone.utc
FIXED_NOW = datetime.datetime(2026, 11, 18, 14, 0, tzinfo=UTC)  # Afternoon slot
BP = 10
LBE_MULTIPLIER = 0.5


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


@pytest.fixture
def frozen_now():
    """Freeze ``datetime.datetime.now`` inside the greencrowd module only."""
    fake_datetime_module = SimpleNamespace(
        datetime=_FrozenDateTime,
        timezone=datetime.timezone,
        timedelta=datetime.timedelta,
    )
    with patch("app.engine.greencrowdStrategy.datetime", fake_datetime_module):
        yield


def _at(month, day, hour=10):
    return datetime.datetime(2026, month, day, hour, 0, tzinfo=UTC)


def _record(task_id, user_id, created_at):
    return SimpleNamespace(taskId=task_id, userId=user_id, created_at=created_at)


def _dynamic(records, list_ids_tasks=None, task=None, user_id="user-1"):
    task = task or SimpleNamespace(id="task-a", externalTaskId="poi_1")
    list_ids_tasks = list_ids_tasks or [{"id": "task-a", "externalTaskId": "poi_1"}]
    return get_dynamic_values_from_tasks(
        task=task,
        list_ids_tasks=list_ids_tasks,
        all_records=QueryRecords(records),
        user=SimpleNamespace(id=user_id),
        variable_basic_points=BP,
        variable_lbe_multiplier=LBE_MULTIPLIER,
    )


# --------------------------------------------------------------------------
# DIM_S (streak bonus): DIM_S = round(BP * 2 ** (consecutive_days / 5))
# --------------------------------------------------------------------------

STREAK_CASES = [
    # (id, activity days for user-1, computed consecutive_days, DIM_S)
    ("no_activity", [], 0, 10),
    ("only_today", [_at(11, 18)], 1, 11),
    # KNOWN BUG: a real 3-day streak is counted as 2.
    ("three_consecutive_days", [_at(11, d) for d in (16, 17, 18)], 2, 13),
    # KNOWN BUG: a real 5-day streak is counted as 3.
    ("five_consecutive_days", [_at(11, d) for d in range(14, 19)], 3, 15),
    # KNOWN BUG: a real 10-day streak is counted as 5 (bonus doubles).
    ("ten_consecutive_days", [_at(11, d) for d in range(9, 19)], 5, 20),
    # Gaps break a real streak; current logic still counts today.
    ("every_other_day", [_at(11, d) for d in (14, 16, 18)], 1, 11),
    # KNOWN BUG: 30 active days in October, then idle for 19 days,
    # still yields a "streak" of 6 instead of 0.
    ("idle_19_days_after_30_active", [_at(10, d) for d in range(1, 31)], 6, 23),
]


@pytest.mark.parametrize(
    "days,expected_consecutive,expected_dim_s",
    [c[1:] for c in STREAK_CASES],
    ids=[c[0] for c in STREAK_CASES],
)
def test_dim_s_current_values(frozen_now, days, expected_consecutive, expected_dim_s):
    records = [_record("task-a", "user-1", d) for d in days]

    result = _dynamic(records)

    assert result["DIM_S"] == round(BP * 2 ** (expected_consecutive / 5))
    assert result["DIM_S"] == expected_dim_s


def test_dim_s_ignores_other_users_activity(frozen_now):
    records = [_record("task-a", "user-2", _at(11, d)) for d in range(9, 19)]
    records.append(_record("task-a", "user-1", _at(11, 18)))

    assert _dynamic(records)["DIM_S"] == 11  # only user-1's single day counts


def test_dim_s_counts_multiple_records_on_same_day_once(frozen_now):
    records = [_record("task-a", "user-1", _at(11, 18, h)) for h in (8, 9, 10, 11)]

    assert _dynamic(records)["DIM_S"] == 11


# --------------------------------------------------------------------------
# All five dimensions for a fixed dataset
# --------------------------------------------------------------------------


def test_all_dimensions_for_fixed_dataset(frozen_now):
    """
    Dataset (now = 2026-11-18 14:00 UTC, Afternoon slot):

    POIs: poi_1 = {task-a, task-b}, poi_2 = {task-c}; scoring task-a.

    DIM_BP : 1 of 2 poi_1 tasks has records -> 10 - round(10 * 1/2) = 5
    DIM_LBE: task-a has 3 records, avg per POI = 7 / 2 = 3.5, 3 < 3.5
             -> round(10 * 0.5) = 5
    DIM_TD : task-a slots = Morning, Evening, Afternoon; current slot
             Afternoon has 1, others 2 -> round((1 - 1/2) * 10) = 5
    DIM_PP : user-1 at 09:00 and 12:00 -> avg gap 180 min, last gap 120 min,
             alpha = 1/3, smoothed = 20 + 120 = 140 -> round(140/180 * 10) = 8
    DIM_S  : user-1 active only today -> 1 day -> round(10 * 2**0.2) = 11
    """
    records = [
        _record("task-a", "user-1", _at(11, 18, 9)),  # Morning
        _record("task-a", "user-2", _at(11, 17, 20)),  # Evening
        _record("task-a", "user-3", _at(11, 15, 15)),  # Afternoon
        _record("task-c", "user-1", _at(11, 18, 12)),
        _record("task-c", "user-2", _at(11, 17, 13)),
        _record("task-c", "user-2", _at(11, 16, 15)),
        _record("task-c", "user-3", _at(11, 15, 16)),
    ]
    list_ids_tasks = [
        {"id": "task-a", "externalTaskId": "poi_1"},
        {"id": "task-b", "externalTaskId": "poi_1"},
        {"id": "task-c", "externalTaskId": "poi_2"},
    ]

    result = _dynamic(records, list_ids_tasks=list_ids_tasks)

    assert result == {
        "DIM_BP": 5,
        "DIM_LBE": 5,
        "DIM_TD": 5,
        "DIM_PP": 8,
        "DIM_S": 11,
    }


# --------------------------------------------------------------------------
# calculate_points: how the stored total is composed
# --------------------------------------------------------------------------


def _strategy_with_valid_hash():
    strategy = _build_strategy_with_mocked_container()
    strategy.user_points_service.get_points_of_simulated_task.return_value = []
    return strategy


@pytest.mark.asyncio
async def test_calculate_points_sums_each_dimension_exactly_once():
    strategy = _strategy_with_valid_hash()
    task = _build_sim_task_dict(
        "poi_1", dim_bp=1, dim_lbe=2, dim_td=4, dim_pp=8, dim_s=16
    )
    data = {"simulationHash": "h", "tasks": [task]}

    with patch(
        "app.engine.greencrowdStrategy.calculate_hash_simulated_strategy",
        return_value="h",
    ):
        points, case_name, callback_data = await strategy.calculate_points(
            "game-1", "poi_1", "user-1", data
        )

    # Distinct powers of two: any dropped or double-counted dimension
    # produces a different total.
    assert points == 31
    assert case_name == "Valid Simulation"
    assert callback_data is None


@pytest.mark.asyncio
async def test_calculate_points_depends_on_dimension_order():
    """
    Current behaviour: dimensions are read by list position
    (``dimensions[0]`` must be DIM_BP, ..., ``dimensions[4]`` DIM_S).
    A reordered payload yields ``None`` lookups and a TypeError.
    Expected to change when dimensions are looked up by key.
    """
    strategy = _strategy_with_valid_hash()
    task = _build_sim_task_dict("poi_1")
    task["dimensions"] = list(reversed(task["dimensions"]))
    data = {"simulationHash": "h", "tasks": [task]}

    with patch(
        "app.engine.greencrowdStrategy.calculate_hash_simulated_strategy",
        return_value="h",
    ), pytest.raises(TypeError):
        await strategy.calculate_points("game-1", "poi_1", "user-1", data)
