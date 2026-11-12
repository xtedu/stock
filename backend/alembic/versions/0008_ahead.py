"""forward-looking calendar + news digest

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10

预判能力：
- calendar_events：宏观日历 / 限售解禁 / 业绩预告，均为「尚未发生」的事件
- news_items.digest：利好消息的消化度（前 5 日涨幅、量比、距 20 日高点），
  用于区分「已兑现的利好」与「尚未发酵的利好」。
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_items", sa.Column("digest", JSONB))

    op.create_table(
        "calendar_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("event_date", sa.Date, nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("detail", JSONB),
        sa.Column("importance", sa.Integer, server_default="1"),
        sa.Column("related_codes", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("kind", "event_date", "title",
                            name="uq_calendar_kind_date_title"),
    )
    op.create_index("ix_calendar_events_date", "calendar_events", ["event_date"])


def downgrade() -> None:
    op.drop_index("ix_calendar_events_date", table_name="calendar_events")
    op.drop_table("calendar_events")
    op.drop_column("news_items", "digest")
