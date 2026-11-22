"""
Real HTTP + PostgreSQL end-to-end tests for the DSL field ``user.streak_days``.

A published custom strategy awards ``user.streak_days`` points, so the points
in each response are the streak the engine computed. Earlier days are set up
by inserting backdated awards straight into PostgreSQL; today's awards go
through the API. On PostgreSQL the active days are grouped in SQL in the
game's timezone, a path the SQLite unit tests do not cover.

Everything is created under a per-run prefix and deleted afterwards.
"""

import datetime
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.model import import_all_models
from app.model.api_requests import ApiRequests
from app.model.game_params import GamesParams
from app.model.games import Games
from app.model.strategy_definition import StrategyDefinition
from app.model.strategy_execution_log import StrategyExecutionLog
from app.model.task_params import TasksParams
from app.model.tasks import Tasks
from app.model.user_actions import UserActions
from app.model.user_game_config import UserGameConfig
from app.model.user_points import UserPoints
from app.model.users import Users
from app.model.wallet import Wallet
from app.model.wallet_transactions import WalletTransactions
from tests.helpers.real_e2e import (
    REAL_E2E_MARKS,
    RealE2EConfig,
    fetch_keycloak_access_token,
)

pytestmark = REAL_E2E_MARKS

# Register every table on the ORM metadata. The models above have foreign keys
# to tables (e.g. ``apikey``) whose models nothing in this module imports, so
# the cleanup queries fail with NoReferencedTableError when this file runs on
# its own.
import_all_models()

# Ahead of UTC, so a local day starts on the previous UTC day.
GAME_TIMEZONE = "Asia/Kolkata"

# Awards ``user.streak_days`` points: the response shows the computed streak.
STREAK_AS_POINTS_AST = {
    "type": "program",
    "rules": [
        {
            "type": "rule",
            "when": {
                "type": "compare",
                "op": ">=",
                "left": {"type": "field", "path": "user.streak_days"},
                "right": {"type": "literal", "value": 1},
            },
            "then": [
                {
                    "type": "assign_points",
                    "value": {"type": "field", "path": "user.streak_days"},
                    "case_name": "StreakDays",
                }
            ],
        }
    ],
    "default": {
        "type": "assign_points",
        "value": {"type": "literal", "value": 0},
        "case_name": "NoStreak",
    },
}


@dataclass(frozen=True)
class StreakE2E:
    base_url: str
    token: str
    prefix: str
    game_id: str
    external_game_id: str
    other_game_id: str
    strategy_id: str
    task_ids: dict

    def headers(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self.token}", **(extra or {})}

    def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = self.headers(kwargs.pop("headers", None))
        return httpx.request(
            method,
            f"{self.base_url.rstrip('/')}{path}",
            headers=headers,
            timeout=30.0,
            **kwargs,
        )

    def user(self, name: str) -> str:
        return f"{self.prefix}-{name}"

    def award(self, user: str, key: str, task: str = "daily_streak") -> dict:
        response = self.call(
            "POST",
            f"/games/{self.game_id}/tasks/{task}/points",
            json={"externalUserId": user, "data": {}},
            headers={"Idempotency-Key": f"{user}:{key}"},
        )
        assert response.status_code == 200, response.text
        return response.json()


def _ok(response: httpx.Response, *codes: int) -> dict:
    assert response.status_code in (codes or (200, 201)), response.text
    return response.json()


def _local_today() -> datetime.date:
    return datetime.datetime.now(ZoneInfo(GAME_TIMEZONE)).date()


def _local_instant(day: datetime.date, hour: int, minute: int = 0):
    """A UTC instant at ``hour:minute`` on the game's local ``day``."""
    local = datetime.datetime.combine(
        day, datetime.time(hour, minute), tzinfo=ZoneInfo(GAME_TIMEZONE)
    )
    return local.astimezone(datetime.timezone.utc)


def _backdate(
    engine: Engine, external_user_id: str, task_id, when: datetime.datetime
) -> None:
    """Insert an award at ``when`` for the user, creating the user if needed."""
    with Session(engine) as session:
        user = (
            session.query(Users)
            .filter(Users.externalUserId == external_user_id)
            .one_or_none()
        )
        if user is None:
            user = Users(externalUserId=external_user_id)
            session.add(user)
            session.flush()
        session.add(
            UserPoints(
                userId=user.id,
                taskId=task_id,
                points=1,
                caseName="Backdated",
                created_at=when,
                updated_at=when,
            )
        )
        session.commit()


