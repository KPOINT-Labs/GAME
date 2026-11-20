"""games.timezone column added

Reference timezone for a game's calendar logic (streak days, time-of-day
slots). IANA name, NOT NULL with server default ``UTC``: existing games
become UTC, which is what all calendar logic used before, so their scoring
is unchanged. Adding a column with a constant default is a metadata-only
change in PostgreSQL 11+, so this does not rewrite or lock the table for
long.

Revision ID: d4f1a9c2e7b3
Revises: b8d2e6f4a1c7
Create Date: 2026-11-20 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4f1a9c2e7b3"
down_revision = "b8d2e6f4a1c7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "games",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default="UTC",
        ),
    )


def downgrade():
    op.drop_column("games", "timezone")
