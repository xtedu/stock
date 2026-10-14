# AI 选股软件技术方案

## 1. 产品目标与核心流程

```
数据采集 → 指标筛选（优质股池）→ 安全区间计算 → 盘中/盘后监控 → 触达提醒 → 大模型解读
```

- **优质股筛选**：基本面指标（ROE、营收/利润增速、负债率、现金流）+ 估值指标（PE/PB 及历史分位）打分，选出股票池。
- **安全区间**：以 MA250 为价值中枢、±10% 为基准带，并结合 PE 历史分位做估值压缩，为股票池中每只股票计算低估区买入安全区间。
- **提醒**：每日（或盘中定时）比对最新价与安全区间，触达即推送（飞书/邮件/Server酱）。
- **大模型**：对触达股票生成简要解读（财报摘要、风险提示），不参与数值决策，只做文本增强。

## 2. 技术选型

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | |
| 数据源 | AKShare（主）/ Tushare Pro（备选） | A股行情、财务指标免费获取 |
| 数据库 | PostgreSQL 16（Docker） | 行情按日时序存储，依赖唯一约束 + upsert 实现幂等；数据量在百万级以下，无需 TimescaleDB |
| 缓存/队列 | Redis | 行情缓存、任务队列、去重 |
| 后端 | FastAPI + SQLAlchemy 2.0 + Alembic + Pydantic v2 | |
| 任务调度 | APScheduler（单机够用）；后续可升级 Celery | 每日收盘后批量计算、盘中定时轮询 |
| 大模型 | OpenAI 兼容接口（DeepSeek / 通义 / GPT 均可，仅改配置） | 走 `openai` SDK + `base_url` 切换 |
| 前端（H5） | Vue 3 + Vite + Vant 4 + ECharts | 移动端 H5，后续可平滑包成小程序/PWA |
| 通知 | 飞书机器人 Webhook / Server酱 / 邮件（SMTP） | 可插拔通知通道；飞书绑定步骤见 [飞书通知绑定指南](feishu-notification.md) |
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
                   (时序/关系)      (缓存/队列) (OpenAI兼容)
                         ▲               ▲
                 ┌───────┴───────────────┴─────────┐
                 │  调度器 APScheduler               │
                 │  ├─ 14:00  盘中链(快照→筛选→区间→监控)│
                 │  ├─ 15:40  全市场日线行情入库       │
                 │  ├─ 16:10  优质股筛选              │
                 │  ├─ 16:15  安全区间计算            │
                 │  ├─ 盘中每30min 低估区监控提醒      │
                 │  └─ 周六09:00 全市场基本面同步     │
                 └───────────────┬─────────────────┘
                                 │ AKShare / Tushare
                                 ▼
                              行情数据源
```

## 4. 数据库设计（PostgreSQL）

所有表通过 Alembic 迁移创建，唯一约束是数据幂等的基石。

```sql
-- 股票基本信息
CREATE TABLE stocks (
    code        VARCHAR(10) PRIMARY KEY,        -- 6 位代码，如 600519
    name        VARCHAR(50) NOT NULL,
    industry    VARCHAR(50),
    market      VARCHAR(10),                    -- 主板/创业板/科创板
    list_date   DATE
);

-- 日线行情（前复权 qfq）
CREATE TABLE daily_quotes (
    id          BIGSERIAL PRIMARY KEY,
    code        VARCHAR(10) NOT NULL REFERENCES stocks(code),
    trade_date  DATE NOT NULL,
    open NUMERIC(10,2), high NUMERIC(10,2),
    low  NUMERIC(10,2), close NUMERIC(10,2),
    volume BIGINT, amount NUMERIC(18,2),
    UNIQUE(code, trade_date)                    -- upsert 依据
);

-- 财务指标（按报告期，来自 stock_financial_analysis_indicator）
CREATE TABLE fin_indicators (
    id            SERIAL PRIMARY KEY,
    code          VARCHAR(10) NOT NULL REFERENCES stocks(code),
    report_date   DATE NOT NULL,                -- 报告期
    roe           NUMERIC(10,2),                -- 净资产收益率%
    revenue_yoy   NUMERIC(10,2),                -- 主营业务收入增长率%
    profit_yoy    NUMERIC(10,2),                -- 净利润增长率%
    debt_ratio    NUMERIC(10,2),                -- 资产负债率%
    ocf           NUMERIC(18,2),                -- 每股经营性现金流(元)
    np_deducted   NUMERIC(18,2),                -- 扣非净利润(元)
    ocf_np_ratio  NUMERIC(12,2),                -- 经营现金净流量/净利润%
    UNIQUE(code, report_date)
);

