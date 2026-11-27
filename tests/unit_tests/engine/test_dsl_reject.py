"""
The ``reject`` statement for custom (DSL_FULL) strategies.

A ``reject`` refuses the event: the run halts and raises ``DslRejectedError``
(422, code ``DSL_REJECTED``, ``params.reason`` = the node's reason) instead of
returning a result, so the points service records nothing and the event's
idempotency key stays unused. It is allowed in main rules and ``default``.
The strategy observer logs the run as ``rejected`` (not ``error``), which
keeps it out of error metrics, alerts and "top errors".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import DslRejectedError, DslValidationError
from app.engine import dsl_metrics
from app.engine.dsl_ast import is_valid_reject_reason
from app.engine.dsl_execution_context import ExecutionContext
from app.engine.dsl_interpreter import DslInterpreter
from app.engine.dsl_strategy import DslStrategy
from app.engine.dsl_validator import validate_ast
from app.schema.dsl_schema import SimulationRequest
from app.schema.strategy_definition_schema import StrategyDefinitionRead
from app.services.dsl_simulation_service import DslSimulationService
from app.services.strategy_observability_service import _status_breakdown

# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------


def _lit(node_id, value):
    return {"type": "literal", "id": node_id, "value": value}


def _data(node_id, key):
    return {"type": "field", "id": node_id, "path": f"data.{key}"}


def _reject(node_id, reason="MILESTONE_NOT_REACHED"):
    return {"type": "reject", "id": node_id, "reason": reason}


def _assign(node_id, value, case_name="Scored"):
    return {
        "type": "assign_points",
        "id": node_id,
        "value": value,
        "case_name": case_name,
    }


def _bonus(node_id, key, value):
    return {"type": "add_bonus", "id": node_id, "key": key, "value": value}


def _rule(node_id, when, then, **extra):
    return {"type": "rule", "id": node_id, "when": when, "then": then, **extra}


def _when_data_lt(node_id, key, threshold):
    return {
        "type": "compare",
        "id": node_id,
        "op": "<",
        "left": _data(f"{node_id}_l", key),
        "right": _lit(f"{node_id}_r", threshold),
    }


def _program(rules, default=None, **extra):
    ast = {"type": "program", "id": "p", "rules": rules, **extra}
    if default is not None:
        ast["default"] = default
    return ast


def _milestone_program():
    """Reject a milestone the watched seconds don't cover, else award 10."""
    return _program(
        [
            _rule(
                "r_short",
                _when_data_lt("c_short", "watchedSeconds", 30),
                [_reject("rej_short", "MILESTONE_NOT_REACHED")],
            ),
            _rule("r_ok", _lit("l_true", True), [_assign("a_ok", _lit("v", 10))]),
        ]
    )


async def _run(ast, data=None):
    validate_ast(ast)
    ctx = await ExecutionContext.build_for_ast(
        ast,
        externalGameId="g",
        externalTaskId="t",
        externalUserId="u",
        data=data,
        analytics_service=MagicMock(),
        mock_state={},
    )
    return await DslInterpreter(max_nodes=1000, max_depth=32).execute(ast, ctx)


# --------------------------------------------------------------------------
# Reason rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason, valid",
    [
        ("MILESTONE_NOT_REACHED", True),
        ("A", True),
        ("TOO_SOON_2", True),
        ("A" * 64, True),
        ("A" * 65, False),
        ("", False),
        ("milestone_not_reached", False),
        ("Milestone", False),
        ("2_FAST", False),
        ("_LEADING", False),
        ("HAS-DASH", False),
        ("HAS SPACE", False),
        ("DSL_REJECTED", False),
        ("DSL_ANYTHING", False),
        ("DSLX_IS_FINE", True),
        # A trailing newline must not slip past the end anchor.
        ("NOT_ALLOWED\n", False),
        ("A" * 64 + "\n", False),
        ("NOT_ALLOWED\r\n", False),
        (None, False),
        (5, False),
    ],
)
def test_is_valid_reject_reason(reason, valid):
    assert is_valid_reject_reason(reason) is valid


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------


