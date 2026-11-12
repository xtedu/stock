"""news items and daily brief

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-10

消息面：政策/快讯/公告聚合（news_items）+ 每日 AI 决策简报（news_briefs）。
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "news_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("category", sa.String(20)),
        sa.Column("title", sa.String(400), nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("url", sa.String(500)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("related_codes", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("source", "title", name="uq_news_source_title"),
    )
    op.create_index("ix_news_items_trade_date", "news_items", ["trade_date"])

    op.create_table(
        "news_briefs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("brief_date", sa.Date, nullable=False, unique=True),
        sa.Column("payload", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("news_briefs")
    op.drop_index("ix_news_items_trade_date", table_name="news_items")
    op.drop_table("news_items")
