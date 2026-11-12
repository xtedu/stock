"""架构底座：交易日历 / 全市场特征 / 前向标签 / 参数中心 / 数据质量

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-12

- dim_trade_calendar：真实交易日历，替代工作日近似
- feat_stock_daily：沪深主板全市场每日截面特征，所有策略统一取数
- label_fwd：全市场前向收益标签，一次回填、多策略共用
- strategy_params：阈值/权重中心，带版本可复现
- data_quality：覆盖率/完整率等质量门禁记录
- yaogu_snapshot.score_fallback：区分真值与代理值打分
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dim_trade_calendar",
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("is_open", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
    )

    op.create_table(
        "feat_stock_daily",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("industry", sa.String(50)),
        sa.Column("close", sa.Numeric(10, 2)),
        sa.Column("amount", sa.Numeric(18, 2)),
        sa.Column("pct_chg", sa.Numeric(8, 4)),
        sa.Column("ret5", sa.Numeric(8, 4)),
        sa.Column("ret20", sa.Numeric(8, 4)),
        sa.Column("ret60", sa.Numeric(8, 4)),
        sa.Column("ma20", sa.Numeric(10, 2)),
        sa.Column("ma60", sa.Numeric(10, 2)),
        sa.Column("ma250", sa.Numeric(10, 2)),
        sa.Column("bias20", sa.Numeric(8, 4)),
        sa.Column("ma20_slope_pct", sa.Numeric(8, 4)),
        sa.Column("vol20", sa.Numeric(8, 4)),
        sa.Column("vol60", sa.Numeric(8, 4)),
        sa.Column("amt20", sa.Numeric(18, 2)),
        sa.Column("amt_ratio", sa.Numeric(8, 4)),
        sa.Column("pos60", sa.Numeric(8, 4)),
        sa.Column("limit_up", sa.Boolean()),
        sa.Column("streak", sa.Integer()),
        sa.Column("pe", sa.Numeric(10, 2)),
        sa.Column("pb", sa.Numeric(10, 2)),
        sa.Column("pe_pct5y", sa.Numeric(8, 2)),
        sa.Column("roe_avg", sa.Numeric(8, 2)),
        sa.Column("profit_cagr", sa.Numeric(10, 2)),
        sa.Column("rev_cagr", sa.Numeric(10, 2)),
        sa.Column("debt_ratio", sa.Numeric(8, 2)),
        sa.Column("mf_main_net_pct", sa.Numeric(8, 2)),
        sa.Column("mf_main_net_5d", sa.Numeric(18, 2)),
        sa.Column("ind_heat3", sa.Integer()),
        sa.Column("feature_version", sa.String(20)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("code", "as_of",
                            name="uq_feat_stock_daily_code_date"),
    )
    op.create_index("ix_feat_stock_daily_code", "feat_stock_daily", ["code"])
    op.create_index("ix_feat_stock_daily_as_of", "feat_stock_daily", ["as_of"])

    op.create_table(
        "label_fwd",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("horizon", sa.Integer(), nullable=False),
        sa.Column("fwd_ret", sa.Numeric(10, 4)),
        sa.Column("bench_ret", sa.Numeric(10, 4)),
        sa.Column("excess", sa.Numeric(10, 4)),
        sa.Column("fwd_max_dd", sa.Numeric(10, 4)),
        sa.Column("label_version", sa.String(20)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("code", "as_of", "horizon",
                            name="uq_label_fwd_code_date_horizon"),
    )
    op.create_index("ix_label_fwd_code", "label_fwd", ["code"])
    op.create_index("ix_label_fwd_as_of", "label_fwd", ["as_of"])

    op.create_table(
        "strategy_params",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("value", postgresql.JSONB()),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("effective_from", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("key", "version",
                            name="uq_strategy_params_key_version"),
    )

    op.create_table(
        "data_quality",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dataset", sa.String(40), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("metric", sa.String(40), nullable=False),
        sa.Column("value", sa.Numeric(12, 4)),
        sa.Column("status", sa.String(10)),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column("checked_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("dataset", "as_of", "metric",
                            name="uq_data_quality_ds_date_metric"),
    )
    op.create_index("ix_data_quality_as_of", "data_quality", ["as_of"])

    op.add_column("yaogu_snapshot",
                  sa.Column("score_fallback", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("yaogu_snapshot", "score_fallback")
    op.drop_index("ix_data_quality_as_of", table_name="data_quality")
    op.drop_table("data_quality")
    op.drop_table("strategy_params")
    op.drop_index("ix_label_fwd_as_of", table_name="label_fwd")
    op.drop_index("ix_label_fwd_code", table_name="label_fwd")
    op.drop_table("label_fwd")
    op.drop_index("ix_feat_stock_daily_as_of", table_name="feat_stock_daily")
    op.drop_index("ix_feat_stock_daily_code", table_name="feat_stock_daily")
    op.drop_table("feat_stock_daily")
    op.drop_table("dim_trade_calendar")
