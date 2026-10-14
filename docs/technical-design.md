# AI 选股软件技术方案

## 1. 产品目标与核心流程

```
数据采集 → 指标筛选（优质股池）→ 安全区间计算 → 盘中/盘后监控 → 触达提醒 → 大模型解读
```

- **优质股筛选**：基本面指标（ROE、营收/利润增速、负债率、现金流）+ 估值指标（PE/PB 及历史分位）打分，选出股票池。
- **安全区间**：基于历史价格与估值分位、技术支撑位，为股票池中每只股票计算买入安全区间。
- **提醒**：每日（或盘中定时）比对最新价与安全区间，触达即推送（飞书/邮件/Server酱）。
- **大模型**：对触达股票生成简要解读（财报摘要、风险提示），不参与数值决策，只做文本增强。

## 2. 技术选型

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | |
| 数据源 | AKShare（主）/ Tushare Pro（备选） | A股行情、财务指标免费获取 |
| 数据库 | PostgreSQL 15 + TimescaleDB 扩展（Docker） | 行情是时序数据，TimescaleDB 有分区与压缩优势；不引入扩展也可纯 PG 跑通 |
| 缓存/队列 | Redis | 行情缓存、任务队列、去重 |
| 后端 | FastAPI + SQLAlchemy 2.0 + Alembic + Pydantic v2 | |
| 任务调度 | APScheduler（单机够用）；后续可升级 Celery | 每日收盘后批量计算、盘中定时轮询 |
| 大模型 | OpenAI 兼容接口（DeepSeek / 通义 / GPT 均可，仅改配置） | 走 `openai` SDK + `base_url` 切换 |
| 前端（H5） | Vue 3 + Vite + Vant 4 + ECharts | 移动端 H5，后续可平滑包成小程序/PWA |
| 通知 | 飞书机器人 Webhook / Server酱 / 邮件（SMTP） | 可插拔通知通道 |
| 部署 | docker-compose 一键起 | db / redis / backend / frontend |

## 3. 系统架构

```
┌────────────┐   ┌──────────────────────────────────────────┐
│  H5 前端    │──▶│  FastAPI 后端                             │
│ Vue3+Vant  │   │  ├─ /api/pool        股票池                │
└────────────┘   │  ├─ /api/zones       安全区间              │
                 │  ├─ /api/alerts      提醒记录/规则         │
                 │  ├─ /api/insights    AI解读               │
                 │  └─ /api/auth        简单登录(JWT)         │
                 └───────┬───────────────┬─────────┬─────────┘
                         │               │         │
                    PostgreSQL         Redis    大模型API
                    (Timescale)      (缓存/队列) (OpenAI兼容)
                         ▲               ▲
                 ┌───────┴───────────────┴─────────┐
                 │  调度器 APScheduler               │
                 │  ├─ 15:30 拉日线/财务 → 入库      │
                 │  ├─ 16:00 筛选+安全区间计算       │
                 │  └─ 盘中每30min 比对价格→推送提醒  │
                 └───────────────┬─────────────────┘
                                 │ AKShare / Tushare
                                 ▼
                              行情数据源
```

## 4. 数据库设计（PostgreSQL）

