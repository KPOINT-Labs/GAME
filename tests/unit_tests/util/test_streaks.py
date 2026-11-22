import datetime

import pytest

from app.util.streaks import count_consecutive_active_days, current_streak, streak_runs

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


def _loader(active_days, calls=None):
    async def load(first_day, last_day):
        if calls is not None:
            calls.append((first_day, last_day))
        return [d for d in active_days if first_day <= d <= last_day]

    return load


TODAY = D(2026, 11, 22)


def _back(n):
    return TODAY - datetime.timedelta(days=n)


@pytest.mark.asyncio
async def test_current_streak_without_today_uses_the_grace_day():
    days = [_back(1), _back(2), _back(3)]

    assert await current_streak(_loader(days), TODAY) == 3


@pytest.mark.asyncio
async def test_current_streak_including_today_adds_today_when_not_yet_active():
    days = [_back(1), _back(2), _back(3)]

    assert await current_streak(_loader(days), TODAY, include_today=True) == 4


@pytest.mark.asyncio
async def test_current_streak_including_today_does_not_double_count_today():
    days = [TODAY, _back(1), _back(2), _back(3)]

    assert await current_streak(_loader(days), TODAY, include_today=True) == 4
    assert await current_streak(_loader(days), TODAY) == 4


@pytest.mark.asyncio
async def test_current_streak_including_today_is_one_after_a_gap_or_no_history():
    assert await current_streak(_loader([_back(2)]), TODAY, include_today=True) == 1
    assert await current_streak(_loader([]), TODAY, include_today=True) == 1
    assert await current_streak(_loader([]), TODAY) == 0


@pytest.mark.asyncio
async def test_current_streak_reads_back_in_adjacent_growing_chunks():
    days = [_back(n) for n in range(1, 101)]  # 100 days ending yesterday
    calls = []

    assert await current_streak(_loader(days, calls), TODAY, include_today=True) == 101
    assert [(last - first).days + 1 for first, last in calls] == [64, 128]
    assert calls[1][1] == calls[0][0] - datetime.timedelta(days=1)
