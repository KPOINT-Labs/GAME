"""
Specification tests for the per-award points breakdown (written test-first).

Contract under test:

* ``UserPoints.breakdown`` is a nullable JSONB column holding::

      {"version": 1,
       "components": [{"key": str, "points": int, "inputs": {...}?}, ...]}

* A strategy's ``calculate_points`` may return an optional 4th element,
  ``breakdown``. Strategies returning 2 or 3 elements keep working and
  persist ``breakdown = None``.
* The assignment service rejects a breakdown whose component points do not
  sum to ``points`` (nothing is persisted), and stores it on its own column,
  not inside ``data``.
* ``PointsData`` (``pointsData[]`` on the details endpoints) exposes it.
* GREENCROWD returns a breakdown keyed by dimension and reads dimensions by
  key rather than list position.

These fail until the feature is implemented.
"""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects.postgresql import JSONB

from app.core.exceptions import InternalServerError
from app.model.user_points import UserPoints
from app.schema.user_points_schema import PointsData, UserPointsAssign
from app.services.user_points_service import UserPointsService
from tests.unit_tests.engine.test_greencrowdStrategy import (
    _build_sim_task_dict,
    _build_strategy_with_mocked_container,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations" / "versions"


def _breakdown(*components):
    return {
        "version": 1,
        "components": [{"key": key, "points": pts} for key, pts in components],
    }


# --------------------------------------------------------------------------
# Model, migration and schemas
# --------------------------------------------------------------------------


def test_user_points_model_has_nullable_jsonb_breakdown_column():
    columns = UserPoints.__table__.c

    assert "breakdown" in columns
    assert columns["breakdown"].nullable is True
    assert isinstance(columns["breakdown"].type, JSONB)


def test_a_migration_adds_breakdown_column_to_userpoints():
    pattern = re.compile(
        r"op\.add_column\(\s*[\"']userpoints[\"']\s*,\s*sa\.Column\(\s*"
        r"[\"']breakdown[\"']"
    )
    matches = [
        path.name
        for path in MIGRATIONS_DIR.glob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert matches, "no Alembic migration adds userpoints.breakdown"


def test_user_points_assign_schema_accepts_breakdown():
    breakdown = _breakdown(("DIM_BP", 3), ("DIM_S", 2))

    schema = UserPointsAssign(
        userId="u",
        taskId="t",
        points=5,
        caseName="Valid Simulation",
        data={},
        description="d",
        breakdown=breakdown,
    )

    assert schema.breakdown == breakdown


def test_user_points_assign_schema_breakdown_defaults_to_none():
    schema = UserPointsAssign(
        userId="u", taskId="t", points=5, caseName="c", data={}, description="d"
    )

    assert schema.breakdown is None


def test_points_data_exposes_breakdown():
    breakdown = _breakdown(("DIM_BP", 3), ("DIM_S", 2))

    item = PointsData(
        points=5,
        caseName="Valid Simulation",
        created_at="2026-11-18T14:00:00Z",
        breakdown=breakdown,
    )

    assert item.breakdown == breakdown
    assert item.model_dump()["breakdown"] == breakdown


def test_points_data_breakdown_is_optional():
    item = PointsData(points=5, caseName="c", created_at="2026-11-18T14:00:00Z")

    assert item.breakdown is None


# --------------------------------------------------------------------------
# Repository: details query includes breakdown
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_details_query_selects_breakdown_into_points_data():
    """
    ``get_all_UserPoints_by_taskId_with_details`` uses Postgres-only
    functions, so capture and inspect the statement instead of running it.
    """
    from app.repository.user_points_repository import UserPointsRepository

    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    repository = UserPointsRepository(session_factory=MagicMock(return_value=context))

    await repository.get_all_UserPoints_by_taskId_with_details("task-1")

    statement_sql = str(session.execute.call_args[0][0])
    assert "breakdown" in statement_sql


# --------------------------------------------------------------------------
# Assignment service: persisting and validating the breakdown
# --------------------------------------------------------------------------


class _Strategy:
    def __init__(self, result):
        self._result = result

    async def calculate_points(
        self, externalGameId, externalTaskId, externalUserId, data
    ):
        return self._result


class TestAssignmentPersistsBreakdown(unittest.IsolatedAsyncioTestCase):
    GAME_UUID = "00000000-0000-0000-0000-000000000001"

    def setUp(self):
        self.user_points_repository = AsyncMock()
        self.users_repository = AsyncMock()
        self.game_repository = AsyncMock()
        self.task_repository = AsyncMock()
        self.wallet_repository = AsyncMock()
        self.wallet_transaction_repository = AsyncMock()

        db_session = MagicMock()
        for name in ("commit", "rollback", "flush", "close"):
            setattr(db_session, name, AsyncMock())
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=db_session)
        session_context.__aexit__ = AsyncMock(return_value=False)
        self.user_points_repository.session_factory = MagicMock(
            return_value=session_context
        )
        self.user_points_repository.read_by_user_task_and_idempotency = AsyncMock(
            return_value=None
        )
        self.user_points_repository.create = AsyncMock(
            return_value=SimpleNamespace(created_at="2026-11-18T14:00:00")
        )
        self.wallet_repository.upsert_points_balance = AsyncMock(
            return_value=SimpleNamespace(id="wallet-1", pointsBalance=10)
        )
        self.wallet_transaction_repository.create = AsyncMock(
            return_value=SimpleNamespace(id="txn-1")
        )
        self.game_repository.read_by_column.return_value = SimpleNamespace(
            id="game-1", externalGameId="external-game-1"
        )
        self.task_repository.read_by_gameId_and_externalTaskId.return_value = (
            SimpleNamespace(id="task-1", strategyId="strategy-1")
        )
        self.users_repository.read_by_column.return_value = SimpleNamespace(
            id="user-id-1", externalUserId="user_1"
        )

        self.service = UserPointsService(
            user_points_repository=self.user_points_repository,
            users_repository=self.users_repository,
            users_game_config_repository=AsyncMock(),
            game_repository=self.game_repository,
            task_repository=self.task_repository,
            wallet_repository=self.wallet_repository,
            wallet_transaction_repository=self.wallet_transaction_repository,
        )
        self.service.strategy_service.get_strategy_by_id = MagicMock(
            return_value=object()
        )

    def _use_strategy_returning(self, result):
        self.service.strategy_service.get_Class_by_id = MagicMock(
            return_value=_Strategy(result)
        )

    async def _assign(self, data=None):
        schema = SimpleNamespace(externalUserId="user_1", data=data or {})
        result = await self.service.assign_points_to_user(
            self.GAME_UUID, "task-external-1", schema, False, "api-key"
        )
        return result, schema

    def _persisted(self):
        self.user_points_repository.create.assert_awaited_once()
        return self.user_points_repository.create.call_args[0][0]

    async def test_four_tuple_breakdown_is_persisted_on_its_own_field(self):
        breakdown = _breakdown(("DIM_BP", 4), ("DIM_S", 6))
        self._use_strategy_returning((10, "Valid Simulation", None, breakdown))

        result, schema = await self._assign()

        persisted = self._persisted()
        self.assertEqual(result.points, 10)
        self.assertEqual(persisted.points, 10)
        self.assertEqual(persisted.breakdown, breakdown)
        self.assertNotIn("breakdown", persisted.data or {})

    async def test_breakdown_with_inputs_is_persisted_unchanged(self):
        breakdown = {
            "version": 1,
            "components": [
                {"key": "DIM_BP", "points": 4},
                {"key": "DIM_S", "points": 6, "inputs": {"consecutive_days": 3}},
            ],
        }
        self._use_strategy_returning((10, "Valid Simulation", None, breakdown))

        await self._assign()

        self.assertEqual(self._persisted().breakdown, breakdown)

    async def test_breakdown_and_callback_data_are_both_kept(self):
        breakdown = _breakdown(("A", 7))
        self._use_strategy_returning((7, "case", {"foo": "bar"}, breakdown))

        _, schema = await self._assign()

        persisted = self._persisted()
        self.assertEqual(persisted.breakdown, breakdown)
        self.assertEqual(schema.data["callbackData"], {"foo": "bar"})

    async def test_two_tuple_strategy_persists_null_breakdown(self):
        self._use_strategy_returning((5, "BasicReward"))

        await self._assign()

        persisted = self._persisted()
        self.assertEqual(persisted.points, 5)
        self.assertIsNone(getattr(persisted, "breakdown", "MISSING"))

    async def test_three_tuple_strategy_persists_null_breakdown(self):
        self._use_strategy_returning((5, "case", {"foo": "bar"}))

        await self._assign()

        self.assertIsNone(getattr(self._persisted(), "breakdown", "MISSING"))

    async def test_breakdown_not_summing_to_points_is_rejected(self):
        self._use_strategy_returning(
            (10, "Valid Simulation", None, _breakdown(("DIM_BP", 4), ("DIM_S", 5)))
        )

        with self.assertRaises(InternalServerError):
            await self._assign()

        self.user_points_repository.create.assert_not_awaited()
        self.wallet_repository.upsert_points_balance.assert_not_awaited()

    async def test_breakdown_without_components_is_rejected(self):
        self._use_strategy_returning((10, "case", None, {"version": 1}))

        with self.assertRaises(InternalServerError):
            await self._assign()

        self.user_points_repository.create.assert_not_awaited()


# --------------------------------------------------------------------------
# GREENCROWD produces a keyed breakdown
# --------------------------------------------------------------------------


def _greencrowd_with_valid_hash():
    strategy = _build_strategy_with_mocked_container()
    strategy.user_points_service.get_points_of_simulated_task.return_value = []
    return strategy


async def _greencrowd_calculate(task):
    strategy = _greencrowd_with_valid_hash()
    data = {"simulationHash": "h", "tasks": [task]}
    with patch(
        "app.engine.greencrowdStrategy.calculate_hash_simulated_strategy",
        return_value="h",
    ):
        return await strategy.calculate_points("game-1", "poi_1", "user-1", data)


EXPECTED_GREENCROWD_BREAKDOWN = _breakdown(
    ("DIM_BP", 1), ("DIM_LBE", 2), ("DIM_TD", 4), ("DIM_PP", 8), ("DIM_S", 16)
)


@pytest.mark.asyncio
async def test_greencrowd_returns_breakdown_matching_points():
    task = _build_sim_task_dict(
        "poi_1", dim_bp=1, dim_lbe=2, dim_td=4, dim_pp=8, dim_s=16
    )

    result = await _greencrowd_calculate(task)

    assert len(result) == 4
    points, case_name, _, breakdown = result
    assert points == 31
    assert case_name == "Valid Simulation"
    assert breakdown == EXPECTED_GREENCROWD_BREAKDOWN
    assert sum(c["points"] for c in breakdown["components"]) == points


@pytest.mark.asyncio
async def test_greencrowd_reads_dimensions_by_key_not_position():
    task = _build_sim_task_dict(
        "poi_1", dim_bp=1, dim_lbe=2, dim_td=4, dim_pp=8, dim_s=16
    )
    task["dimensions"] = list(reversed(task["dimensions"]))

    points, _, _, breakdown = await _greencrowd_calculate(task)

    assert points == 31
    assert breakdown == EXPECTED_GREENCROWD_BREAKDOWN


@pytest.mark.asyncio
async def test_greencrowd_invalid_hash_still_returns_two_tuple():
    strategy = _greencrowd_with_valid_hash()
    data = {"simulationHash": "expected", "tasks": [_build_sim_task_dict("poi_1")]}

    with patch(
        "app.engine.greencrowdStrategy.calculate_hash_simulated_strategy",
        return_value="other",
    ):
        result = await strategy.calculate_points("game-1", "poi_1", "user-1", data)

    assert result == (-1, "Invalid hash")
