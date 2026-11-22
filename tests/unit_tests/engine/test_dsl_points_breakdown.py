"""
Points breakdown for custom (DSL) strategies.

``assign_points.value`` is the ``base`` component and every ``add_bonus``
statement adds a named component. The interpreter returns the breakdown,
``DslStrategy`` hands it to the points service as the 4th tuple element
(which persists it on ``userpoints.breakdown``), and the simulate endpoint
returns it so the editor can show it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import DslExecutionError, DslValidationError
from app.engine.base_strategy import BaseStrategy
from app.engine.dsl_ast import bonus_points, build_points_breakdown, is_valid_bonus_key
from app.engine.dsl_execution_context import ExecutionContext
from app.engine.dsl_interpreter import DslInterpreter
from app.engine.dsl_strategy import DslStrategy
from app.engine.dsl_validator import validate_ast
from app.schema.dsl_schema import SimulationRequest
from app.schema.strategy_definition_schema import StrategyDefinitionRead
from app.services.dsl_simulation_service import DslSimulationService

# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------


def _lit(node_id, value):
    return {"type": "literal", "id": node_id, "value": value}


def _data(node_id, key):
    return {"type": "field", "id": node_id, "path": f"data.{key}"}


def _bonus(node_id, key, value, mode=None):
    node = {"type": "add_bonus", "id": node_id, "key": key, "value": value}
    if mode is not None:
        node["mode"] = mode
    return node


def _assign(node_id, value, case_name="Scored"):
    return {
        "type": "assign_points",
        "id": node_id,
        "value": value,
        "case_name": case_name,
    }


def _rule(node_id, when, then):
    return {"type": "rule", "id": node_id, "when": when, "then": then}


def _when_data_gt(node_id, key, threshold):
    return {
        "type": "compare",
        "id": node_id,
        "op": ">",
        "left": _data(f"{node_id}_l", key),
        "right": _lit(f"{node_id}_r", threshold),
    }


def _program(rules, default=None, **extra):
    ast = {"type": "program", "id": "p", "rules": rules, **extra}
    if default is not None:
        ast["default"] = default
    return ast


def _breakdown(*components):
    return {
        "version": 1,
        "components": [{"key": k, "points": p} for k, p in components],
    }


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


def _streak_program():
    """Streak bonus rule (no assign) followed by the base rule."""
    return _program(
        [
            _rule(
                "r_streak",
                _when_data_gt("c_streak", "streak_days", 6),
                [_bonus("b_streak", "streak", _lit("lv_streak", 5))],
            ),
            _rule(
                "r_base",
                _lit("l_true", True),
                [
                    _bonus("b_quality", "quality", _data("d_quality", "quality")),
                    _assign("a_base", _lit("lv_base", 10), "Completed"),
                ],
            ),
        ]
    )


# --------------------------------------------------------------------------
# Helpers in dsl_ast
# --------------------------------------------------------------------------


def test_build_points_breakdown_puts_base_first_and_sums_components():
    total, breakdown = build_points_breakdown(
        10, [("streak", "fixed", 5), ("quality", "fixed", -2)]
    )

    assert total == 13
    assert breakdown == _breakdown(("base", 10), ("streak", 5), ("quality", -2))


def test_build_points_breakdown_with_no_bonuses_is_base_only():
    assert build_points_breakdown(7, []) == (7, _breakdown(("base", 7)))


def test_build_points_breakdown_sums_repeated_keys_across_modes():
    total, breakdown = build_points_breakdown(
        10, [("streak", "multiplier", 2), ("other", "fixed", 1), ("streak", "fixed", 1)]
    )

    assert total == 22
    assert breakdown == _breakdown(("base", 10), ("streak", 11), ("other", 1))


@pytest.mark.parametrize(
    "base, factor, bonus",
    [
        (10, 2, 10),  # double points
        (10, 3, 20),  # triple
        (10, 1, 0),  # x1 adds nothing
        (5, 1.5, 3),  # 2.5 rounds half up
        (3, 1.5, 2),  # 1.5 rounds half up
        (10, 1.15, 2),  # 1.4999999999999991 in floats; still 1.5 -> 2
        (5, 0.5, -2),  # halving 5 gives 3 points in total
        (10, 0, -10),  # x0 cancels the base
        (0, 2, 0),
    ],
)
def test_bonus_points_for_multipliers(base, factor, bonus):
    assert bonus_points("multiplier", factor, base) == bonus
    total, _ = build_points_breakdown(base, [("m", "multiplier", factor)])
    assert total == base + bonus
    assert isinstance(total, int)


def test_bonus_points_for_fixed_bonus_is_the_value():
    assert bonus_points("fixed", 2.5, 10) == 2.5


@pytest.mark.parametrize(
    "key, valid",
    [
        ("streak", True),
        ("Streak_Bonus_2", True),
        ("x" * 64, True),
        ("x" * 65, False),
        ("", False),
        ("base", False),
        ("first-time", False),
        ("with space", False),
        (5, False),
        (None, False),
    ],
)
def test_is_valid_bonus_key(key, valid):
    assert is_valid_bonus_key(key) is valid


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------


def test_validator_accepts_add_bonus_in_main_rules():
    validate_ast(_streak_program())


@pytest.mark.parametrize("key", ["base", "", "first-time", "x" * 65, 3])
def test_validator_rejects_invalid_bonus_key(key):
    ast = _program(
        [_rule("r", _lit("l", True), [_bonus("b", key, _lit("v", 1))])],
    )

    with pytest.raises(DslValidationError, match="add_bonus.key"):
        validate_ast(ast)


def test_validator_requires_add_bonus_value():
    ast = _program(
        [_rule("r", _lit("l", True), [{"type": "add_bonus", "id": "b", "key": "k"}])]
    )

    with pytest.raises(DslValidationError, match="missing required key 'value'"):
        validate_ast(ast)


def test_validator_rejects_unknown_add_bonus_keys():
    bonus = _bonus("b", "k", _lit("v", 1))
    bonus["case_name"] = "nope"
    ast = _program([_rule("r", _lit("l", True), [bonus])])

    with pytest.raises(DslValidationError):
        validate_ast(ast)


@pytest.mark.parametrize("mode", ["fixed", "multiplier"])
def test_validator_accepts_bonus_modes(mode):
    validate_ast(
        _program(
            [
                _rule(
                    "r",
                    _lit("l", True),
                    [_bonus("b", "k", _lit("v", 2), mode), _assign("a", _lit("v2", 1))],
                )
            ]
        )
    )


@pytest.mark.parametrize("mode", ["percent", "", None, 2])
def test_validator_rejects_unknown_bonus_mode(mode):
    bonus = _bonus("b", "k", _lit("v", 2))
    bonus["mode"] = mode
    ast = _program([_rule("r", _lit("l", True), [bonus])])

    with pytest.raises(DslValidationError, match="add_bonus.mode"):
        validate_ast(ast)


def test_validator_rejects_add_bonus_as_default():
    ast = _program([], default=_bonus("b", "k", _lit("v", 1)))

    with pytest.raises(DslValidationError, match="not allowed inside 'default'"):
        validate_ast(ast)


@pytest.mark.parametrize(
    "section, context", [("pre_rules", "pre"), ("post_rules", "post")]
)
def test_validator_rejects_add_bonus_in_extend_sections(section, context):
    ast = _program(
        [],
        **{section: [_rule("r", _lit("l", True), [_bonus("b", "k", _lit("v", 1))])]},
    )

    with pytest.raises(DslValidationError, match=f"not allowed inside '{context}'"):
        validate_ast(ast)


# --------------------------------------------------------------------------
# Interpreter
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_without_bonuses_reports_base_only_breakdown():
    ast = _program([_rule("r", _lit("l", True), [_assign("a", _lit("v", 4))])])

    result = await _run(ast)

    assert result["points"] == 4
    assert result["breakdown"] == _breakdown(("base", 4))


@pytest.mark.asyncio
async def test_bonuses_from_earlier_rules_and_same_branch_add_to_base():
    result = await _run(_streak_program(), data={"streak_days": 9, "quality": 3})

    assert result["points"] == 18
    assert result["case_name"] == "Completed"
    assert result["breakdown"] == _breakdown(
        ("base", 10), ("streak", 5), ("quality", 3)
    )


@pytest.mark.asyncio
async def test_bonus_rule_that_does_not_match_adds_nothing():
    result = await _run(_streak_program(), data={"streak_days": 2, "quality": 0})

    assert result["points"] == 10
    assert result["breakdown"] == _breakdown(("base", 10), ("quality", 0))


@pytest.mark.asyncio
async def test_same_bonus_key_twice_is_one_summed_component():
    ast = _program(
        [
            _rule("r1", _lit("l1", True), [_bonus("b1", "extra", _lit("v1", 2))]),
            _rule(
                "r2",
                _lit("l2", True),
                [
                    _bonus("b2", "other", _lit("v2", 1)),
                    _bonus("b3", "extra", _lit("v3", 3)),
                    _assign("a", _lit("v4", 1)),
                ],
            ),
        ]
    )

    result = await _run(ast)

    assert result["points"] == 7
    assert result["breakdown"] == _breakdown(("base", 1), ("extra", 5), ("other", 1))


@pytest.mark.asyncio
async def test_negative_bonus_is_a_penalty():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [_bonus("b", "late", _lit("v", -3)), _assign("a", _lit("v2", 10))],
            )
        ]
    )

    result = await _run(ast)

    assert result["points"] == 7
    assert result["breakdown"] == _breakdown(("base", 10), ("late", -3))


@pytest.mark.asyncio
async def test_default_assign_discards_bonuses():
    # Bonuses need an explicit rule assign_points; the fallback default
    # awards its own value only.
    ast = _program(
        [
            _rule("r", _lit("l", True), [_bonus("b", "streak", _lit("v", 5))]),
            _rule(
                "r2",
                _lit("l2", False),
                [
                    _bonus("b2", "x2", _lit("m", 2), "multiplier"),
                    _assign("a", _lit("v2", 10)),
                ],
            ),
        ],
        default=_assign("d", _lit("dv", 1), "Fallback"),
    )

    result = await _run(ast)

    assert result["points"] == 1
    assert result["case_name"] == "Fallback"
    assert result["breakdown"] == _breakdown(("base", 1))
    # The bonus statement still ran (and shows in the trace).
    assert any(e["nodeId"] == "b" for e in result["trace"])


@pytest.mark.asyncio
async def test_double_points_multiplier_adds_the_base_again():
    # The requested scenario: a 5-day streak doubles a video share's points.
    ast = _program(
        [
            _rule(
                "r_streak",
                _when_data_gt("c", "streak_days", 4),
                [_bonus("b", "streak", _lit("m", 2), "multiplier")],
            ),
            _rule(
                "r_share", _lit("l", True), [_assign("a", _lit("v", 10), "VideoShared")]
            ),
        ]
    )

    with_streak = await _run(ast, data={"streak_days": 5})
    without_streak = await _run(ast, data={"streak_days": 4})

    assert with_streak["points"] == 20
    assert with_streak["breakdown"] == _breakdown(("base", 10), ("streak", 10))
    assert without_streak["points"] == 10
    assert without_streak["breakdown"] == _breakdown(("base", 10))


@pytest.mark.asyncio
async def test_multiplier_uses_the_base_of_a_later_assign():
    # The multiplier is recorded before the base is known and resolved
    # against whichever rule assigns points.
    ast = _program(
        [
            _rule(
                "r1",
                _lit("l1", True),
                [_bonus("b", "boost", _lit("m", 1.5), "multiplier")],
            ),
            _rule(
                "r2",
                _when_data_gt("c", "level", 0),
                [_assign("a_hi", _lit("v_hi", 20), "High")],
            ),
            _rule("r3", _lit("l3", True), [_assign("a_lo", _lit("v_lo", 4), "Low")]),
        ]
    )

    assert (await _run(ast, data={"level": 1}))["breakdown"] == _breakdown(
        ("base", 20), ("boost", 10)
    )
    assert (await _run(ast, data={"level": 0}))["breakdown"] == _breakdown(
        ("base", 4), ("boost", 2)
    )


@pytest.mark.asyncio
async def test_multipliers_and_fixed_bonuses_stack_on_the_base():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [
                    _bonus("b1", "streak", _lit("m1", 2), "multiplier"),
                    _bonus("b2", "event", _lit("m2", 1.5), "multiplier"),
                    _bonus("b3", "quality", _lit("q", 3)),
                    _assign("a", _lit("v", 10)),
                ],
            )
        ]
    )

    result = await _run(ast)

    # Each multiplier applies to the base alone: 10 + 10 + 5 + 3.
    assert result["points"] == 28
    assert result["breakdown"] == _breakdown(
        ("base", 10), ("streak", 10), ("event", 5), ("quality", 3)
    )


@pytest.mark.asyncio
async def test_bonuses_without_any_assign_are_discarded():
    ast = _program(
        [_rule("r", _lit("l", True), [_bonus("b", "streak", _lit("v", 5))])],
    )

    result = await _run(ast)

    assert result["points"] == 0
    assert result["case_name"] is None
    assert result["breakdown"] is None


@pytest.mark.asyncio
async def test_return_before_assign_leaves_no_breakdown():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [
                    _bonus("b", "streak", _lit("v", 5)),
                    {"type": "return", "id": "ret"},
                ],
            ),
            _rule("r2", _lit("l2", True), [_assign("a", _lit("v2", 1))]),
        ]
    )

    result = await _run(ast)

    assert result["points"] == 0
    assert result["breakdown"] is None


@pytest.mark.asyncio
async def test_non_numeric_bonus_raises_execution_error():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [_bonus("b", "k", _data("d", "label")), _assign("a", _lit("v", 1))],
            )
        ]
    )

    with pytest.raises(DslExecutionError) as excinfo:
        await _run(ast, data={"label": "gold"})

    assert excinfo.value.code == "DSL_ADD_BONUS_NOT_NUMBER"
    assert excinfo.value.params == {"nodeId": "b", "actualType": "str"}


@pytest.mark.asyncio
async def test_fractional_fixed_bonus_raises_execution_error():
    # Award points are integers: a 0.5 bonus could be simulated but never
    # stored, so it fails up front with its own code.
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [_bonus("b", "k", _data("d", "q")), _assign("a", _lit("v", 1))],
            )
        ]
    )

    with pytest.raises(DslExecutionError) as excinfo:
        await _run(ast, data={"q": 0.5})

    assert excinfo.value.code == "DSL_ADD_BONUS_NOT_INTEGER"
    assert excinfo.value.params == {"nodeId": "b", "value": 0.5}


@pytest.mark.asyncio
async def test_whole_float_fixed_bonus_is_stored_as_int():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [_bonus("b", "k", _data("d", "q")), _assign("a", _lit("v", 1))],
            )
        ]
    )

    result = await _run(ast, data={"q": 2.0})

    assert result["points"] == 3
    component = result["breakdown"]["components"][1]
    assert component == {"key": "k", "points": 2}
    assert isinstance(component["points"], int)


@pytest.mark.asyncio
async def test_fractional_multiplier_factor_is_allowed():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [
                    _bonus("b", "k", _lit("m", 1.5), "multiplier"),
                    _assign("a", _lit("v", 5)),
                ],
            )
        ]
    )

    result = await _run(ast)

    assert result["points"] == 8
    assert result["breakdown"] == _breakdown(("base", 5), ("k", 3))


@pytest.mark.asyncio
async def test_boolean_bonus_raises_execution_error():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [_bonus("b", "k", _lit("bv", True)), _assign("a", _lit("v", 1))],
            )
        ]
    )

    with pytest.raises(DslExecutionError):
        await _run(ast)


@pytest.mark.asyncio
async def test_trace_records_each_bonus_and_the_base_value():
    result = await _run(_streak_program(), data={"streak_days": 9, "quality": 3})

    entries = {
        e["nodeId"]: e
        for e in result["trace"]
        if e["type"] in ("add_bonus", "assign_points")
    }
    assert entries["b_streak"]["value"] == 5
    assert entries["b_quality"]["value"] == 3
    assert entries["a_base"]["value"] == 10
    assert entries["a_base"]["branch"] == "match"


# --------------------------------------------------------------------------
# DslStrategy result tuple
# --------------------------------------------------------------------------


def _strategy(ast, *, type_="DSL_FULL", parent=None):
    definition = StrategyDefinitionRead(
        id="def-1",
        realmId="realm-a",
        name="breakdown",
        description="",
        type=type_,
        astJson=ast,
        version=1,
        status="DRAFT",
        parentStrategyId="mock_parent" if parent else None,
    )
    return DslStrategy(
        definition=definition,
        interpreter=DslInterpreter(max_nodes=1000, max_depth=32),
        analytics_service=MagicMock(),
        parent_strategy=parent,
    )


@pytest.mark.asyncio
async def test_dsl_strategy_returns_breakdown_as_fourth_element():
    strategy = _strategy(_streak_program())

    result = await strategy.calculate_points(
        "g", "t", "u", data={"streak_days": 9, "quality": 3}
    )

    assert result == (
        18,
        "Completed",
        None,
        _breakdown(("base", 10), ("streak", 5), ("quality", 3)),
    )


@pytest.mark.asyncio
async def test_dsl_strategy_keeps_callback_data_alongside_breakdown():
    ast = _program(
        [
            _rule(
                "r",
                _lit("l", True),
                [
                    {
                        "type": "set_callback_data",
                        "id": "cb",
                        "key": "why",
                        "value": _lit("cbv", "streak"),
                    },
                    _bonus("b", "streak", _lit("bv", 2)),
                    _assign("a", _lit("v", 3)),
                ],
            )
        ]
    )

    result = await _strategy(ast).calculate_points("g", "t", "u", data={})

    assert result == (
        5,
        "Scored",
        {"why": "streak"},
        _breakdown(("base", 3), ("streak", 2)),
    )


@pytest.mark.asyncio
async def test_dsl_strategy_without_assign_keeps_two_tuple():
    ast = _program([_rule("r", _lit("l", True), [_bonus("b", "k", _lit("v", 1))])])

    result = await _strategy(ast).calculate_points("g", "t", "u", data={})

    assert result == (0, None)


class _Parent(BaseStrategy):
    def __init__(self):
        super().__init__(
            strategy_name="MockParent",
            strategy_description="",
            strategy_name_slug="mock_parent",
            strategy_version="0.0.1",
            variable_basic_points=1,
            variable_bonus_points=1,
        )

    async def calculate_points(self, *args, **kwargs):
        return 5, "ParentResult"


@pytest.mark.asyncio
async def test_dsl_extend_result_has_no_breakdown():
    ast = {
        "type": "program",
        "id": "p",
        "rules": [],
        "post_rules": [
            _rule(
                "pr",
                _lit("l", True),
                [{"type": "set_points", "id": "sp", "value": _lit("v", 7)}],
            )
        ],
    }
    validate_ast(ast)

    result = await _strategy(
        ast, type_="DSL_EXTEND", parent=_Parent()
    ).calculate_points("g", "t", "u", data={})

    assert result == (7, "ParentResult")


# --------------------------------------------------------------------------
# Simulate endpoint service
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simulation_response_includes_breakdown():
    service = DslSimulationService(MagicMock(), MagicMock())

    response = await service._run_ast(
        _streak_program(),
        SimulationRequest(
            externalGameId="g",
            externalTaskId="t",
            externalUserId="u",
            data={"streak_days": 9, "quality": 3},
        ),
    )

    assert response.points == 18.0
    assert response.breakdown == _breakdown(("base", 10), ("streak", 5), ("quality", 3))


@pytest.mark.asyncio
async def test_simulation_response_breakdown_is_null_without_assign():
    ast = _program([_rule("r", _lit("l", False), [_assign("a", _lit("v", 1))])])
    service = DslSimulationService(MagicMock(), AsyncMock())

    response = await service._run_ast(
        ast,
        SimulationRequest(externalGameId="g", externalTaskId="t", externalUserId="u"),
    )

    assert response.points == 0
    assert response.breakdown is None