```sql
-- 股票基本信息
CREATE TABLE stocks (
    code        VARCHAR(10) PRIMARY KEY,      -- 600519.SH
    name        VARCHAR(50) NOT NULL,
    industry    VARCHAR(50),
    market      VARCHAR(10),                  -- 主板/创业板/科创板
    list_date   DATE,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- 日线行情（TimescaleDB 超表；纯 PG 就建 (code, trade_date) 唯一索引）
CREATE TABLE daily_quotes (
    code        VARCHAR(10) NOT NULL REFERENCES stocks(code),
    trade_date  DATE NOT NULL,
    open NUMERIC(10,2), high NUMERIC(10,2),
    low  NUMERIC(10,2), close NUMERIC(10,2),
    volume BIGINT, amount NUMERIC(18,2),
    UNIQUE(code, trade_date)
);
-- SELECT create_hypertable('daily_quotes', 'trade_date');

-- 财务指标（按报告期）
CREATE TABLE fin_indicators (
    code         VARCHAR(10) NOT NULL,
    report_date  DATE NOT NULL,               -- 报告期
    roe          NUMERIC(6,2),
    revenue_yoy  NUMERIC(8,2),                -- 营收同比%
    profit_yoy   NUMERIC(8,2),                -- 净利润同比%
    debt_ratio   NUMERIC(6,2),                -- 资产负债率%
    ocf          NUMERIC(18,2),               -- 经营现金流
    pe           NUMERIC(8,2),  pb NUMERIC(8,2),
    UNIQUE(code, report_date)
);

-- 优质股池（筛选结果快照）
CREATE TABLE stock_pool (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(10) REFERENCES stocks(code),
    score       NUMERIC(6,2),                 -- 综合评分
    reason      JSONB,                        -- 各指标命中明细
    batch_date  DATE NOT NULL,                -- 本批次日期
    active      BOOLEAN DEFAULT true
);

-- 安全区间
CREATE TABLE safety_zones (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(10) REFERENCES stocks(code),
    zone_low    NUMERIC(10,2),                -- 区间下沿
    zone_high   NUMERIC(10,2),                -- 区间上沿
    method      VARCHAR(30),                  -- 计算方式
    detail      JSONB,                        -- 分位数/支撑位等中间量
    calc_date   DATE NOT NULL,
    UNIQUE(code, calc_date)
);

-- 提醒规则与记录
CREATE TABLE alert_rules (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER,
    code       VARCHAR(10),
    trigger    VARCHAR(20) DEFAULT 'enter_zone', -- enter_zone / below_low
    channels   JSONB,                            -- ["feishu","email"]
    enabled    BOOLEAN DEFAULT true
);
CREATE TABLE alert_logs (
    id           BIGSERIAL PRIMARY KEY,
    code         VARCHAR(10),
    price        NUMERIC(10,2),
    zone_low     NUMERIC(10,2),
    zone_high    NUMERIC(10,2),
    notified_at  TIMESTAMPTZ DEFAULT now(),
    ai_summary   TEXT                            -- 大模型解读缓存
);
```

## 5. 核心算法

### 5.1 优质股筛选（打分制，可配置权重）

硬性门槛（任一不满足直接剔除）：
- 上市满 3 年、非 ST、近一年无亏损
- 最近 4 个报告期 ROE 均 ≥ 10%
- 资产负债率 ≤ 60%（金融地产单独放宽）

打分项（0–100 加权）：
- ROE 均值、营收/利润 3 年复合增速、经营现金流/净利润比
- PE/PB 所处自身 5 年历史分位（越低分越高）

结果：`stock_pool` 写入综合评分与命中明细（JSONB 方便前端展示）。

### 5.2 安全区间计算（多法融合，取交集/保守值）

1. **估值分位法**：取近 5 年 PE（或 PB）10%~30% 分位对应的估值，乘以当前 EPS/BPS，折算出价格区间。
2. **价格分位法**：近 3 年复权收盘价的 15%~35% 分位。
3. **技术支撑法**：250 日均线、近一年前低、布林带下轨三者取较高者作为下沿参考。

最终区间 = 各方法下沿取较高值、上沿取较低值（保守），记录 `method` 与 `detail` 供前端画区间带。

### 5.3 触发与去重

- 盘中每 30 分钟（交易时段）拉实时价；收盘后全量刷新。
- `close ∈ [zone_low, zone_high]` 且该 code 当日未推送过（Redis SETNX 去重）→ 触发。
- 触发后调大模型生成 100 字内解读（含风险声明），写入 `alert_logs.ai_summary`，再经通知通道推送。

## 6. 大模型配置（OpenAI 兼容，改配置即切换厂商）

