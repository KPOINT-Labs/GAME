import logging
import traceback
from datetime import date
from typing import List, Optional
from uuid import UUID

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.v1.endpoints.games_common import (
    _game_access_kwargs,
    _map_write_exception,
    _resolve_correlation_id,
    _resolve_idempotency_key,
)
from app.core.config import configs
from app.core.container import Container
from app.core.exceptions import ForbiddenError, InternalServerError
from app.middlewares.auth_context import AuditLogger, audit_log
from app.middlewares.authentication import auth_api_key_or_oauth2, auth_oauth2
from app.model.cohorts import COHORT_PATTERN
from app.schema.task_schema import (
    AddActionDidByUserInTask,
    AsignPointsToExternalUserId,
    AssignedPointsToExternalUserId,
    ResponseAddActionDidByUserInTask,
    SimulatedPointsAssignedToUser,
)
from app.schema.user_points_schema import (
    AllPointsByGame,
    AllPointsByGameWithDetails,
    GameLeaderboard,
    PointsAssignedToUser,
    TaskLeaderboard,
    UserActivity,
)
from app.services.abuse_prevention_service import AbusePreventionService
from app.services.task_service import TaskService
from app.services.user_actions_service import UserActionsService
from app.services.user_points_service import UserPointsService
from app.services.user_service import UserService
from app.util.calculate_hash_simulated_strategy import calculate_hash_simulated_strategy

router = APIRouter(
    prefix="/games",
    tags=["games"],
)

logger = logging.getLogger(__name__)


summary_get_points_by_gameId = "Retrieve Points by Game ID"
response_example_get_points_by_gameId = {
    "externalGameId": "game-readme-001",
    "created_at": "2026-02-10T12:20:00Z",
    "task": [
        {
            "externalTaskId": "task-login",
            "points": [
                {
                    "externalUserId": "user-123",
                    "points": 120,
                    "timesAwarded": 6,
                },
                {
                    "externalUserId": "user-456",
                    "points": 40,
                    "timesAwarded": 2,
                },
            ],
        },
        {
            "externalTaskId": "task-share",
            "points": [
                {
                    "externalUserId": "user-123",
                    "points": 30,
                    "timesAwarded": 1,
                }
            ],
        },
    ],
}