def _days_ago(n: int, hour: int = 12, minute: int = 0) -> datetime.datetime:
    return _local_instant(_local_today() - datetime.timedelta(days=n), hour, minute)


def _cleanup(engine: Engine, prefix: str, game_ids: list, strategy_ids: list) -> None:
    with Session(engine) as session:
        user_ids = [
            row.id
            for row in session.query(Users.id).filter(
                Users.externalUserId.like(f"{prefix}%")
            )
        ]
        wallet_ids = [
            row.id
            for row in session.query(Wallet.id).filter(Wallet.userId.in_(user_ids))
        ]
        task_ids = [
            row.id for row in session.query(Tasks.id).filter(Tasks.gameId.in_(game_ids))
        ]
        deletes = [
            (WalletTransactions, WalletTransactions.walletId.in_(wallet_ids)),
            (Wallet, Wallet.id.in_(wallet_ids)),
            (UserPoints, UserPoints.userId.in_(user_ids)),
            (UserPoints, UserPoints.taskId.in_(task_ids)),
            (UserGameConfig, UserGameConfig.userId.in_(user_ids)),
            (UserGameConfig, UserGameConfig.gameId.in_(game_ids)),
            (UserActions, UserActions.userId.in_(user_ids)),
            (ApiRequests, ApiRequests.userId.in_(user_ids)),
            (Users, Users.id.in_(user_ids)),
            (TasksParams, TasksParams.taskId.in_(task_ids)),
            (Tasks, Tasks.id.in_(task_ids)),
            (GamesParams, GamesParams.gameId.in_(game_ids)),
            (Games, Games.id.in_(game_ids)),
            (
                StrategyExecutionLog,
                StrategyExecutionLog.strategyId.in_(
                    [f"custom:{sid}" for sid in strategy_ids] + strategy_ids
                ),
            ),
            (StrategyDefinition, StrategyDefinition.id.in_(strategy_ids)),
        ]
        for model, condition in deletes:
            session.query(model).filter(condition).delete(synchronize_session=False)
        session.commit()


@pytest.fixture(scope="module")
def streak_e2e(real_e2e_config: RealE2EConfig, postgres_engine: Engine):
    token = fetch_keycloak_access_token(
        keycloak_url=real_e2e_config.keycloak_url,
        realm=real_e2e_config.keycloak_realm,
        client_id=real_e2e_config.keycloak_client_id,
        client_secret=real_e2e_config.keycloak_client_secret,
        username=real_e2e_config.admin_username,
        password=real_e2e_config.admin_password,
    )
    prefix = f"e2e-streak-{uuid.uuid4().hex[:12]}"
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
                    "name": f"{prefix} streak as points",
                    "type": "DSL_FULL",
                    "astJson": STREAK_AS_POINTS_AST,
                },
            )
        )
        strategy_ids.append(strategy["id"])
        _ok(post(f"/strategies/custom/{strategy['id']}/publish", None))

        games = {}
        for name in ("main", "other"):
            game = _ok(
                post(
                    "/games",
                    {
                        "externalGameId": f"{prefix}-{name}",
                        "platform": "web",
                        "strategyId": "default",
                        "timezone": GAME_TIMEZONE,
                    },
                )
            )
            games[name] = str(game["gameId"])
            game_ids.append(games[name])

        custom_strategy_id = f"custom:{strategy['id']}"
        for game_id, external_task_id, strategy_id in (
            (games["main"], "daily_streak", custom_strategy_id),
            (games["main"], "video_view", "default"),
            (games["other"], "daily_streak", "default"),
        ):
            _ok(
                post(
                    f"/games/{game_id}/tasks",
                    {"externalTaskId": external_task_id, "strategyId": strategy_id},
                )
            )

        with Session(postgres_engine) as session:
            rows = session.query(Tasks).filter(Tasks.gameId.in_(game_ids)).all()
            task_ids = {(str(t.gameId), t.externalTaskId): t.id for t in rows}
            strategies = {t.id: t.strategyId for t in rows}

        main_daily = task_ids[(games["main"], "daily_streak")]
        assert strategies[main_daily] == custom_strategy_id

        yield StreakE2E(
            base_url=base,
            token=token,
            prefix=prefix,
            game_id=games["main"],
            external_game_id=f"{prefix}-main",
            other_game_id=games["other"],
            strategy_id=strategy["id"],
            task_ids={
                "daily_streak": main_daily,
                "video_view": task_ids[(games["main"], "video_view")],
                "other_game": task_ids[(games["other"], "daily_streak")],
            },
        )
    finally:
        _cleanup(postgres_engine, prefix, game_ids, strategy_ids)


