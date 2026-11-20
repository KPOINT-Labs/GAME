"""userpoints ("userId", created_at) INCLUDE ("taskId", points) index

Serves ``GET /games/{gameId}/users/{externalUserId}/activity``: a user's
awards in a time range, grouped into local days, plus the current-streak
lookback. Without it Postgres found the user's rows through
``uq_user_points_user_task_idempotency`` and then fetched every row from the
table for ``created_at`` and ``points``. With it the reads are one
index-only range scan.

Built ``CONCURRENTLY`` so writes are not blocked (hence the autocommit
block). A failed concurrent build leaves an INVALID index behind, so an
invalid leftover is rebuilt and the migration fails loudly if the index is
not valid afterwards.

Revision ID: e6b3c8d1f4a2
Revises: d4f1a9c2e7b3
Create Date: 2026-11-20 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision = "e6b3c8d1f4a2"
down_revision = "d4f1a9c2e7b3"
branch_labels = None
depends_on = None

NAME = "ix_userpoints_user_created"


def _index_is_valid(name):
    """True/False for an existing index's ``indisvalid``, None if absent."""
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name"
            ),
            {"name": name},
        )
        .scalar()
    )


def _drop():
    op.drop_index(
        NAME, table_name="userpoints", postgresql_concurrently=True, if_exists=True
    )


def upgrade():
    with op.get_context().autocommit_block():
        if not context.is_offline_mode() and _index_is_valid(NAME) is False:
            _drop()  # leftover from an interrupted earlier run
        op.create_index(
            NAME,
            "userpoints",
            ["userId", "created_at"],
            unique=False,
            postgresql_include=["taskId", "points"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        if not context.is_offline_mode() and _index_is_valid(NAME) is not True:
            raise RuntimeError(
                f"Index {NAME} is missing or invalid after CREATE INDEX "
                "CONCURRENTLY. Re-run the migration."
            )


def downgrade():
    with op.get_context().autocommit_block():
        _drop()
