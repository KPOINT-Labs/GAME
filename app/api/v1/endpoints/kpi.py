from datetime import datetime, timezone

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.config import configs
from app.core.container import Container
from app.core.database import Database

router = APIRouter(
    prefix="/kpi",
    tags=["kpi"],
)


summary_health_check = "Health Check"
response_example_health_check = {
    "status": "KPI service is running",
    "timestamp": "2026-02-10T18:30:00Z",
}

responses_health_check = {
    200: {
        "description": "KPI service is healthy",
        "content": {"application/json": {"example": response_example_health_check}},
    },
    500: {
        "description": "Internal server error while evaluating health status",
        "content": {
            "application/json": {
                "example": {"detail": "Error when checking KPI health"}
            }
        },
    },
}

description_health_check = """
Returns a lightweight availability signal for the KPI API service.

### Authentication
- No authentication required.

### Success (200)
Returns:
- `status`: service health message
- `timestamp`: UTC timestamp of the health response generation

### Error Cases
- `500`: unexpected runtime failure while generating the health response

Liveness only: this does not check the database. Use `/kpi/ready` for
readiness.

<sub>**Id_endpoint:** `health_check`</sub>"""


@router.get(
    "/health_check",
    summary=summary_health_check,
    description=description_health_check,
    responses=responses_health_check,
)
async def health_check():
    """
    Check the health of the KPI service.

    Returns:
        dict: The health status of the KPI service.
    """
    return {
        "status": "KPI service is running",
        "timestamp": datetime.now(timezone.utc),
    }


summary_readiness_check = "Readiness Check (database reachable)"
description_readiness_check = """
Reports whether this instance can serve requests: the database answers
`SELECT 1` within `DB_READY_TIMEOUT_SECONDS` (default 2s).

Use it as the **readiness** probe (stop routing traffic while it fails). Use
`/kpi/health_check`, which does not touch the database, as the **liveness**
probe, so a database outage does not restart API pods in a loop.

No authentication required.

### Responses
- `200`: `{"status": "ready", "database": "ok"}`
- `503`: `{"status": "not ready", "database": "unavailable"}`

<sub>**Id_endpoint:** `readiness_check`</sub>"""


@router.get(
    "/ready",
    summary=summary_readiness_check,
    description=description_readiness_check,
    responses={
        200: {
            "description": "Database reachable",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ready",
                        "database": "ok",
                        "timestamp": "2026-11-20T10:00:00Z",
                    }
                }
            },
        },
        503: {
            "description": "Database unreachable; instance should not receive traffic"
        },
    },
)
@inject
async def readiness_check(db: Database = Depends(Provide[Container.db])):
    """
    Check that the database is reachable.

    Returns:
        JSONResponse: 200 when ``SELECT 1`` succeeds in time, else 503.
    """
    ok = await db.ping(timeout_seconds=configs.DB_READY_TIMEOUT_SECONDS)
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ready" if ok else "not ready",
            "database": "ok" if ok else "unavailable",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
