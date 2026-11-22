"""A user's activity calendar in a game: active days and streaks.

A day is *active* when the user received at least one points award on any
of the game's tasks, counted in the game's timezone. Streaks use the same
day-counting rule as GREENCROWD's streak bonus (``app.util.streaks``).
GREENCROWD only sees awards on its own tasks, so in a game that mixes
strategies its streak can be shorter than this one; they match when every
task in the game uses GREENCROWD.
"""

import datetime

from fastapi import HTTPException, status

from app.schema.user_points_schema import (
    ActivityDay,
    ActivityStreaks,
    StreakRun,
    UserActivity,
)
from app.services.game_access import get_authorized_game
from app.services.user_points._base import UserPointsContext
from app.util.streaks import current_streak, streak_runs
from app.util.timezones import utc_bounds_of_local_days, zone_of

DEFAULT_ACTIVITY_DAYS = 30
# Upper bound on from..to; keeps the period query and the response small.
MAX_ACTIVITY_DAYS = 45


class PointsActivityMixin(UserPointsContext):
    async def _active_days(self, game_id, user_id, zone, first_day, last_day):
        """
        ``{local date: (points, awards)}`` for the user's awards on the game's
        local days ``first_day..last_day``. The repository groups by day (in
        SQL on PostgreSQL), so this is at most one entry per day.
        """
        start, end = utc_bounds_of_local_days(zone, first_day, last_day)
        rows = await self.user_points_repository.get_active_days_of_user_in_game(
            game_id, user_id, zone.key, start, end
        )
        return {day: (points, awards) for day, points, awards in rows}

    async def _current_streak(self, game_id, user_id, zone, today):
        """The user's current streak as of ``today``, over all history."""

        async def load(first_day, last_day):
            return await self._active_days(game_id, user_id, zone, first_day, last_day)

        return await current_streak(load, today)

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
        45 days. ``streaks.current`` is as of today over all history; the
        active days, ``runs`` and ``longest`` cover the period only. An
        unknown user, or one without awards, has no active days.

        Raises:
            NotFoundError: If the game does not exist.
            ForbiddenError: If the caller may not access the game.
            HTTPException: ``422`` if ``from`` is after ``to`` or the period
                is longer than 45 days.
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
        days: dict = {}
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
