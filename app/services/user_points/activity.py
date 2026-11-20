"""A user's activity calendar in a game: active days and streaks.

A day is *active* when the user received at least one points award on any
of the game's tasks, counted in the game's timezone. Streaks use the same
day-counting rule as GREENCROWD's streak bonus (``app.util.streaks``).
GREENCROWD only sees awards on its own tasks, so in a game that mixes
strategies its streak can be shorter than this one; they match when every
task in the game uses GREENCROWD.
"""

import datetime
from collections import defaultdict

from fastapi import HTTPException, status

from app.schema.user_points_schema import (
    ActivityDay,
    ActivityStreaks,
    StreakRun,
    UserActivity,
)
from app.services.game_access import get_authorized_game
from app.services.user_points._base import UserPointsContext
from app.util.streaks import ONE_DAY, count_consecutive_active_days, streak_runs
from app.util.timezones import local_datetime, zone_of

DEFAULT_ACTIVITY_DAYS = 30
MAX_ACTIVITY_DAYS = 366
# The current streak is looked up backwards in growing windows; this many
# days first, doubling while the streak reaches the window's edge.
CURRENT_STREAK_FIRST_WINDOW_DAYS = 64
CURRENT_STREAK_MAX_DAYS = 365 * 20


def _utc_bounds(zone, first_day, last_day):
    """UTC instants covering the game's local days ``first_day..last_day``."""
    start = datetime.datetime.combine(first_day, datetime.time.min, tzinfo=zone)
    end = datetime.datetime.combine(last_day + ONE_DAY, datetime.time.min, tzinfo=zone)
    return start.astimezone(datetime.timezone.utc), end.astimezone(
        datetime.timezone.utc
    )


class PointsActivityMixin(UserPointsContext):
    async def _active_days(self, game_id, user_id, zone, first_day, last_day):
        """``{local date: [points, awards]}`` for the user's awards in the range."""
        start, end = _utc_bounds(zone, first_day, last_day)
        rows = await self.user_points_repository.get_award_times_of_user_in_game(
            game_id, user_id, start, end
        )
        days = defaultdict(lambda: [0, 0])
        for created_at, points in rows:
            day = days[local_datetime(created_at, zone).date()]
            day[0] += points or 0
            day[1] += 1
        return days

    async def _current_streak(self, game_id, user_id, zone, today):
        """
        The user's current streak as of ``today``, over all history.

        Fetches only as far back as the streak goes: a window of recent days,
        doubled while the streak still reaches the window's oldest day.
        """
        window = CURRENT_STREAK_FIRST_WINDOW_DAYS
        while True:
            first_day = today - datetime.timedelta(days=window - 1)
            days = await self._active_days(game_id, user_id, zone, first_day, today)
            streak = count_consecutive_active_days(days.keys(), today)
            last_counted = today if today in days else today - ONE_DAY
            oldest_counted = last_counted - datetime.timedelta(days=streak - 1)
            if streak == 0 or oldest_counted > first_day:
                return streak
            if window >= CURRENT_STREAK_MAX_DAYS:
                return streak
            window *= 2

    async def get_user_activity(
        self,
        gameId,
        externalUserId: str,
        *,
        from_date: datetime.date | None = None,
        to_date: datetime.date | None = None,
        api_key: str = None,
        oauth_user_id: str = None,
        is_admin: bool = False,
        enforce_scope: bool = False,
    ) -> UserActivity:
        """
        A user's active days and streaks in a game over a period.

        Days are local days in the game's timezone. The period defaults to
        the last 30 days ending today (in that timezone) and may span at most
        366 days. ``streaks.current`` is as of today over all history; the
        active days, ``runs`` and ``longest`` cover the period only. An
        unknown user, or one without awards, has no active days.

        Raises:
            NotFoundError: If the game does not exist.
            ForbiddenError: If the caller may not access the game.
            HTTPException: ``422`` if ``from`` is after ``to`` or the period
                is longer than 366 days.
        """
        if enforce_scope:
            game = await get_authorized_game(
                self.game_repository,
                gameId,
                api_key=api_key,
                oauth_user_id=oauth_user_id,
                is_admin=is_admin,
            )
        else:
            game = await self.game_repository.read_by_column(
                "id", gameId, not_found_message=f"Game with gameId: {gameId} not found"
            )
        zone = zone_of(getattr(game, "timezone", None))
        today = datetime.datetime.now(zone).date()
        to_date = to_date or today
        from_date = from_date or to_date - datetime.timedelta(
            days=DEFAULT_ACTIVITY_DAYS - 1
        )
        if from_date > to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'from' must be on or before 'to'",
            )
        if (to_date - from_date).days + 1 > MAX_ACTIVITY_DAYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"The period can span at most {MAX_ACTIVITY_DAYS} days",
            )

        user = await self.users_repository.read_by_column(
            "externalUserId", externalUserId, not_found_raise_exception=False
        )
        days = {}
        current = 0
        if user is not None:
            days = await self._active_days(game.id, user.id, zone, from_date, to_date)
            current = await self._current_streak(game.id, user.id, zone, today)

        runs = [
            StreakRun(start=start, end=end, length=length)
            for start, end, length in streak_runs(days.keys())
        ]
        return UserActivity(
            externalGameId=game.externalGameId,
            externalUserId=externalUserId,
            timezone=zone.key,
            from_=from_date,
            to=to_date,
            activeDays=[
                ActivityDay(date=day, points=points, awards=awards)
                for day, (points, awards) in sorted(days.items())
            ],
            streaks=ActivityStreaks(
                current=current,
                longest=max((run.length for run in runs), default=0),
                runs=runs,
            ),
        )
