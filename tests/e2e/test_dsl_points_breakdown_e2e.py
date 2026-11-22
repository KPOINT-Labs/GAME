"""
Real HTTP + PostgreSQL end-to-end tests for custom-strategy points breakdowns.

A published custom strategy awards 10 base points for a shared item, double
points (a x2 multiplier bonus) when ``data.streak_days >= 5`` (in a rule of
its own, before the base rule) and a fixed quality bonus taken from
``data.quality``. When nothing was shared the fallback ``default`` awards 1
point without bonuses. Each award must store ``base`` + bonuses on
``userpoints.breakdown`` (JSONB), award their sum, and expose the breakdown on
the points details endpoint and in simulate.

Everything is created under a per-run prefix and deleted afterwards.
"""

import uuid
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.model.user_points import UserPoints
from app.model.users import Users
from tests.e2e.test_dsl_streak_days_e2e import _cleanup, _ok
from tests.helpers.real_e2e import (
    REAL_E2E_MARKS,
    RealE2EConfig,
    fetch_keycloak_access_token,
)

pytestmark = REAL_E2E_MARKS

TASK = "complete_survey"

BASE_PLUS_BONUSES_AST = {
    "type": "program",
    "rules": [
        {
            "type": "rule",
            "when": {
                "type": "compare",
                "op": ">=",
                "left": {"type": "field", "path": "data.streak_days"},
                "right": {"type": "literal", "value": 5},
            },
            "then": [
                {
                    "type": "add_bonus",
                    "key": "streak",
                    "mode": "multiplier",
                    "value": {"type": "literal", "value": 2},
                }
            ],
        },
        {
            "type": "rule",
            "when": {
                "type": "compare",
                "op": "==",
                "left": {"type": "field", "path": "data.shared"},
                "right": {"type": "literal", "value": True},
            },
            "then": [
                {
                    "type": "add_bonus",
                    "key": "quality",
                    "value": {"type": "field", "path": "data.quality"},
                },
                {
                    "type": "assign_points",
                    "value": {"type": "literal", "value": 10},
                    "case_name": "Completed",
                },
            ],
        },
    ],
    "default": {
        "type": "assign_points",
        "value": {"type": "literal", "value": 1},
        "case_name": "Fallback",
    },
}


def _breakdown(*components):
    return {
        "version": 1,
        "components": [{"key": k, "points": p} for k, p in components],
    }


@dataclass(frozen=True)
class BreakdownE2E:
    base_url: str
    token: str
    prefix: str
    game_id: str
    external_game_id: str

    def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})}
        return httpx.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=30.0,
            **kwargs,
        )

    def user(self, name: str) -> str:
        return f"{self.prefix}-{name}"

    def award(self, user: str, data: dict) -> dict:
        return _ok(
            self.call(
                "POST",
                f"/games/{self.game_id}/tasks/{TASK}/points",
                json={"externalUserId": user, "data": data},
                headers={"Idempotency-Key": f"{user}:{uuid.uuid4().hex}"},
            )
        )


def _stored_breakdowns(engine: Engine, external_user_id: str) -> list:
    with Session(engine) as session:
        rows = (
            session.query(UserPoints.points, UserPoints.breakdown)
            .join(Users, Users.id == UserPoints.userId)
            .filter(Users.externalUserId == external_user_id)
            .all()
        )
    return [(row.points, row.breakdown) for row in rows]


@pytest.fixture(scope="module")
def breakdown_e2e(real_e2e_config: RealE2EConfig, postgres_engine: Engine):
    token = fetch_keycloak_access_token(
        keycloak_url=real_e2e_config.keycloak_url,
        realm=real_e2e_config.keycloak_realm,
        client_id=real_e2e_config.keycloak_client_id,
        client_secret=real_e2e_config.keycloak_client_secret,
        username=real_e2e_config.admin_username,
        password=real_e2e_config.admin_password,
    )
    prefix = f"e2e-breakdown-{uuid.uuid4().hex[:12]}"
    game_ids: list = []
    strategy_ids: list = []
    base = real_e2e_config.base_url.rstrip("/")
    auth = {"Authorization": f"Bearer {token}"}

    def post(path, body):
        return httpx.post(f"{base}{path}", json=body, headers=auth, timeout=30.0)

    try:
        strategy = _ok(
            post(
                "/strategies/custom",
                {
                    "name": f"{prefix} base plus bonuses",
                    "type": "DSL_FULL",
                    "astJson": BASE_PLUS_BONUSES_AST,
                },
            )
        )
        strategy_ids.append(strategy["id"])
        _ok(post(f"/strategies/custom/{strategy['id']}/publish", None))

        game = _ok(
            post(
                "/games",
                {
                    "externalGameId": f"{prefix}-game",
                    "platform": "web",
                    "strategyId": "default",
                },
            )
        )
        game_id = str(game["gameId"])
        game_ids.append(game_id)
        _ok(
            post(
                f"/games/{game_id}/tasks",
                {"externalTaskId": TASK, "strategyId": f"custom:{strategy['id']}"},
            )
        )

        yield BreakdownE2E(
            base_url=base,
            token=token,
            prefix=prefix,
            game_id=game_id,
            external_game_id=f"{prefix}-game",
        )
    finally:
        _cleanup(postgres_engine, prefix, game_ids, strategy_ids)


