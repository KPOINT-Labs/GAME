"""cohort tables added

``usergamecohort`` holds each user's current cohorts per game (set by user
actions). ``userpointscohort`` snapshots those cohorts onto each points award
so leaderboards can rank within a cohort.

Revision ID: c3e1f7a9b2d4
Revises: a7c4e2b9d1f3
Create Date: 2026-11-19 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c3e1f7a9b2d4"
down_revision = "a7c4e2b9d1f3"
branch_labels = None
depends_on = None


def _base_columns():
    """Columns every table inherits from ``app.model.base_model.BaseModel``."""
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("apiKey_used", sa.String(), nullable=True),
        sa.Column("oauth_user_id", sa.String(), nullable=True),
    ]


def upgrade():
    op.create_table(
        "usergamecohort",
        *_base_columns(),
        sa.Column("userId", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gameId", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cohort", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["apiKey_used"], ["apikey.apiKey"]),
        sa.ForeignKeyConstraint(["oauth_user_id"], ["oauthusers.provider_user_id"]),
        sa.ForeignKeyConstraint(["userId"], ["users.id"]),
        sa.ForeignKeyConstraint(["gameId"], ["games.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "userId", "gameId", "cohort", name="uq_usergamecohort_user_game_cohort"
        ),
    )
    op.create_index(
        op.f("ix_usergamecohort_id"), "usergamecohort", ["id"], unique=False
    )
    op.create_index(
        "ix_usergamecohort_game_user",
        "usergamecohort",
        ["gameId", "userId"],
        unique=False,
    )

    op.create_table(
        "userpointscohort",
        *_base_columns(),
        sa.Column("userPointsId", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cohort", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["apiKey_used"], ["apikey.apiKey"]),
        sa.ForeignKeyConstraint(["oauth_user_id"], ["oauthusers.provider_user_id"]),
        sa.ForeignKeyConstraint(
            ["userPointsId"], ["userpoints.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "userPointsId", "cohort", name="uq_userpointscohort_points_cohort"
        ),
    )
    op.create_index(
        op.f("ix_userpointscohort_id"), "userpointscohort", ["id"], unique=False
    )
    op.create_index(
        "ix_userpointscohort_cohort_points",
        "userpointscohort",
        ["cohort", "userPointsId"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_userpointscohort_cohort_points", table_name="userpointscohort")
    op.drop_index(op.f("ix_userpointscohort_id"), table_name="userpointscohort")
    op.drop_table("userpointscohort")
    op.drop_index("ix_usergamecohort_game_user", table_name="usergamecohort")
    op.drop_index(op.f("ix_usergamecohort_id"), table_name="usergamecohort")
    op.drop_table("usergamecohort")
