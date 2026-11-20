"""Streaks of consecutive active days.

Shared by GREENCROWD scoring (``DIM_S``) and the activity endpoint, so the
streak a client shows is the streak scoring uses. Dates are the game's local
days (see ``app.util.timezones``).
"""

from __future__ import annotations

import datetime
from typing import Iterable

ONE_DAY = datetime.timedelta(days=1)


def count_consecutive_active_days(
    active_dates: Iterable[datetime.date], today: datetime.date
) -> int:
    """
    Length of the user's current streak of consecutive active days.

    Counts back one day at a time from ``today`` while each day has activity.
    If ``today`` has no activity yet, counting starts from yesterday (grace
    day), because today's award is usually not recorded when dimensions are
    calculated. A streak whose last active day is two or more days ago is 0.

    Days are the game's local days: callers pass ``active_dates`` and
    ``today`` already converted to the game's timezone.

    Args:
        active_dates (Iterable[datetime.date]): Days with activity, in any
            order and possibly repeated. Dates after ``today`` are ignored.
        today (datetime.date): The reference day.

    Returns:
        int: Number of consecutive active days ending today (or yesterday).
    """
    days = {d for d in active_dates if d <= today}
    day = today if today in days else today - ONE_DAY
    streak = 0
    while day in days:
        streak += 1
        day -= ONE_DAY
    return streak


def streak_runs(
    active_dates: Iterable[datetime.date],
) -> list[tuple[datetime.date, datetime.date, int]]:
    """
    Split active days into maximal runs of consecutive days.

    Args:
        active_dates (Iterable[datetime.date]): Days with activity, in any
            order and possibly repeated.

    Returns:
        list[tuple[date, date, int]]: ``(start, end, length)`` per run, oldest
        first.
    """
    runs = []
    for day in sorted(set(active_dates)):
        if runs and day - runs[-1][1] == ONE_DAY:
            start, _, length = runs[-1]
            runs[-1] = (start, day, length + 1)
        else:
            runs.append((day, day, 1))
    return runs
