# 定时任务框架讲解文档（讲解版）

> 本文只讲**调度框架本身**：为什么这么选、任务怎么定义、什么时候触发、失败怎么办、边界在哪。
> 每节末尾给出**讲解要点**，文末附**参数速查表**、**十分钟讲解提纲**、**常见问答**与**局限清单**。
> 算法内容见 [算法技术文档](algorithm.md)，工程架构见 [技术方案](technical-design.md)。

---

## 0. 一句话总览

**用 APScheduler 3.x 的 `BackgroundScheduler`，在 FastAPI 同一个进程内跑 6 个 cron 任务**，负责"什么时候算"；
**"算什么、算完对不对"全部交给 service 层算法**（幂等、覆盖、去重都在算法里，不在调度里）。

```
[触发器 cron] → [调度器进程内线程] → [job 函数] → [_with_db 开/关会话] → [service 层的幂等逻辑]
     ↑时间                    ↑谁执行              ↑干什么         ↑资源管理            ↑正确性保证
```

**这张图是整篇文档的骨架**：调度框架只解决"时间点 + 谁来跑 + 别跑重"，它**不解决数据正确性**。

---

## 1. 技术选型：为什么是 APScheduler，而不是别的

| 方案 | 机制 | 本项目是否采用 | 原因 |
|---|---|---|---|
| 系统 crontab | OS 级定时 | 否 | 定时逻辑散落在部署脚本外，无法跟着数据库/容器一起交付；改时间要登上机器 |
| 独立调度容器（如 cron 容器调 API） | 容器内 curl 触发 | 否 | 多一个容器；任务与 service 函数隔了一层 HTTP，改参数要同时改两处 |
| Celery + beat | 消息队列 + worker + 独立 beat 进程 | 否（未来演进） | 需要 Redis 做 broker、要单独 worker 容器；本项目任务**总数 6 个、单机、无横向扩展需求**，太重 |
| **APScheduler（本项目）** | **进程内线程调度** | **是** | 零额外组件、与 FastAPI 同生共死、任务函数直接调用 service、代码即配置 |

**选取标准**：任务数量少 + 单机 + 不需要分布式 → **进程内调度器**的复杂度最低。

**讲解要点**
1. 框架选型的第一问不是"哪个强"，而是"**我的部署形态是什么**"。这套判断逻辑要讲出来：
   单机少任务 → 进程内；多机/高并发/需可靠投递 → 独立 broker + worker。
2. Celery 的"heavy"体现在哪：**broker（Redis）+ result backend + worker 进程 + beat 进程**四件套，
   而 APScheduler 只多了一个**后台线程**。
3. 反过来说，进程内调度器**失去了什么**（这是下一步演进的原因）：
   没有任务持久化、没有分布式锁、没有任务状态查询、进程重启即"失忆"。