`backend/config.yaml`：

```yaml
llm:
  enabled: true
  base_url: "https://api.deepseek.com/v1"   # DeepSeek；可换 dashscope 兼容地址/OpenAI
  api_key: "${LLM_API_KEY}"                  # 从环境变量读，不入库不入库
  model: "deepseek-chat"
  timeout: 20
  max_tokens: 300
  prompt_template: |
    你是证券分析助手。股票 {name}({code}) 当前价 {price}，进入历史安全区间
    [{zone_low}, {zone_high}]。已知：ROE {roe}%，PE分位 {pe_pct}%。
    用不超过100字说明其进入区间的可能原因与主要风险，结尾附"非投资建议"。
```

切换厂商只需改 `base_url` / `model` / 环境变量 key，代码零改动。

## 7. 前端（H5）

- Vue 3 + Vite + Vant 4 + ECharts，移动优先，可后期套壳成 PWA/小程序。
- 页面：
  1. **股票池**：卡片列表，评分、指标徽标、所属行业。
  2. **个股详情**：K线 + 安全区间色带叠加（ECharts markArea）、筛选理由、历史提醒。
  3. **提醒列表**：触发时间、价格、AI 解读全文。
  4. **我的**：提醒规则（按股票/全池）、通知通道绑定。
- 图表关键点：日线 series + `markArea` 画 `[zone_low, zone_high]` 区间带 + 最新价 markLine。

## 8. Docker 与部署

`docker-compose.yml`（关键服务）：

```yaml
services:
  db:
    image: timescale/timescaledb:latest-pg15
    environment:
      POSTGRES_DB: stock
      POSTGRES_USER: stock
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes: [ "pgdata:/var/lib/postgresql/data" ]
    ports: [ "5432:5432" ]

  redis:
    image: redis:7-alpine
    ports: [ "6379:6379" ]

  backend:
    build: ./backend
    env_file: .env            # LLM_API_KEY / DB / 通知 webhook
    depends_on: [db, redis]
    ports: [ "8000:8000" ]

  frontend:
    build: ./frontend
    ports: [ "80:80" ]        # nginx 托管 H5 静态资源 + /api 反代 backend

volumes:
  pgdata:
```

## 9. 项目结构（建议）

```
stock/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 入口
│   │   ├── api/               # 路由: pool/zones/alerts/insights/auth
│   │   ├── models/            # SQLAlchemy 模型
│   │   ├── schemas/           # Pydantic
│   │   ├── services/
│   │   │   ├── datafeed.py    # AKShare/Tushare 采集
│   │   │   ├── screener.py    # 优质股筛选
│   │   │   ├── safety_zone.py # 安全区间计算
│   │   │   ├── notifier.py    # 飞书/邮件/Server酱
│   │   │   └── llm.py         # OpenAI 兼容客户端
│   │   ├── scheduler.py       # APScheduler 任务
│   │   └── config.py
│   ├── config.yaml
│   ├── alembic/
│   └── requirements.txt
└── frontend/
    └── src/ (views: Pool / Detail / Alerts / Mine)
```

## 10. 里程碑

1. **M1 数据底座**：compose 起库 → 建表 → 全量拉取日线+财务入库。
2. **M2 计算内核**：筛选打分 + 安全区间计算，CLI 可跑通并回写库。
3. **M3 服务与提醒**：FastAPI 只读接口 + 调度器 + 飞书通知全链路。
4. **M4 H5 前端**：股票池/详情（区间图）/提醒列表。
5. **M5 AI 解读 + 规则管理**：接入大模型配置、用户自定义提醒规则与通道。

## 11. 风险与合规

- AKShare 为网页抓取型免费源，需限速、加失败重试与缓存；生产可切 Tushare Pro 积分接口。
- 个股页与推送统一附带"仅为数据工具，非投资建议"。
- 提醒频率做去重与每日上限，避免轰炸。
