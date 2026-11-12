"""float market cap for digest sizing

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-10

消息消化度按市值分层：小盘股波动天然更大，阈值需放宽，
否则同一涨幅会在大盘股上被误判为「已兑现」。
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stocks", sa.Column("float_mv", sa.Numeric(20, 2)))


def downgrade() -> None:
    op.drop_column("stocks", "float_mv")