**依赖版本**：`apscheduler>=3.10,<4.0`（[requirements.txt](../backend/requirements.txt#L10)）。
> 注意 3.x 与 4.x 是**两套 API**（4.x 改为 `Scheduler` + `add_schedule`，且长期未正式发布）。
> 本项目用 3.x API（`BackgroundScheduler` / `add_job`），**锁死上界 `<4.0` 就是在防这个破坏性升级**。

---

## 2. 核心概念：调度器只有四个零件

APScheduler 3.x 的模型可以拆成四个角色，**用这四个词去讲，比讲代码更清楚**：

| 零件 | 本项目取值 | 作用 |
|---|---|---|
| **Trigger（触发器）** | `cron`（7 处）、`day_of_week/hour/minute` | 决定"下一次什么时候跑" |
| **Job（任务）** | `_intraday_chain`、`sync_money_flow`、`run_monitor_job` … | 一个可调用对象 |
| **Executor（执行器）** | 默认 `ThreadPoolExecutor` | 在**线程**里执行 job（不是进程、不是 asyncio） |
| **JobStore（任务存储）** | 默认 `MemoryJobStore`（未显式配置） | 任务**只存在内存**，进程重启就没了 → 靠 `startup` 时重新 `setup_jobs()` 重建 |

配套的**调度器实例**只有一个（[scheduler.py](../backend/app/scheduler.py#L12)）：

```python
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
```

- `BackgroundScheduler` = 前台线程之外的**后台线程**跑调度循环，不会阻塞 FastAPI 主线程。
- `timezone` 显式指定，**和容器 `TZ=Asia/Shanghai`、数据库时区三者对齐**（见 §4.3）。

**讲解要点**
1. **"线程执行"这个事实决定了三件事**：任务函数可以是普通同步函数（不用 async）；同一任务默认不会并发重入（`max_instances` 兜底）；任务里**阻塞数据库/网络会占住线程池的线程**。
2. **`MemoryJobStore` 是"代码即配置"的另一面**：任务表不在数据库里，重启后靠代码重建。
   好处：改时间只需改代码 + 重启；坏处：**运行时动态增删的任务会丢**。
3. 为什么不用 async 函数：APScheduler 3.x 的 `ThreadPoolExecutor` 执行的是同步可调用对象；
   混入 `async def` 需要额外包装，本项目统一用同步函数（`lambda: _with_db(...)`）。

### 2.1 触发器的翻译练习：把代码读成时间规则

把 [setup_jobs](../backend/app/scheduler.py#L44-L77) 的写法读成自然语言，这是**最容易被考的基础题**：

| Job id | cron 表达式（代码） | 自然语言 | 实际触发时刻（工作日） |
|---|---|---|---|
| `intraday_run` | `mon-fri h=14 m=0` | 每周一至五 14:00 | 14:00 |
| `sync_quotes` | `mon-fri h=15 m=40` | 每周一至五 15:40 | 15:40 |
| `screen` | `mon-fri h=16 m=10` | 每周一至五 16:10 | 16:10 |
| `calc_zones` | `mon-fri h=16 m=15` | 每周一至五 16:15 | 16:15 |
| `sync_money_flow` | `mon-fri h=16 m=20` | 每周一至五 16:20 | 16:20 |
| `monitor` | `mon-fri hour="9-14" minute="*/30"` | 每周一至五 9 点至 14 点内每 30 分钟 | **9:00, 9:30, 10:00 … 14:00, 14:30（每日 12 次）** |
| `sync_fundamentals` | `sat h=9 m=0` | 每周六 09:00 | 09:00 |

**`hour="9-14", minute="*/30"` 的展开是重点**：这是**范围 + 步长**的组合，等于
`{9,10,11,12,13,14} × {0,30}` = 12 个触发点。

> 注意两个必须讲清的细节（也是文档内外口径不一致的地方）：
> - 前端任务页写的是「9:30-15:00 每 30 分钟」，**实际是 9:00-14:30**（浏览器实测见 [Jobs.vue](../frontend/src/views/Jobs.vue#L115) 与代码的差异）。
> - 其中 **11:30 / 12:00 / 12:30** 落在午休时段：此时快照未更新 → 同一价格 → 被 Redis 去重拦下，**无副作用但不做无用功更省**。

**踩坑提醒**：APScheduler 的 cron 字段**不认"交易日历"**。
`mon-fri` 只排除周末，**法定节假日照跑**——国庆当天 15:40 依然会去拉行情（拿到空数据或旧数据）。

---

## 3. 任务全景：一条时间线 + 两条链路

### 3.1 时间线（按日内顺序）

```
09:00 ─┬─ 09:30 ─ 10:00 … 14:00 ─ 14:30 (隔 30 分钟, monitor)
       │                        │
       │                        └─ 14:00 盘中链（1-2 分钟）
       │                             快照 → 资金流 → 筛选 → 区间 → 监控
       │
15:40 ─ 收盘行情入库（全市场日线，6 线程，15-20 分钟）
16:00 ─  ↑ 预计结束
16:10 ─ 选股筛选（官方口径）
16:15 ─ 安全区间（官方口径）
16:20 ─ 资金流收盘官方数据（覆盖当日盘中记录）

周六 09:00 ─ 全市场基本面同步（财务 + 估值，数小时）
```

时间点不是随意的，**三个间隔都是"给前一个任务让出资源"**：

| 间隔 | 为什么要留 |
|---|---|
| 15:40 → 16:10（30 min） | 行情同步实测 15-20 分钟，**必须留足**，否则 16:10 的筛选会读到只同步了一半的日线 |
| 16:10 → 16:15（5 min） | 筛选写 `stock_pool`，区间计算**依赖当前 active 股池**，必须先选完再算 |
| 16:15 → 16:20（5 min） | 区间与资金流互不依赖，错开只为减少数据库/外网瞬时压力 |

**讲解要点**：调度表里**任务间的隐式依赖**是最容易踩也最容易被忽略的地方。
`calc_zones` 依赖 `screen` 的结果、`monitor` 依赖 `calc_zones` 的结果——**这些依赖靠"时间先后"保证，而不是靠"调用链"保证**。
一旦 15:40 的行情同步拖到 16:11 还没结束，16:10 的筛选就会基于旧数据跑（**时间依赖是脆弱的**）→ 这就是局限 §9 里"应换成显式依赖（DAG）"的由来。

### 3.2 两条链路的语义差异（盘中 vs 收盘）

| | 盘中链（14:00） | 收盘链（15:40 / 16:10 / 16:15 / 16:20） |
|---|---|---|
| 价格来源 | `intraday_spot` 快照 | `daily_quotes` 官方日线 |
| 数据标记 | `source = "intraday"` | `source = "official"` |
| 目的 | 收盘前拿到结论，**留交易窗口** | 沉淀当日最终口径 |
| 覆盖关系 | 被收盘链覆盖 | 覆盖盘中链 |

**为什么要 14:00 跑而不是等收盘**：等收盘算完，当天已经没有决策余地了。
**为什么不担心盘中数据污染**：三层隔离——快照只进 `intraday_spot` 表（不写日线）、结果带 `source` 标记、收盘链覆盖盘中（细节见 [algorithm.md §3.5 / §5](algorithm.md)）。

**讲解要点（常被追问）**：14:00 跑出来的结论**当天可能失效**——14:00 到收盘还有 1 小时行情。
这是**刻意的取舍**：要"来得及决策"就必须接受"数据不最终"。文档和前端文案都要写清"盘中结论可能随收盘变化"。

### 3.3 一条链 = 一个会话：`_intraday_chain` 的设计

[`_intraday_chain`](../backend/app/scheduler.py#L25-L41) 五步**共用同一个 `SessionLocal()`**：

```python
db = SessionLocal()
try:
    sync_spot_snapshot(db)          # 快照
    money_flow.sync_money_flow(db, intraday=True)   # 资金流
    run_screen(db, intraday=True)   # 筛选
    calc_zones(db, intraday=True)   # 区间
    check_and_notify(db)            # 监控
except Exception as e:
    log.exception(...)
finally:
    db.close()
```

- **共用一个会话的理由**：五步之间的 SQLAlchemy 身份映射/缓存一致，且只需开一次连接。
- **代价**：会话在整条链期间不释放连接（1-2 分钟里占用一个数据库连接）；5 个 service 各自 `commit`，链中间失败时**前面的已提交、后面的没有**（不是原子事务）。

**讲解要点**：能主动说出"**这条链不是事务，是'尽力而为的流水线'**"就很到位。
要不要改成原子？**不要**——同步类任务按步提交是刻意设计，因为外网抓取不可回滚，"已同步的数据不要丢"比"要么全成要么全不成"更重要。

---

## 4. 六个关键设计决策（逐条讲"为什么"）

### 4.1 进程内调度 → 单 uvicorn worker 是**硬前提**

启动方式（[docker-entrypoint.sh](../backend/docker-entrypoint.sh#L8)）：

```sh
exec uvicorn app.main:app --host 0.0.0.0 --port 8000     # 注意：没有 --workers
```

**为什么这是硬约束**：`BackgroundScheduler` 是**进程内**的。
如果改成 `--workers 4`，FastAPI 会起 **4 个独立进程**，**每个进程都执行一次 `startup` → 各自注册并调度同一批任务** → 同一时间点任务被执行 4 次。

后果具体是什么：
- 15:40 的行情同步**并发 4 倍**（每个进程 6 线程 = 24 线程）→ 直接触发数据源限流；
- 提醒可能重复推送（虽然 Redis `SET NX` 能兜住，但这是**第二道防线**，不该依赖）；
- 数据库写入量放大 4 倍。

**讲解要点（高分点）**：这是"**进程内调度 + 多进程部署**"的经典冲突。
解法有三条，讲出来比只报"我们只开 1 个 worker"更像懂行：
1. 单 worker（本项目现状，最简单）；
2. 引入**分布式锁**（Redis `SET NX`）包住 job 入口，多 worker 时只有一个抢到；
3. 把调度**外移到独立进程/容器**（Celery beat 或单独一个 `scheduler` 容器），Web 侧只保留 API。

### 4.2 会话生命周期统一收敛到 `_with_db`

[`_with_db`](../backend/app/scheduler.py#L15-L22) 是所有"单步任务"的统一外壳：

```python
def _with_db(fn, **kwargs):
    db = SessionLocal()
    try:
        return fn(db, **kwargs)
    except Exception as e:
        log.exception("scheduled job failed: %s", e)   # 吞掉异常，不让调度线程崩
    finally:
        db.close()                                      # 一定关会话
```

三个作用必须讲全：
1. **开/关会话**：`finally` 保证异常路径也不泄漏连接池；
2. **异常隔离**：`log.exception` 记录堆栈后**不外抛**——APScheduler 的线程执行器里抛异常只会打日志，但显式捕获能保证 `finally` 一定执行；
3. **参数注入**：用 `functools` 式的 `lambda: _with_db(datafeed.sync_all_quotes, years=5)` 把参数固化在注册处，避免 job 函数签名被调度器参数污染。

**不一致点（可以作为"代码洁癖"加分项主动指出）**：
`monitor` 任务注册的是 [`run_monitor_job`](../backend/app/services/monitor.py#L89-L95)，它**自己开了会话**，没有走 `_with_db`。
两处写法并存是历史原因，**统一成一种外壳**是可做的改进。

**为什么不用 FastAPI 依赖注入 `Depends(get_db)`**：调度器执行时**没有请求上下文**，
依赖注入体系不生效，所以必须手工 `SessionLocal()`——这是"**同一份 service 函数同时服务 API 与调度**"的代价。

### 4.3 时区三重对齐（一个静默杀手）

| 层 | 设置 | 位置 |
|---|---|---|
| 容器 OS | `TZ=Asia/Shanghai` | [Dockerfile](../backend/Dockerfile#L4) |
| APScheduler | `BackgroundScheduler(timezone="Asia/Shanghai")` | [scheduler.py](../backend/app/scheduler.py#L12) |
| 数据库 | db 服务 `TZ=Asia/Shanghai` | [docker-compose.yml](../docker-compose.yml#L10) |

**为什么必须三重对齐**：容器默认是 **UTC**。
- APScheduler 时区错 → `hour=15, minute=40` 会在**北京时间 23:40** 触发；
- 容器 OS 时区错 → service 里的 `date.today()`（[safety_zone](../backend/app/services/safety_zone.py#L156)、[money_flow](../backend/app/services/money_flow.py#L121)）算出**前一天/后一天**，写进数据库的 `trade_date` 全部错位。

**讲解要点**：这是"**没报错但数据全错**"的最典型缺陷——**时区问题不会抛异常，只会让日期静默偏移一天**。
实测踩坑记录：容器时钟曾为 **2026-10-15 08:0x（盘前）**，导致"当日"判断与预期不符，最终决定盘前值不写入当日（见 [algorithm.md §5](algorithm.md)）。

### 4.4 三个"自保参数"：`max_instances` / `misfire_grace_time` / `replace_existing`

每个 job 都显式写了这三项（`monitor` 除外，见下）：

```python
scheduler.add_job(..., id="sync_quotes", replace_existing=True,
                  max_instances=1, misfire_grace_time=3600)
```

| 参数 | 取值 | 语义 | 防的问题 |
|---|---|---|---|
| `max_instances` | `1` | 同一 job **最多 1 个实例在跑** | 15:40 的行情同步超过 24 小时没结束，第二天 15:40 又起一个 → 两个同时跑（双倍限流+双写） |
| `misfire_grace_time` | 1800 / 3600 / 86400 秒 | 到点没跑成，**多久内补跑仍有效** | 容器重启 / 前一个任务占满线程池导致错过触发点 |
| `coalesce`（默认 `True`） | 未显式写 | 补跑时**多次错过合并成一次** | 停机 3 天，补跑时把 3 次行情同步排队依次跑 |
| `replace_existing` | `True` | 同 id 重复注册时**覆盖旧定义** | 防止重复 `setup_jobs()` 抛 `ConflictingIdError` |

**`misfire_grace_time` 的取值逻辑（讲出来很加分）**：不是随便给，而是**按任务的"时效性要求"给**。

| Job | grace | 为什么是这个量级 |
|---|---|---|
| `intraday_run` / `screen` / `calc_zones` / `sync_money_flow` | 1800 秒（30 min） | 半小时内补跑，数据还算"今天"的 |
| `sync_quotes` | 3600 秒（1 h） | 收盘行情补跑 1 小时内仍合理 |
| `sync_fundamentals` | 86400 秒（1 天） | 基本面一周一次，晚一天无所谓 —— **补跑时间 ≤ 任务周期** |
| `monitor`（未写） | **默认 1 秒** | ⚠️ 见下 |

**三个教学细节**
1. **`misfire_grace_time` 的合理上界 ≈ 任务自身周期**：基本面任务周期是 7 天，给 1 天完全够；给 7 天就会"补跑上一次的旧活"。
2. **`monitor` 没写 `misfire_grace_time`** → 用 APScheduler 默认值 **1 秒**。
   含义：启动/阻塞晚 2 秒就**直接跳过这次触发**（对 30 分钟一次的监控影响很小，但**这是显式缺省，应该补齐**）。
   另外它连 `max_instances` 也没写（默认恰好是 1，所以暂无风险）——**能讲出"默认值恰好等于期望值"和"显式优于隐式"**。
3. **`coalesce` 与 `misfire` 是一对**：`coalesce=True` 才让"停机期间漏的 N 次"合并成 1 次；配 `max_instances=1` 才不会补跑堆积。**三者是一组"防堆叠"的组合拳。**

### 4.5 调度器不保证正确性 → 幂等全在 service 层

**这是本框架最重要的设计分工**，必须一句话讲透：

> 调度器只管"**触发**"，不管"**触发几次、结果对不对**"。

所以每个任务在被重复触发时都必须**幂等**，而幂等的实现**全部在算法层**：

| 任务 | 重复触发会怎样 | 幂等机制所在 |
|---|---|---|
| 行情/基本面同步 | 重复 upsert 同日数据 | `on_conflict_do_update`（唯一约束 `(code, trade_date)`） |
| 筛选 | 重复写 `stock_pool` | 先把旧批次 `active=False` 再写新批次 |
| 区间 | 同日重算会**唯一键冲突** | **先删当日再插**（[safety_zone.py L160-L161](../backend/app/services/safety_zone.py#L160-L161)） |
| 资金流 | 盘中值覆盖收盘官方值 | `official_exists` 守卫 + `official` 覆盖 `intraday` 的 upsert |
| 提醒 | 同一只票当天重复推送 | Redis `SET NX EX 86400` |

**讲解要点**：**"手动按钮"和"定时触发"调用的是同一个 service 函数**（见 §5），
所以**幂等设计顺带覆盖了手动路径**——这是"幂等做在 service 层、不做在调度层"的最大收益。
反过来说，如果幂等做在 API 层，定时链路就会绕过去、重复数据照样产生。

### 4.6 长任务与短任务的执行形态差异

| | 手动触发端点 | 定时触发 |
|---|---|---|
| 执行者 | FastAPI 的**线程池**（`def` 端点由 Starlette 丢进 threadpool） | APScheduler 的**线程池** |
| 会阻塞谁 | 只占一个 worker 线程，**不阻塞事件循环** | 只占一个调度线程 |
| 超时 | 受客户端超时约束（前端 30 分钟，见 §5） | 无超时，靠 `max_instances` 防堆叠 |

**讲解要点**：为什么 `jobs.py` 里的端点都写成 `def` 而不是 `async def`？
——因为它们是**同步阻塞的耗时任务**。写成 `async def` + 同步阻塞调用，会**卡死事件循环**，整个服务无响应。
`def` + Starlette 线程池 = 明确把阻塞工作放到工作线程。**这是 FastAPI 里的高频面试点。**

---

## 5. 手动触发通道：代码复用的样子

### 5.1 API 端点（[api/jobs.py](../backend/app/api/jobs.py)）

| 端点 | 调用 | 耗时量级 |
|---|---|---|
| `POST /api/jobs/sync-quotes?years=5` | `datafeed.sync_all_quotes` | 15-20 分钟 |
| `POST /api/jobs/sync-fundamentals?years=5` | `datafeed.sync_all_fundamentals` | 数小时 |
| `POST /api/jobs/sync-one/{code}` | `datafeed.sync_stock_fundamentals` | 秒级 |
| `POST /api/jobs/screen` | `screener.run_screen` | 分钟级 |
| `POST /api/jobs/calc-zones` | `safety_zone.calc_zones` | 1-2 分钟 |
| `POST /api/jobs/intraday` | **五步串起来**（= 调度链的手动版） | 1-2 分钟 |
| `POST /api/jobs/money-flow?intraday=false` | `money_flow.sync_money_flow` | 秒级 |
| `POST /api/jobs/monitor` | `monitor.check_and_notify` | 秒级 |

**`/intraday` 与 `_intraday_chain` 是"同一逻辑的两副皮"**：

| | 定时链 `_intraday_chain` | 手动端点 `/intraday` |
|---|---|---|
| 会话 | 自己开 `SessionLocal()` | 用 `Depends(get_db)` |
| 步骤顺序 | 快照 → 资金流 → 筛选 → 区间 → 监控 | **完全相同** |
| 返回值 | 只写日志 | 返回 JSON（`spot/money_flow/screen/zones/monitor`） |

**讲解要点**：这里存在**重复实现**——同一条链写了两遍。
理想做法是把"链"抽成一个 service 函数（如 `intraday_chain(db)`），调度器注入会话调用它，API 注入会话也调用它。
**现状是"一处改、另一处可能漏改"的隐患**，属于可指出、可改进的点（局限 §9）。

### 5.2 前端：为什么要单独一个 axios 实例

[api.js](../frontend/src/api.js#L3-L5)：

```js
const api    = axios.create({ baseURL: '/', timeout: 15000 })     // 普通查询 15 秒
const jobApi = axios.create({ baseURL: '/', timeout: 1800000 })  // 任务触发 30 分钟
```

**为什么要两套**：全局 15 秒超时对"同步行情（15-20 分钟）"必然超时。
**但这里要说清一个陷阱（加分点）**：前端超时**不代表后端停止**——axios 只是放弃等待，
后端线程仍在跑完；用户看到"失败"，其实任务成功、数据已入库。
**这是"客户端超时 ≠ 服务端取消"**，真正要"取消"需要服务端支持（如 `asyncio.CancelledError` / 任务状态查询 / 后台任务队列）。

任务页（[Jobs.vue](../frontend/src/views/Jobs.vue)）除了按钮，还把**时间表硬编码**在页面里
（[L109-L117](../frontend/src/views/Jobs.vue#L109-L117)）：
**这是一处"文档与代码双份维护"**——改调度时间要同时改 `scheduler.py` 与 `Jobs.vue`，
否则前端展示的就是错的时间表（**已实测存在不一致**，见 §2.1 的监控时间）。

---

## 6. 失败、容错与重启行为

### 6.1 三层容错，各管一段

| 层 | 手段 | 覆盖的失败 |
|---|---|---|
| **调度层** | `max_instances=1` / `misfire_grace_time` / `replace_existing` | 并发重入、错过触发、重复注册 |
| **任务层** | `_with_db` 的 `try/except/finally`；`_intraday_chain` 外层兜底 | 会话泄漏、单任务异常打崩链路 |
| **数据层** | 单条 `try/except` + 单条 `rollback`（同步任务）、单只异常跳过 | 单只股票脏数据不中断整批（约定见项目记忆） |

**讲解要点**：**"失败要在最接近失败点的地方被吸收"**。
外网抓取的单只失败 → 就地跳过并记 warning（不要抛到批任务）；
整批失败 → 任务层记 `log.exception`；
**只有"整个任务崩了"才应该体现在日志级别上（ERROR）**，避免日志噪声淹没真问题。

### 6.2 重启行为（进程内调度器最需要讲清的一点）

```
容器重启
  → entrypoint: alembic upgrade head    （先保证表结构最新）
  → uvicorn 启动 → FastAPI startup 钩子 → scheduler.start()
      → setup_jobs(): 重新注册 7 个 job（replace_existing=True）
      → scheduler.start(): 启动后台调度线程
```

- **任务定义不落库**，重启后**由代码重建**（`MemoryJobStore` 的必然结果）。
- **错过的时间点不会自动补**：靠 `misfire_grace_time` 判定。
  例：15:40 容器正在重启，15:41 调度器就绪 → 若在 grace（3600 s）内，**会立刻补跑**。
- **重启前正在执行的任务直接消失**（线程随进程终止），**没有"重新入队"机制**。

**讲解要点**：这就是"进程内调度 + 无持久化 jobstore"的代价。
**关键任务不能指望它绝对不丢**——要么接受"漏了就漏了，等下一次"（本项目现状：日频任务漏一次影响可控），
要么演进到需要持久化与重试的框架（Celery + Redis 投递保证）。

### 6.3 FastAPI 生命周期写法的时代差异

[main.py](../backend/app/main.py#L32-L39) 用的是 `@app.on_event("startup"/"shutdown")`：

```python
@app.on_event("startup")
def on_startup():
    scheduler.start()
```

**要能指出这是旧 API**：FastAPI 已推荐改用 `lifespan` 上下文管理器（`@asynccontextmanager`）。
新式写法里 `scheduler.start()` 与 `scheduler.shutdown()` 在同一个 `async with` 块内，
语义更清晰（**startup 里取到的资源，能在 shutdown 里成对释放**）。
本项目功能上没问题，属于**技术债性质的写法陈旧**。

---

## 7. 可观测性：现在有什么、缺什么

**现状（只有日志）**：

| 能看到的 | 位置 |
|---|---|
| 任务开始/结束 | `log.info("intraday chain done")`、`log.info("screen done: %d selected")` |
| 任务失败 | `_with_db` 的 `log.exception("scheduled job failed: %s")` |
| 执行结果 | 手动触发时前端把 JSON 原样打印（`<pre>{{ result }}</pre>`） |
| 调度表 | 前端硬编码展示 |

**缺什么（诚实清单）**：

1. **没有任务状态查询接口**：无法回答"昨天 16:10 的筛选成功了没？"→ 只能 `docker compose logs backend`。
2. **没有执行历史表**：`job_runs(id, job_id, started_at, finished_at, status, result)` 这样的表没有；
   于是**"最近一次成功时间"无法在页面上展示**。
3. **没有失败告警**：任务连续失败不会通知任何人（**只有业务提醒通道，没有运维告警通道**）。
4. **没有 `/api/health` 之外的存活探针**：调度线程死掉不会被发现（`health` 只返回 `ok`，不含 `scheduler.running`）。

**讲解要点**：能主动说"**我能观测到的是日志、不是任务状态**"，
并给出最小改进（`job_runs` 表 + `GET /api/jobs/runs` + 将 `scheduler.running` 纳入 health），说明对生产化的理解。

---

## 8. 常见陷阱清单（踩过 / 必须避免）

| # | 陷阱 | 后果 | 规避 |
|---|---|---|---|
| 1 | `uvicorn --workers N` | 同一任务**执行 N 次** | 保持单 worker，或加分布式锁 / 外移调度 |
| 2 | 容器 `TZ` 未设（默认 UTC） | 触发时间整体偏移、`date.today()` 写错日期 | 三重时区对齐（§4.3） |
| 3 | 长任务排在短任务之前且间隔不足 | 后一个任务读到**半成品数据** | 留足间隔（15:40 → 16:10）或用依赖编排 |
| 4 | 忘记 `max_instances=1` | 上一轮没跑完，下一轮又起 → 双倍抓取被限流 | 显式声明 |
| 5 | 忘记 `misfire_grace_time` | 默认 **1 秒**，稍晚即跳过该次触发 | 按任务周期显式设置 |
| 6 | 在 job 里抛异常不捕获 | 会话泄漏（连接池耗尽） | `_with_db` 的 `finally: db.close()` |
| 7 | 用 `async def` 端点跑阻塞任务 | **事件循环被卡死**，全服务无响应 | 同步任务写 `def`，交给线程池 |
| 8 | 依赖"前端 30 分钟超时"来判定任务失败 | 前端报错但后端仍在跑，**状态判断错** | 区分"客户端超时"与"服务端失败" |
| 9 | 假定"所有任务都幂等" | 重复触发产生重复/冲突数据 | 幂等必须逐个任务落实在 service 层 |
| 10 | 认为 `mon-fri` 会跳过节假日 | 节假日照跑，抓空数据、发无效推送 | 引入交易日历判断 |
| 11 | 调度表同时在代码与前端各写一份 | 两处不一致（**已发生**） | 单一数据源：后端提供 `GET /api/jobs/schedule` |
| 12 | 用 `cron` 表达"每 30 分钟"时误用 `*/30` | 实际触发点为 9:00-14:30，与文案不符 | 明确范围+步长的展开（§2.1） |

---

## 9. 局限与演进路线（诚实清单）

1. **无分布式能力**：进程内调度 + 单 worker，**无法横向扩展**。
2. **任务定义无持久化**：重启靠代码重建，**运行时动态任务会丢**，无执行历史。
3. **依赖靠"时间先后"，不靠"显式编排"**：前一个任务超时 → 后一个读到旧数据。
4. **无交易日历**：节假日照跑。
5. **无任务状态/失败告警**：只有日志，失败无人知。
6. **盘中链与手动端点两份实现**：改动有漏改风险；调度表也要与前端双份维护。
7. **`monitor` 的 grace 用了默认 1 秒**，属隐式缺省。
8. **生命周期用旧 API**（`on_event`），未迁移到 `lifespan`。

**演进优先级（按"代价/收益"排）**：

| 优先级 | 动作 | 收益 |
|---|---|---|
| 1 | 把"链"抽成 service 函数（`intraday_chain(db)`），调度与 API 共用 | 去掉重复实现，消除漏改风险 |
| 2 | 调度表由后端出接口（`GET /api/jobs/schedule`），前端只渲染 | 消除文档/页面的双份维护 |
| 3 | 加 `job_runs` 表 + `GET /api/jobs/runs` + 把 `scheduler.running` 并入 health | 可观测性从 0 到 1 |
| 4 | 引入交易日历过滤 | 节假日不做无用抓取与推送 |
| 5 | 任务失败时走通知通道告警（复用 `notifier`） | 失败无人知 → 有人知 |
| 6 | 需要多副本时：Redis 分布式锁（`SET NX`）包住 job 入口 | 解锁横向扩展 |
| 7 | 需要可靠投递/重试/可视化管理时：**Celery + beat（持久化 schedule + worker）** | 任务不丢、可重试、有 UI |

**讲解要点**：演进路线要讲清"**什么时候才该上 Celery**"——
当出现"多副本部署 / 任务不能丢 / 需要重试与可视化"任一条时，进程内调度就该退役了。
**在单机日频场景（本项目）里，APScheduler 是复杂度最低的正确选择**——这正好呼应 §1 的选型逻辑。

---

## 10. 参数速查表（被问到数值可直接查）

| 项 | 值 | 位置 |
|---|---|---|
| 调度器类 / 时区 | `BackgroundScheduler` / `Asia/Shanghai` | [scheduler.py L12](../backend/app/scheduler.py#L12) |
| JobStore / Executor | 默认 `MemoryJobStore` / 默认 `ThreadPoolExecutor`（均未显式配置） | — |
| 依赖版本 | `apscheduler>=3.10,<4.0` | [requirements.txt L10](../backend/requirements.txt#L10) |
| 容器 OS 时区 | `TZ=Asia/Shanghai` | [Dockerfile L4](../backend/Dockerfile#L4) |
| uvicorn worker 数 | **1（未指定 `--workers`）** | [docker-entrypoint.sh L8](../backend/docker-entrypoint.sh#L8) |
| 任务总数 | **7 个**（6 个定时点 + 1 个监控高频任务；`monitor` 每 30 分钟 × 12 次/日） | [setup_jobs L44-L77](../backend/app/scheduler.py#L44-L77) |
| 盘中链时间 | 工作日 14:00 | `intraday_run` |
| 行情同步时间 | 工作日 15:40（`years=5`，6 线程） | `sync_quotes` |
| 筛选 / 区间 / 资金流时间 | 工作日 16:10 / 16:15 / 16:20 | `screen` / `calc_zones` / `sync_money_flow` |
| 监控触发点 | 工作日 9:00–14:30 每 30 分钟（共 12 次） | `monitor` |
| 基本面同步时间 | 周六 09:00（`years=5`，4 线程） | `sync_fundamentals` |
| `max_instances` | 1（显式写在 6 个 cron 任务上；`monitor` 未写，用默认 1） | `add_job(...)` |
| `misfire_grace_time` | 盘中链/筛选/区间/资金流 1800 s；行情 3600 s；基本面 86400 s；**监控默认 1 s** | `add_job(...)` |
| `coalesce` | 默认 `True`（未显式写） | — |
| `replace_existing` | `True` | `add_job(...)` |
| 前端任务超时 | 30 分钟（`jobApi`），普通查询 15 秒（`api`） | [api.js L3-L5](../frontend/src/api.js#L3-L5) |
| 提醒去重 TTL | 86400 秒（`SET NX EX`） | [monitor.py L25-L28](../backend/app/services/monitor.py#L25-L28) |

---

## 11. 十分钟讲解提纲

| 时长 | 内容 | 一句话开场 |
|---|---|---|
| 1 min | 全景：四个零件 + 职责分工图 | "调度框架只解决'什么时候跑、谁来跑、别跑重'，**正确性全在算法层**。" |
| 1 min | 选型对比（crontab / Celery / APScheduler） | "单机 6 个任务，进程内调度器的复杂度最低；上 Celery 要四件套。" |
| 2 min | 任务时间线 + 两条链路 + 时间间隔的理由 | "时间点不是排着好看：15:40 到 16:10 那 30 分钟，是给行情同步留的。" |
| 2 min | 三个自保参数（重点） | "`max_instances` 防重入、`misfire_grace_time` 防漏跑、`coalesce` 防堆积——**一组组合拳**。" |
| 2 min | 单 worker 硬约束 + 时区三重对齐（重点） | "改成 4 个 worker，同一批任务就会跑 4 遍；TZ 不设，任务会在半夜 23:40 触发。" |
| 1 min | 幂等与手动触发复用 | "定时和手动调的是同一个 service 函数，所以幂等做在 service 层，顺带覆盖了手动路径。" |
| 1 min | 局限与演进 | "进程内调度换不来横向扩展；等需要多副本或任务不能丢，就该换 Celery。" |

**讲"三个自保参数"和"单 worker + 时区"时一定要给具体后果**——
"会重复跑 4 次""会在 23:40 触发""日期静默错一天"这类**可验证的后果**，比背参数含义有说服力得多。

---

## 12. 常见提问与回答要点

| 提问 | 回答要点 |
|---|---|
| 为什么不用 crontab / Celery？ | 单机 + 6 个任务 → 进程内最省；crontab 脱离交付物，Celery 要 broker+worker+beat 四件套 |
| APScheduler 四个零件是什么？ | Trigger（何时）/ Job（做什么）/ Executor（谁执行：线程池）/ JobStore（存哪：内存） |
| 为什么必须单 worker？ | 调度器是**进程内**的；多 worker = 多份调度器 = 同一任务执行 N 次（双倍限流、双写） |
| 任务定义存在哪？ | 内存 JobStore，**不落库**；重启后由 `setup_jobs()` 按代码重建（`replace_existing=True`） |
| 容器重启，错过的任务会补跑吗？ | 看 `misfire_grace_time`：在 grace 内**会立即补跑**；`coalesce=True` 把多次错过合并成一次 |
| `monitor` 为什么没有 grace 配置？ | 漏写 → 用默认 **1 秒**，晚 2 秒即跳过；因 30 分钟一次影响可控，但属应补齐的隐式缺省 |
| 任务失败了会怎样？ | 抛异常被 `log.exception` 记日志后吞掉，`finally` 关会话；**没有重试、没有告警、没有状态记录** |
| 任务之间怎么保证顺序？ | **靠时间间隔，不靠调用链**——所以前一个超时，后一个会读到旧数据（这是弱点） |
| 为什么端点写 `def` 不写 `async def`？ | 同步阻塞任务写 `async def` 会卡死事件循环；`def` 由 Starlette 丢线程池执行 |
| 前端超时报错，任务真的失败了吗？ | 不一定。**客户端超时 ≠ 服务端取消**，后端线程会继续跑完，数据照常入库 |
| 为什么 14:00 跑盘中链而不是等收盘？ | 收盘算完就没决策窗口了；代价是结论可能被最后 1 小时行情推翻——**刻意的取舍** |
| 怎么扩展到多副本？ | 三条路：保持单 worker / Redis 分布式锁包住入口 / 调度外移（Celery beat 或独立调度容器） |
| 什么时候该换 Celery？ | 出现"多副本 / 任务不能丢 / 需要重试与可视化"任一条时，进程内调度就该退役 |

---

## 13. 代码地图（讲的时候按这个顺序翻）

| 顺序 | 文件 | 讲什么 |
|---|---|---|
| 1 | [scheduler.py](../backend/app/scheduler.py) | 调度器实例、`_with_db`、`_intraday_chain`、`setup_jobs`、`start/shutdown` |
| 2 | [main.py](../backend/app/main.py#L32-L39) | startup/shutdown 钩子（旧 API，生命周期起点） |
| 3 | [docker-entrypoint.sh](../backend/docker-entrypoint.sh) | `alembic upgrade head` → 单 worker uvicorn |
| 4 | [Dockerfile](../backend/Dockerfile#L4) / [docker-compose.yml](../docker-compose.yml#L10) | `TZ` 时区对齐 |
| 5 | [api/jobs.py](../backend/app/api/jobs.py) | 手动触发端点（与定时链路复用同一批 service） |
| 6 | [monitor.py](../backend/app/services/monitor.py#L25-L28) | 提醒去重（Redis `SET NX EX 86400`） |
| 7 | [api.js](../frontend/src/api.js#L3-L5) / [Jobs.vue](../frontend/src/views/Jobs.vue) | 长超时实例、任务页与调度表 |