def test_first_award_of_a_new_user_is_a_one_day_streak(streak_e2e: StreakE2E):
    result = streak_e2e.award(streak_e2e.user("new"), "a1")

    assert result["points"] == 1
    assert result["caseName"] == "StreakDays"


def test_streak_through_yesterday_counts_today_and_is_stable_within_the_day(
    streak_e2e: StreakE2E, postgres_engine: Engine
):
    user = streak_e2e.user("steady")
    for n in (1, 2):
        _backdate(
            postgres_engine, user, streak_e2e.task_ids["daily_streak"], _days_ago(n)
        )

    first = streak_e2e.award(user, "a1")
    second = streak_e2e.award(user, "a2")

    assert first["points"] == 3
    assert second["points"] == 3


def test_replaying_an_award_returns_it_without_a_new_row(
    streak_e2e: StreakE2E, postgres_engine: Engine
):
    user = streak_e2e.user("replay")
    _backdate(postgres_engine, user, streak_e2e.task_ids["daily_streak"], _days_ago(1))

    first = streak_e2e.award(user, "same-key")
    replay = streak_e2e.award(user, "same-key")

    assert first["points"] == replay["points"] == 2
    with Session(postgres_engine) as session:
        awards = (
            session.query(UserPoints)
            .join(Users, Users.id == UserPoints.userId)
            .filter(Users.externalUserId == user)
            .count()
        )
    assert awards == 2  # the backdated award and one award today


def test_a_missed_day_resets_the_streak(streak_e2e: StreakE2E, postgres_engine: Engine):
    user = streak_e2e.user("gap")
    for n in (2, 3, 4):
        _backdate(
            postgres_engine, user, streak_e2e.task_ids["daily_streak"], _days_ago(n)
        )

    assert streak_e2e.award(user, "a1")["points"] == 1


def test_any_task_of_the_game_counts_but_other_games_do_not(
    streak_e2e: StreakE2E, postgres_engine: Engine
):
    user = streak_e2e.user("tasks")
    _backdate(postgres_engine, user, streak_e2e.task_ids["video_view"], _days_ago(1))
    _backdate(postgres_engine, user, streak_e2e.task_ids["other_game"], _days_ago(2))

    # Yesterday (another task, same game) + today; the day before only has
    # activity in the other game.
    assert streak_e2e.award(user, "a1")["points"] == 2


def test_days_are_the_games_local_days(streak_e2e: StreakE2E, postgres_engine: Engine):
    user = streak_e2e.user("timezone")
    # 00:30 local yesterday is 19:00 UTC the day before: counted as yesterday
    # only when days are grouped in the game's timezone.
    _backdate(
        postgres_engine,
        user,
        streak_e2e.task_ids["daily_streak"],
        _days_ago(1, hour=0, minute=30),
    )

    assert streak_e2e.award(user, "a1")["points"] == 2


def test_activity_endpoint_reports_the_same_streak(
    streak_e2e: StreakE2E, postgres_engine: Engine
):
    user = streak_e2e.user("activity")
    for n in (1, 2, 3):
        _backdate(
            postgres_engine, user, streak_e2e.task_ids["daily_streak"], _days_ago(n)
        )

    awarded = streak_e2e.award(user, "a1")["points"]
    activity = _ok(
        streak_e2e.call("GET", f"/games/{streak_e2e.game_id}/users/{user}/activity")
    )

    assert awarded == 4
    assert activity["streaks"]["current"] == awarded


def test_simulate_reads_the_live_streak_or_a_mocked_value(
    streak_e2e: StreakE2E, postgres_engine: Engine
):
    user = streak_e2e.user("simulate")
    _backdate(postgres_engine, user, streak_e2e.task_ids["daily_streak"], _days_ago(1))
    request = {
        "externalGameId": streak_e2e.external_game_id,
        "externalTaskId": "daily_streak",
        "externalUserId": user,
        "astJson": STREAK_AS_POINTS_AST,
    }

    live = _ok(streak_e2e.call("POST", "/strategies/custom/simulate", json=request))
    mocked = _ok(
        streak_e2e.call(
            "POST",
            "/strategies/custom/simulate",
            json={**request, "mockState": {"user.streak_days": 10}},
        )
    )

    assert live["points"] == 2
    assert mocked["points"] == 10
    with Session(postgres_engine) as session:
        persisted = (
            session.query(UserPoints)
            .join(Users, Users.id == UserPoints.userId)
            .filter(Users.externalUserId == user)
            .count()
        )
    assert persisted == 1  # simulation records nothing
