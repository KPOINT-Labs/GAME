"""userpoints task/user index also covers points (leaderboards)

Leaderboards sum ``points`` per user over a game's or task's awards. The
existing ``ix_user_points_task_user_created ("taskId", "userId",
created_at)`` finds those rows but every one still needed a table fetch for
``points``. The replacement has the same key columns plus ``INCLUDE
(points)``, so the leaderboard scan is index-only while the write-path
lookups it already served are unchanged. One index replaces the other, so
inserts do not maintain an extra index.

Both indexes are built and dropped ``CONCURRENTLY`` so writes to
``userpoints`` are not blocked; that cannot run inside a transaction, hence
the autocommit blocks. A failed concurrent build leaves an INVALID index
behind, so the migration rebuilds an invalid leftover and only drops the
old index once the new one is valid; it never leaves the table without one.

Revision ID: b8d2e6f4a1c7
Revises: c3e1f7a9b2d4
Create Date: 2026-11-19 16:00:00.000000

"""

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision = "b8d2e6f4a1c7"
down_revision = "c3e1f7a9b2d4"
branch_labels = None
depends_on = None

OLD = "ix_user_points_task_user_created"
NEW = "ix_user_points_task_user_created_points"
COLUMNS = ["taskId", "userId", "created_at"]


def _index_is_valid(name):
    """
    Return True/False for an existing index's ``indisvalid``, or None if absent.

    A failed ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index behind
    under its name. ``IF NOT EXISTS`` would then skip the rebuild on retry,
    so callers check validity explicitly instead of trusting the name.
    """
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


def _replace_index(build, build_name, drop_name):
    """
    Build ``build_name`` concurrently, then drop ``drop_name`` only once the
    new index is valid, so the table is never left without a usable index.
    """
    with op.get_context().autocommit_block():
        if context.is_offline_mode():
            build()
            op.drop_index(
                drop_name,
                table_name="userpoints",
                postgresql_concurrently=True,
                if_exists=True,
            )
            return
        if _index_is_valid(build_name) is False:
            # Leftover from an interrupted earlier run: rebuild it.
            op.drop_index(
                build_name,
                table_name="userpoints",
                postgresql_concurrently=True,
                if_exists=True,
            )
        build()
        if _index_is_valid(build_name) is not True:
            raise RuntimeError(
                f"Index {build_name} is missing or invalid after CREATE INDEX "
                f"CONCURRENTLY; keeping {drop_name}. Re-run the migration."
            )
        op.drop_index(
            drop_name,
            table_name="userpoints",
            postgresql_concurrently=True,
            if_exists=True,
        )


def upgrade():
    _replace_index(
        lambda: op.create_index(
            NEW,
            "userpoints",
            COLUMNS,
            unique=False,
            postgresql_include=["points"],
            postgresql_concurrently=True,
            if_not_exists=True,
        ),
        build_name=NEW,
        drop_name=OLD,
    )


def downgrade():
    _replace_index(
        lambda: op.create_index(
            OLD,
            "userpoints",
            COLUMNS,
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        ),
        build_name=OLD,
        drop_name=NEW,
    )
