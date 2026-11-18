"""userpoints.breakdown column added

Stores a per-component explanation of each award's ``points`` (for example
the GREENCROWD dimensions DIM_BP, DIM_LBE, DIM_TD, DIM_PP and DIM_S).
Nullable: existing rows and strategies that do not provide a breakdown keep
NULL.

Revision ID: a7c4e2b9d1f3
Revises: f9d3a8c5b2e1
Create Date: 2026-11-18 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a7c4e2b9d1f3"
down_revision = "f9d3a8c5b2e1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "userpoints",
        sa.Column("breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade():
    op.drop_column("userpoints", "breakdown")