def test_validator_accepts_reject_in_main_rules():
    validate_ast(_milestone_program())


def test_validator_accepts_reject_in_else_branches():
    ast = _program(
        [
            _rule(
                "r",
                _when_data_lt("c", "x", 1),
                [_assign("a", _lit("v", 1))],
                else_if=[{"when": _lit("l2", True), "then": [_reject("rej1")]}],
                **{"else": [_reject("rej2", "OTHER")]},
            )
        ]
    )

    validate_ast(ast)


def test_validator_accepts_reject_as_default():
    validate_ast(_program([], default=_reject("rej")))


@pytest.mark.parametrize(
    "reason", ["lower", "DSL_REJECTED", "", "A" * 65, 3, "NOT_ALLOWED\n"]
)
def test_validator_rejects_invalid_reason(reason):
    ast = _program([], default=_reject("rej", reason))

    with pytest.raises(DslValidationError, match="reject.reason"):
        validate_ast(ast)


def test_validator_requires_reason():
    ast = _program([], default={"type": "reject", "id": "rej"})

    with pytest.raises(DslValidationError, match="missing required key 'reason'"):
        validate_ast(ast)


def test_validator_rejects_unknown_reject_keys():
    node = _reject("rej")
    node["message"] = "nope"

    with pytest.raises(DslValidationError, match="unexpected keys"):
        validate_ast(_program([], default=node))


@pytest.mark.parametrize(
    "section, context", [("pre_rules", "pre"), ("post_rules", "post")]
)
def test_validator_rejects_reject_in_extend_sections(section, context):
    ast = _program([], **{section: [_rule("r", _lit("l", True), [_reject("rej")])]})

    with pytest.raises(DslValidationError, match=f"not allowed inside '{context}'"):
        validate_ast(ast)


# --------------------------------------------------------------------------
# Interpreter
# --------------------------------------------------------------------------


async def test_reject_raises_422_with_fixed_code_and_reason():
    with pytest.raises(DslRejectedError) as exc_info:
        await _run(_milestone_program(), data={"watchedSeconds": 12})

    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "DSL_REJECTED"
    assert exc.params == {"reason": "MILESTONE_NOT_REACHED", "nodeId": "rej_short"}
    assert exc.detail == {
        "code": "DSL_REJECTED",
        "params": {"reason": "MILESTONE_NOT_REACHED", "nodeId": "rej_short"},
        "message": "Event rejected by strategy: MILESTONE_NOT_REACHED",
    }
    assert exc.headers == {"X-Node-Id": "rej_short"}


async def test_rule_not_matching_lets_the_award_through():
    result = await _run(_milestone_program(), data={"watchedSeconds": 45})

    assert result["points"] == 10
    assert result["case_name"] == "Scored"


async def test_reject_halts_before_later_statements_and_rules():
    ast = _program(
        [
            _rule(
                "r1",
                _lit("l1", True),
                [_reject("rej"), _assign("a_same", _lit("v1", 1))],
            ),
            _rule("r2", _lit("l2", True), [_assign("a_later", _lit("v2", 2))]),
        ]
    )

    with pytest.raises(DslRejectedError) as exc_info:
        await _run(ast)

    node_ids = [entry["nodeId"] for entry in exc_info.value.trace]
    assert "a_same" not in node_ids
    assert "a_later" not in node_ids


async def test_reject_after_bonuses_and_callback_data_discards_them():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [
                    {
                        "type": "set_callback_data",
                        "id": "cb",
                        "key": "k",
                        "value": _lit("cbv", 1),
                    },
                    _bonus("b", "streak", _lit("bv", 5)),
                    _reject("rej"),
                ],
            )
        ]
    )

    # No result at all: the caller only sees the error.
    with pytest.raises(DslRejectedError):
        await _run(ast)


async def test_default_reject_fires_when_no_rule_matches():
    ast = _program(
        [_rule("r", _lit("l", False), [_assign("a", _lit("v", 1))])],
        default=_reject("rej_default", "NO_RULE_MATCHED"),
    )

    with pytest.raises(DslRejectedError) as exc_info:
        await _run(ast)

    assert exc_info.value.params["reason"] == "NO_RULE_MATCHED"


