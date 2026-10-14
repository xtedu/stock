"""init schema

Revision ID: 0001
Revises:
Create Date: 2026-10-13

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stocks",
        sa.Column("code", sa.String(10), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("industry", sa.String(50)),
        sa.Column("market", sa.String(10)),
        sa.Column("list_date", sa.Date),
    )
    op.create_table(
        "daily_quotes",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), sa.ForeignKey("stocks.code"), nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("open", sa.Numeric(10, 2)),
        sa.Column("high", sa.Numeric(10, 2)),
        sa.Column("low", sa.Numeric(10, 2)),
        sa.Column("close", sa.Numeric(10, 2)),
        sa.Column("volume", sa.BigInteger),
        sa.Column("amount", sa.Numeric(18, 2)),
        sa.UniqueConstraint("code", "trade_date"),
    )
    op.create_index("ix_daily_quotes_code_date", "daily_quotes",
                    ["code", "trade_date"])
    op.create_table(
        "fin_indicators",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), sa.ForeignKey("stocks.code"), nullable=False),
        sa.Column("report_date", sa.Date, nullable=False),
        sa.Column("roe", sa.Numeric(6, 2)),
        sa.Column("revenue_yoy", sa.Numeric(8, 2)),
        sa.Column("profit_yoy", sa.Numeric(8, 2)),
        sa.Column("debt_ratio", sa.Numeric(6, 2)),
        sa.Column("ocf", sa.Numeric(18, 2)),
        sa.Column("pe", sa.Numeric(8, 2)),
        sa.Column("pb", sa.Numeric(8, 2)),
        sa.UniqueConstraint("code", "report_date"),
    )
    op.create_table(
        "stock_pool",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), sa.ForeignKey("stocks.code"), nullable=False),
        sa.Column("score", sa.Numeric(6, 2)),
        sa.Column("reason", postgresql.JSONB),
        sa.Column("batch_date", sa.Date, nullable=False),
        sa.Column("active", sa.Boolean, server_default=sa.text("true")),
    )
    op.create_table(
        "safety_zones",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), sa.ForeignKey("stocks.code"), nullable=False),
        sa.Column("zone_low", sa.Numeric(10, 2)),
        sa.Column("zone_high", sa.Numeric(10, 2)),
        sa.Column("method", sa.String(30)),
        sa.Column("detail", postgresql.JSONB),
        sa.Column("calc_date", sa.Date, nullable=False),
        sa.UniqueConstraint("code", "calc_date"),
    )
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer),
        sa.Column("code", sa.String(10)),
        sa.Column("trigger", sa.String(20), server_default="enter_zone"),
        sa.Column("channels", postgresql.JSONB),
        sa.Column("enabled", sa.Boolean, server_default=sa.text("true")),
    )
    op.create_table(
        "alert_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10)),
        sa.Column("price", sa.Numeric(10, 2)),
        sa.Column("zone_low", sa.Numeric(10, 2)),
        sa.Column("zone_high", sa.Numeric(10, 2)),
        sa.Column("notified_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("ai_summary", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("alert_logs")
    op.drop_table("alert_rules")
    op.drop_table("safety_zones")
    op.drop_table("stock_pool")
    op.drop_table("fin_indicators")
    op.drop_index("ix_daily_quotes_code_date", table_name="daily_quotes")
    op.drop_table("daily_quotes")
    op.drop_table("stocks")
