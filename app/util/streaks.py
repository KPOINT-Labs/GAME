"""Streaks of consecutive active days.

Shared by GREENCROWD scoring (``DIM_S``), the activity endpoint and the DSL
``user.streak_days`` field so all of them count days the same way. They can still see different awards: GREENCROWD
only its own tasks, the activity endpoint all of a game's tasks. Dates are
the game's local days (see ``app.util.timezones``).
"""

from __future__ import annotations

import datetime
from typing import Awaitable, Callable, Iterable

ONE_DAY = datetime.timedelta(days=1)
# The current streak is looked up backwards in growing windows; this many
# days first, doubling while the streak reaches the window's edge.
CURRENT_STREAK_FIRST_WINDOW_DAYS = 64
CURRENT_STREAK_MAX_DAYS = 365 * 20

ActiveDaysLoader = Callable[
    [datetime.date, datetime.date], Awaitable[Iterable[datetime.date]]
]


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


async def current_streak(
    load_active_days: ActiveDaysLoader,
    today: datetime.date,
    *,
    include_today: bool = False,
) -> int:
    """
    The user's current streak as of ``today``, over all history.

    Reads only as far back as the streak goes, in non-overlapping chunks of
    days: the most recent 64 days, then the 128 before them, then 256, and so
    on, stopping at the first chunk that contains a gap. No day is read twice.

    Args:
        load_active_days: Async callable ``(first_day, last_day)`` returning
            the active local days in that inclusive range.
        today (datetime.date): The reference day, in the game's timezone.
        include_today (bool): Treat ``today`` as active even if nothing is
            recorded yet. Scoring uses this: the award being scored makes
            today active, so the streak it rewards always includes today and
            does not change between the first and later awards of a day.

    Returns:
        int: Consecutive active days ending today (or yesterday, when
        ``include_today`` is false and today has no activity yet).
    """
    active: set = {today} if include_today else set()
    chunk = CURRENT_STREAK_FIRST_WINDOW_DAYS
    last_day = today
    while True:
        first_day = last_day - datetime.timedelta(days=chunk - 1)
        active.update(await load_active_days(first_day, last_day))
        streak = count_consecutive_active_days(active, today)
        last_counted = today if today in active else today - ONE_DAY
        oldest_counted = last_counted - datetime.timedelta(days=streak - 1)
        if streak == 0 or oldest_counted > first_day:
            return streak
        if (today - first_day).days + 1 >= CURRENT_STREAK_MAX_DAYS:
            return streak
        last_day = first_day - ONE_DAY
        chunk *= 2