async def test_default_reject_does_not_fire_when_a_rule_assigns():
    ast = _program(
        [_rule("r", _lit("l", True), [_assign("a", _lit("v", 3))])],
        default=_reject("rej_default"),
    )

    result = await _run(ast)

    assert result["points"] == 3


async def test_reject_trace_ends_with_the_reject_entry():
    with pytest.raises(DslRejectedError) as exc_info:
        await _run(_milestone_program(), data={"watchedSeconds": 0})

    assert exc_info.value.trace[-1] == {
        "nodeId": "rej_short",
        "type": "reject",
        "value": "MILESTONE_NOT_REACHED",
        "branch": "reject",
    }


# --------------------------------------------------------------------------
# DslStrategy and the observer
# --------------------------------------------------------------------------


def _strategy(ast, observer=None):
    definition = StrategyDefinitionRead(
        id="def-1",
        realmId="realm-a",
        name="reject",
        description="",
        type="DSL_FULL",
        astJson=ast,
        version=1,
        status="DRAFT",
    )
    return DslStrategy(
        definition=definition,
        interpreter=DslInterpreter(max_nodes=1000, max_depth=32),
        analytics_service=MagicMock(),
        observer=observer,
    )


async def test_dsl_strategy_propagates_reject():
    with pytest.raises(DslRejectedError):
        await _strategy(_milestone_program()).calculate_points(
            "g", "t", "u", data={"watchedSeconds": 1}
        )


async def test_observer_records_rejected_status_with_reason_and_trace():
    observer = MagicMock()
    observer.record = AsyncMock()

    with pytest.raises(DslRejectedError):
        await _strategy(_milestone_program(), observer).calculate_points(
            "g", "t", "u", data={"watchedSeconds": 1}
        )

    kwargs = observer.record.await_args.kwargs
    assert kwargs["status"] == "rejected"
    assert kwargs["errorCode"] == "MILESTONE_NOT_REACHED"
    assert kwargs["points"] is None
    assert kwargs["trace"][-1]["type"] == "reject"
    assert kwargs["nodesExecuted"] == len(kwargs["trace"])


async def test_observer_records_ok_when_the_event_is_accepted():
    observer = MagicMock()
    observer.record = AsyncMock()

    await _strategy(_milestone_program(), observer).calculate_points(
        "g", "t", "u", data={"watchedSeconds": 60}
    )

    assert observer.record.await_args.kwargs["status"] == "ok"


def test_rejected_runs_are_not_counted_as_errors():
    def errors(realm):
        return dsl_metrics.dsl_execution_errors_total.labels(
            realm=realm, strategy_type="DSL_FULL", code="MILESTONE_NOT_REACHED"
        )._value.get()

    before = errors("reject-test-realm")
    dsl_metrics.observe(
        realm="reject-test-realm",
        strategy_type="DSL_FULL",
        status="rejected",
        duration_seconds=0.001,
        nodes_executed=3,
        error_code="MILESTONE_NOT_REACHED",
    )

    assert errors("reject-test-realm") == before


def test_status_breakdown_counts_rejected_separately():
    breakdown = _status_breakdown({"ok": 5, "rejected": 3, "error": 1, "weird": 1})

    assert breakdown.ok == 5
    assert breakdown.rejected == 3
    assert breakdown.error == 1
    assert breakdown.other == 1
    assert breakdown.total == 10


# --------------------------------------------------------------------------
# Simulate (the editor's Test button)
# --------------------------------------------------------------------------


async def test_simulate_returns_the_reject_error():
    service = DslSimulationService(MagicMock(), MagicMock())

    with pytest.raises(DslRejectedError) as exc_info:
        await service._run_ast(
            _milestone_program(),
            SimulationRequest(
                externalGameId="g",
                externalTaskId="t",
                externalUserId="u",
                data={"watchedSeconds": 5},
            ),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.params["nodeId"] == "rej_short"
