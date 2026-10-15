"""compare analysis records

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-14

保存股票池「对比分析」结果：选中的标的组合、逐只对比指标、
大模型定性研判（overall 结论 + 逐只看多/中性/看空），支持历史回看。
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compare_analyses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("codes", JSONB),
        sa.Column("items", JSONB),
        sa.Column("ai_summary", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("compare_analyses")
