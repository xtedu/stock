"""valuation table + fin indicators extra columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "valuation_daily",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), sa.ForeignKey("stocks.code"), nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("pe", sa.Numeric(10, 2)),
        sa.Column("pb", sa.Numeric(10, 2)),
        sa.UniqueConstraint("code", "trade_date"),
    )
    op.create_index("ix_valuation_daily_code_date", "valuation_daily",
                    ["code", "trade_date"])
    op.add_column("fin_indicators",
                  sa.Column("np_deducted", sa.Numeric(18, 2)))
    op.add_column("fin_indicators",
                  sa.Column("ocf_np_ratio", sa.Numeric(8, 2)))


def downgrade() -> None:
    op.drop_column("fin_indicators", "ocf_np_ratio")
    op.drop_column("fin_indicators", "np_deducted")
    op.drop_index("ix_valuation_daily_code_date", table_name="valuation_daily")
    op.drop_table("valuation_daily")
