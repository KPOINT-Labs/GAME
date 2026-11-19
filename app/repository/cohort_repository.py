from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Callable, Iterable, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.model.cohorts import UserGameCohort, UserPointsCohort
from app.repository.base_repository import BaseRepository


class CohortRepository(BaseRepository):
    """
    Reads and writes cohort tags: a user's current cohorts per game, and the
    cohorts snapshotted onto each points award.
    """

    def __init__(
        self, session_factory: Callable[..., AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        super().__init__(session_factory, UserGameCohort)

    @asynccontextmanager
    async def _session(self, session: Optional[AsyncSession]):
        """Reuse the caller's session, or open (and close) a new one."""
        if session is not None:
            yield session
            return
        async with self.session_factory() as own_session:
            yield own_session

    async def get_user_game_cohorts(
        self, user_id, game_id, *, session: Optional[AsyncSession] = None
    ) -> list[str]:
        """
        Return the user's current cohorts in a game, sorted by tag.

        Args:
            user_id: Internal user identifier.
            game_id: Internal game identifier.
            session (Optional[AsyncSession]): Caller's session, to read inside
                a larger transaction.

        Returns:
            list[str]: The cohort tags; empty when the user has none.
        """
        stmt = (
            select(UserGameCohort.cohort)
            .filter(
                UserGameCohort.userId == user_id,
                UserGameCohort.gameId == game_id,
            )
            .order_by(UserGameCohort.cohort)
        )
        async with self._session(session) as s:
            return list((await s.execute(stmt)).scalars().all())

    async def replace_user_game_cohorts(
        self, user_id, game_id, cohorts: Iterable[str], *, api_key: str = None
    ) -> None:
        """
        Replace the user's cohorts in a game with ``cohorts``, atomically.

        An empty ``cohorts`` clears the user's cohorts in the game.

        Args:
            user_id: Internal user identifier.
            game_id: Internal game identifier.
            cohorts (Iterable[str]): The new, already validated cohort tags.
            api_key (str): Caller's API key, recorded on the new rows.
        """
        async with self.session_factory() as session:
            await session.execute(
                sa_delete(UserGameCohort).where(
                    UserGameCohort.userId == user_id,
                    UserGameCohort.gameId == game_id,
                )
            )
            session.add_all(
                UserGameCohort(
                    userId=user_id, gameId=game_id, cohort=c, apiKey_used=api_key
                )
                for c in cohorts
            )
            await session.commit()

    async def add_points_cohorts(
        self,
        user_points_id,
        cohorts: Iterable[str],
        *,
        session: AsyncSession,
        api_key: str = None,
    ) -> None:
        """
        Attach cohort tags to a points award inside the caller's transaction.

        Only flushes; the caller commits (or rolls back) together with the
        points row itself.

        Args:
            user_points_id: The ``userpoints`` row the cohorts belong to.
            cohorts (Iterable[str]): Already validated, de-duplicated tags.
            session (AsyncSession): The caller's open session.
            api_key (str): Caller's API key, recorded on the new rows.
        """
        session.add_all(
            UserPointsCohort(userPointsId=user_points_id, cohort=c, apiKey_used=api_key)
            for c in cohorts
        )
        await session.flush()

    async def get_points_cohorts(self, user_points_id) -> list[str]:
        """Return the cohorts attached to one points award, sorted by tag."""
        stmt = (
            select(UserPointsCohort.cohort)
            .filter(UserPointsCohort.userPointsId == user_points_id)
            .order_by(UserPointsCohort.cohort)
        )
        async with self.session_factory() as session:
            return list((await session.execute(stmt)).scalars().all())
