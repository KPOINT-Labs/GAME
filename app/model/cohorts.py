"""Cohort tags for users and for the points they earn.

Cohorts let a game rank users within groups (a school, a class, a region)
on its leaderboards. They flow in two steps:

* :class:`UserGameCohort` holds a user's *current* cohorts in one game. A user
  action that carries ``cohorts`` replaces this set.
* :class:`UserPointsCohort` snapshots those cohorts onto each points award at
  the moment it is written. Cohort leaderboards rank from these rows, so an
  award stays in the cohort the user had when they earned it, even if the
  user moves to another cohort later.
"""

from datetime import datetime

from pydantic import ConfigDict
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlmodel import Column, Field, ForeignKey, String

from app.model.base_model import (
    BaseModel,
    audit_foreign_keys,
    created_at_field,
    updated_at_field,
)

# Shared limits for cohort tags; also enforced by the request schemas.
COHORT_MAX_LENGTH = 64
COHORT_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
MAX_COHORTS_PER_REQUEST = 10


class UserGameCohort(BaseModel, table=True):
    """
    A cohort the user currently belongs to within one game.

    Attributes:
        userId (str): The user.
        gameId (str): The game the membership applies to.
        cohort (str): The cohort tag.
    """

    __table_args__ = (
        *audit_foreign_keys(api_key=True, oauth_user=True),
        UniqueConstraint(
            "userId", "gameId", "cohort", name="uq_usergamecohort_user_game_cohort"
        ),
        Index("ix_usergamecohort_game_user", "gameId", "userId"),
    )

    userId: str = Field(
        sa_column=Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    )
    # Cascade so deleting a game (GameRepository.delete_game_by_id) also
    # removes its memberships instead of failing on this foreign key.
    gameId: str = Field(
        sa_column=Column(
            UUID(as_uuid=True),
            ForeignKey("games.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    cohort: str = Field(sa_column=Column(String(COHORT_MAX_LENGTH), nullable=False))

    # These tables were created with NOT NULL audit timestamps.
    created_at: datetime = created_at_field(nullable=False)
    updated_at: datetime = updated_at_field(nullable=False)

    model_config = ConfigDict(from_attributes=True)


class UserPointsCohort(BaseModel, table=True):
    """
    A cohort attached to one points award, copied when the award was written.

    Attributes:
        userPointsId (str): The points award (``userpoints`` row).
        cohort (str): The cohort tag.
    """

    __table_args__ = (
        *audit_foreign_keys(api_key=True, oauth_user=True),
        UniqueConstraint(
            "userPointsId", "cohort", name="uq_userpointscohort_points_cohort"
        ),
        # Serves the cohort leaderboard: filter by cohort, then join to the
        # points rows.
        Index("ix_userpointscohort_cohort_points", "cohort", "userPointsId"),
    )

    userPointsId: str = Field(
        sa_column=Column(
            UUID(as_uuid=True),
            ForeignKey("userpoints.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    cohort: str = Field(sa_column=Column(String(COHORT_MAX_LENGTH), nullable=False))

    # These tables were created with NOT NULL audit timestamps.
    created_at: datetime = created_at_field(nullable=False)
    updated_at: datetime = updated_at_field(nullable=False)

    model_config = ConfigDict(from_attributes=True)
