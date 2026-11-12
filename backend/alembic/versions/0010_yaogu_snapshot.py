"""yaogu daily snapshot

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11

妖股/涨停池每日快照：把每天梯队里每只涨停股（含涨停原因、封板时间、
炸板次数、封单额、换手率等）落库，用于回看历史与后续晋级统计。
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yaogu_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_date", sa.Date(), nullable=False, index=True),
        sa.Column("code", sa.String(10), nullable=False, index=True),
        sa.Column("name", sa.String(50)),
        sa.Column("industry", sa.String(50)),
        sa.Column("close", sa.Numeric(10, 2)),
        sa.Column("amount_yi", sa.Numeric(12, 2)),
        sa.Column("streak", sa.Integer(), server_default="1"),
        sa.Column("high_days", sa.String(20)),
        sa.Column("limit_up_type", sa.String(20)),
        sa.Column("first_limit_time", sa.String(10)),
        sa.Column("last_limit_time", sa.String(10)),
        sa.Column("break_count", sa.Integer()),
        sa.Column("seal_amount_yi", sa.Numeric(12, 2)),
        sa.Column("turnover_rate", sa.Numeric(8, 2)),
        sa.Column("float_mv_yi", sa.Numeric(12, 2)),
        sa.Column("ret20_pct", sa.Numeric(8, 2)),
        sa.Column("amplitude_pct", sa.Numeric(8, 2)),
        sa.Column("reason", sa.Text()),
        sa.Column("is_open_board", sa.Boolean()),
        sa.Column("score", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("trade_date", "code",
                            name="uq_yaogu_snapshot_date_code"),
    )


def downgrade() -> None:
    op.drop_table("yaogu_snapshot")
