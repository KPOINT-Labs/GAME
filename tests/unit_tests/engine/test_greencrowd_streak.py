"""
Specification tests for the GREENCROWD streak fix (written test-first).

Definition of a streak, as these tests pin it:

* Activity is grouped by UTC calendar day; several records on one day count
  as one day. Only the scored user's records count.
* The streak is the number of consecutive active days ending today.
* Grace day: if the user has not been active today yet, a streak ending
  yesterday is still alive (today's award is usually not recorded yet when
  dimensions are calculated). A streak whose last active day is two or more
  days ago is 0.
* Any missing day breaks the streak; older activity before the gap does not
  count.
* Dates after today (clock skew) are ignored.

The bonus formula is unchanged: ``DIM_S = round(BP * 2 ** (streak / 5))``.

The counting lives in a pure helper,
``count_consecutive_active_days(active_dates, today) -> int``, in
``app.engine.greencrowdStrategy``.

These fail until the fix is implemented. When it lands, the KNOWN BUG cases
in ``test_greencrowd_characterization.py`` must be updated to match.
"""

import datetime

import pytest

import app.engine.greencrowdStrategy as greencrowd
from tests.unit_tests.engine import test_greencrowd_characterization as chars
from tests.unit_tests.engine.test_greencrowdStrategy import (
    _build_strategy_with_mocked_container,
)

# Reuse the group 1 fixture that freezes "now" at 2026-11-18 14:00 UTC.
frozen_now = chars.frozen_now

TODAY = chars.FIXED_NOW.date()
BP = chars.BP
_at = chars._at
_record = chars._record
_dynamic = chars._dynamic


def _day(offset):
    """Date ``offset`` days before TODAY."""
    return TODAY - datetime.timedelta(days=offset)


def count_streak(active_dates, today):
    """Call the helper under test; fails (not errors) while it is missing."""
    helper = getattr(greencrowd, "count_consecutive_active_days", None)
    assert helper is not None, (
        "app.engine.greencrowdStrategy.count_consecutive_active_days "
        "is not implemented"
    )
    return helper(active_dates, today)


# --------------------------------------------------------------------------
# Pure helper: count_consecutive_active_days(active_dates, today)
# --------------------------------------------------------------------------

HELPER_CASES = [
    # (id, active dates, expected streak)
    ("no_activity", [], 0),
    ("only_today", [_day(0)], 1),
    ("three_days_ending_today", [_day(2), _day(1), _day(0)], 3),
    ("ten_days_ending_today", [_day(n) for n in range(10)], 10),
    ("grace_streak_ending_yesterday", [_day(2), _day(1)], 2),
    ("last_active_two_days_ago", [_day(3), _day(2)], 0),
    ("gap_breaks_streak", [_day(n) for n in (0, 1, 2, 4, 5, 6, 7)], 3),
    ("every_other_day", [_day(0), _day(2), _day(4)], 1),
    ("old_activity_only", [_day(n) for n in range(19, 49)], 0),
]


@pytest.mark.parametrize(
    "dates,expected",
    [c[1:] for c in HELPER_CASES],
    ids=[c[0] for c in HELPER_CASES],
)
def test_count_consecutive_active_days(dates, expected):
    assert count_streak(dates, TODAY) == expected


def test_count_ignores_duplicates_and_order():
    dates = [_day(0), _day(2), _day(1), _day(0), _day(1)]

    assert count_streak(dates, TODAY) == 3


def test_count_ignores_future_dates():
    dates = [_day(-1), _day(0), _day(1)]  # tomorrow, today, yesterday

    assert count_streak(dates, TODAY) == 2


def test_count_ignores_future_dates_when_not_active_today():
    dates = [_day(-2), _day(1)]  # two days ahead, yesterday

    assert count_streak(dates, TODAY) == 1


def test_count_accepts_any_iterable():
    assert count_streak({_day(0), _day(1)}, TODAY) == 2
    assert count_streak((d for d in [_day(0)]), TODAY) == 1


# --------------------------------------------------------------------------
# DIM_S from get_dynamic_values_from_tasks (BP = 10)
# --------------------------------------------------------------------------

DIM_S_CASES = [
    # (id, activity datetimes for user-1, expected streak, expected DIM_S)
    ("no_activity", [], 0, 10),
    ("only_today", [_at(11, 18)], 1, 11),
    ("three_consecutive_days", [_at(11, d) for d in (16, 17, 18)], 3, 15),
    ("five_consecutive_days", [_at(11, d) for d in range(14, 19)], 5, 20),
    ("ten_consecutive_days", [_at(11, d) for d in range(9, 19)], 10, 40),
    ("every_other_day", [_at(11, d) for d in (14, 16, 18)], 1, 11),
    ("idle_19_days_after_30_active", [_at(10, d) for d in range(1, 31)], 0, 10),
    ("grace_yesterday_and_day_before", [_at(11, 16), _at(11, 17)], 2, 13),
    ("last_active_two_days_ago", [_at(11, 15), _at(11, 16)], 0, 10),
    (
        "gap_resets_streak",
        [_at(11, d) for d in (10, 11, 12, 13, 14, 16, 17, 18)],
        3,
        15,
    ),
]


@pytest.mark.parametrize(
    "days,expected_streak,expected_dim_s",
    [c[1:] for c in DIM_S_CASES],
    ids=[c[0] for c in DIM_S_CASES],
)
def test_dim_s_uses_true_consecutive_day_streak(
    frozen_now, days, expected_streak, expected_dim_s
):
    records = [_record("task-a", "user-1", d) for d in days]

    result = _dynamic(records)

    assert result["DIM_S"] == round(BP * 2 ** (expected_streak / 5))
    assert result["DIM_S"] == expected_dim_s


def test_dim_s_uses_utc_day_boundaries(frozen_now):
    utc = datetime.timezone.utc
    records = [
        _record(
            "task-a", "user-1", datetime.datetime(2026, 11, 17, 23, 59, tzinfo=utc)
        ),
        _record("task-a", "user-1", datetime.datetime(2026, 11, 18, 0, 1, tzinfo=utc)),
    ]

    assert _dynamic(records)["DIM_S"] == 13  # 2-day streak


def test_dim_s_only_counts_scored_users_activity(frozen_now):
    records = [_record("task-a", "user-2", _at(11, d)) for d in range(9, 19)]
    records += [_record("task-a", "user-1", _at(11, d)) for d in (17, 18)]

    assert _dynamic(records)["DIM_S"] == 13  # user-1's own 2-day streak


# --------------------------------------------------------------------------
# Documentation shown to users matches the formula
# --------------------------------------------------------------------------


def test_logic_graph_streak_label_matches_formula():
    strategy = _build_strategy_with_mocked_container()

    source = strategy.generate_logic_graph(format="svg").source

    assert "2^(Days_Consecutive / 5)" in source
    assert "/ 7" not in source
