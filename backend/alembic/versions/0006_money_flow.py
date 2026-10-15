"""money flow table

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14

个股资金流（东方财富汇总口径）：主力/超大单/大单净额与净占比。
盘中写入 source=intraday（开盘至今累计），收盘后 official 覆盖当日记录。
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "money_flow",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), sa.ForeignKey("stocks.code"),
                  nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("main_net", sa.Numeric(18, 2)),
        sa.Column("main_net_pct", sa.Numeric(10, 2)),
        sa.Column("super_net", sa.Numeric(18, 2)),
        sa.Column("super_net_pct", sa.Numeric(10, 2)),
        sa.Column("big_net", sa.Numeric(18, 2)),
        sa.Column("big_net_pct", sa.Numeric(10, 2)),
        sa.Column("main_net_5d", sa.Numeric(18, 2)),
        sa.Column("source", sa.String(10)),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("code", "trade_date", name="uq_money_flow_code_date"),
    )


def downgrade() -> None:
    op.drop_table("money_flow")
