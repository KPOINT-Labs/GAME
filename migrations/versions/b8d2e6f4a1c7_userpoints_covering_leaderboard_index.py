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
the autocommit blocks.

Revision ID: b8d2e6f4a1c7
Revises: c3e1f7a9b2d4
Create Date: 2026-11-19 16:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b8d2e6f4a1c7"
down_revision = "c3e1f7a9b2d4"
branch_labels = None
depends_on = None

OLD = "ix_user_points_task_user_created"
NEW = "ix_user_points_task_user_created_points"


def upgrade():
    with op.get_context().autocommit_block():
        op.create_index(
            NEW,
            "userpoints",
            ["taskId", "userId", "created_at"],
            unique=False,
            postgresql_include=["points"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.drop_index(
            OLD,
            table_name="userpoints",
            postgresql_concurrently=True,
            if_exists=True,
        )


def downgrade():
    with op.get_context().autocommit_block():
        op.create_index(
            OLD,
            "userpoints",
            ["taskId", "userId", "created_at"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.drop_index(
            NEW,
            table_name="userpoints",
            postgresql_concurrently=True,
            if_exists=True,
        )
