# 数据库优化讲解文档（讲解版）

> 本文回答三个问题：**数据到底有多少？**（表数据量与增长）**现在做了什么优化？**（已有设计）
> **还能怎么优化？**（实测对照实验 + 待办清单）
> 文中每个数字都来自本项目数据库的实测（命令见 [§11 附录](#11-附录复现命令)），不用估算值充数。
> 算法内容见 [算法技术文档](algorithm.md)，调度框架见 [定时任务框架讲解文档](scheduler.md)，工程架构见 [技术方案](technical-design.md)。

---

## 0. 先看清"盘子"：数据库是什么、我们要优化什么

| 项 | 实测值 |
|---|---|
| 数据库 | PostgreSQL **16.15**（Docker 镜像 ParadeDB pg16），库名/用户均为 `stock` |
| 业务表 | **11 张**（另有约 40 张 PostGIS 自带空表，非业务） |
| 总数据量 | **约 2.0 GB**（≈ 1250 万行业务数据） |
| 最大两张表 | `daily_quotes` 1130 MB / `valuation_daily` 923 MB，**合计占全库 96%** |
| 索引占表空间比例 | 大表**索引 ≈ 堆（heap）的 90%**（daily_quotes：593 MB 堆 + 536 MB 索引） |
| 关键成本参数 | `shared_buffers=1985MB`、`work_mem=19MB`、`effective_cache_size=5957MB`、`random_page_cost=4`（**默认值，SSD 偏保守**） |
| 容器共享内存 | `/dev/shm` = **64MB**（Docker 默认，未调）→ **引发本库当前最严重的问题（见 §3）** |

**一句话概括本库的优化重点**：
**"两张千万级大表 + 索引和堆一样大 + 统计信息严重失真 + autovacuum 从未成功"**——
所以优化的主战场在 `daily_quotes` / `valuation_daily`，**其余 9 张表都在万行以下，优化它们收益近乎为零（还浪费写入）**。

**讲解要点**：先说"哪张表值得优化"，比一上来讲 SQL 技巧重要得多。
可以让学生猜"11 张表里哪几张需要优化"，答案是**只有 3 张（两张千万级 + 一张十万级）**，
**判断依据是"查询耗时是否由它主导"**，不是"表数量"。

---

## 1. 表的数据量（实测）

### 1.1 全表盘点（按总大小降序）

| 表 | 实测行数 | 总大小 | 堆(heap) | 索引(index) | 单行成本 | 说明 |
|---|---|---|---|---|---|---|
| `daily_quotes` | **6,215,688** | 1130 MB | 593 MB | 536 MB | ~182 B | 前复权日线（时序，**最大**） |
| `valuation_daily` | **6,219,340** | 923 MB | 402 MB | 521 MB | ~148 B | PE/PB 日度序列 |
| `fin_indicators` | 118,774 | 18 MB | 12 MB | 6.5 MB | ~155 B | 财务指标（按报告期） |
| `intraday_spot` | 5,923 | 1136 kB | 784 kB | 320 kB | ~196 B | 盘中快照（**单只一行，覆盖写**） |
| `stocks` | 5,574 | 464 kB | 288 kB | 144 kB | ~85 B | 股票基本信息 |
| `stock_pool` | 412 | 392 kB | 320 kB | 32 kB | **~951 B** | 股池快照（`reason` JSONB 大） |
| `safety_zones` | 202 | 288 kB | 184 kB | 64 kB | **~1.4 kB** | 安全区间（`detail` JSONB 大） |
| `money_flow` | 200 | 88 kB | 24 kB | 32 kB | ~440 B | 资金流 |
| `alert_logs` | 33 | 32 kB | 8 kB | 16 kB | — | 提醒记录 |
| `compare_analyses` | 3 | 32 kB | 8 kB | 16 kB | **~10.7 kB** | 对比分析（`items` JSONB 很大） |
| `alert_rules` | 0 | 32 kB | 8 kB | 16 kB | — | 提醒规则（未启用） |

### 1.2 数据画像（为什么是 621 万行）

`daily_quotes` 实测：

| 项 | 实测值 |
|---|---|
| 交易日跨度 | **1212 个交易日**，`2021-10-14` → `2026-10-14`（正好 **5 年**） |
| 股票数 | **5,574** 只 |
| 平均每只 | **1115 行**（最长 1212 行；次新股上市晚所以平均小于全量） |
| 交叉验证 | 5574 × 1212 × 92% ≈ 6.22 M ✅（与实测行数吻合） |

5 年这个数字**不是巧合**：`sync_all_quotes(years=5)` 的 `start = today - 5×365`（[datafeed.py L89](../backend/app/services/datafeed.py#L89)）。
同理 `valuation_daily` 也是 5 年 × 5574 只。

### 1.3 增长速率与容量推算

**每个交易日的新增**（按代码逻辑拆解）：

| 表 | 日增行数 | 依据 |
|---|---|---|
| `daily_quotes` | **≤5,574**（每只票 1 行新日期） | 断点续传只补新日期（[L86](../backend/app/services/datafeed.py#L86)） |
| `valuation_daily` | **≤5,574** | 同上 |
| `stock_pool` | **400** | 每日 2 次筛选（盘中 14:00 + 收盘 16:10）× 200 只 |
| `safety_zones` | **200** | 每日 1 次区间计算（同日先删后插） |
| `money_flow` | **200** | 每日 200 只 |
| `fin_indicators` | ~89 | 5574 × 4 期/年 ÷ 250 |
| `intraday_spot` / `stocks` | **0**（覆盖写） | 主键 upsert |

**年度与中期推算**（250 个交易日/年）：

| 表 | 年增行数 | 年增体积 | 1 年后 | 3 年后 |
|---|---|---|---|---|
| `daily_quotes` | +1.39 M | **+253 MB** | ~1.38 GB | ~1.9 GB |
| `valuation_daily` | +1.39 M | **+206 MB** | ~1.13 GB | ~1.5 GB |
| `stock_pool` | +100 k | +95 MB | ~0.5 GB | ~0.8 GB |
| `safety_zones` | +50 k | +71 MB | ~0.36 GB | ~0.5 GB |
| `money_flow` | +50 k | +22 MB | ~0.11 GB | ~0.15 GB |
| `fin_indicators` | +22 k | +3 MB | 基本不变 | 基本不变 |
| **合计** | **~2.9 M 行/年** | **~650 MB/年** | **~2.7 GB** | **~4.0 GB** |

**两个结论**
1. **总量可控**：每年 650 MB，10 年才 8 GB —— 属于"不需要分库分表"的量级（**这也是技术方案里"无需 TimescaleDB"的依据**）。
2. **但增长是无界的**：断点续传**只增不删**，5 年后表里会有**超过 5 年**的数据（`min_date` 不会前进）。
   → **需要"保留策略"**：要么定期归档，要么按年分区（见 §6 待优化 P3）。

**讲解要点**
- 算增长要会**从代码推**，不是猜：`断点续传 + 每日 250 交易日 + 每次 1 行` → 年增 1.39 M 行，这是可以手算出来的。
- **"数据量分级决定优化手段"**：千万级（重写查询、瘦身索引、分区）／十万级（顺手优化）／万级以下（**别动**）。

---

## 2. 索引现状：一多半空间被索引占掉

### 2.1 索引清单（含**实测物理大小**与扫描次数）

> `idx_scan` 是"自上次统计重置以来该索引被用于扫描的次数"，**它不能证明索引"没用"**（见 2.4）。

| 表 | 索引名 | 列 | 唯一 | 实测大小 | idx_scan |
|---|---|---|---|---|---|
| `daily_quotes` | `daily_quotes_code_trade_date_key` | (code, trade_date) | ✅ | **200 MB** | 162,099 |
| `daily_quotes` | `ix_daily_quotes_code_date` | (code, trade_date) | ❌ | **200 MB** | 74,770 |
| `daily_quotes` | `daily_quotes_pkey` | (id) | ✅ | **137 MB** | **0** |
| `valuation_daily` | `valuation_daily_code_trade_date_key` | (code, trade_date) | ✅ | **194 MB** | **0** |
| `valuation_daily` | `ix_valuation_daily_code_date` | (code, trade_date) | ❌ | **194 MB** | 2,203 |
| `valuation_daily` | `valuation_daily_pkey` | (id) | ✅ | **133 MB** | **0** |
| `fin_indicators` | `fin_indicators_code_report_date_key` | (code, report_date) | ✅ | 3.8 MB | 5,764 |
| `fin_indicators` | `fin_indicators_pkey` | (id) | ✅ | 2.7 MB | 0 |
| `intraday_spot` | `intraday_spot_pkey` | (code) | ✅ | 320 kB | 433 |
| `stocks` | `stocks_pkey` | (code) | ✅ | 144 kB | 8,281 |
| `money_flow` | `uq_money_flow_code_date` | (code, trade_date) | ✅ | 16 kB | 407 |
| `safety_zones` | `safety_zones_code_calc_date_key` | (code, calc_date) | ✅ | 32 kB | 464 |
| `stock_pool` | `stock_pool_pkey` | (id) | ✅ | 32 kB | 1 |
| `alert_logs` / `alert_rules` / `compare_analyses` | `*_pkey` | (id) | ✅ | 各 16 kB | 2 / 4 / 16 |

**合计约 16 个业务索引，其中大表索引占 1058 MB（536 + 521 MB）。**

### 2.2 发现 ①：大表上有一对**完全重复**的索引

| 表 | 唯一约束索引 | 规则索引 | 是否同列同序 | 合计浪费 |
|---|---|---|---|---|
| `daily_quotes` | `daily_quotes_code_trade_date_key` (code, trade_date) | `ix_daily_quotes_code_date` (code, trade_date) | **完全相同** | 200 MB |
| `valuation_daily` | `valuation_daily_code_trade_date_key` (code, trade_date) | `ix_valuation_daily_code_date` (code, trade_date) | **完全相同** | 194 MB |

**成因（值得讲）**：唯一约束会**自动创建一个索引**，而迁移/模型里又**额外 `create_index` 了一遍同样的列** → 磁盘上躺了两份一模一样的 B-tree。

**代价有两个，第二个更疼**：
1. **多占 394 MB**；
2. **每次 upsert 要多维护一个索引**——本项目每天对 5574 只票做 upsert（见 §5.3），**索引维护是写入放大的主要来源之一**，行情同步耗时 15-20 分钟里有一部分就花在这里。

**日常发现手段**（可教给学生）：

```sql
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' ORDER BY tablename, indexdef;
```
把 `indexdef` 里的列部分截出来对比，**同表同列同序的索引就是候选删除对象**。

### 2.3 发现 ②：代理主键索引 `id` **从未被使用**

`daily_quotes_pkey`(137 MB)、`valuation_daily_pkey`(133 MB)、`fin_indicators_pkey`、`stock_pool_pkey`、`safety_zones_pkey`、`money_flow_pkey` 的 `idx_scan` 都是 **0**。

**原因**：这两张大表的**所有查询都是按 `(code, trade_date)` 访问**（见 §4 与 §5 的全部查询），**没有任何一处按 `id` 查**。`id BIGSERIAL` 只是插入时用来保证行唯一，对查询毫无贡献。

**这意味着**：若把主键从 `id` 改成 `(code, trade_date)`，可再省 **270 MB**，且少维护一个索引 → **两张表合计可省约 664 MB（394 + 270）**，约等于全库体积的 1/3。

> ⚠️ 但改主键需要数据重写（`ALTER TABLE ... DROP CONSTRAINT / ADD PRIMARY KEY`），属于**长期项**，不是随手可做（见 §6 P2/P3）。

### 2.4 别被 `idx_scan = 0` 骗了（重要测量学）

两个必须知道的坑：

1. **`ON CONFLICT DO UPDATE` 的冲突检测会走唯一索引，但通常不计入 `idx_scan`**。
   所以 `uq_money_flow_code_date`=407、`valuation_daily_code_trade_date_key`=0 **不代表索引没用**，只代表"没有 SELECT 用它"。
2. **统计计数可能被重置**（`pg_stat_reset()`、实例重建都会清零）。用 `idx_scan` 判"该不该删索引"时，
   必须**结合代码里的查询形态**一起判断——本项目正是这样判断的：**代码里没有 `WHERE id = ...`，所以 pkey 可以判定为冗余**。

**讲解要点**：**"0 次扫描"要能和"索引确实没人用"分开**。能主动说出这两个坑，说明真的做过测量而不是看个数字就下结论。

---

## 3. 本库当前最严重的问题：统计信息失真 + autovacuum 从未成功

这一节是本文最有价值的部分——**它不报错、不影响业务功能，却让所有查询计划建立在错误信息上**。

### 3.1 现象：规划器以为大表只有 **134 行**

实测 `pg_stat_user_tables`：

| 表 | 统计的 `n_live_tup` | 实际行数 | 偏差 |
|---|---|---|---|
| `daily_quotes` | **134** | 6,215,688 | **差 4.6 万倍** |
| `valuation_daily` | **0** | 6,219,340 | 差无穷大 |
| `fin_indicators` | **0** | 118,774 | 差无穷大 |
| `stocks` / `intraday_spot` / `alert_logs` | 0 / 0 / 2 | 5,574 / 5,923 / 33 | 失真 |

同时：`autoanalyze_count = 0`、`autovacuum_count = 0`、`last_analyze / last_autoanalyze / last_vacuum` **全为空**
→ **这些表从来没有被 vacuum/analyze 过**；`daily_quotes` 的死元组累积到 **160,705**。

### 3.2 根因链：Docker 的 `/dev/shm` 只有 64 MB

排查过程（可复现）：

```sh
# ① 容器共享内存：只有 64 MB（Docker 默认值）
$ docker compose exec -T db sh -c "df -h /dev/shm"
shm  64M  1.1M  63M  2%  /dev/shm

# ② 手动 VACUUM 直接报错
$ docker compose exec -T db psql -U stock -d stock -c "VACUUM (ANALYZE) daily_quotes"
ERROR:  could not resize shared memory segment "/PostgreSQL.3088739606"
        to 132635392 bytes: No space left on device

# ③ 数据库的并行维护设置
dynamic_shared_memory_type = posix        # 用 /dev/shm 做共享内存
max_parallel_maintenance_workers = 2
```

**因果链（三环）**：

```
/dev/shm = 64 MB（Docker 默认）
   └─▶ 并行 VACUUM 需要 132 MB 的共享内存段 → "No space left on device"
         └─▶ autovacuum 每次尝试都失败 → autovacuum_count 永远为 0
               └─▶ 死元组越积越多（16 万）+ 统计信息永不更新（以为只有 134 行）
```

**注意一个反直觉的点**：`autovacuum` 的触发阈值是"死元组 > 20% × 活元组"（[参数](#8-参数与容量速查表)：`autovacuum_vacuum_scale_factor=0.2`）。
按真实数据量（621 万）算，要累积 **124 万死元组**才会触发；
而按**失真的统计**（134 行）算，阈值只要 ~77 —— **本该频繁触发的 autovacuum 却因为共享内存不足而每次都失败**。
所以"统计失真"与"不清理"互为因果，**是一个自我强化的坏循环**。

### 3.3 实测验证：关掉并行后立刻成功

```sh
$ docker compose exec -T db psql -U stock -d stock \
    -c "SET max_parallel_maintenance_workers = 0" \
    -c "VACUUM (ANALYZE) daily_quotes" \
    -c "SELECT relname, n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname='daily_quotes'"
VACUUM
 relname      | n_live_tup | n_dead_tup
--------------+------------+------------
 daily_quotes |    6215740 |          0     ← 死元组 154,840 → 0，一次成功
```

（`SET` 只作用于该会话，**未修改数据库配置**，可放心复现。）

### 3.4 正确修复：给 db 服务分配足够的共享内存

在 [docker-compose.yml](../docker-compose.yml) 的 `db` 服务上加一行（推荐 **`shm_size: 1g`**）：

```yaml
  db:
    image: paradedb/paradedb:latest-pg16
    environment:
      POSTGRES_USER: stock
      POSTGRES_PASSWORD: stock
      POSTGRES_DB: stock
      TZ: Asia/Shanghai
    shm_size: 1g          # ← 新增：Docker 默认仅 64MB，会导致并行 VACUUM 失败
    ports: ["5433:5432"]
```

**为什么是 1g 而不是 256m**：PostgreSQL 的并行操作会按 `work_mem × 并行 worker 数` 申请共享内存段
（本次 VACUUM 就要 132 MB），再叠加并行查询/哈希连接的开销，**1 GB 是官方对 Docker 部署的常见建议值**。

### 3.5 修复后的收益（实测）

先执行 `ANALYZE` 更新统计（`n_live_tup`：`134` → `6,222,467`），随后同一条查询的计划发生了实质变化：

| 查询 | ANALYZE 前 | ANALYZE 后 |
|---|---|---|
| 每只票最近两根日线（窗口函数） | **1011.8 ms** | **573.9 ms** |
| 最新交易日 `GROUP BY max` | **861.2 ms** | **605.8 ms** |
| 行情列表（`DISTINCT ON`） | **119.1 ms** | **78.4 ms** |

> **严谨性提醒**：这三次测量之间**既做了 ANALYZE，缓存也被预热了**，所以"变快"是两者共同作用的结果，
> 不能 100% 归因于统计信息。**要严格归因需要多次采样取稳定值**——`EXPLAIN ANALYZE` 单次数字是**有波动的**。

**但"统计信息正确"的独立价值必须讲清楚**：统计错误时，规划器对每张表的行数、选择率、代价全是错的，
**它可能在 99% 的情况下恰好选对计划，也可能在某个查询上灾难性地选错**（例如以为表只有 134 行 → 选 Nested Loop，
实际要循环 600 万次）。**"没出错"是运气，不是设计。**

**讲解要点（这是本文最该讲的"能力点"）**：
> "我们的数据库不报错，但统计信息说大表只有 134 行，autovacuum 从来没成功过——原因是 Docker 默认的 64 MB 共享内存装不下并行清理需要的段。"

能把**不报错的性能隐患**挖出来并定位到基础设施配置，比"加了个索引快了一倍"高级得多。

---

## 4. 项目已经做的查询优化（做对了什么）

> 这一节是"现状盘点"。**每一项都给出"为什么这么写"，而不是"这么写了"**。

| # | 优化 | 位置 | 为什么这样做 |
|---|---|---|---|
| 1 | **批量取数替代 N+1** | [pool.py L26-L44](../backend/app/api/pool.py#L26-L44)、[compare.py `_quotes`/`_zones`](../backend/app/services/compare.py#L17-L51) | 一次 `WHERE code IN (...)` 取回全部 200 只的数据，再在 Python 里用字典分组；**避免 200 次往返** |
| 2 | **窗口函数取"每只票最近 N 根"** | [pool.py L26-L33](../backend/app/api/pool.py#L26-L33) | `row_number() OVER (PARTITION BY code ORDER BY trade_date DESC)` 一条 SQL 解决"每组取前 N"；**但它的代价是全表扫描（见 §5.2 的更好写法）** |
| 3 | **批量 upsert + 幂等** | [datafeed `_upsert_quotes` L54-L74](../backend/app/services/datafeed.py#L54-L74)、`_upsert_val` | `INSERT ... ON CONFLICT DO UPDATE`：**一次语句写 N 行**，天然幂等；替代"先查再插"的两次往返与唯一键冲突 |
| 4 | **断点续传** | [L82-L86](../backend/app/services/datafeed.py#L82-L86) | 先取每只票 `max(trade_date)`，**只同步落后的股票**，避免重复全量抓取 |
| 5 | **会话复用 + 连接池** | [db.py](../backend/app/db.py) | 池化连接、`pool_pre_ping` 应对失效连接；`_intraday_chain` 五步共用一个 `Session`（[scheduler.py](../backend/app/scheduler.py#L25-L41)） |
| 6 | **并发抓取 + 主线程串行入库** | [L92-L95](../backend/app/services/datafeed.py#L92-L95)、`sync_all_fundamentals` | 6 线程抓网络、**入库仍在主线程**——避免多线程争抢同一个 SQLAlchemy `Session` |
| 7 | **单条异常隔离 + rollback** | `sync_all_quotes` / `sync_all_fundamentals` 循环内 `try/except` | 单只股票脏数据不中断整批同步；失败后 `rollback` 再继续，避免会话脏状态 |
| 8 | **只取需要的列** | 多处 `db.query(DailyQuote.close)` 等 | 少一次 `SELECT *` 的列宽开销（在宽表上收益明显，本项目字段不多，收益有限） |

### 4.1 一个必须讲清的取舍：**断点续传 = 每日重写 5 年数据**

看代码链条：

```python
start = (today - timedelta(days=years * 365)).strftime("%Y%m%d")   # L89：起点是"5 年前"
futs = [ex.submit(_fetch_quotes, c, start, end) for c in todo]     # L93：对每只落后票，拉 5 年窗口
_upsert_quotes(db, code, df)                                       # L95：把 5 年窗口里的每一行都 upsert
```

**后果**：被同步的每只票，**每次都会重新 upsert ~1167 行（5 年）**，其中 99.9% 的值没变。

**为什么这样写？（重要——这是前复权数据的固有问题）**
本项目日线用的是 **`adjust="qfq"` 前复权**（[L48](../backend/app/services/datafeed.py#L48)）。
**前复权价格会随每一次新的除权除息事件整体改变**（历史价格全部按新基准重算）。
所以"只拉增量"看似省事，**却会让历史价格停留在旧复权基准上，与最新价格不可比**——
每日整体重写 5 年窗口，是用**写入量**换**复权口径一致性**。

**代价与优化方向（写进 §6 P2）**：写入放大 → 死元组 → 依赖 autovacuum（而 autovacuum 又因 §3 的问题从未成功）。
折中方案：**日常只拉增量（`start = max(latest+1, today-5y)`），每周/每月做一次全量刷新**。

**讲解要点**：**"为什么这段代码看起来低效却这么写"**，是面试和答辩里最容易被追问、也最能体现深度的地方。
答"因为有前复权"才算答到点上；答"为了简单"就露怯了。

---

## 5. 优化实测：三组对照实验（本文核心）

> 方法论：**先把"读了多少行、返回多少行、用了什么计划"量出来**，再动 SQL。
> 全部使用 `EXPLAIN (ANALYZE, BUFFERS)`（**必须带 `ANALYZE`**，只写 `EXPLAIN` 看到的是估算而非事实）。

### 5.1 全量查询实测总表

| 查询 | 代码位置 | 计划主节点 | 实测耗时 | 返回行数 |
|---|---|---|---|---|
| 每只票最近两根日线（窗口） | [pool.py L26-L33](../backend/app/api/pool.py#L26-L33) | **Parallel Seq Scan daily_quotes** | **1011.8 ms** | 400 |
| 最新交易日 `GROUP BY max` | [datafeed.py L82-L84](../backend/app/services/datafeed.py#L82-L84) | **Parallel Seq Scan** | **861.2 ms** | 5,574 |
| 行情列表 `DISTINCT ON` | [stocks.py L18-L25](../backend/app/api/stocks.py#L18-L25) | Index Scan + Merge Join | 119.1 ms | 100 |
| 行情列表带关键词 `ILIKE` | [stocks.py L22](../backend/app/api/stocks.py#L22) | **Seq Scan stocks**（name 无索引） | 146.0 ms | 38 |
| 行情总数 `count(*) + EXISTS` | [stocks.py L28-L33](../backend/app/api/stocks.py#L28-L33) | Index Only Scan（5,574 次） | 61.5 ms | 1 |
| 财务聚合 `avg + having` | [screener.py L95-L103](../backend/app/services/screener.py#L95-L103) | Seq Scan + HashAggregate | 90.9 ms | 5,562 |
| 股池列表（JOIN + 排序 + LIMIT 200） | [pool.py L15-L19](../backend/app/api/pool.py#L15-L19) | Seq Scan（表仅 412 行） | **2.1 ms** | 200 |
| 财务行数按代码 `count` | [screener.py L121-L122](../backend/app/services/screener.py#L121-L122) | Seq Scan + HashAggregate | ~90 ms | 5,574 |
| 单票最近 250 根日线 | [zones.py](../backend/app/api/zones.py) | **Index Scan Backward** ✅ | **0.164 ms** | 250 |
| 单票全程 PE 序列 | [screener.py L65-L68](../backend/app/services/screener.py#L65-L68) | **Index Scan** ✅ | **0.902 ms** | 1,210 |
| 安全区间倒序 | `safety_zones ORDER BY calc_date DESC` | Seq Scan（仅 202 行） | 0.353 ms | 100 |
| 提醒记录倒序 | [alerts.py L13-L14](../backend/app/api/alerts.py#L13-L14) | Seq Scan（仅 33 行） | 0.164 ms | 33 |
| 对比历史倒序 | `compare_analyses ORDER BY created_at` | Seq Scan（仅 3 行） | 0.099 ms | 3 |
| 盘中快照当日 | [monitor.py](../backend/app/services/monitor.py) | Seq Scan（仅 5,923 行） | 0.672 ms | 0 |
| 资金流批量取最新 | [money_flow `load_latest`](../backend/app/services/money_flow.py) | Seq Scan（仅 200 行） | 0.106 ms | 2 |

**一眼看出谁在拖后腿**：**只有前两条（1011 ms / 861 ms）是"秒级"的**，而且都是 `daily_quotes` 上的**全程扫描**；
其余查询都在 0.1–150 ms 之间且**表小或走了索引**。

### 5.2 实验 A：窗口函数 → `LATERAL` 逐键索引扫描（**120×**）

**问题**：股池页要"200 只票各取最近两根日线"，却让数据库**扫完整张 621 万行的表**。
实际需要的只有 **200 × 2 = 400 行**（占 0.006%）。

**原写法**（[pool.py L26-L33](../backend/app/api/pool.py#L26-L33)）：

```sql
SELECT * FROM (
  SELECT code, trade_date, open, close,
         row_number() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
  FROM daily_quotes WHERE code IN (SELECT code FROM stock_pool WHERE active IS TRUE)
) t WHERE rn <= 2;
```
实测：**573.9 ms**（ANALYZE 后；ANALYZE 前 1011.8 ms），计划为
`Parallel Seq Scan on daily_quotes → Hash Join → Sort → WindowAgg`，**每 worker 扫 207 万行**。

**改写（语义完全等价）**：

```sql
SELECT q.code, q.trade_date, q.open, q.close
FROM (SELECT code FROM stock_pool WHERE active IS TRUE) p
CROSS JOIN LATERAL (
  SELECT d.code, d.trade_date, d.open, d.close
  FROM daily_quotes d WHERE d.code = p.code
  ORDER BY d.trade_date DESC LIMIT 2
) q;
```
实测：**4.8 ms**，计划为
`Nested Loop → (每 code) Index Scan Backward using ix_daily_quotes_code_date LIMIT 2`，**loops=200、每次 0.021 ms**（并带 `Memoize`）。

**差异根源**：`(code, trade_date)` 索引天然按这两个字段有序。
- **窗口函数**要求"先按 code 分区再按日期排序"，规划器只能**全表读进来再排**；
- **`LATERAL` + `LIMIT 2`** 让优化器对**每只票做一次索引倒序扫描并立刻停下**（`rows=2`），
  代价从"扫描 621 万行"降到"**200 次索引定位 + 400 行**"。

### 5.3 实验 B：行情列表 `DISTINCT ON` → `LATERAL`（**78×**）

**原写法**（[stocks.py L18-L25](../backend/app/api/stocks.py#L18-L25)）：

```sql
SELECT DISTINCT ON (q.code) q.code, s.name, s.industry, q.close, q.trade_date
FROM daily_quotes q JOIN stocks s ON s.code = q.code
WHERE (:kw_empty OR s.name ILIKE :like OR q.code ILIKE :like)
ORDER BY q.code, q.trade_date DESC
LIMIT :limit OFFSET :offset;
```
实测：**78.4 ms**（ANALYZE 后；前 119.1 ms）。计划：
`Merge Join → Incremental Sort → Unique → Limit`，**q 侧索引扫 120,862 行**（为拿 100 行结果多读 1200 倍）。

**改写**：

```sql
SELECT s.code, s.name, q.close, q.trade_date
FROM stocks s
CROSS JOIN LATERAL (
  SELECT close, trade_date FROM daily_quotes d
  WHERE d.code = s.code ORDER BY d.trade_date DESC LIMIT 1
) q
ORDER BY s.code LIMIT 100 OFFSET 0;
```
实测：**1.0 ms**。计划：`Index Scan stocks_pkey（只读 100 行，LIMIT 下推）→ LATERAL Index Scan Backward ×100（各 0.009 ms）`。

**这里多了一个教科书级的知识点：`LIMIT` 下推**。
原写法因为 `DISTINCT ON` 要全局去重，**必须先把每只票的行都排好**，"只要 100 条"这个信息用不上；
LATERAL 写法是"外层驱动表 + 每行补一条"，于是 `ORDER BY s.code LIMIT 100` **能直接告诉外层只需读 100 只票**，读多少算多少。

**附带发现（同一条 SQL 的 ILIKE）**：`WHERE s.name ILIKE '%银行%'` 时 `stocks` 走 **Seq Scan**（name 列无索引），
实测 **146.0 ms**、扫 5574 行、返回 38 行。当前 stocks 只有 5574 行，**加索引收益不大（且模糊查询前缀含 `%` 用不上 B-tree）**；
若将来要做"名称搜索联想"，正确手段是 **`pg_trgm` + GIN 索引**（见 §6 P3）。

### 5.4 实验 C：把"全表聚合"变成"缓存状态"

`datafeed.sync_all_quotes` **每次执行**都要跑"每只票最新交易日"的聚合（[L82-L84](../backend/app/services/datafeed.py#L82-L84)）：

```sql
SELECT code, max(trade_date) FROM daily_quotes GROUP BY code;   -- 实测 861→606 ms，全程扫描 621 万行
```

**这条查询的本质是"维护同步状态"，却每次全表重算**。
它有两个可优化方向（§6 P2）：
1. 把结果持久化到一张 `quote_sync_state(code, last_date)` 表，**同步完顺手更新**（读 5574 行，`< 5 ms`）；
2. 或者退一步：**只在表数据变化时才重算**（本表每日只变 5574 行）。

### 5.5 三组实验的方法论小结（可以直接背下来）

```
① 先量：EXPLAIN (ANALYZE, BUFFERS) 看"实际读了多少行 / 返回多少行 / 什么计划"
② 找病：读行数 ÷ 返回行数 的比值（本例 207 万 ÷ 400 = 5000 倍，这就是病）
③ 再改计划：统计信息 → 索引 → SQL 重写（**优先重写，代价最小**）
④ 最后改结构：分区 / 缓存 / 瘦身（改动大，收益要更大才做）
⑤ 改完必须复测：用同一个 EXPLAIN 命令对比，而不是"感觉快了"
```

**一句话精华**：**"让数据库按索引只读它真正需要的那几百行，而不是先读进来再过滤。"**

---

## 6. 待优化清单（按"收益 / 风险"排序）

### P0-1 给 db 容器 `shm_size`（**一行配置，收益最大**）

| 项 | 内容 |
|---|---|
| 现状 | `/dev/shm = 64MB` → 并行 VACUUM 报 `No space left on device` → **autovacuum 从未成功**、统计信息失真 4.6 万倍 |
| 改法 | [docker-compose.yml](../docker-compose.yml) 的 `db` 服务加 `shm_size: 1g` |
| 收益 | 恢复 autovacuum 与统计信息自动维护；死元组不再累积；**所有查询的计划质量回到正确基线** |
| 风险 | 极低（仅资源占用，容器重建后生效） |

### P0-2 把大表 ANALYZE 纳入日常

| 项 | 内容 |
|---|---|
| 现状 | 大表**从未被 analyze**；实测手动 ANALYZE 后三条查询分别快 2×/1.4×/1.5× |
| 改法 | 短期：每周在同步后手动 `ANALYZE daily_quotes, valuation_daily;`；根治：修好 shm（P0-1）后由 autovacuum 自动完成 |
| 附加建议 | 给大表设**更灵敏的自动清理阈值**：`ALTER TABLE daily_quotes SET (autovacuum_analyze_scale_factor = 0.02, autovacuum_vacuum_scale_factor = 0.05);`（默认 0.1/0.2 对 621 万行的表太迟钝） |
| 风险 | 低（`ANALYZE` 只读采样、不锁写） |

### P1-1 重写两处"全表扫描型"查询（收益 78×～120×）

| 项 | 内容 |
|---|---|
| 现状 | [pool.py L26-L33](../backend/app/api/pool.py#L26-L33)（573.9 ms）、[stocks.py L18-L25](../backend/app/api/stocks.py#L18-L25)（78.4 ms） |
| 改法 | 改为 §5.2 / §5.3 的 `LATERAL` 写法（语义等价，实测 **4.8 ms / 1.0 ms**） |
| 涉及文件 | `api/pool.py`、`api/stocks.py`；`services/compare.py` 的 `_quotes` 也可同法改造 |
| 风险 | 中（需回归验证"最近两根/最新一根"的取值正确性；`OFFSET` 分页语义需一并验证） |

### P1-2 删掉两个重复索引（省 394 MB + 降低写入放大）

```sql
DROP INDEX IF EXISTS ix_daily_quotes_code_date;        -- 200 MB，与唯一约束同列同序
DROP INDEX IF EXISTS ix_valuation_daily_code_date;     -- 194 MB，同上
```

| 项 | 内容 |
|---|---|
| 为什么安全 | 唯一约束索引 `..._code_trade_date_key` 列与顺序**完全相同**；查询会自动改走它；upsert 的冲突检测也走它 |
| 收益 | **省 394 MB**；大表每次 upsert 少维护一个 B-tree（**直接缩短 15-20 分钟的行情同步**） |
| 注意 | ⚠️ 不要反向删唯一约束索引（约束需要它）；删前用 `pg_stat_user_indexes` 复核一次 |

### P2-1 消除"每次同步全表重算最新交易日"（606 ms → <5 ms）

见 §5.4。方案：新增 `quote_sync_state(code, last_date, updated_at)`，同步完成后 `upsert` 状态行；
`sync_all_quotes` 改读这张小表。**顺带把 §4.1 的"每日重写 5 年"改为「增量 + 定期全量刷新」**，写入量与死元组都会大幅下降。

### P2-2 主键瘦身：`id BIGSERIAL` → `(code, trade_date)`

| 项 | 内容 |
|---|---|
| 依据 | 两张大表的 `_pkey` **idx_scan = 0**，代码里没有任何按 `id` 的查询 |
| 收益 | 再省 **270 MB**，总省（含 P1-2）约 **664 MB** |
| 风险 | **高**：需 `DROP CONSTRAINT` + `ADD PRIMARY KEY`，期间会重写索引并持锁；改动涉及 ORM 模型与迁移 |
| 结论 | 列为长期项，**数据量不紧迫时不必做**（年增仅 650 MB） |

### P3-1 增长驱动的索引（**现在不要加**，给出阈值）

当前这些表的查询都在 0.1–0.35 ms（**全表扫描 33 行/202 行/3 行反而更快**）。
**给小表加索引是本项目最典型的"伪优化"**——只会拖慢写入。等表长到 **5 万行以上**再加：

```sql
-- 达到阈值后再考虑（当前小表无需）
CREATE INDEX CONCURRENTLY ix_stock_pool_active_score ON stock_pool (active, score DESC);
CREATE INDEX CONCURRENTLY ix_safety_zones_calc_date    ON safety_zones (calc_date DESC);
CREATE INDEX CONCURRENTLY ix_alert_logs_notified_at    ON alert_logs (notified_at DESC);
CREATE INDEX CONCURRENTLY ix_compare_analyses_created  ON compare_analyses (created_at DESC);
```

### P3-2 大表保留策略 / 分区

`daily_quotes`、`valuation_daily` **断点续传只增不删**，5 年后即超 5 年数据。
方案：① 按年 `RANGE` 分区（新数据落在热分区，旧分区可归档到独立表空间或 detach）；
② 或直接删除 N 年前的冷数据（若策略只依赖近 5 年指标，历史保留意义有限）。
**前提**：先确认业务上是否还需要 5 年前的数据（本项目指标以 5 年窗口为主）。

### P3-3 参数调优（SSD 场景）

| 参数 | 当前 | 建议 | 理由 |
|---|---|---|---|
| `random_page_cost` | **4**（机械盘默认） | **1.1** | SSD 随机读与顺序读代价接近；保持 4 会让规划器**低估索引扫描、偏爱 Seq Scan**——**这正好是 §5 两处"该走索引却全表扫"的隐性帮凶** |
| `effective_io_concurrency` | 1 | 200 | SSD 可并发提交 IO |
| `shared_buffers` | 1985 MB | 保持（或随内存上调） | 两张热表 2 GB > shared_buffers，**缓存装不下时首次访问必读磁盘** |
| `work_mem` | 19 MB | 谨慎上调（如 32–64 MB） | 影响排序/哈希；但过高会与 §3 的共享内存问题叠加（每 worker 都要申请），**须先修 `shm_size`** |

### P3-4 写入路径优化（同步耗时的另一半）

- `sync_all_fundamentals` 对 5574 只票**每只一次 `commit`** → 改为**每 50 只提交一次**（减少 fsync 次数，数倍提速）；
- upsert 时**先按主键排序再写**，可减少索引页随机分裂；
- 配合 P1-2 的索引瘦身，写入放大同时下降。

### P3-5 JSONB 字段（提醒给将来）

`safety_zones.detail`（**1.4 kB/行**）、`stock_pool.reason`（951 B/行）、`compare_analyses.items`（10.7 kB/行）体积偏大。
当前**没有任何按 JSON 路径的过滤**，所以**不需要 GIN 索引**（加了纯亏写入）。
但 **`safety_zones` 年增 71 MB 主要就是 JSONB** → 优化方向：① 只存前端/算法真需要的字段；② 历史区间记录定期归档。

---

## 7. 方法论：优化顺序与"不要做的优化"

### 7.1 正确顺序（五步，本项目的实践）

```
① 量：EXPLAIN (ANALYZE, BUFFERS) + pg_stat_user_tables + pg_stat_user_indexes
      ↓  弄清"谁慢、慢在读了太多行、还是选了错的计划"
② 统计信息：ANALYZE / autovacuum 是否正常（**本库的问题就在这一步，见 §3**）
      ↓  统计错了，后面所有优化都是碰运气
③ 索引：先"删冗余"，再"加必要"（本库：删 394 MB 冗余 > 加任何新索引）
      ↓
④ SQL 重写：把"全表 + 过滤排序"改成"逐键 + 索引取数"（§5 的 120×/78×）
      ↓
⑤ 结构：分区 / 缓存状态表 / 主键瘦身 / 归档（改动大，收益需更大才做）
```

### 7.2 判断一条查询是否健康的三个数

| 指标 | 健康 | 病态（本项目实测） |
|---|---|---|
| **读行数 ÷ 返回行数** | < 10 | `207 万 ÷ 400 = 5000 倍`（§5.2）、`12 万 ÷ 100 = 1200 倍`（§5.3） |
| 计划节点 | Index Scan / Index Only Scan | `Seq Scan` + `Sort` 在有索引的列上 |
| `BUFFERS` | `hit` 为主、`read` 很少 | `read` 高说明缓存装不下（大表常见） |

### 7.3 不要做的 5 件事（本项目明确不做）

| ✗ 不要 | 为什么 |
|---|---|
| 给万行以下的小表加索引 | 实测这些查询 0.1–0.35 ms，**全表扫描快到无可优化**；加索引只拖慢写入（§6 P3-1） |
| 给没有 JSON 路径查询的 JSONB 加 GIN | 索引从不被使用，**纯增加写入与磁盘**（§6 P3-5） |
| 把金额/价格从 `NUMERIC` 改 `FLOAT` | 财务数据**精度是硬要求**，省一点空间的代价是算错 |
| 盲目加应用层缓存 | 缓存掩盖问题、引入一致性问题；本项目只有"小表 + 索引"就够（Redis 只用于**提醒去重**） |
| 只为"页面好看"加索引 | 索引是**用写入换读取**；本项目写入（每日 upsert 数万行）本身就是瓶颈之一 |

---

## 8. 参数与容量速查表

| 项 | 实测值 / 建议 |
|---|---|
| PG 版本 | 16.15（ParadeDB pg16 镜像） |
| `shared_buffers` | 1985 MB |
| `work_mem` | 19 MB |
| `maintenance_work_mem` | 496 MB |
| `effective_cache_size` | 5957 MB |
| `max_connections` | 100 |
| `random_page_cost` | **4**（默认；SSD 建议 1.1） |
| `effective_io_concurrency` | 1（SSD 建议 200） |
| `dynamic_shared_memory_type` | `posix`（依赖 `/dev/shm`） |
| `/dev/shm`（容器） | **64 MB**（⚠️ 问题根源；建议 `shm_size: 1g`） |
| `autovacuum` | on；`naptime=1min`、`vacuum_scale_factor=0.2` |
| autovacuum 实际执行次数 | **0**（大表；因共享内存不足持续失败） |
| 最大表 | `daily_quotes` 6,215,688 行 / 1130 MB |
| 最大索引 | `..._code_trade_date_key` / `ix_daily_quotes_code_date` 各 200 MB |
| 可回收空间 | 重复索引 **394 MB** + 冗余代理主键 **270 MB** ≈ **664 MB** |
| 年增体积 | 约 **650 MB/年**（合计约 290 万行/年） |
| 最慢查询 | 窗口函数取最近两根日线 **573.9 ms**（可降至 **4.8 ms**） |
| 最快查询 | 单票 250 根日线 **0.164 ms**（已走索引，无需优化） |

---

## 9. 十分钟讲解提纲

| 时长 | 内容 | 一句话开场 |
|---|---|---|
| 1 min | §0 总览：2 GB、11 张表、96% 集中在 2 张表 | "**优化的第一问是'优化哪张表'**，答案是只有 2 张值得优化。" |
| 2 min | §1 数据量：621 万行怎么算出来的（1212 交易日 × 5574 只）、年增 1.39 M 行 | "这个数字不用猜，从`断点续传 + 每交易日 1 行`就能手算。" |
| 1 min | §2 索引：1058 MB、**一对完全重复的索引、代理主键 0 次使用** | "索引占的空间和堆一样多，而且有两份是一模一样躺着的。" |
| 2 min | §3 **统计信息失真 + autovacuum 从未成功**（重点） | "库不报错，但规划器以为大表只有 134 行——根因是 Docker 默认 64MB 共享内存。" |
| 2 min | §5 三组对照实验（重点，讲 120× 与 78×） | "同一条语义的查询，换一种写法从 573 ms 变成 4.8 ms，差在'只读需要的那几百行'。" |
| 1 min | §6 待优化清单 + §7 方法论 | "先量、再改计划、最后改结构；小表加索引是伪优化。" |
| 1 min | §7.3 不要做的 5 件事 | "能说出'不做什么'，比列一堆优化更有说服力。" |

**讲 §3 与 §5 时一定要给出"实测命令 + 前后数字"**——
"`VACUUM` 报 `No space left on device` → 关掉并行后成功、死元组 154,840 → 0"这种**一步到位的证据链**，最能让听众信服。

---

## 10. 常见问答

| 提问 | 回答要点 |
|---|---|
| 为什么是 621 万行？ | 5,574 只 × 1212 个交易日 ×92%（次新股上市晚）= 6.22 M；`years=5` 决定跨度。 |
| 数据量算大吗？ | **不大**：每年 650 MB、10 年才 8 GB，**不需要分库分表/TimescaleDB**（这是技术方案的选型依据）。 |
| 为什么索引和堆一样大？ | 大表有两个**重复**索引（394 MB）+ 一个**没人用**的代理主键索引（270 MB）；B-tree 每一行都要占空间。 |
| `idx_scan=0` 是不是可以删？ | **不能只看这个数**：`ON CONFLICT` 的冲突检测走索引但不计入 `idx_scan`，且统计可能被重置。要**结合代码里的查询形态**判断。 |
| 为什么 VACUUM 会失败？ | Docker 默认 `/dev/shm` 只有 64 MB，`dynamic_shared_memory_type=posix` 的并行 VACUUM 需要 132 MB 段 → `No space left on device`。 |
| 修好 shm 之后 autovacuum 会清理吗？ | 会，但阈值（20% of live tuples）对 621 万行的表意味着要等 124 万死元组；**建议同时下调该表的 `autovacuum_*_scale_factor`**。 |
| 统计信息失真有什么实际危害？ | 规划器对行数/选择率/代价的判断全错，**可能在某个查询上灾难性选错计划**（如以为 134 行而选 Nested Loop）。"没出问题"是运气。 |
| 为什么不给小表加索引？ | 实测那些查询 0.1–0.35 ms，全表扫描已经最优；加索引只增加写入成本（**用写入换读取**，而本项目写入本身就不轻）。 |
| `LATERAL` 为什么快这么多？ | 它让优化器**对每只股票做一次索引倒序扫描并 `LIMIT 2` 就停**，读 400 行；窗口函数必须**先把 621 万行全读进来再排**。 |
| `LIMIT` 下推是什么？ | `ORDER BY s.code LIMIT 100` 能让外层驱动表**只读 100 行就停**；而 `DISTINCT ON` 必须全局去重，**用不上这个信息**。 |
| `ILIKE '%银行%'` 为什么不用索引？ | B-tree 不支持**前置通配**（`%` 开头）；正解是 `pg_trgm` + GIN。当前 `stocks` 仅 5574 行，不值得加。 |
| 为什么每天要重写 5 年数据？ | 因为是**前复权（qfq）**数据，除权除息会让历史价格整体变化；这是**写入量换口径一致性**的取舍（可用"增量 + 定期全量"折中）。 |
| `random_page_cost=4` 有什么影响？ | 这是机械盘默认值；SSD 上会**低估索引扫描、偏爱全表扫描**，是 §5 那两处"该走索引却全表扫"的隐性帮凶。 |
| 为什么 I/O 参数写 1.1 而不是 1.0？ | PostgreSQL 的经验值：SSD 上 1.1 足够，取 1.0 会让规划器过度偏爱索引（含低选择率场景）。 |

---

## 11. 附录：复现命令

> Windows PowerShell 环境。**SQL 内只使用单引号**。`-P pager=off` 关闭分页便于阅读。

```sh
# 表大小与统计
docker compose exec -T db psql -U stock -d stock -P pager=off -c "SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) total, pg_size_pretty(pg_relation_size(relid)) heap, pg_size_pretty(pg_indexes_size(relid)) idx FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC"

# 索引清单与大小
docker compose exec -T db psql -U stock -d stock -P pager=off -c "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY tablename, indexname"
docker compose exec -T db psql -U stock -d stock -P pager=off -c "SELECT relname, indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) FROM pg_stat_user_indexes ORDER BY pg_relation_size(indexrelid) DESC"

# 数据跨度与增长依据
docker compose exec -T db psql -U stock -d stock -P pager=off -c "SELECT count(DISTINCT trade_date) days, count(DISTINCT code) codes, min(trade_date), max(trade_date) FROM daily_quotes"

# 统计信息 / autovacuum / 死元组
docker compose exec -T db psql -U stock -d stock -P pager=off -c "SELECT relname, n_live_tup, n_dead_tup, autovacuum_count, autoanalyze_count, last_vacuum, last_autoanalyze FROM pg_stat_user_tables ORDER BY n_dead_tup DESC"

# 关键参数
docker compose exec -T db psql -U stock -d stock -P pager=off -c "SHOW shared_buffers"
docker compose exec -T db psql -U stock -d stock -P pager=off -c "SHOW random_page_cost"

# 共享内存（问题根源）
docker compose exec -T db sh -c "df -h /dev/shm"

# 手动维护（可复现 §3.3）
docker compose exec -T db psql -U stock -d stock -c "SET max_parallel_maintenance_workers = 0" -c "VACUUM (ANALYZE) daily_quotes"
docker compose exec -T db psql -U stock -d stock -c "ANALYZE daily_quotes"

# 执行计划对比（§5 的两组实验，可自行改写后复测）
docker compose exec -T db psql -U stock -d stock -P pager=off -c "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM (SELECT code, trade_date, close, row_number() OVER (PARTITION BY code ORDER BY trade_date DESC) rn FROM daily_quotes WHERE code IN (SELECT code FROM stock_pool WHERE active IS TRUE)) t WHERE rn <= 2"
docker compose exec -T db psql -U stock -d stock -P pager=off -c "EXPLAIN (ANALYZE, BUFFERS) SELECT q.code, q.trade_date, q.close FROM (SELECT code FROM stock_pool WHERE active IS TRUE) p CROSS JOIN LATERAL (SELECT d.code, d.trade_date, d.close FROM daily_quotes d WHERE d.code = p.code ORDER BY d.trade_date DESC LIMIT 2) q"
```

**注意**：本文撰写期间为取得实测数据，曾在库上执行过 `ANALYZE daily_quotes / valuation_daily` 与一次 `VACUUM (ANALYZE) daily_quotes`（**仅维护性操作，不改数据、不改配置**）。
`max_parallel_maintenance_workers=0` 是**会话级**设置，未落到数据库配置。
