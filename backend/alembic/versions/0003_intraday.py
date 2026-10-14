"""intraday spot snapshot table

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13

"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intraday_spot",
        sa.Column("code", sa.String(10), primary_key=True),
        sa.Column("price", sa.Numeric(10, 2)),
        sa.Column("pe", sa.Numeric(10, 2)),
        sa.Column("pb", sa.Numeric(10, 2)),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("intraday_spot")
