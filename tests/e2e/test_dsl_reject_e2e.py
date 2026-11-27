"""
Real HTTP + PostgreSQL end-to-end tests for the DSL ``reject`` statement.

A published custom strategy rejects a video milestone that the watched
seconds don't cover (``data.watchedSeconds < 30``) with the reason
``MILESTONE_NOT_REACHED``, and awards 10 points otherwise. A rejected award
must fail with 422 ``DSL_REJECTED`` and store nothing, so a later event with
the same idempotency key that the rules accept still earns the points.

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

TASK = "watch_video"

MILESTONE_AST = {
    "type": "program",
    "rules": [
        {
            "type": "rule",
            "when": {
                "type": "compare",
                "op": "<",
                "left": {"type": "field", "path": "data.watchedSeconds"},
                "right": {"type": "literal", "value": 30},
            },
            "then": [
                {
                    "type": "reject",
                    "id": "rej_25",
                    "reason": "MILESTONE_NOT_REACHED",
                }
            ],
        }
    ],
    "default": {
        "type": "assign_points",
        "value": {"type": "literal", "value": 10},
        "case_name": "Milestone25",
    },
}

REJECTED_DETAIL = {
    "code": "DSL_REJECTED",
    "params": {"reason": "MILESTONE_NOT_REACHED", "nodeId": "rej_25"},
    "message": "Event rejected by strategy: MILESTONE_NOT_REACHED",
}


@dataclass(frozen=True)
class RejectE2E:
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

    def award(self, user: str, data: dict, idempotency_key: str) -> httpx.Response:
        return self.call(
            "POST",
            f"/games/{self.game_id}/tasks/{TASK}/points",
            json={"externalUserId": user, "data": data},
            headers={"Idempotency-Key": idempotency_key},
        )


def _stored_points(engine: Engine, external_user_id: str) -> list:
    with Session(engine) as session:
        rows = (
            session.query(UserPoints.points, UserPoints.caseName)
            .join(Users, Users.id == UserPoints.userId)
            .filter(Users.externalUserId == external_user_id)
            .all()
        )
    return [(row.points, row.caseName) for row in rows]


@pytest.fixture(scope="module")
def reject_e2e(real_e2e_config: RealE2EConfig, postgres_engine: Engine):
    token = fetch_keycloak_access_token(
        keycloak_url=real_e2e_config.keycloak_url,
        realm=real_e2e_config.keycloak_realm,
        client_id=real_e2e_config.keycloak_client_id,
        client_secret=real_e2e_config.keycloak_client_secret,
        username=real_e2e_config.admin_username,
        password=real_e2e_config.admin_password,
    )
    prefix = f"e2e-reject-{uuid.uuid4().hex[:12]}"
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
                    "name": f"{prefix} milestone guard",
                    "type": "DSL_FULL",
                    "astJson": MILESTONE_AST,
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

        yield RejectE2E(
            base_url=base,
            token=token,
            prefix=prefix,
            game_id=game_id,
            external_game_id=f"{prefix}-game",
        )
    finally:
        _cleanup(postgres_engine, prefix, game_ids, strategy_ids)


def test_rejected_award_returns_422_and_stores_nothing(
    reject_e2e: RejectE2E, postgres_engine: Engine
):
    user = reject_e2e.user("short")

    response = reject_e2e.award(
        user, {"watchedSeconds": 12}, idempotency_key=f"{user}:m25"
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == REJECTED_DETAIL
    assert _stored_points(postgres_engine, user) == []


def test_same_idempotency_key_earns_points_once_accepted(
    reject_e2e: RejectE2E, postgres_engine: Engine
):
    user = reject_e2e.user("retry")
    key = f"{user}:m25"

    rejected = reject_e2e.award(user, {"watchedSeconds": 5}, idempotency_key=key)
    accepted = reject_e2e.award(user, {"watchedSeconds": 31}, idempotency_key=key)
    replayed = reject_e2e.award(user, {"watchedSeconds": 31}, idempotency_key=key)

    assert rejected.status_code == 422, rejected.text
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["points"] == 10
    # The replay returns the stored award instead of adding another.
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["points"] == 10
    assert _stored_points(postgres_engine, user) == [(10, "Milestone25")]


def test_accepted_event_is_awarded_normally(
    reject_e2e: RejectE2E, postgres_engine: Engine
):
    user = reject_e2e.user("long")

    response = reject_e2e.award(
        user, {"watchedSeconds": 45}, idempotency_key=f"{user}:m25"
    )

    assert response.status_code == 200, response.text
    assert response.json()["points"] == 10
    assert _stored_points(postgres_engine, user) == [(10, "Milestone25")]


def test_simulate_returns_the_reject_error(reject_e2e: RejectE2E):
    response = reject_e2e.call(
        "POST",
        "/strategies/custom/simulate",
        json={
            "externalGameId": reject_e2e.external_game_id,
            "externalTaskId": TASK,
            "externalUserId": reject_e2e.user("simulate"),
            "data": {"watchedSeconds": 1},
            "astJson": MILESTONE_AST,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == REJECTED_DETAIL


def test_reject_reason_with_reserved_prefix_is_refused_at_create(
    reject_e2e: RejectE2E,
):
    ast = {
        "type": "program",
        "rules": [],
        "default": {"type": "reject", "reason": "DSL_REJECTED"},
    }

    response = reject_e2e.call(
        "POST",
        "/strategies/custom",
        json={
            "name": f"{reject_e2e.prefix} reserved reason",
            "type": "DSL_FULL",
            "astJson": ast,
        },
    )

    assert response.status_code == 400, response.text
    assert "reject.reason" in response.text