responses_get_points_by_gameId = {
    200: {
        "description": "Points aggregated by game retrieved successfully",
        "content": {
            "application/json": {"example": response_example_get_points_by_gameId}
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Game not found for the provided id",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Could not find item with gameId = 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path parameters",
    },
    500: {
        "description": "Internal server error while retrieving game points",
    },
}

description_get_points_by_gameId = """
Returns game-level point aggregation grouped by task and user.

### Path Parameter
- `gameId` (`UUID`, required): Internal game identifier.

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Success (200)
Returns:
- `externalGameId`
- `created_at`
- `task`: list of tasks with points per user (`externalUserId`, `points`, `timesAwarded`)

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: no game found with the provided `gameId`
- `422`: malformed `gameId` (invalid UUID format)
- `500`: retrieval failure

### Freshness
When the server's response cache is enabled, this result is cached for up
to `RESPONSE_CACHE_AGGREGATE_TTL_SECONDS` (10 minutes by default): points
awarded in the meantime may not show yet. Changes to the game or its tasks
are reflected immediately.

<sub>**Id_endpoint:** `get_points_by_gameId`</sub>
"""  # noqa


@router.get(
    "/{gameId}/points",
    response_model=AllPointsByGame,
    summary=summary_get_points_by_gameId,
    description=description_get_points_by_gameId,
    responses=responses_get_points_by_gameId,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_points_by_gameId(
    gameId: UUID,
    service: UserPointsService = Depends(Provide[Container.user_points_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve points associated with a specific game by its ID.

    Args:
        gameId (UUID): The ID of the game.
        service (UserPointsService): Injected UserPointsService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        AllPointsByGame: The points details for the specified game.
    """
    auth = audit.auth
    await audit.info("Points retrieval by game ID", {"gameId": str(gameId)})

    return await service.get_points_by_gameId(
        gameId, **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin)
    )


summary_get_points_by_gameId_with_details = "Retrieve Points by Game ID with Details"
response_example_get_points_by_gameId_with_details = {
    "externalGameId": "game-readme-001",
    "created_at": "2026-02-10T12:20:00Z",
    "task": [
        {
            "externalTaskId": "task-login",
            "points": [
                {
                    "externalUserId": "user-123",
                    "points": 120,
                    "timesAwarded": 6,
                    "pointsData": [
                        {
                            "points": 20,
                            "caseName": "daily_login",
                            "created_at": "2026-02-10T08:00:00Z",
                        },
                        {
                            "points": 100,
                            "caseName": "weekly_bonus",
                            "created_at": "2026-02-10T12:00:00Z",
                        },
                    ],
                }
            ],
        }
    ],
}

responses_get_points_by_gameId_with_details = {
    200: {
        "description": "Detailed points by game retrieved successfully",
        "content": {
            "application/json": {
                "example": response_example_get_points_by_gameId_with_details
            }
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Game not found for the provided id",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Could not find item with gameId = 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path parameters",
    },
    500: {
        "description": "Internal server error while retrieving detailed points",
    },
}

description_get_points_by_gameId_with_details = """
Returns game-level point aggregation with per-award detail records.

### Path Parameter
- `gameId` (`UUID`, required): Internal game identifier.

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Success (200)
Returns:
- `externalGameId`
- `created_at`
- `task`: list of tasks with per-user totals and `pointsData` history (`points`, `caseName`, `created_at`)

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: no game found with the provided `gameId`
- `422`: malformed `gameId` (invalid UUID format)
- `500`: retrieval failure

### Freshness
When the server's response cache is enabled, this result is cached for up
to `RESPONSE_CACHE_AGGREGATE_TTL_SECONDS` (10 minutes by default): points
awarded in the meantime may not show yet. Changes to the game or its tasks
are reflected immediately.

<sub>**Id_endpoint:** `get_points_by_gameId_with_details`</sub>
"""  # noqa


@router.get(
    "/{gameId}/points/details",
    response_model=AllPointsByGameWithDetails,
    summary=summary_get_points_by_gameId_with_details,
    description=description_get_points_by_gameId_with_details,
    responses=responses_get_points_by_gameId_with_details,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_points_by_gameId_with_details(
    gameId: UUID,
    service: UserPointsService = Depends(Provide[Container.user_points_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve points associated with a specific game by its ID.

    Args:
        gameId (UUID): The ID of the game.
        service (UserPointsService): Injected UserPointsService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        AllPointsByGame: The points details for the specified game.
    """
    auth = audit.auth
    await audit.info(
        "Points retrieval by game ID with details",
        {"gameId": str(gameId)},
    )

    return await service.get_points_by_gameId_with_details(
        gameId, **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin)
    )


summary_get_leaderboard_by_gameId = "Retrieve Game Leaderboard"
response_example_get_leaderboard_by_gameId = {
    "externalGameId": "game-readme-001",
    "totalUsers": 3,
    "limit": 10,
    "offset": 0,
    "entries": [
        {"rank": 1, "externalUserId": "user-123", "points": 150, "timesAwarded": 7},
        {"rank": 2, "externalUserId": "user-456", "points": 40, "timesAwarded": 2},
        {"rank": 2, "externalUserId": "user-789", "points": 40, "timesAwarded": 4},
    ],
    "cohort": None,
    "user": {
        "rank": 2,
        "externalUserId": "user-456",
        "points": 40,
        "timesAwarded": 2,
    },
}

responses_get_leaderboard_by_gameId = {
    200: {
        "description": "Leaderboard page retrieved successfully",
        "content": {
            "application/json": {"example": response_example_get_leaderboard_by_gameId}
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Game not found for the provided id",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Game with gameId: 4ce32be2-77f6-4ffc-8e07-78dc220f0521 not found"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path or query parameters",
    },
    500: {
        "description": "Internal server error while retrieving the leaderboard",
    },
}

description_get_leaderboard_by_gameId = """
Returns users ranked by their total points across every task in the game.

### Path Parameter
- `gameId` (`UUID`, required): Internal game identifier.

### Query Parameters
- `limit` (`int`, optional, default `10`, `1`-`100`): Page size.
- `offset` (`int`, optional, default `0`): Number of ranked users to skip.
- `externalUserId` (`string`, optional): Also return this user's rank in `user`.
- `cohort` (`string`, optional): Rank within one cohort. Only points earned
  while the user was in that cohort count (see "Cohorts" below).

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Ranking
- Users are ordered by total points, highest first.
- Tied users share a rank and the next rank is skipped (`1, 2, 2, 4`).
- Ranks are global, so they stay correct on later pages.
- Users with no points in the game are not listed. A game with no points
  returns an empty `entries` list, not `404`.

### Requested user (`externalUserId`)
- `user.rank` is the user's overall rank in the game (not affected by
  `limit`/`offset`) if it is `100` or better, including ties at 100.
- A user ranked below 100 gets `rank: 0`, with their real `points`.
- A user with no points in the game, or who does not exist, gets
  `rank: 0` and `points: 0`. This is not a `404`.
- `user` is `null` when `externalUserId` is not sent.

### Cohorts (`cohort`)
- A user action with `cohorts` sets the user's current cohorts in the game.
  Each later points award is tagged with the cohorts the user had at that
  moment (or with the award request's own `cohorts`).
- With `cohort`, only awards tagged with it count. `rank`, `totalUsers` and
  `user.rank` are all within the cohort.
- A user who moves to another cohort keeps their earlier points in the old
  cohort's leaderboard.
- Points awarded before cohorts were introduced have no tags, so they do not
  appear on cohort leaderboards.
- `cohort` is echoed in the response (`null` when not sent).

### Success (200)
Returns:
- `externalGameId`
- `totalUsers`: number of ranked users in the game (for pagination)
- `limit`, `offset`: the page that was returned
- `entries`: list of `rank`, `externalUserId`, `points`, `timesAwarded`
- `user`: the requested user's row in the same shape, or `null`
- `cohort`: the cohort filter that was applied, or `null`

### Freshness
When the server's response cache is enabled, a leaderboard is cached and
recomputed when it expires (`RESPONSE_CACHE_LEADERBOARD_TTL_SECONDS`,
6 hours by default), not after every award. Points awarded since then,
including the requested user's in `user`, may not show yet. Changes to the
game or its tasks are reflected immediately. For a user's current total,
use the user points endpoints.

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: no game found with the provided `gameId`
- `422`: malformed `gameId`, `limit`/`offset` out of range, or invalid `cohort`
- `500`: retrieval failure

<sub>**Id_endpoint:** `get_leaderboard_by_gameId`</sub>
"""  # noqa


@router.get(
    "/{gameId}/leaderboard",
    response_model=GameLeaderboard,
    summary=summary_get_leaderboard_by_gameId,
    description=description_get_leaderboard_by_gameId,
    responses=responses_get_leaderboard_by_gameId,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_leaderboard_by_gameId(
    gameId: UUID,
    limit: int = Query(10, ge=1, le=100, description="Page size (1-100)."),
    offset: int = Query(0, ge=0, description="Number of ranked users to skip."),
    externalUserId: Optional[str] = Query(
        None, description="Also return this user's rank in `user`."
    ),
    cohort: Optional[str] = Query(
        None,
        pattern=COHORT_PATTERN,
        description="Rank only points earned while the user was in this cohort.",
    ),
    service: UserPointsService = Depends(Provide[Container.user_points_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve a page of a game's leaderboard.

    Args:
        gameId (UUID): The ID of the game.
        limit (int): Page size.
        offset (int): Number of ranked users to skip.
        externalUserId (Optional[str]): User whose rank to include.
        cohort (Optional[str]): Cohort tag to rank within.
        service (UserPointsService): Injected UserPointsService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        GameLeaderboard: Ranked users for the requested page.
    """
    auth = audit.auth
    await audit.info(
        "Leaderboard retrieval by game ID",
        {
            "gameId": str(gameId),
            "limit": limit,
            "offset": offset,
            "externalUserId": externalUserId,
            "cohort": cohort,
        },
    )
    return await service.get_leaderboard_by_gameId(
        gameId,
        limit=limit,
        offset=offset,
        externalUserId=externalUserId,
        cohort=cohort,
        **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin),
    )


summary_get_task_leaderboard = "Retrieve Task Leaderboard"
response_example_get_task_leaderboard = {
    "externalGameId": "game-readme-001",
    "externalTaskId": "task-login",
    "totalUsers": 2,
    "limit": 10,
    "offset": 0,
    "entries": [
        {"rank": 1, "externalUserId": "user-123", "points": 120, "timesAwarded": 6},
        {"rank": 2, "externalUserId": "user-456", "points": 40, "timesAwarded": 2},
    ],
    "cohort": "school-12",
    "user": {
        "rank": 0,
        "externalUserId": "user-789",
        "points": 0,
        "timesAwarded": 0,
    },
}

responses_get_task_leaderboard = {
    **responses_get_leaderboard_by_gameId,
    200: {
        "description": "Task leaderboard page retrieved successfully",
        "content": {
            "application/json": {"example": response_example_get_task_leaderboard}
        },
    },
    404: {
        "description": "Game not found, or task not found in the game",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Task not found with externalTaskId: task-login for gameId: 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
}

description_get_task_leaderboard = """
Returns users ranked by their points within one task of a game.

Same ranking, paging and `user` rules as `GET /games/{gameId}/leaderboard`,
but only points awarded in this task count.

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalTaskId` (`string`, required): External task identifier.

### Query Parameters
- `limit` (`int`, optional, default `10`, `1`-`100`): Page size.
- `offset` (`int`, optional, default `0`): Number of ranked users to skip.
- `externalUserId` (`string`, optional): Also return this user's rank in `user`.
- `cohort` (`string`, optional): Rank within one cohort. Only points earned
  while the user was in that cohort count (see "Cohorts" below).

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Ranking
- Users are ordered by points in this task, highest first.
- Tied users share a rank and the next rank is skipped (`1, 2, 2, 4`).
- Ranks are global, so they stay correct on later pages.
- Users with no points in the task are not listed. A task with no points
  returns an empty `entries` list, not `404`.

### Requested user (`externalUserId`)
- `user.rank` is the user's overall rank in the task (not affected by
  `limit`/`offset`) if it is `100` or better, including ties at 100.
- A user ranked below 100 gets `rank: 0`, with their real `points`.
- A user with no points in the task, or who does not exist, gets
  `rank: 0` and `points: 0`. This is not a `404`.
- `user` is `null` when `externalUserId` is not sent.

### Cohorts (`cohort`)
- A user action with `cohorts` sets the user's current cohorts in the game.
  Each later points award is tagged with the cohorts the user had at that
  moment (or with the award request's own `cohorts`).
- With `cohort`, only awards tagged with it count. `rank`, `totalUsers` and
  `user.rank` are all within the cohort.
- A user who moves to another cohort keeps their earlier points in the old
  cohort's leaderboard.
- Points awarded before cohorts were introduced have no tags, so they do not
  appear on cohort leaderboards.
- `cohort` is echoed in the response (`null` when not sent).

### Success (200)
Returns:
- `externalGameId`, `externalTaskId`
- `totalUsers`: number of ranked users in the task (for pagination)
- `limit`, `offset`: the page that was returned
- `entries`: list of `rank`, `externalUserId`, `points`, `timesAwarded`
- `user`: the requested user's row in the same shape, or `null`
- `cohort`: the cohort filter that was applied, or `null`

### Freshness
When the server's response cache is enabled, a leaderboard is cached and
recomputed when it expires (`RESPONSE_CACHE_LEADERBOARD_TTL_SECONDS`,
6 hours by default), not after every award. Points awarded since then,
including the requested user's in `user`, may not show yet. Changes to the
game or its tasks are reflected immediately. For a user's current total,
use the user points endpoints.

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: game not found, or task not found in the game
- `422`: malformed `gameId`, `limit`/`offset` out of range, or invalid `cohort`
- `500`: retrieval failure

<sub>**Id_endpoint:** `get_task_leaderboard`</sub>
"""  # noqa


@router.get(
    "/{gameId}/tasks/{externalTaskId}/leaderboard",
    response_model=TaskLeaderboard,
    summary=summary_get_task_leaderboard,
    description=description_get_task_leaderboard,
    responses=responses_get_task_leaderboard,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_task_leaderboard(
    gameId: UUID,
    externalTaskId: str,
    limit: int = Query(10, ge=1, le=100, description="Page size (1-100)."),
    offset: int = Query(0, ge=0, description="Number of ranked users to skip."),
    externalUserId: Optional[str] = Query(
        None, description="Also return this user's rank in `user`."
    ),
    cohort: Optional[str] = Query(
        None,
        pattern=COHORT_PATTERN,
        description="Rank only points earned while the user was in this cohort.",
    ),
    service: UserPointsService = Depends(Provide[Container.user_points_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve a page of one task's leaderboard.

    Args:
        gameId (UUID): The ID of the game.
        externalTaskId (str): The external ID of the task.
        limit (int): Page size.
        offset (int): Number of ranked users to skip.
        externalUserId (Optional[str]): User whose rank to include.
        cohort (Optional[str]): Cohort tag to rank within.
        service (UserPointsService): Injected UserPointsService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        TaskLeaderboard: Ranked users for the requested page.
    """
    auth = audit.auth
    await audit.info(
        "Leaderboard retrieval by task",
        {
            "gameId": str(gameId),
            "externalTaskId": externalTaskId,
            "limit": limit,
            "offset": offset,
            "externalUserId": externalUserId,
            "cohort": cohort,
        },
    )
    return await service.get_task_leaderboard(
        gameId,
        externalTaskId,
        limit=limit,
        offset=offset,
        externalUserId=externalUserId,
        cohort=cohort,
        **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin),
    )


summary_get_points_of_user_in_game = "Retrieve User Points in Game"
response_example_get_points_of_user_in_game = [
    {
        "externalUserId": "user-123",
        "points": 120,
        "timesAwarded": 6,
    }
]

responses_get_points_of_user_in_game = {
    200: {
        "description": "User points in game retrieved successfully",
        "content": {
            "application/json": {"example": response_example_get_points_of_user_in_game}
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Game not found for the provided id",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Game not found by gameId: 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path parameters",
    },
    500: {
        "description": "Internal server error while retrieving user points in game",
    },
}

description_get_points_of_user_in_game = """
Returns point totals for one user within one game.

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalUserId` (`string`, required): External user identifier.

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Success (200)
Returns a list of point aggregates for the specified user in the game:
- `externalUserId`
- `points`
- `timesAwarded`

A user with no points in the game, a user who does not exist, and a game
with no tasks all return an empty list (`[]`), not `404`.

### Freshness
When the server's response cache is enabled the result is cached, but any
points awarded or action posted for the user, and any change to the game or
its tasks, refreshes it: the next request reflects the write.

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: game not found
- `422`: malformed path parameters
- `500`: retrieval failure

<sub>**Id_endpoint:** `get_points_of_user_in_game`</sub>
"""  # noqa


@router.get(
    "/{gameId}/users/{externalUserId}/points",
    response_model=List[PointsAssignedToUser],
    summary=summary_get_points_of_user_in_game,
    description=description_get_points_of_user_in_game,
    responses=responses_get_points_of_user_in_game,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_points_of_user_in_game(
    gameId: UUID,
    externalUserId: str,
    service: UserPointsService = Depends(Provide[Container.user_points_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve points of a user in a specific game.

    Args:
        gameId (UUID): The ID of the game.
        externalUserId (str): The external user ID.
        service (UserPointsService): Injected UserPointsService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        List[PointsAssignedToUser]: The points details of the user in the
          specified game.
    """
    auth = audit.auth
    await audit.info(
        "User points retrieval by game ID",
        {"gameId": str(gameId), "externalUserId": externalUserId},
    )
    return await service.get_points_of_user_in_game(
        gameId,
        externalUserId,
        **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin),
    )


summary_get_user_activity = "Retrieve User Activity (Active Days and Streaks)"
response_example_get_user_activity = {
    "externalGameId": "game-readme-001",
    "externalUserId": "user_123",
    "timezone": "Asia/Kolkata",
    "basis": "awards",
    "from": "2026-11-01",
    "to": "2026-11-30",
    "activeDays": [
        {"date": "2026-11-02", "points": 30, "awards": 3},
        {"date": "2026-11-03", "points": 10, "awards": 1},
        {"date": "2026-11-27", "points": 20, "awards": 2},
        {"date": "2026-11-28", "points": 10, "awards": 1},
    ],
    "streaks": {
        "current": 2,
        "longest": 2,
        "runs": [
            {"start": "2026-11-02", "end": "2026-11-03", "length": 2},
            {"start": "2026-11-27", "end": "2026-11-28", "length": 2},
        ],
    },
}

responses_get_user_activity = {
    200: {
        "description": "Activity calendar retrieved successfully",
        "content": {
            "application/json": {"example": response_example_get_user_activity}
        },
    },
    401: {"description": "Unauthorized: missing/invalid credentials"},
    403: {"description": "Forbidden: invalid API key or no access to this game"},
    404: {"description": "Game not found for the provided id"},
    422: {
        "description": (
            "Malformed dates, 'from' after 'to', or a period longer than 45 days"
        ),
    },
    500: {"description": "Internal server error while retrieving activity"},
}

description_get_user_activity = """
Returns a user's **activity calendar** in a game: the days they were active,
and their streaks of consecutive active days.

This is a day-by-day summary, not an event feed (recorded actions are at
`/users/{externalUserId}/actions`).

### What counts as an active day
- A day on which the user received **at least one points award** in this game
  (`basis: "awards"`). Recording an action without an award does not count.
- Days are **local days in the game's timezone** (`timezone` in the response;
  set on the game, default UTC).

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalUserId` (`string`, required): External user identifier.

### Query Parameters
- `from` (`date`, optional): First day of the period, `YYYY-MM-DD`.
  Defaults to 29 days before `to` (a 30-day period).
- `to` (`date`, optional): Last day of the period. Defaults to today in the
  game's timezone.
- The period can span at most 45 days (`to - from + 1 <= 45`).

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Success (200)
- `activeDays`: one entry per active day in the period, oldest first, with
  the day's `points` and number of `awards`.
- `streaks.current`: consecutive active days ending **today**, or yesterday
  if there is no award yet today. It is counted over all history, so it can
  be longer than the period, and it does not depend on `from`/`to`.
- Day counting uses the same rule as GREENCROWD's streak bonus, but this
  endpoint counts awards on **all** of the game's tasks, while GREENCROWD
  counts only awards on tasks that use GREENCROWD. The two streaks match
  when every task in the game uses GREENCROWD.
- `streaks.runs`: runs of consecutive active days within the period, oldest
  first. A run that continues past either edge of the period is cut at the
  edge.
- `streaks.longest`: the longest run within the period.
- An unknown user, or one with no awards, gets an empty calendar and zero
  streaks (not `404`).

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or no access to this game
- `404`: game not found
- `422`: malformed dates, `from` after `to`, or a period longer than 45 days
- `500`: retrieval failure

<sub>**Id_endpoint:** `get_user_activity`</sub>
"""  # noqa


@router.get(
    "/{gameId}/users/{externalUserId}/activity",
    response_model=UserActivity,
    summary=summary_get_user_activity,
    description=description_get_user_activity,
    responses=responses_get_user_activity,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_user_activity(
    gameId: UUID,
    externalUserId: str,
    from_: Optional[date] = Query(
        None, alias="from", description="First day of the period (YYYY-MM-DD)."
    ),
    to: Optional[date] = Query(
        None, description="Last day of the period (YYYY-MM-DD); default today."
    ),
    service: UserPointsService = Depends(Provide[Container.user_points_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve a user's active days and streaks in a game.

    Args:
        gameId (UUID): The ID of the game.
        externalUserId (str): The external user ID.
        from_ (Optional[date]): First day of the period (``from`` in the URL).
        to (Optional[date]): Last day of the period.
        service (UserPointsService): Injected UserPointsService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        UserActivity: The user's activity calendar for the period.
    """
    auth = audit.auth
    await audit.info(
        "User activity retrieval",
        {
            "gameId": str(gameId),
            "externalUserId": externalUserId,
            "from": str(from_) if from_ else None,
            "to": str(to) if to else None,
        },
    )
    return await service.get_user_activity(
        gameId,
        externalUserId,
        from_date=from_,
        to_date=to,
        **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin),
    )


summary_get_points_simulated_of_user_in_game = "Retrieve Simulated User Points in Game"
response_example_get_points_simulated_of_user_in_game = {
    "simulationHash": "8e9fc0f2ef79ed3fca6053a5932f7a6d8f3f3f77b2437d2b7d8ea59e21a4fd4e",
    "tasks": [
        {
            "externalUserId": "user-123",
            "externalTaskId": "task-login",
            "userGroup": "control",
            "dimensions": [
                {"name": "engagement", "value": 0.74},
                {"name": "consistency", "value": 0.61},
            ],
            "totalSimulatedPoints": 42,
            "expirationDate": "2026-02-11T00:00:00Z",
        },
        {
            "externalUserId": "user-123",
            "externalTaskId": "task-share",
            "userGroup": "control",
            "dimensions": [
                {"name": "engagement", "value": 0.55},
                {"name": "consistency", "value": 0.48},
            ],
            "totalSimulatedPoints": 18,
            "expirationDate": "2026-02-11T00:00:00Z",
        },
    ],
}

responses_get_points_simulated_of_user_in_game = {
    200: {
        "description": "Simulated points retrieved successfully",
        "content": {
            "application/json": {
                "example": response_example_get_points_simulated_of_user_in_game
            }
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid/expired bearer token",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: token user is not allowed to access the requested external user id",
        "content": {
            "application/json": {
                "example": {"detail": "You are not authorized to access this resource."}
            }
        },
    },
    404: {
        "description": "Game or user context not found for simulation",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Could not find simulation context for gameId = 4ce32be2-77f6-4ffc-8e07-78dc220f0521 and externalUserId = user-123"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path parameters",
    },
    500: {
        "description": "Internal server error (for example: missing SECRET_KEY or simulation processing failure)",
    },
}

description_get_points_simulated_of_user_in_game = """
Returns simulated points for a user in a game, including per-task simulation breakdown.

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalUserId` (`string`, required): External user identifier to simulate.

### Authentication
- Requires OAuth2 bearer token (`Authorization: Bearer <access_token>`).
- API key authentication is not accepted on this endpoint.

### Access Control
- Caller can only access simulations allowed by token/user matching rules.
- Unauthorized cross-user access returns `403`.

### Success (200)
Returns:
- `simulationHash`: deterministic hash for simulation payload integrity
- `tasks`: list of simulated task point objects (`dimensions`, `userGroup`, `totalSimulatedPoints`, `expirationDate`)

### Error Cases
- `401`: missing/invalid/expired bearer token
- `403`: not authorized for requested `externalUserId`
- `404`: simulation context not found
- `422`: malformed path parameters
- `500`: missing environment configuration (`SECRET_KEY`) or simulation failure

<sub>**Id_endpoint:** `get_points_simulated_of_user_in_game`</sub>
"""  # noqa


@router.get(
    "/{gameId}/users/{externalUserId}/points/simulated",
    response_model=SimulatedPointsAssignedToUser,
    summary=summary_get_points_simulated_of_user_in_game,
    description=description_get_points_simulated_of_user_in_game,
    responses=responses_get_points_simulated_of_user_in_game,
    dependencies=[Depends(auth_oauth2)],
)
@inject
async def get_points_simulated_of_user_in_game(
    gameId: UUID,
    externalUserId: str,
    service: UserPointsService = Depends(Provide[Container.user_points_service]),
    service_user: UserService = Depends(Provide[Container.user_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve simulated points of a user in a specific game.

    Args:
        gameId (UUID): The ID of the game.
        externalUserId (str): The external user ID.
        service (UserPointsService): Injected UserPointsService dependency.
        service_user (UserService): Injected UserService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        List[SimulatedPointsAssignedToUser]: The simulated points details of the user in the specified game.
    """
    if not configs.SECRET_KEY:
        raise InternalServerError(
            detail="SECRET_KEY is not set. Please set the SECRET_KEY in the environment variables"
        )
    auth = audit.auth
    if auth.oauth_user_id != externalUserId:
        raise ForbiddenError(detail="You are not authorized to access this resource.")

    tasks_simulated, externalGameId = (
        await service.get_points_simulated_of_user_in_game(
            gameId,
            externalUserId,
            oauth_user_id=auth.oauth_user_id,
            assign_control_group=True,
            is_admin=auth.is_admin,
            enforce_scope=True,
        )
    )

    simulationHash = calculate_hash_simulated_strategy(
        tasks_simulated,
        externalGameId,
        externalUserId,
    )

    response = SimulatedPointsAssignedToUser(
        simulationHash=simulationHash, tasks=tasks_simulated
    )

    await audit.info(
        "Simulated user points retrieval by game ID",
        {
            "gameId": str(gameId),
            "externalUserId": externalUserId,
            "response": response.model_dump(),
        },
    )
    return response


summary_user_action = "User Action"
request_example_user_action = {
    "typeAction": "TASK_COMPLETED",
    "data": {
        "durationSeconds": 84,
        "source": "mobile-app",
        "metadata": {"difficulty": "easy"},
    },
    "description": "User completed the task from mobile flow",
    "externalUserId": "user-123",
}

response_example_user_action = {
    "id": "8f9d6bc1-2b5f-4cab-b82a-2b0e61bf7c1d",
    "created_at": "2026-02-10T12:40:00Z",
    "updated_at": "2026-02-10T12:40:00Z",
    "typeAction": "TASK_COMPLETED",
    "data": {
        "durationSeconds": 84,
        "source": "mobile-app",
        "metadata": {"difficulty": "easy"},
    },
    "description": "User completed the task from mobile flow",
    "externalUserId": "user-123",
    "message": "Successfully created",
}

responses_user_action = {
    200: {
        "description": "User action registered successfully",
        "content": {"application/json": {"example": response_example_user_action}},
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Game, task, or user not found for the provided identifiers",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Could not find task with externalTaskId = task-login for gameId = 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path/body payload",
        "content": {
            "application/json": {
                "example": {
                    "detail": [
                        {
                            "loc": ["body", "typeAction"],
                            "msg": "field required",
                            "type": "value_error.missing",
                        }
                    ]
                }
            }
        },
    },
    429: {
        "description": "Too many requests: rate limit exceeded",
        "content": {
            "application/json": {
                "example": {
                    "detail": "API key rate limit exceeded for sensitive task operations."
                }
            }
        },
    },
    500: {
        "description": "Internal server error while registering user action",
        "content": {
            "application/json": {
                "example": {"detail": "Error when registering user action in task"}
            }
        },
    },
}

description_user_action = """
Registers an explicit user action event for a task inside a game.

Recording an action does **not** award points; use
`POST /games/{gameId}/tasks/{externalTaskId}/points` to score an event.

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalTaskId` (`string`, required): External task identifier.

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Request Body
- `typeAction` (`string`, required): Business action/event type.
- `data` (`object`, required): Structured event payload, stored for audit, analytics and exports.
- `description` (`string`, required): Human-readable action description.
- `externalUserId` (`string`, required): External user identifier that triggered the action.
- `cohorts` (`string[]`, optional): Replaces the user's current cohorts in this
  game; later points awards are tagged with them for cohort leaderboards. Omit
  to leave them unchanged, send `[]` to clear them. Up to 10 tags of 1-64
  characters from `A-Z a-z 0-9 _ . : -`.

### Success (200)
Returns the persisted action event:
- action metadata (`id`, timestamps)
- action payload (`typeAction`, `data`, `description`)
- actor (`externalUserId`)
- operation message (`message`)

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: game/task/user not found
- `422`: malformed path parameters or invalid request payload
- `500`: action registration failure

<sub>**Id_endpoint:** `user_action_in_task`</sub>
"""  # noqa


@router.post(
    "/{gameId}/tasks/{externalTaskId}/action",
    response_model=ResponseAddActionDidByUserInTask,
    summary=summary_user_action,
    description=description_user_action,
    responses=responses_user_action,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def user_action_in_task(
    gameId: UUID,
    externalTaskId: str,
    request: Request,
    schema: AddActionDidByUserInTask = Body(
        ..., examples=[request_example_user_action]
    ),
    service: UserActionsService = Depends(Provide[Container.user_actions_service]),
    abuse_prevention_service: AbusePreventionService = Depends(
        Provide[Container.abuse_prevention_service]
    ),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Register a user action in a task within a game. This endpoint is used to
    assign points to a user for a specific task within a game, when the game
    requires it.

    Args:
        gameId (UUID): The ID of the game.
        externalTaskId (str): The external task ID.
        schema (AddActionDidByUserInTask): The schema for adding an action.
        service (UserActionsService): Injected UserActionsService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        ResponseAddActionDidByUserInTask: The details of the action added.
    """
    auth = audit.auth
    correlation_id = _resolve_correlation_id(request)
    if isinstance(schema.data, dict):
        schema.data.setdefault("correlationId", correlation_id)
        schema.data.setdefault("eventId", correlation_id)
    client_ip = abuse_prevention_service.extract_client_ip(request)
    await abuse_prevention_service.enforce_task_mutation_limits(
        api_key=auth.api_key,
        client_ip=client_ip,
        external_user_id=schema.externalUserId,
    )
    await audit.info(
        "User action in task",
        {
            "gameId": str(gameId),
            "externalTaskId": externalTaskId,
            "correlationId": correlation_id,
            "body": schema.model_dump(),
        },
    )
    try:
        return await service.user_add_action_in_task(
            gameId,
            externalTaskId,
            schema,
            auth.api_key,
            oauth_user_id=auth.oauth_user_id,
            is_admin=auth.is_admin,
            enforce_scope=True,
        )
    except Exception as exc:
        mapped_exc = _map_write_exception(exc, correlation_id=correlation_id)
        error_payload = {
            "gameId": str(gameId),
            "externalTaskId": externalTaskId,
            "externalUserId": schema.externalUserId,
            "correlationId": correlation_id,
            "errorType": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        logger.exception(
            "user_action_in_task failed",
            extra=error_payload,
        )
        await audit.error("User action in task failed", error_payload)
        raise mapped_exc


summary_assign_points_to_user = "Assign Points to User"
request_example_assign_points_to_user = {
    "externalUserId": "user-123",
    "data": {
        "event": "task_completed",
        "source": "mobile-app",
    },
    "isSimulated": False,
}

response_example_assign_points_to_user = {
    "points": 20,
    "caseName": "variable_basic_points",
    "isACreatedUser": True,
    "gameId": "4ce32be2-77f6-4ffc-8e07-78dc220f0520",
    "externalTaskId": "task-login",
    "created_at": "2026-02-10T12:30:00Z",
}

responses_assign_points_to_user = {
    200: {
        "description": "Points assigned successfully",
        "content": {
            "application/json": {"example": response_example_assign_points_to_user}
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Game or task not found for provided identifiers",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Could not find task with externalTaskId = task-login for gameId = 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path/body payload",
        "content": {
            "application/json": {
                "example": {
                    "detail": [
                        {
                            "loc": ["path", "gameId"],
                            "msg": "value is not a valid uuid",
                            "type": "type_error.uuid",
                        }
                    ]
                }
            }
        },
    },
    429: {
        "description": "Too many requests: rate limit exceeded",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Daily API key quota exceeded for sensitive task operations."
                }
            }
        },
    },
    500: {
        "description": "Internal server error while assigning points",
        "content": {
            "application/json": {
                "example": {"detail": "Error when assigning points to user in task"}
            }
        },
    },
}

description_assign_points_to_user = """
Assigns points to one user for a specific task in a game.

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalTaskId` (`string`, required): External task identifier.

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Request Body
- `externalUserId` (`string`, required): External user identifier that will receive points.
- `data` (`object`, optional): Task/event payload used by scoring strategy.
- `isSimulated` (`boolean`, optional): If `true`, executes simulation logic when supported.
- `cohorts` (`string[]`, optional): Cohort tags to record on this award. When
  omitted, the award gets the user's current cohorts in the game (set by user
  actions); `[]` records none. Same tag rules as on actions.

### Success (200)
Returns the assigned points event:
- `points`
- `caseName`
- `isACreatedUser`
- `gameId`
- `externalTaskId`
- `created_at`

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: game/task association not found
- `422`: malformed path parameters or invalid request payload
- `500`: assignment failure

<sub>**Id_endpoint:** `assign_points_to_user`</sub>
"""  # noqa


@router.post(
    "/{gameId}/tasks/{externalTaskId}/points",
    response_model=AssignedPointsToExternalUserId,
    summary=summary_assign_points_to_user,
    description=description_assign_points_to_user,
    responses=responses_assign_points_to_user,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def assign_points_to_user(
    gameId: UUID,
    externalTaskId: str,
    request: Request,
    schema: AsignPointsToExternalUserId = Body(
        ..., examples=[request_example_assign_points_to_user]
    ),
    service: UserPointsService = Depends(Provide[Container.user_points_service]),
    abuse_prevention_service: AbusePreventionService = Depends(
        Provide[Container.abuse_prevention_service]
    ),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Assign points to a user for a specific task in a game.

    Args:
        gameId (UUID): The ID of the game.
        externalTaskId (str): The external task ID.
        schema (AsignPointsToExternalUserId): The schema for assigning points.
        service (UserPointsService): Injected UserPointsService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        AssignedPointsToExternalUserId: The details of the points assigned.
    """
    auth = audit.auth
    correlation_id = _resolve_correlation_id(request)
    idempotency_key = _resolve_idempotency_key(request)
    if schema.data is None:
        schema.data = {}
    if isinstance(schema.data, dict):
        schema.data.setdefault("correlationId", correlation_id)
        if idempotency_key:
            schema.data.setdefault("eventId", idempotency_key)
    client_ip = abuse_prevention_service.extract_client_ip(request)
    await abuse_prevention_service.enforce_task_mutation_limits(
        api_key=auth.api_key,
        client_ip=client_ip,
        external_user_id=schema.externalUserId,
    )
    await audit.info(
        "Points assignment to user",
        {
            "gameId": str(gameId),
            "externalTaskId": externalTaskId,
            "correlationId": correlation_id,
            "idempotencyKey": idempotency_key,
            "body": schema.model_dump(),
        },
    )
    isSimulated = schema.isSimulated if hasattr(schema, "isSimulated") else False
    try:
        return await service.assign_points_to_user(
            gameId,
            externalTaskId,
            schema,
            isSimulated,
            auth.api_key,
            oauth_user_id=auth.oauth_user_id,
            is_admin=auth.is_admin,
            enforce_scope=True,
        )
    except Exception as exc:
        mapped_exc = _map_write_exception(exc, correlation_id=correlation_id)
        error_payload = {
            "gameId": str(gameId),
            "externalTaskId": externalTaskId,
            "externalUserId": schema.externalUserId,
            "correlationId": correlation_id,
            "idempotencyKey": idempotency_key,
            "errorType": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        logger.exception(
            "assign_points_to_user failed",
            extra=error_payload,
        )
        await audit.error("Points assignment to user failed", error_payload)
        raise mapped_exc


summary_get_points_by_task_id = "Retrieve Points by Task ID"
response_example_get_points_by_task_id = [
    {
        "externalUserId": "user-123",
        "points": 120,
        "timesAwarded": 6,
    },
    {
        "externalUserId": "user-456",
        "points": 40,
        "timesAwarded": 2,
    },
]

responses_get_points_by_task_id = {
    200: {
        "description": "Points by task retrieved successfully",
        "content": {
            "application/json": {"example": response_example_get_points_by_task_id}
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Game/task not found for provided identifiers",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Could not find task with externalTaskId = task-login for gameId = 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path parameters",
    },
    500: {
        "description": "Internal server error while retrieving task points",
    },
}

description_get_points_by_task_id = """
Returns point totals for all users in a specific task.

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalTaskId` (`string`, required): External task identifier.

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Success (200)
Returns a list of per-user aggregates:
- `externalUserId`
- `points`
- `timesAwarded`

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: game/task association not found
- `422`: malformed path parameters
- `500`: retrieval failure

### Freshness
When the server's response cache is enabled, this result is cached for up
to `RESPONSE_CACHE_AGGREGATE_TTL_SECONDS` (10 minutes by default): points
awarded in the meantime may not show yet. Changes to the game or its tasks
are reflected immediately.

<sub>**Id_endpoint:** `get_points_by_task_id`</sub>
"""  # noqa


@router.get(
    "/{gameId}/tasks/{externalTaskId}/points",
    response_model=List[PointsAssignedToUser],
    summary=summary_get_points_by_task_id,
    description=description_get_points_by_task_id,
    responses=responses_get_points_by_task_id,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_points_by_task_id(
    gameId: UUID,
    externalTaskId: str,
    service: TaskService = Depends(Provide[Container.task_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve points by task ID.

    Args:
        gameId (UUID): The ID of the game.
        externalTaskId (str): The external task ID.
        service (TaskService): Injected TaskService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        List[PointsAssignedToUser]: The points details for the specified task.
    """
    auth = audit.auth
    await audit.info(
        "Points retrieval by task ID",
        {"gameId": str(gameId), "externalTaskId": externalTaskId},
    )
    return await service.get_points_by_task_id(
        gameId,
        externalTaskId,
        **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin),
    )


summary_get_points_of_user_by_task_id = "Retrieve User Points by Task ID"
response_example_get_points_of_user_by_task_id = {
    "externalUserId": "user-123",
    "points": 120,
    "timesAwarded": 6,
}

responses_get_points_of_user_by_task_id = {
    200: {
        "description": "User points by task retrieved successfully",
        "content": {
            "application/json": {
                "example": response_example_get_points_of_user_by_task_id
            }
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Task not found in the game",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Task not found with externalTaskId: task-login for gameId: 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path parameters",
        "content": {
            "application/json": {
                "example": {
                    "detail": [
                        {
                            "loc": ["path", "gameId"],
                            "msg": "value is not a valid uuid",
                            "type": "type_error.uuid",
                        }
                    ]
                }
            }
        },
    },
    500: {
        "description": "Internal server error while retrieving user task points",
        "content": {
            "application/json": {
                "example": {"detail": "Error when retrieving points for user in task"}
            }
        },
    },
}

description_get_points_of_user_by_task_id = """
Returns the aggregated points of one user in one task within a game.

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalTaskId` (`string`, required): External task identifier.
- `externalUserId` (`string`, required): External user identifier.

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Success (200)
Returns:
- `externalUserId`
- `points` (total points awarded in this task)
- `timesAwarded` (number of scoring events for this user-task pair)

A user with no points in the task, or who does not exist, gets
`points: 0` and `timesAwarded: 0`, not `404`.

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: task not found in the game
- `422`: malformed path parameters
- `500`: retrieval failure

<sub>**Id_endpoint:** `get_points_of_user_by_task_id`</sub>
"""  # noqa


@router.get(
    "/{gameId}/tasks/{externalTaskId}/users/{externalUserId}/points",
    response_model=PointsAssignedToUser,
    summary=summary_get_points_of_user_by_task_id,
    description=description_get_points_of_user_by_task_id,
    responses=responses_get_points_of_user_by_task_id,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_points_of_user_by_task_id(
    gameId: UUID,
    externalTaskId: str,
    externalUserId: str,
    service: TaskService = Depends(Provide[Container.task_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve points of a user by task ID.

    Args:
        gameId (UUID): The ID of the game.
        externalTaskId (str): The external task ID.
        externalUserId (str): The external user ID.
        service (TaskService): Injected TaskService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        PointsAssignedToUser: The points details of the user for the specified
          task.
    """
    auth = audit.auth
    await audit.info(
        "User points retrieval by task ID",
        {
            "gameId": str(gameId),
            "externalTaskId": externalTaskId,
            "externalUserId": externalUserId,
        },
    )
    return await service.get_points_of_user_by_task_id(
        gameId,
        externalTaskId,
        externalUserId,
        **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin),
    )


summary_get_points_by_task_id_with_details = (
    "Retrieve Detailed Points by Task ID"  # noqa
)
response_example_get_points_by_task_id_with_details = [
    {
        "externalUserId": "user-123",
        "points": 120,
        "timesAwarded": 6,
        "pointsData": [
            {
                "points": 20,
                "caseName": "variable_basic_points",
                "data": {"event": "task_completed", "source": "mobile-app"},
                "description": "Awarded after completing task",
                "created_at": "2026-02-10T12:30:00Z",
            },
            {
                "points": 10,
                "caseName": "bonus_consistency",
                "data": {"streak": 3},
                "description": "Consistency bonus",
                "created_at": "2026-02-10T13:15:00Z",
            },
        ],
    },
    {
        "externalUserId": "user-456",
        "points": 40,
        "timesAwarded": 2,
        "pointsData": [
            {
                "points": 20,
                "caseName": "variable_basic_points",
                "data": {"event": "task_completed", "source": "web"},
                "description": "Awarded after completing task",
                "created_at": "2026-02-10T12:45:00Z",
            }
        ],
    },
]

responses_get_points_by_task_id_with_details = {
    200: {
        "description": "Detailed points by task retrieved successfully",
        "content": {
            "application/json": {
                "example": response_example_get_points_by_task_id_with_details
            }
        },
    },
    401: {
        "description": "Unauthorized: missing/invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Invalid authentication credentials"}
            }
        },
    },
    403: {
        "description": "Forbidden: invalid or inactive API key",
        "content": {
            "application/json": {
                "example": {"detail": "API key is invalid or does not exist."}
            }
        },
    },
    404: {
        "description": "Game/task combination not found",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Task not found with externalTaskId: task-login for gameId: 4ce32be2-77f6-4ffc-8e07-78dc220f0521"
                }
            }
        },
    },
    422: {
        "description": "Validation error in path parameters",
        "content": {
            "application/json": {
                "example": {
                    "detail": [
                        {
                            "loc": ["path", "gameId"],
                            "msg": "value is not a valid uuid",
                            "type": "type_error.uuid",
                        }
                    ]
                }
            }
        },
    },
    500: {
        "description": "Internal server error while retrieving detailed task points",
        "content": {
            "application/json": {
                "example": {"detail": "Error when retrieving detailed points by task"}
            }
        },
    },
}

description_get_points_by_task_id_with_details = """
Returns detailed scoring history for all users in a task, including aggregated totals and per-award entries.

### Path Parameters
- `gameId` (`UUID`, required): Internal game identifier.
- `externalTaskId` (`string`, required): External task identifier.

### Authentication
- Requires either `X-API-Key` or `Authorization: Bearer <access_token>`.

### Success (200)
Returns a list where each item represents one user in the task:
- `externalUserId`
- `points` (total accumulated points in the task)
- `timesAwarded` (number of award events)
- `pointsData[]` (detailed award events with `points`, `caseName`, `data`, `description`, `created_at`)

### Error Cases
- `401`: missing or invalid auth credentials
- `403`: API key rejected or inactive
- `404`: task not found for the provided game/task identifiers
- `422`: malformed path parameters
- `500`: retrieval failure

<sub>**Id_endpoint:** get_points_by_task_id_with_details</sub>
"""  # noqa


@router.get(
    "/{gameId}/tasks/{externalTaskId}/points/details",
    response_model=List[dict],  # WIP FIX
    summary=summary_get_points_by_task_id_with_details,
    description=description_get_points_by_task_id_with_details,
    responses=responses_get_points_by_task_id_with_details,
    dependencies=[Depends(auth_api_key_or_oauth2)],
)
@inject
async def get_points_by_task_id_with_details(
    gameId: UUID,
    externalTaskId: str,
    service: TaskService = Depends(Provide[Container.task_service]),
    audit: AuditLogger = Depends(audit_log("game")),
):
    """
    Retrieve detailed points by task ID.

    Args:
        gameId (UUID): The ID of the game.
        externalTaskId (str): The external task ID.
        service (TaskService): Injected TaskService dependency.
        audit (AuditLogger): Per-request audit logger bound to the auth context.

    Returns:
        List[dict]: Detailed points information for the specified task.
    """
    auth = audit.auth
    await audit.info(
        "Detailed points retrieval by task ID",
        {"gameId": str(gameId), "externalTaskId": externalTaskId},
    )
    return await service.get_points_by_task_id_with_details(
        gameId,
        externalTaskId,
        **_game_access_kwargs(auth.api_key, auth.oauth_user_id, auth.is_admin),
    )
