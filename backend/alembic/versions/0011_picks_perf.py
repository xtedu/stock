"""index daily + picks snapshot

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-11

- index_daily：沪深300/中证500/中证1000 基准行情
- picks：每日选股落库 + 前向收益，构成策略评估闭环
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "index_daily",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("close", sa.Numeric(12, 2)),
        sa.Column("pct_chg", sa.Numeric(10, 4)),
        sa.UniqueConstraint("code", "trade_date"),
    )

    op.create_table(
        "picks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_date", sa.Date(), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("rank", sa.Integer()),
        sa.Column("score", sa.Numeric(6, 2)),
        sa.Column("close", sa.Numeric(10, 2)),
        sa.Column("industry", sa.String(50)),
        sa.Column("profit_growth", sa.Numeric(10, 2)),
        sa.Column("revenue_growth", sa.Numeric(10, 2)),
        sa.Column("roe_avg", sa.Numeric(10, 2)),
        sa.Column("debt_ratio", sa.Numeric(10, 2)),
        sa.Column("zone_low", sa.Numeric(10, 2)),
        sa.Column("zone_high", sa.Numeric(10, 2)),
        sa.Column("zone_status", sa.String(10)),
        sa.Column("ma20_slope_up", sa.Boolean()),
        sa.Column("ret20", sa.Numeric(8, 2)),
        sa.Column("ret60", sa.Numeric(8, 2)),
        sa.Column("tags", postgresql.JSONB()),
        sa.Column("source", sa.String(10)),
        sa.Column("fwd_ret_5", sa.Numeric(10, 4)),
        sa.Column("fwd_ret_10", sa.Numeric(10, 4)),
        sa.Column("fwd_ret_20", sa.Numeric(10, 4)),
        sa.Column("fwd_ret_60", sa.Numeric(10, 4)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("batch_date", "code", name="uq_picks_date_code"),
    )

    op.create_index("ix_index_daily_trade_date", "index_daily", ["trade_date"])
    op.create_index("ix_picks_batch_date", "picks", ["batch_date"])
    op.create_index("ix_picks_code", "picks", ["code"])


def downgrade() -> None:
    op.drop_table("picks")
    op.drop_table("index_daily")