def test_award_stores_base_and_bonuses_and_returns_their_sum(
    breakdown_e2e: BreakdownE2E, postgres_engine: Engine
):
    user = breakdown_e2e.user("streak")

    result = breakdown_e2e.award(user, {"shared": True, "streak_days": 9, "quality": 2})

    assert result["points"] == 22
    assert result["caseName"] == "Completed"
    assert _stored_breakdowns(postgres_engine, user) == [
        (22, _breakdown(("base", 10), ("streak", 10), ("quality", 2)))
    ]


def test_fallback_default_awards_its_value_without_bonuses(
    breakdown_e2e: BreakdownE2E, postgres_engine: Engine
):
    user = breakdown_e2e.user("fallback")

    # The streak rule matches, but no rule assigns points (nothing shared),
    # so the default's 1 point is awarded alone.
    result = breakdown_e2e.award(
        user, {"shared": False, "streak_days": 9, "quality": 2}
    )

    assert result["points"] == 1
    assert result["caseName"] == "Fallback"
    assert _stored_breakdowns(postgres_engine, user) == [(1, _breakdown(("base", 1)))]


def test_bonus_rule_that_does_not_match_is_left_out(
    breakdown_e2e: BreakdownE2E, postgres_engine: Engine
):
    user = breakdown_e2e.user("nostreak")

    result = breakdown_e2e.award(user, {"shared": True, "streak_days": 4, "quality": 3})

    assert result["points"] == 13
    assert _stored_breakdowns(postgres_engine, user) == [
        (13, _breakdown(("base", 10), ("quality", 3)))
    ]


def test_non_numeric_bonus_is_rejected_and_nothing_is_stored(
    breakdown_e2e: BreakdownE2E, postgres_engine: Engine
):
    user = breakdown_e2e.user("badbonus")

    response = breakdown_e2e.call(
        "POST",
        f"/games/{breakdown_e2e.game_id}/tasks/{TASK}/points",
        json={
            "externalUserId": user,
            "data": {"shared": True, "streak_days": 1, "quality": "high"},
        },
    )

    assert response.status_code >= 400, response.text
    assert _stored_breakdowns(postgres_engine, user) == []


def test_points_details_endpoint_returns_the_breakdown(breakdown_e2e: BreakdownE2E):
    user = breakdown_e2e.user("details")
    breakdown_e2e.award(user, {"shared": True, "streak_days": 7, "quality": 0})

    details = _ok(
        breakdown_e2e.call(
            "GET", f"/games/{breakdown_e2e.game_id}/tasks/{TASK}/points/details"
        )
    )

    awards = [
        award
        for entry in details
        if entry.get("externalUserId") == user
        for award in entry.get("pointsData") or []
    ]
    assert [a["breakdown"] for a in awards] == [
        _breakdown(("base", 10), ("streak", 10), ("quality", 0))
    ]


def test_simulate_returns_the_breakdown_without_storing_it(
    breakdown_e2e: BreakdownE2E, postgres_engine: Engine
):
    user = breakdown_e2e.user("simulate")

    result = _ok(
        breakdown_e2e.call(
            "POST",
            "/strategies/custom/simulate",
            json={
                "externalGameId": breakdown_e2e.external_game_id,
                "externalTaskId": TASK,
                "externalUserId": user,
                "data": {"shared": True, "streak_days": 8, "quality": 1},
                "astJson": BASE_PLUS_BONUSES_AST,
            },
        )
    )

    assert result["points"] == 21
    assert result["breakdown"] == _breakdown(
        ("base", 10), ("streak", 10), ("quality", 1)
    )
    assert _stored_breakdowns(postgres_engine, user) == []
