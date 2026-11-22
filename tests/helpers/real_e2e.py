"""
Shared helpers for real HTTP + PostgreSQL end-to-end tests (``e2e_real_http``).

These tests call a live API (``E2E_BASE_URL``) and assert against its
PostgreSQL database (``E2E_DATABASE_URL``). The fixtures that wrap these
helpers live in ``tests/e2e/conftest.py``.
"""

import os
from dataclasses import dataclass

import httpx
import pytest

REAL_E2E_MARKS = [
    pytest.mark.e2e_real_http,
    pytest.mark.skipif(
        os.getenv("RUN_REAL_E2E", "0") != "1",
        reason=(
            "Set RUN_REAL_E2E=1 and configure E2E environment variables "
            "to run real HTTP + PostgreSQL end-to-end tests."
        ),
    ),
]


@dataclass(frozen=True)
class RealE2EConfig:
    base_url: str
    database_url: str
    keycloak_url: str
    keycloak_realm: str
    keycloak_client_id: str
    keycloak_client_secret: str
    admin_username: str
    admin_password: str


def require_env(name: str, fallback: str | None = None) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        if fallback is None:
            raise KeyError(name)
        return fallback
    return value


def fetch_keycloak_access_token(
    *,
    keycloak_url: str,
    realm: str,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
) -> str:
    token_url = (
        f"{keycloak_url.rstrip('/')}/realms/{realm}/protocol/openid-connect/token"
    )
    payload = {
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password,
    }
    response = httpx.post(token_url, data=payload, timeout=30.0)
    assert response.status_code == 200, (
        f"Unable to obtain Keycloak token from {token_url}. "
        f"status={response.status_code}, body={response.text}"
    )
    body = response.json()
    access_token = body.get("access_token")
    assert access_token, f"Keycloak token response missing access_token: {body}"
    return access_token


def optional_env(name: str, fallback: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return fallback
    return value


def load_real_e2e_config() -> RealE2EConfig:
    """Real-E2E settings from the environment; fails the test when missing."""
    try:
        database_url = require_env("E2E_DATABASE_URL", os.getenv("DATABASE_URL"))
        keycloak_url = require_env(
            "E2E_KEYCLOAK_URL",
            os.getenv("KEYCLOAK_URL", "http://localhost:8080"),
        )
        keycloak_realm = require_env(
            "E2E_KEYCLOAK_REALM",
            os.getenv("KEYCLOAK_REALM"),
        )
        keycloak_client_id = require_env(
            "E2E_KEYCLOAK_CLIENT_ID",
            os.getenv("KEYCLOAK_CLIENT_ID"),
        )
        keycloak_client_secret = require_env(
            "E2E_KEYCLOAK_CLIENT_SECRET",
            os.getenv("KEYCLOAK_CLIENT_SECRET"),
        )
        admin_username = require_env(
            "E2E_KEYCLOAK_ADMIN_USERNAME",
            os.getenv("KEYCLOAK_ADMIN_USERNAME", "game_admin@example.com"),
        )
        admin_password = require_env(
            "E2E_KEYCLOAK_ADMIN_PASSWORD",
            os.getenv("KEYCLOAK_USER_WITH_ROLE_PASSWORD"),
        )
    except KeyError as exc:
        missing_name = str(exc).strip("'")
        pytest.fail(
            "RUN_REAL_E2E=1 requires environment variable "
            f"`{missing_name}` (or its documented fallback).",
            pytrace=False,
        )
        raise

    if not database_url.startswith("postgresql"):
        pytest.fail(
            "E2E_DATABASE_URL / DATABASE_URL must point to PostgreSQL "
            f"(received: {database_url})",
            pytrace=False,
        )

    return RealE2EConfig(
        base_url=os.getenv("E2E_BASE_URL", "http://localhost:8000/api/v1"),
        database_url=database_url,
        keycloak_url=keycloak_url,
        keycloak_realm=keycloak_realm,
        keycloak_client_id=keycloak_client_id,
        keycloak_client_secret=keycloak_client_secret,
        admin_username=admin_username,
        admin_password=admin_password,
    )
