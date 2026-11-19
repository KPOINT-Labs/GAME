"""
Unit tests for cohort tagging in the services:

* ``UserActionsService`` replaces the user's cohorts when an action has them.
* ``PointsPersistenceMixin._attach_cohorts`` decides which cohorts an award
  gets, and the points write runs it inside its own transaction.
* ``assign_points_to_user`` passes the game and any explicit cohorts through.

The database side (snapshot + cohort leaderboards) is covered against SQLite
in ``tests/unit_tests/repository/test_cohort_leaderboard_flow.py``.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.schema.task_schema import AddActionDidByUserInTask
from app.services.user_actions_service import UserActionsService
from app.services.user_points_service import UserPointsService
from tests.unit_tests.services.test_points_breakdown import _Strategy


def _points_service(cohort_repository=None):
    user_points_repository = AsyncMock()
    db_session = MagicMock()
    for name in ("commit", "rollback", "flush", "close"):
        setattr(db_session, name, AsyncMock())
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=db_session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    user_points_repository.session_factory = MagicMock(return_value=session_context)
    user_points_repository.read_by_user_task_and_idempotency = AsyncMock(
        return_value=None
    )
    user_points_repository.create = AsyncMock(
        return_value=SimpleNamespace(id="points-1", created_at="2026-11-19")
    )
    wallet_repository = AsyncMock()
    wallet_repository.upsert_points_balance = AsyncMock(
        return_value=SimpleNamespace(id="wallet-1")
    )
    wallet_transaction_repository = AsyncMock()
    wallet_transaction_repository.create = AsyncMock(
        return_value=SimpleNamespace(id="txn-1")
    )
    service = UserPointsService(
        user_points_repository=user_points_repository,
        users_repository=AsyncMock(),
        users_game_config_repository=AsyncMock(),
        game_repository=AsyncMock(),
        task_repository=AsyncMock(),
        wallet_repository=wallet_repository,
        wallet_transaction_repository=wallet_transaction_repository,
        cohort_repository=cohort_repository,
    )
    return service, db_session


def _cohort_repo(current=("school-a",)):
    repo = AsyncMock()
    repo.get_user_game_cohorts = AsyncMock(return_value=list(current))
    return repo


class TestAttachCohorts(unittest.IsolatedAsyncioTestCase):
    async def _attach(self, repo, *, game_id="game-1", cohorts=None):
        service, session = _points_service(repo)
        await service._attach_cohorts(
            session,
            user_points=SimpleNamespace(id="points-1"),
            user_id="user-1",
            game_id=game_id,
            cohorts=cohorts,
            api_key="key-1",
        )
        return session

    async def test_snapshots_current_cohorts_when_none_given(self):
        repo = _cohort_repo(["grade-7", "school-a"])

        session = await self._attach(repo)

        repo.get_user_game_cohorts.assert_awaited_once_with(
            "user-1", "game-1", session=session
        )
        repo.add_points_cohorts.assert_awaited_once_with(
            "points-1", ["grade-7", "school-a"], session=session, api_key="key-1"
        )

    async def test_explicit_cohorts_override_without_lookup(self):
        repo = _cohort_repo()

        await self._attach(repo, cohorts=["event-x"])

        repo.get_user_game_cohorts.assert_not_awaited()
        self.assertEqual(repo.add_points_cohorts.call_args.args[1], ["event-x"])

    async def test_explicit_empty_list_records_no_cohorts(self):
        repo = _cohort_repo()

        await self._attach(repo, cohorts=[])

        repo.get_user_game_cohorts.assert_not_awaited()
        repo.add_points_cohorts.assert_not_awaited()

    async def test_user_without_cohorts_gets_none(self):
        repo = _cohort_repo([])

        await self._attach(repo)

        repo.add_points_cohorts.assert_not_awaited()

    async def test_no_game_means_no_snapshot(self):
        repo = _cohort_repo()

        await self._attach(repo, game_id=None)

        repo.get_user_game_cohorts.assert_not_awaited()
        repo.add_points_cohorts.assert_not_awaited()

    async def test_no_cohort_repository_is_a_no_op(self):
        # Positional/legacy setups without the repository keep working.
        await self._attach(None)


class TestPointsWriteTagsCohorts(unittest.IsolatedAsyncioTestCase):
    async def _persist(self, service, **kwargs):
        return await service._persist_points_wallet_and_transaction(
            user_id="user-1",
            task_id="task-1",
            points=5,
            case_name="case",
            data_to_add={},
            description="d",
            api_key="key-1",
            external_user_id="u",
            external_task_id="t",
            game_id="game-1",
            **kwargs,
        )

    async def test_tags_new_award_in_the_same_transaction(self):
        repo = _cohort_repo(["school-a"])
        service, session = _points_service(repo)

        await self._persist(service)

        repo.add_points_cohorts.assert_awaited_once_with(
            "points-1", ["school-a"], session=session, api_key="key-1"
        )
        session.commit.assert_awaited_once()

    async def test_idempotent_replay_does_not_retag(self):
        repo = _cohort_repo(["school-b"])
        service, _ = _points_service(repo)
        existing = SimpleNamespace(id="points-old")
        service.user_points_repository.read_by_user_task_and_idempotency = AsyncMock(
            return_value=existing
        )

        result, _, _ = await self._persist(service, idempotency_key="evt-1")

        self.assertIs(result, existing)
        repo.get_user_game_cohorts.assert_not_awaited()
        repo.add_points_cohorts.assert_not_awaited()


class TestAssignmentPassesCohorts(unittest.IsolatedAsyncioTestCase):
    async def _assign(self, cohorts):
        service, _ = _points_service(_cohort_repo())
        service.game_repository.read_by_column.return_value = SimpleNamespace(
            id="game-1", externalGameId="external-game-1"
        )
        service.task_repository.read_by_gameId_and_externalTaskId.return_value = (
            SimpleNamespace(id="task-1", strategyId="strategy-1")
        )
        service.users_repository.read_by_column.return_value = SimpleNamespace(
            id="user-1", externalUserId="user_1"
        )
        service.strategy_service.get_strategy_by_id = MagicMock(return_value=object())
        service.strategy_service.get_Class_by_id = MagicMock(
            return_value=_Strategy((5, "case"))
        )
        service._persist_points_wallet_and_transaction = AsyncMock(
            return_value=(SimpleNamespace(created_at="2026-11-19"), None, None)
        )
        schema = SimpleNamespace(externalUserId="user_1", data={}, cohorts=cohorts)

        await service.assign_points_to_user(
            "00000000-0000-0000-0000-000000000001",
            "task-ext",
            schema,
            False,
            "key-1",
        )
        return service._persist_points_wallet_and_transaction.call_args.kwargs

    async def test_passes_game_and_explicit_cohorts(self):
        kwargs = await self._assign(["event-x"])

        self.assertEqual(
            (kwargs["game_id"], kwargs["cohorts"]), ("game-1", ["event-x"])
        )

    async def test_omitted_cohorts_pass_none_for_snapshot(self):
        kwargs = await self._assign(None)

        self.assertEqual((kwargs["game_id"], kwargs["cohorts"]), ("game-1", None))


class TestActionSetsCohorts(unittest.IsolatedAsyncioTestCase):
    def _service(self, cohort_repository):
        task_repository = AsyncMock()
        task_repository.read_by_gameId_and_externalTaskId.return_value = (
            SimpleNamespace(status="open", gameId="game-1")
        )
        users_repository = AsyncMock()
        users_repository.get_or_create_by_externalUserId.return_value = SimpleNamespace(
            id="user-1"
        )

        async def _create(action):
            stored = {
                **action.model_dump(),
                "id": str(uuid4()),
                "created_at": "2026-11-19T00:00:00",
                "updated_at": "2026-11-19T00:00:00",
            }
            return SimpleNamespace(**stored, model_dump=lambda: stored)

        actions_repository = AsyncMock()
        actions_repository.create = AsyncMock(side_effect=_create)
        return UserActionsService(
            user_actions_repository=actions_repository,
            users_repository=users_repository,
            game_repository=AsyncMock(),
            task_repository=task_repository,
            cohort_repository=cohort_repository,
        )

    async def _act(self, service, cohorts):
        return await service.user_add_action_in_task(
            "game-1",
            "task-1",
            AddActionDidByUserInTask(
                typeAction="JOIN",
                data={},
                description="d",
                externalUserId="user_1",
                cohorts=cohorts,
            ),
            api_key="key-1",
        )

    async def test_cohorts_replace_users_cohorts_in_game(self):
        repo = AsyncMock()

        await self._act(self._service(repo), ["school-a", "grade-7"])

        repo.replace_user_game_cohorts.assert_awaited_once_with(
            "user-1", "game-1", ["school-a", "grade-7"], api_key="key-1"
        )

    async def test_empty_list_clears(self):
        repo = AsyncMock()

        await self._act(self._service(repo), [])

        repo.replace_user_game_cohorts.assert_awaited_once_with(
            "user-1", "game-1", [], api_key="key-1"
        )

    async def test_omitted_cohorts_leave_them_unchanged(self):
        repo = AsyncMock()

        await self._act(self._service(repo), None)

        repo.replace_user_game_cohorts.assert_not_awaited()

    async def test_no_cohort_repository_still_records_action(self):
        result = await self._act(self._service(None), ["school-a"])

        self.assertEqual(result.message, "Action added successfully")


def test_leaderboard_cohort_query_param_declares_tag_pattern():
    """Both leaderboards validate ``cohort`` with the shared tag pattern, so a
    malformed value is rejected with 422 before reaching the service."""
    from app.main import app
    from app.model.cohorts import COHORT_PATTERN

    paths = app.openapi()["paths"]
    for path in (
        "/games/{gameId}/leaderboard",
        "/games/{gameId}/tasks/{externalTaskId}/leaderboard",
    ):
        params = {p["name"]: p for p in paths[path]["get"]["parameters"]}
        schema = params["cohort"]["schema"]
        patterns = [schema.get("pattern")] + [
            option.get("pattern") for option in schema.get("anyOf", [])
        ]
        assert COHORT_PATTERN in patterns, (path, schema)
