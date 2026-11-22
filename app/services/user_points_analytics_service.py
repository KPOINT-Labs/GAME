"""
Analytics service for user points.

Aggregations, temporal queries and metrics over ``user_points`` records.
Extracted from ``UserPointsService`` so CRUD/write paths and analytic queries
have separate, focused services.

These methods are thin pass-throughs over ``UserPointsRepository`` analytics
queries; they are consumed primarily by strategy engines (``app/engine``) to
compute rewards based on historical user behaviour.
"""

import datetime
from typing import Any

from app.repository.user_points_repository import UserPointsRepository
from app.services.base_service import BaseService
from app.util.streaks import current_streak
from app.util.timezones import utc_bounds_of_local_days, zone_of


class UserPointsAnalyticsService(BaseService):
    def __init__(self, user_points_repository: UserPointsRepository) -> None:
        self.user_points_repository = user_points_repository
        super().__init__(user_points_repository)

    async def count_measurements_by_external_task_id(self, external_task_id) -> Any:
        """Count all points rows recorded for an external task."""
        return await self.user_points_repository.count_measurements_by_external_task_id(
            external_task_id
        )

    async def get_user_task_measurements_count(
        self, externalTaskId, externalUserId
    ) -> Any:
        """Count a user's awards on a given external task."""
        return await self.user_points_repository.get_user_task_measurements_count(
            externalTaskId, externalUserId
        )

    async def get_user_task_measurements_count_the_last_seconds(
        self, externalTaskId, externalUserId, seconds
    ) -> Any:
        """Count a user's awards on a task within the last ``seconds``."""
        return await self.user_points_repository.get_user_task_measurements_count_the_last_seconds(
            externalTaskId, externalUserId, seconds
        )

    async def get_avg_time_between_tasks_by_user_and_game_task(
        self, externalGameId, externalTaskId, externalUserId
    ) -> Any:
        """Average seconds between a user's consecutive awards on a task
        (``-1`` with fewer than two awards)."""
        return await self.user_points_repository.get_avg_time_between_tasks_by_user_and_game_task(  # noqa
            externalGameId, externalTaskId, externalUserId
        )

    async def get_avg_time_between_tasks_for_all_users(
        self, externalGameId, externalTaskId
    ) -> Any:
        """Average seconds between consecutive awards on a task across all
        users (``-1`` with fewer than two awards)."""
        return await self.user_points_repository.get_avg_time_between_tasks_for_all_users(  # noqa
            externalGameId, externalTaskId
        )

    async def get_last_window_time_diff(self, externalTaskId, externalUserId) -> Any:
        """Seconds between a user's two most recent awards on a task."""
        return await self.user_points_repository.get_last_window_time_diff(
            externalTaskId, externalUserId
        )

    async def get_new_last_window_time_diff(
        self, externalTaskId, externalUserId, externalGameId
    ) -> Any:
        """Seconds elapsed since a user's most recent award on a task."""
        return await self.user_points_repository.get_new_last_window_time_diff(
            externalTaskId, externalUserId, externalGameId
        )

    async def get_user_task_measurements(self, externalTaskId, externalUserId) -> Any:
        """Return the ordered timestamps of a user's awards on a task."""
        return await self.user_points_repository.get_user_task_measurements(
            externalTaskId, externalUserId
        )

    async def count_personal_records_by_external_game_id(
        self, external_game_id, externalUserId
    ) -> Any:
        """Count the number of records for a user in a game."""
        return await self.user_points_repository.count_personal_records_by_external_game_id(
            external_game_id, externalUserId
        )

    async def user_has_record_before_in_externalTaskId_last_min(
        self, externalTaskId, externalUserId, minutes
    ) -> Any:
        """Check if a user has a record in the task in the last `minutes` minutes."""
        return await self.user_points_repository.user_has_record_before_in_externalTaskId_last_min(
            externalTaskId, externalUserId, minutes
        )

    async def get_global_avg_by_external_game_id(self, external_game_id) -> Any:
        """
        Get the global average time rewarded. Ignores entries with 0 minutes.
        """
        return await self.user_points_repository.get_global_avg_by_external_game_id(
            external_game_id
        )

    async def get_personal_avg_by_external_game_id(
        self, external_game_id, externalUserId
    ) -> Any:
        """
        Get the personal average time rewarded. Ignores entries with 0 minutes.
        """
        return await self.user_points_repository.get_personal_avg_by_external_game_id(
            external_game_id, externalUserId
        )

    async def get_user_streak_days_including_today(
        self, externalGameId, externalUserId
    ) -> int:
        """
        The user's streak of consecutive active days in a game, counting today.

        Backs the DSL ``user.streak_days`` field. A day is active when the
        user has at least one award on any of the game's tasks, in the game's
        timezone (the same rule as the activity endpoint). Today always counts
        because the award being scored makes it active, so the value is at
        least 1 and is the same for every award of the day.

        Args:
            externalGameId: External identifier of the game.
            externalUserId: External identifier of the user.

        Returns:
            int: Consecutive active days ending today, today included; 1 for
            a user with no earlier activity (or an unknown game/user).
        """
        scope = await self.user_points_repository.get_streak_scope(
            externalGameId, externalUserId
        )
        if scope is None or scope[2] is None:
            return 1
        game_id, tz_name, user_id = scope
        zone = zone_of(tz_name)
        today = datetime.datetime.now(zone).date()

        async def load(first_day, last_day):
            start, end = utc_bounds_of_local_days(zone, first_day, last_day)
            rows = await self.user_points_repository.get_active_days_of_user_in_game(
                game_id, user_id, zone.key, start, end
            )
            return [day for day, _points, _awards in rows]

        return await current_streak(load, today, include_today=True)