-- 估值历史（PE(TTM)/PB 日度）
CREATE TABLE valuation_daily (
    id          BIGSERIAL PRIMARY KEY,
    code        VARCHAR(10) NOT NULL REFERENCES stocks(code),
    trade_date  DATE NOT NULL,
    pe          NUMERIC(10,2),                  -- PE(TTM)
    pb          NUMERIC(10,2),                  -- 市净率
    UNIQUE(code, trade_date)
);

-- 盘中全市场快照（每日覆盖，不污染日线）
CREATE TABLE intraday_spot (
    code        VARCHAR(10) PRIMARY KEY,        -- 单只股票仅保留最新一条
    price       NUMERIC(10,2),
    pe          NUMERIC(10,2),
    pb          NUMERIC(10,2),
    trade_date  DATE NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- 优质股池（筛选结果快照，每批次全量替换 active）
CREATE TABLE stock_pool (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(10) REFERENCES stocks(code),
    score       NUMERIC(6,2),                   -- 综合评分 0-100
    reason      JSONB,                          -- 各指标命中明细+scores
    batch_date  DATE NOT NULL,
    active      BOOLEAN DEFAULT true
);

-- 安全区间（同日重算先删后插，避免唯一约束冲突）
CREATE TABLE safety_zones (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(10) REFERENCES stocks(code),
    zone_low    NUMERIC(10,2),                  -- 区间下沿
    zone_high   NUMERIC(10,2),                  -- 区间上沿
    method      VARCHAR(30),                    -- center_anchor
    detail      JSONB,                          -- center_ma250/pe_percentile/warn_line 等
    calc_date   DATE NOT NULL,
    UNIQUE(code, calc_date)
);

-- 提醒规则
CREATE TABLE alert_rules (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER,
    code       VARCHAR(10),                      -- NULL 表示全股池
    trigger    VARCHAR(20) DEFAULT 'enter_zone', -- 当前 enter_zone 语义=低于下沿
    channels   JSONB,                            -- ["feishu"]
    enabled    BOOLEAN DEFAULT true
);

-- 提醒记录
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

### 5.2 安全区间计算（MA250 中枢锚定）

原"估值带 ∩ 价格带 ∩ 支撑位"三法融合存在结构性缺陷：技术支撑位 `max(MA250, 布林下轨)` 天然高于价格分位带上沿，导致交集恒为空、96% 标的退化为价格分位带。现改为**中枢锚定法**：

1. **价值中枢**：`center = MA250`（近 250 个交易日收盘价均值，代表中期价值中枢）。
2. **基准区间**：`[center × 0.90, center × 1.10]`，即中枢上下 10%。
3. **估值压缩**：当前 PE 的近 5 年分位 ≥ 70%（偏贵）时，将上沿压缩到中枢本身，即区间变为 `[0.90×center, center]`。
4. **预警线**：布林下轨 `MA20 − 2σ20`，仅作展示参考（深度低估加仓线），不参与区间取交集。

`method = "center_anchor"`，`detail` 记录 `center_ma250`、`pe_percentile`、`warn_line`、`pe_squeeze`、`valuation`、`price_pct`、`source`（official/intraday）。

### 5.3 触发与提醒去重

- **触发条件**：仅当 `price < zone_low`（低于区间下沿，即低估区）时触发。落入区间内或高于上沿均不推送。
- **价格来源**：盘中优先取 `intraday_spot` 当日快照价，无快照则取最近 `daily_quotes.close`。
- **当日去重**：Redis `SETNX alert:{code}:{date} 1 EX 86400`，同一只股票当天只推一次。
- **通知流程**：触发后调大模型生成 100 字内解读（含"非投资建议"声明），写入 `alert_logs.ai_summary`，再经 `alert_rules.channels` 指定的通道（飞书等）推送。

## 6. 数据定时同步方案

所有定时任务由 `scheduler.py` 的 APScheduler `BackgroundScheduler` 管理，统一设 `max_instances=1` 防止并发重叠，`misfire_grace_time` 容忍错过执行。调度时区 `Asia/Shanghai`。

### 6.1 调度时间表

| 时间 | 任务 | 说明 | 预估耗时 |
|---|---|---|---|
| 工作日 14:00 | 盘中链：`sync_spot_snapshot` → `run_screen(intraday=True)` → `calc_zones(intraday=True)` → `check_and_notify` | 收盘前出结果，为用户预留决策窗口 | 1-2 分钟 |
| 工作日 15:40 | `sync_all_quotes(years=5)` | 全市场日线行情入库（6 线程并发） | 15-20 分钟 |
| 工作日 16:10 | `run_screen` | 按硬门槛+打分更新优质股池 | 3-5 分钟 |
| 工作日 16:15 | `calc_zones` | 为股池标的计算安全区间 | 1-2 分钟 |
| 工作日 9:30-14:59 每 30 分钟 | `run_monitor_job`（`check_and_notify`） | 比对最新价与区间，触发低估区提醒 | 秒级 |
| 周六 09:00 | `sync_all_fundamentals(years=5)` | 财务指标 + 估值历史全量同步 | 数小时 |

### 6.2 盘中链 vs 收盘链

两条链路独立、互不污染：

- **盘中链（14:00）**：通过 `intraday_spot` 表存储当日快照价，筛选和区间计算时若发现当日快照则叠加参与计算，结果 `detail.source = "intraday"`。快照仅作盘中估值折算，**不写入 `daily_quotes`**，收盘后官方数据链自动覆盖。
- **收盘链（15:40 / 16:10 / 16:15）**：用真实收盘日线数据重跑筛选与区间，`detail.source = "official"`，覆盖盘中结果。

### 6.3 手动触发

前端「任务」页面（`/jobs`）提供所有任务的手动按钮，对应 `POST /api/jobs/*` 端点。长任务（行情/基本面）使用独立的长超时 axios 实例（30 分钟）。也可直接命令行调用 service 函数。

## 7. 数据采集核心逻辑

数据源为 AKShare（免费网页抓取型），所有采集函数在 `services/datafeed.py`。

### 7.1 股票列表

`stock_info_a_code_name()` 拉取 A 股全部代码+名称，`on_conflict_do_nothing(code)` 增量入库，不更新已有名称（避免临时改名噪声）。

### 7.2 日线行情（`sync_all_quotes`）

1. 先 `sync_stock_list` 确保代码表最新。
2. 查 `daily_quotes` 每只股票的 `max(trade_date)`，**仅拉取 `latest < today` 的股票**（断点续传）。
3. 用 `ThreadPoolExecutor(workers=6)` 并发调用 `stock_zh_a_hist(symbol, period="daily", start, end, adjust="qfq")` 拉前复权日线。
4. `_upsert_quotes` 按 `(code, trade_date)` 做 `on_conflict_do_update`，覆盖 open/high/low/close/volume/amount。
5. 单只失败仅打 warning，不中断整体。

### 7.3 盘中快照（`sync_spot_snapshot`）

单次 `stock_zh_a_spot_em()` 拉全市场最新价/PE/PB，按 `code` 主键 `on_conflict_do_update` 覆盖 `intraday_spot`（含 price/pe/pb/trade_date/updated_at）。约 10 秒完成。

### 7.4 财务指标（`sync_fin_indicators` / `sync_all_fundamentals`）

- `stock_financial_analysis_indicator(symbol, start_year)` 拉报告期指标。
- 字段映射：ROE、营收同比、净利润同比、资产负债率、每股经营现金流、扣非净利润、经营现金流/净利润比。
- `_upsert_fin` 按 `(code, report_date)` upsert。
- 全市场同步：4 线程并发抓取，主线程串行 upsert（避免 DB 连接竞争），单只异常 `rollback` 后继续。

### 7.5 估值历史（`sync_valuation`）

- `stock_value_em(symbol)` 拉 PE(TTM)/PB 日度序列。
- 仅保留近 `years` 年数据（`cutoff` 过滤），按 `(code, trade_date)` upsert。

### 7.6 异常与重试

- 所有 `_fetch_*` 包裹 try/except，失败返回 `None`，上层跳过该只股票。
- 当前未做指数退避重试（AKShare 偶发失败可接受，下次调度自动补拉）。
- 生产环境建议加请求间隔（`time.sleep`）避免触发源站限流。

## 8. 数据去重与幂等设计

系统从三个层面保证重复执行不会产生脏数据。

### 8.1 数据库唯一约束（第一层）

| 表 | 唯一键 | 作用 |
|---|---|---|
| `stocks` | `code` | 股票列表增量插入不重复 |
| `daily_quotes` | `(code, trade_date)` | 同一股票同日行情仅一条，upsert 覆盖 |
| `fin_indicators` | `(code, report_date)` | 同一报告期仅一条，upsert 覆盖 |
| `valuation_daily` | `(code, trade_date)` | 同日估值仅一条，upsert 覆盖 |
| `intraday_spot` | `code` | 单只股票仅保留最新快照，覆盖更新 |
| `safety_zones` | `(code, calc_date)` | 同一计算日仅一条 |

所有写入采用 PostgreSQL `INSERT ... ON CONFLICT DO UPDATE`（upsert）而非先删后插，保证并发安全与原子性。

### 8.2 业务层幂等（第二层）

- **日线断点续传**：`sync_all_quotes` 只拉 `max(trade_date) < today` 的股票，已同步到当日的自动跳过，重复执行零成本。
- **股池批次替换**：`run_screen` 开始时 `UPDATE stock_pool SET active=false`，再插入新批次 `active=true`，保证同一时刻只有一个有效批次，历史批次保留可追溯。
- **安全区间同日重算**：`calc_zones` 先 `DELETE FROM safety_zones WHERE calc_date = :date` 再插入，**绕开 `(code, calc_date)` 唯一约束冲突**（直接 upsert 会因 detail JSON 比对复杂而放弃）。同日重跑结果一致。
- **盘中快照覆盖**：`intraday_spot` 按 code 覆盖，不积累历史，收盘后由正式日线接管。

### 8.3 提醒去重（第三层）

- Redis `SETNX alert:{code}:{date}` 带 86400 秒过期，同一只股票同一天无论监控跑多少次只推一次。
- `alert_logs` 记录所有推送历史（不设唯一约束，允许跨日重复记录）。
- 调度器 `max_instances=1` 保证同一时刻只有一个监控任务在跑，不会并发触发同一标的。

### 8.4 盘中/收盘数据隔离

- 盘中快照价只进 `intraday_spot`，**绝不写入 `daily_quotes`**，避免污染正式日线。
- 筛选/区间结果的 `detail.source` 标记 `intraday` 或 `official`，前端可区分展示。
- 收盘官方数据链执行后，同 `calc_date` 的区间记录被覆盖（`DELETE + INSERT`），自动从盘中结果切换为正式结果。

## 9. 大模型配置（OpenAI 兼容，改配置即切换厂商）

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

## 10. 前端（H5）

- Vue 3 + Vite + Vant 4 + ECharts，移动优先，可后期套壳成 PWA/小程序。
- 页面：
  1. **股票池**：卡片列表，评分、指标徽标、所属行业。
  2. **个股详情**：K线 + 安全区间色带叠加（ECharts markArea）、筛选理由、历史提醒。
  3. **提醒列表**：触发时间、价格、AI 解读全文。
  4. **任务**（`/jobs`）：手动触发行情同步、基本面同步、筛选、区间计算、盘中链、监控；长任务使用 30 分钟超时的 axios 实例。
  5. **我的**：提醒规则（按股票/全池）、通知通道绑定。
- 图表关键点：日线 series + `markArea` 画 `[zone_low, zone_high]` 区间带 + 最新价 markLine。

## 11. Docker 与部署

`docker-compose.yml`（关键服务）：

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_DB: stock
      POSTGRES_USER: stock
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes: [ "pgdata:/var/lib/postgresql/data" ]
    ports: [ "5433:5432" ]       # 主机 5433，避免与本地 PG 冲突

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

## 12. 项目结构（建议）

```
stock/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI 入口（含 lifespan 启停调度器）
│   │   ├── models.py           # SQLAlchemy 模型（扁平单文件）
│   │   ├── schemas.py          # Pydantic
│   │   ├── database.py         # engine / SessionLocal
│   │   ├── config.py
│   │   ├── scheduler.py        # APScheduler 任务编排（盘中链/盘后链/监控/周同步）
│   │   ├── api/
│   │   │   ├── deps.py
│   │   │   ├── stocks.py
│   │   │   ├── zones.py
│   │   │   ├── alerts.py
│   │   │   ├── insights.py
│   │   │   └── jobs.py         # 手动触发端点 /api/jobs/*
│   │   └── services/
│   │       ├── datafeed.py     # AKShare 采集 + upsert + 断点续传
│   │       ├── screening.py    # 优质股筛选打分
│   │       ├── safety_zone.py  # MA250 中枢锚定安全区间
│   │       ├── monitor.py      # 价格比对 + Redis 当日去重 + 推送
│   │       ├── intraday.py     # 盘中链编排（快照→筛选→区间→监控）
│   │       ├── notification.py # 飞书/邮件/Server酱
│   │       └── llm.py          # OpenAI 兼容客户端
│   ├── config.yaml
│   ├── alembic/
│   └── requirements.txt
└── frontend/
    └── src/
        ├── api/                # axios 实例（默认 + 长超时）
        └── views/ (Pool / Detail / Alerts / Jobs / Mine)
```

## 13. 里程碑

1. **M1 数据底座**：compose 起库 → 建表 → 全量拉取日线+财务入库。
2. **M2 计算内核**：筛选打分 + 安全区间计算，CLI 可跑通并回写库。
3. **M3 服务与提醒**：FastAPI 只读接口 + 调度器 + 飞书通知全链路。
4. **M4 H5 前端**：股票池/详情（区间图）/提醒列表。
5. **M5 AI 解读 + 规则管理**：接入大模型配置、用户自定义提醒规则与通道。

## 14. 风险与合规

- AKShare 为网页抓取型免费源，需限速、加失败重试与缓存；生产可切 Tushare Pro 积分接口。
- 个股页与推送统一附带"仅为数据工具，非投资建议"。
- 提醒频率做去重与每日上限，避免轰炸。
