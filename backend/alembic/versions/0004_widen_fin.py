"""widen fin_indicators numeric columns to avoid overflow

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14

极端值示例：资不抵债公司资产负债率可达 19173.89%，
原 Numeric(6,2)（上限 9999.99）会触发 numeric field overflow。
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

WIDEN = {
    "roe": sa.Numeric(10, 2),
    "revenue_yoy": sa.Numeric(10, 2),
    "profit_yoy": sa.Numeric(10, 2),
    "debt_ratio": sa.Numeric(10, 2),
    "ocf_np_ratio": sa.Numeric(12, 2),
    "pe": sa.Numeric(12, 2),
    "pb": sa.Numeric(12, 2),
}


def upgrade() -> None:
    for col, typ in WIDEN.items():
        op.alter_column("fin_indicators", col, type_=typ, existing_nullable=True)


def downgrade() -> None:
    pass
