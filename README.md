# AI 选股软件

基于 Docker Compose 的一键部署全栈骨架，详见 [技术方案](docs/technical-design.md)。

## 快速开始

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

## 每日自动调度（Asia/Shanghai）

| 时间 | 任务 |
|---|---|
| 交易日 14:00 | 盘中链：全市场快照 → 筛选 → 安全区间 → 监控提醒（收盘前约 50 分钟出结果） |
| 交易日 15:40 | 收盘行情同步（官方数据） |
| 交易日 16:10 / 16:15 | 筛选 / 区间计算（收盘数据覆盖盘中结果） |
| 周六 09:00 | 全市场基本面同步（财务 + 估值历史） |
| 交易时段每 30 分钟 | 安全区间监控提醒 |

## 手动触发任务（无需等定时）

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

## 目录结构

- `backend/` FastAPI + SQLAlchemy + Alembic + APScheduler
- `frontend/` Vue3 + Vant4 H5（nginx 托管 + /api 反代）
- `docs/technical-design.md` 完整技术方案

## 常用命令

```powershell
docker compose down          # 停止
docker compose down -v       # 停止并清数据
docker compose logs -f db    # 查看日志
```

## 注意

- AKShare 免费源需限速，首次全量同步 5 年日线建议盘外执行
- 大模型为 OpenAI 兼容接口，`.env` 改 `LLM_BASE_URL`/`LLM_MODEL` 即可切换 DeepSeek/通义/GPT
- 通知当前支持飞书/Server酱/邮件，在 `.env` 配置对应凭据
