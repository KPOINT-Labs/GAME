import datetime

from app.util.streaks import count_consecutive_active_days, streak_runs

D = datetime.date


def test_streak_runs_splits_consecutive_days_oldest_first():
    days = [
        D(2026, 11, 3),
        D(2026, 11, 1),
        D(2026, 11, 2),
        D(2026, 11, 5),
        D(2026, 11, 2),
    ]

    assert streak_runs(days) == [
        (D(2026, 11, 1), D(2026, 11, 3), 3),
        (D(2026, 11, 5), D(2026, 11, 5), 1),
    ]


def test_streak_runs_empty():
    assert streak_runs([]) == []


def test_streak_runs_cross_month_boundary():
    assert streak_runs([D(2026, 10, 31), D(2026, 11, 1)]) == [
        (D(2026, 10, 31), D(2026, 11, 1), 2)
    ]


def test_current_streak_shared_with_greencrowd():
    from app.engine import greencrowdStrategy

    assert greencrowdStrategy.count_consecutive_active_days is (
        count_consecutive_active_days
    )
