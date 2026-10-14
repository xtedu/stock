# AI 选股软件

基于 A 股行情与基本面数据的**优质股筛选 + 安全区间计算 + 低估区智能提醒**系统，集成大模型解读，Docker Compose 一键部署。

详见完整 [技术方案](docs/technical-design.md)。

## ✨ 核心特性

- **优质股筛选**：基本面（ROE、营收/利润增速、资产负债率、经营现金流）+ 估值（PE/PB 及 5 年历史分位）多维度打分，生成可追溯的优质股池
- **安全区间计算**：MA250 中枢锚定法，结合 PE 历史分位做估值压缩，给出低估区买入区间
- **盘中/盘后双链路**：14:00 盘中链基于实时快照出结果，收盘后用官方日线自动覆盖
- **智能提醒**：价格跌破区间下沿即推送（飞书 / Server酱 / 邮件），Redis 当日去重避免轰炸
- **大模型解读**：OpenAI 兼容接口，触达时自动生成财报摘要与风险提示
- **数据幂等**：唯一约束 + upsert + 断点续传，重复执行不产生脏数据

## 🛠 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11 + FastAPI + SQLAlchemy 2.0 + Alembic |
| 数据库 | PostgreSQL 16 |
| 缓存 | Redis 7（去重、缓存） |
| 调度 | APScheduler |
| 大模型 | OpenAI 兼容（DeepSeek / 通义 / GPT，改配置即切换） |
| 数据源 | AKShare（主）/ Tushare Pro（备） |
| 前端 | Vue 3 + Vite + Vant 4 + ECharts（H5，nginx 托管） |

## 🚀 快速开始

```powershell
# 1. 配置环境变量（首次）
Copy-Item .env.example .env
# 编辑 .env：至少改 POSTGRES_PASSWORD、DATABASE_URL、SECRET_KEY；按需填 LLM_API_KEY / FEISHU_WEBHOOK

# 2. 构建并启动
docker compose up -d --build

# 3. 查看状态
docker compose ps
docker compose logs -f backend

# 4. 访问
# 前端 H5:  http://localhost
# 后端文档: http://localhost:8000/docs
```

启动时 backend 会自动执行 `alembic upgrade head` 建表。

## ⏰ 每日自动调度（Asia/Shanghai）

| 时间 | 任务 |
|---|---|
| 交易日 14:00 | 盘中链：全市场快照 → 筛选 → 安全区间 → 监控提醒（收盘前约 50 分钟出结果） |
| 交易日 15:40 | 收盘行情同步（官方数据，6 线程并发） |
| 交易日 16:10 / 16:15 | 筛选 / 区间计算（收盘数据覆盖盘中结果） |
| 周六 09:00 | 全市场基本面同步（财务指标 + 估值历史） |
| 交易时段每 30 分钟 | 安全区间低估区监控提醒 |

## 🔧 手动触发任务（无需等定时）

前端「任务」页面（`/jobs`）提供按钮，或直接调用接口：

```powershell
# 拉取全部 A 股列表 + 近5年日线（耗时较长，建议首次先跑）
curl -X POST "http://localhost:8000/api/jobs/sync-quotes?years=5"

# 盘中链手动触发（快照+筛选+区间+监控，约 1-2 分钟）
curl -X POST http://localhost:8000/api/jobs/intraday

# 执行筛选 / 计算安全区间 / 立即监控
curl -X POST http://localhost:8000/api/jobs/screen
curl -X POST http://localhost:8000/api/jobs/calc-zones
curl -X POST http://localhost:8000/api/jobs/monitor
```

## 📁 目录结构

```
stock/
├── docker-compose.yml
├── .env.example              # 环境变量模板
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI 入口（含调度器启停）
│   │   ├── scheduler.py      # APScheduler 任务编排
│   │   ├── models.py         # SQLAlchemy 模型
│   │   ├── api/              # 路由（stocks/zones/alerts/insights/jobs）
│   │   └── services/         # datafeed/screening/safety_zone/monitor/intraday/notification/llm
│   ├── alembic/              # 数据库迁移
│   └── requirements.txt
├── frontend/                 # Vue3 H5（Pool/Detail/Alerts/Jobs/Mine）
└── docs/
    └── technical-design.md   # 完整技术方案
```

## 📝 常用命令

```powershell
docker compose down          # 停止
docker compose down -v       # 停止并清数据
docker compose logs -f db    # 查看日志
```

## ⚠️ 注意事项

- AKShare 为免费网页抓取型数据源，需限速；首次全量同步 5 年日线建议盘外执行
- 大模型为 OpenAI 兼容接口，`.env` 改 `LLM_BASE_URL` / `LLM_MODEL` 即可切换 DeepSeek/通义/GPT
- 通知支持飞书 / Server酱 / 邮件，在 `.env` 配置对应凭据
- **仅供学习研究，不构成投资建议**
