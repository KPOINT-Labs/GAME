"""Points-by-task read queries."""

from typing import Any

from app.core.exceptions import NotFoundError
from app.schema.user_points_schema import PointsAssignedToUser
from app.services.game_access import get_authorized_game
from app.services.response_cache import TTL_AGGREGATE, GameAccess, cached_game_read
from app.services.task._base import TaskServiceContext


def _row_as_dict(row: Any) -> Any:
    """A result row as a dict, so it can be cached and serialized as JSON."""
    if hasattr(row, "_asdict"):
        return row._asdict()
    return row


class TaskPointsMixin(TaskServiceContext):
    """Read user-points aggregates scoped to a task."""

    async def get_points_by_task_id(
        self,
        gameId,
        externalTaskId,
        *,
        api_key: str = None,
        oauth_user_id: str = None,
        is_admin: bool = False,
        enforce_scope: bool = False,
    ) -> Any:
        """
        Retrieves points by task ID.

        Args:
            gameId (UUID): The game ID.
            externalTaskId (str): The external task ID.

        Returns:
            list: A list of points associated with the task.

        With ``enforce_scope`` (the HTTP path) the result is cached for
        ``RESPONSE_CACHE_AGGREGATE_TTL_SECONDS``: point awards do not refresh
        it, game/task definition changes do. Rows are returned as dicts there.
        """
        if not enforce_scope:
            game = await self.game_repository.read_by_column(
                "id",
                gameId,
                not_found_message=f"Game not found with gameId: {gameId}",
                only_one=True,
            )
            return await self._task_points_rows(game.id, gameId, externalTaskId)

        async def compute(game):
            rows = await self._task_points_rows(game.id, gameId, externalTaskId)
            return [_row_as_dict(row) for row in rows]

        return await self._cached_task_aggregate(
            "task_points",
            gameId,
            externalTaskId,
            GameAccess(api_key, oauth_user_id, is_admin, enforce_scope),
            compute,
        )

    async def _task_points_rows(self, game_id, gameId, externalTaskId) -> Any:
        """Points per user in a task (uncached rows); 404 if no such task."""
        task = await self.task_repository.read_by_gameId_and_externalTaskId(
            game_id, externalTaskId
        )
        if not task:
            raise NotFoundError(
                f"Task not found with externalTaskId: {externalTaskId} for "
                f"gameId: {gameId}"
            )
        return await self.user_points_repository.get_all_UserPoints_by_taskId(task.id)

    async def _cached_task_aggregate(
        self, namespace, gameId, externalTaskId, access: GameAccess, compute
    ) -> Any:
        """
        Serve a task-wide aggregate from the response cache (scoped callers).

        Args:
            namespace (str): Cache namespace.
            gameId: Internal game identifier.
            externalTaskId (str): External task identifier.
            access (GameAccess): Caller scope (``enforce_scope`` is set).
            compute: Builds the JSON-ready result from the authorized game.

        Returns:
            list[dict]: The aggregate rows as dicts.
        """
        return await cached_game_read(
            self.response_cache,
            namespace=namespace,
            game_id=gameId,
            access=access,
            load_game=lambda: get_authorized_game(
                self.game_repository,
                gameId,
                api_key=access.api_key,
                oauth_user_id=access.oauth_user_id,
                is_admin=access.is_admin,
            ),
            compute=compute,
            params={"externalTaskId": externalTaskId},
            ttl_group=TTL_AGGREGATE,
        )

    async def get_points_of_user_by_task_id(
        self,
        gameId,
        externalTaskId,
        externalUserId,
        *,
        api_key: str = None,
        oauth_user_id: str = None,
        is_admin: bool = False,
        enforce_scope: bool = False,
    ) -> Any:
        """
        Retrieves points of a user by task ID.

        Args:
            gameId (UUID): The game ID.
            externalTaskId (str): The external task ID.
            externalUserId (str): The external user ID.

        Returns:
            The user's points in the task. A user with no points in the task,
            or who does not exist, gets ``points`` and ``timesAwarded`` of 0.

        Raises:
            NotFoundError: If the task does not exist in the game.

        Per-user read: not cached (only the task-wide aggregates are).
        """
        if enforce_scope:
            await get_authorized_game(
                self.game_repository,
                gameId,
                api_key=api_key,
                oauth_user_id=oauth_user_id,
                is_admin=is_admin,
            )
        points_task = await self._task_points_with_details_rows(gameId, externalTaskId)
        user_points = list(
            filter(lambda x: x.externalUserId == externalUserId, points_task)
        )

        if not user_points:
            return PointsAssignedToUser(
                externalUserId=externalUserId, points=0, timesAwarded=0
            )
        return user_points[0]

    async def get_points_by_task_id_with_details(
        self,
        gameId,
        externalTaskId,
        *,
        api_key: str = None,
        oauth_user_id: str = None,
        is_admin: bool = False,
        enforce_scope: bool = False,
    ) -> Any:
        """
        Retrieves points by task ID with details.

        Args:
            gameId (UUID): The game ID.
            externalTaskId (str): The external task ID.

        Returns:
            list: A list of points associated with the task.

        With ``enforce_scope`` (the HTTP path) the result is cached like
        :meth:`get_points_by_task_id`, and rows are returned as dicts (the
        endpoint's ``List[dict]`` response model rejects raw rows).
        """
        if not enforce_scope:
            return await self._task_points_with_details_rows(gameId, externalTaskId)

        async def compute(game):
            rows = await self._task_points_with_details_rows(game.id, externalTaskId)
            return [_row_as_dict(row) for row in rows]

        return await self._cached_task_aggregate(
            "task_points_details",
            gameId,
            externalTaskId,
            GameAccess(api_key, oauth_user_id, is_admin, enforce_scope),
            compute,
        )

    async def _task_points_with_details_rows(self, gameId, externalTaskId) -> Any:
        """Detailed points per user in a task (uncached rows); 404 if no task."""
        task = await self.task_repository.read_by_gameId_and_externalTaskId(
            gameId, externalTaskId
        )
        if not task:
            raise NotFoundError(
                f"Task not found with externalTaskId: {externalTaskId} for "
                f"gameId: {gameId}"
            )
        return await self.user_points_repository.get_all_UserPoints_by_taskId_with_details(  # noqa: E501
            task.id
        )
