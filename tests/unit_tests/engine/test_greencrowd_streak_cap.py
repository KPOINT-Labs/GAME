"""
Tests for the per-game GREENCROWD streak bonus cap.

A game sets the cap with the game param ``variable_streak_bonus_cap`` (a whole
number of points, stored as a string). ``DIM_S`` is limited to the cap in
every experiment group. ``0`` (the strategy default) means no cap; invalid
values are ignored.
"""

import logging
from types import SimpleNamespace

import pytest

from app.engine.greencrowdStrategy import (
    STREAK_BONUS_CAP_PARAM,
    cap_streak_bonus,
    resolve_streak_bonus_cap,
)
from tests.unit_tests.engine import test_greencrowd_characterization as chars
from tests.unit_tests.engine.test_greencrowdStrategy import (
    QueryRecords,
    _build_strategy_with_mocked_container,
)

# Freezes "now" at 2026-11-18 14:00 UTC inside the greencrowd module.
frozen_now = chars.frozen_now

_at = chars._at
_record = chars._record


# --------------------------------------------------------------------------
# resolve_streak_bonus_cap(game_params, default)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "game_params,expected",
    [
        (None, 0),
        ({}, 0),
        ({"variable_basic_points": "20"}, 0),
        ({STREAK_BONUS_CAP_PARAM: "50"}, 50),
        ({STREAK_BONUS_CAP_PARAM: 50}, 50),
        ({STREAK_BONUS_CAP_PARAM: " 50 "}, 50),
        ({STREAK_BONUS_CAP_PARAM: "50.0"}, 50),
        ({STREAK_BONUS_CAP_PARAM: "0"}, 0),
    ],
    ids=[
        "no_params",
        "empty_params",
        "other_params_only",
        "string_value",
        "int_value",
        "surrounding_whitespace",
        "whole_float_string",
        "explicit_zero_means_no_cap",
    ],
)
def test_resolve_streak_bonus_cap(game_params, expected):
    assert resolve_streak_bonus_cap(game_params, default=0) == expected


@pytest.mark.parametrize("raw", ["abc", "-5", "12.5", "nan", "inf", ""])
def test_resolve_streak_bonus_cap_falls_back_to_default_on_invalid(raw, caplog):
    with caplog.at_level(logging.WARNING):
        cap = resolve_streak_bonus_cap({STREAK_BONUS_CAP_PARAM: raw}, default=30)

    assert cap == 30
    if raw:
        assert STREAK_BONUS_CAP_PARAM in caplog.text


# --------------------------------------------------------------------------
# cap_streak_bonus(dim_s_value, cap)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dim_s,cap,expected",
    [
        (40, 0, 40),
        (40, 25, 25),
        (40, 40, 40),
        (40, 100, 40),
        (10, 5, 5),
        (None, 25, None),
    ],
)
def test_cap_streak_bonus(dim_s, cap, expected):
    assert cap_streak_bonus(dim_s, cap) == expected


# --------------------------------------------------------------------------
# simulate_strategy applies the game's cap
# --------------------------------------------------------------------------

TASK = SimpleNamespace(id="task-a", externalTaskId="poi_1")
# Ten consecutive active days ending today: streak 10, DIM_S = 10 * 2**2 = 40.
TEN_DAY_STREAK = [_record("task-a", "user-1", _at(11, d)) for d in range(9, 19)]


def _simulate_dynamic(game_params=None, strategy_default=None):
    strategy = _build_strategy_with_mocked_container()
    if strategy_default is not None:
        strategy.variable_streak_bonus_cap = strategy_default
    strategy.user_points_service.get_all_point_of_tasks_list.return_value = (
        QueryRecords(TEN_DAY_STREAK)
    )
    strategy.user_service.get_user_by_externalUserId.return_value = SimpleNamespace(
        id="user-1"
    )
    data = {"task": TASK, "allTasks": [TASK], "externalUserId": "user-1"}
    if game_params is not None:
        data["gameParams"] = game_params
    return strategy.simulate_strategy(
        data_to_simulate=data, userGroup="dynamic_calculation"
    )


def _dim(result, key):
    return next(d[key] for d in result.dimensions if key in d)


def test_strategy_exposes_cap_variable_defaulting_to_no_cap():
    strategy = _build_strategy_with_mocked_container()

    assert strategy.get_variables()[STREAK_BONUS_CAP_PARAM] == 0


def test_simulate_without_cap_keeps_full_streak_bonus(frozen_now):
    result = _simulate_dynamic()

    assert _dim(result, "DIM_S") == 40


def test_simulate_caps_streak_bonus_from_game_param(frozen_now):
    uncapped = _simulate_dynamic()
    capped = _simulate_dynamic({STREAK_BONUS_CAP_PARAM: "25"})

    assert _dim(capped, "DIM_S") == 25
    assert capped.totalSimulatedPoints == uncapped.totalSimulatedPoints - 15
    # Only DIM_S changes.
    for key in ("DIM_BP", "DIM_LBE", "DIM_TD", "DIM_PP"):
        assert _dim(capped, key) == _dim(uncapped, key)


def test_simulate_cap_above_bonus_has_no_effect(frozen_now):
    result = _simulate_dynamic({STREAK_BONUS_CAP_PARAM: "100"})

    assert _dim(result, "DIM_S") == 40


def test_simulate_uses_strategy_default_when_game_has_no_cap(frozen_now):
    result = _simulate_dynamic({}, strategy_default=30)

    assert _dim(result, "DIM_S") == 30


def test_simulate_game_param_overrides_strategy_default(frozen_now):
    result = _simulate_dynamic({STREAK_BONUS_CAP_PARAM: "0"}, strategy_default=30)

    assert _dim(result, "DIM_S") == 40


def test_simulate_ignores_invalid_game_param(frozen_now):
    result = _simulate_dynamic({STREAK_BONUS_CAP_PARAM: "lots"})

    assert _dim(result, "DIM_S") == 40


def test_simulate_caps_average_score_group():
    """Past awards may predate the cap, so it also applies to averaged values."""
    strategy = _build_strategy_with_mocked_container()
    strategy.user_points_service.get_all_point_of_tasks_list.return_value = [
        SimpleNamespace(
            data={
                "tasks": [],
                "callbackData": [
                    {
                        "dimensions": [
                            {"DIM_BP": 2},
                            {"DIM_LBE": 4},
                            {"DIM_TD": 6},
                            {"DIM_PP": 8},
                            {"DIM_S": 80},
                        ]
                    }
                ],
            }
        )
    ]

    result = strategy.simulate_strategy(
        data_to_simulate={
            "task": TASK,
            "allTasks": [TASK],
            "externalUserId": "user-1",
            "gameParams": {STREAK_BONUS_CAP_PARAM: "20"},
        },
        userGroup="average_score",
    )

    assert _dim(result, "DIM_S") == 20
    assert result.totalSimulatedPoints == 2 + 4 + 6 + 8 + 20
