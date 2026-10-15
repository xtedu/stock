# 大模型应用讲解文档（讲解版）

> 本文回答四个问题：**用的什么模型？在项目里用在哪？怎么用（配 + 调 + 排错）？产生了什么价值？**
> 每个结论都对应到代码位置，末尾附**已知局限**、**改进清单**、**十分钟讲解提纲**与**常见问答**。
> 算法内容见 [算法技术文档](algorithm.md)，调度（LLM 何时被调用）见 [定时任务框架讲解文档](scheduler.md)，
> 数据库见 [数据库优化讲解文档](database.md)，飞书通知见 [飞书是什么](feishu.md)。

---

## 0. 一句话总览

**用 OpenAI 兼容接口接入 DeepSeek，在项目里只做一件事：把"已经算好的结构化数字"翻译成人能读懂的话。**
它**不参与任何数值决策**（不改选股、不改区间、不改触发）——这是本项目用大模型的第一原则。

```
本地算法负责算出"事实"           大模型负责产出"解读"
────────────────────────   →   ────────────────────────
筛选打分 / 安全区间 / 三态位置        提醒解读（≤100 字，含免责声明）
涨幅 / MA250 乖离 / PE 分位      →    对比研判（看多/中性/看空 + 理由 + 关键点位）
资金流（主力净额/净占比/5日）         一句话总体结论
```

**为什么这样切分（必须讲透）**：大模型**不能可靠预测次日涨跌**，
给它一个百分数会产生**伪精确的错觉**。所以本项目**明确禁止它输出概率数字**，
只让它输出**带依据的定性判断**（理由 + 支撑/压力位 + 置信度）。
**"凡是要精确的地方交给代码，凡是要表达的地方交给模型"**——这是整篇文档的骨架。

---

## 1. 用的是什么模型

### 1.1 当前配置（实测自 `.env`）

| 配置项 | 当前值 | 说明 |
|---|---|---|
| `LLM_ENABLED` | **true** | 总开关；关闭后所有大模型调用直接跳过（不报错，只缺内容） |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | **OpenAI 兼容地址**（决定"用哪家"） |
| `LLM_MODEL` | **`deepseek-chat`** | DeepSeek 的对话模型 |
| `LLM_API_KEY` | （在 `.env` 中配置） | 密钥；**不进仓库**（[.env.example](../.env.example#L18) 留空） |
| `LLM_TIMEOUT` | `20` 秒 | 单次请求超时 |
| `LLM_MAX_TOKENS` | `300` | 提醒解读的**输出上限**（对比研判另有 1600） |

配置定义在 [config.py L17-L23](../backend/app/config.py#L17-L23)，通过 Pydantic Settings 从 `.env` 读取。

### 1.2 为什么是"OpenAI 兼容接口"而不是绑定某一家 SDK

关键设计：代码里**只 new 了一个 `OpenAI` 客户端，把 `base_url` 当参数**（[llm.py L15-L18](../backend/app/services/llm.py#L15-L18)）：

```python
def _client() -> OpenAI:
    s = get_settings()
    return OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=s.llm_timeout)
```

**收益**：换厂商**只改 `.env` 三行**（`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`），**代码零改动**。可切换的对象包括：

| 厂商 | `LLM_BASE_URL` 形态 | 备注 |
|---|---|---|
| DeepSeek（当前） | `https://api.deepseek.com/v1` | 默认配置 |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 兼容模式 |
| 智谱 / Kimi / 豆包 | 各家兼容端点 | 同样走 OpenAI 协议 |
| OpenAI | `https://api.openai.com/v1` | |
| **本地部署**（vLLM / Ollama / LM Studio） | `http://主机:端口/v1` | **内网可用、数据不出域** |

**讲解要点**：**"OpenAI 兼容协议已经成了事实标准"**——选它是为了**不被单一厂商锁定**。
这在工程上是"面向接口编程"的实例：代码依赖的是**协议**，不是**厂商**。

### 1.3 它在本项目中的角色边界（一句话版）

> **大模型在本项目里是"解说员"，不是"操盘手"。**
> 数值决策（选谁、贵不贵、要不要提醒）**100% 由本地确定性算法完成**（见 [algorithm.md](algorithm.md)），
> 大模型只做**文本增强**。技术方案里也是这么写的（[technical-design.md §1](technical-design.md)）。

**三个反例（讲的时候很有用）**：
- 不是"让大模型选股"（选股是 5 硬门槛 + 5 因子加权打分，**可复现、可解释**）；
- 不是"让大模型算安全区间"（区间是 MA250 中枢锚定公式，**能算出具体价位**）；
- 不是"让大模型判断是否提醒"（触发是 `price < zone_low` 的**确定性比较** + Redis 当日去重）。

**为什么不让模型做这三件事**：不可复现、不可回测、可能算错且**错了你不知道**。

---

## 2. 在项目里的三处应用（全部调用点）

全项目**只有两个 LLM 函数**（`summarize` / `compare`）、**三个调用入口**：

| # | 场景 | 函数 | 调用点 | 触发方式 | 落库 |
|---|---|---|---|---|---|
| 1 | **提醒解读** | `llm.summarize` | [monitor.py L73](../backend/app/services/monitor.py#L73) | **自动**（安全区间触发时） | `alert_logs.ai_summary` |
| 2 | **实时解读（按需）** | `llm.summarize` | [insights.py L22](../backend/app/api/insights.py#L22) | **手动**（API 调用） | 不落库 |
| 3 | **对比研判** | `llm.compare` | [compare.py L123](../backend/app/services/compare.py#L123) | **手动**（股池多选后点"开始分析"） | `compare_analyses.items` + `ai_summary` |

### 2.1 应用一：提醒解读（自动，嵌在提醒链路里）

链路（[monitor.py](../backend/app/services/monitor.py)）：

```
价格跌破区间下沿 (price < zone_low)
   └─▶ Redis 当日去重 (SET NX EX 86400)     ← 每天每只票只推一次
         └─▶ llm.summarize(code, name, price, zone_low, zone_high)   ← **大模型在这里**
               └─▶ alert_logs.ai_summary  落库
                     └─▶ notifier.notify → 飞书卡片
```

提示词（[llm.py L11-L12](../backend/app/services/llm.py#L11-L12)）：

```
你是证券分析助手。股票 {name}({code}) 当前价 {price}，进入历史安全区间 [{zone_low}, {zone_high}]。
用不超过100字说明其进入区间的可能原因与主要风险，结尾附"非投资建议"。
```

**三处刻意的约束**（都可讲）：
1. **字数上限（100 字）** → 卡片是移动端窄屏，长了没人看；对应 `max_tokens=300` 的硬上限；
2. **回答结构固定**（原因 + 风险）→ 避免模型漫谈；
3. **强制免责声明**（"非投资建议"）→ **合规底线**，不能只靠模型自觉，是**写进提示词的**。

**优雅降级**：`llm.summarize` 失败/未配置时返回 `None`（[L24-L37](../backend/app/services/llm.py#L24-L37)），
**提醒照样推送**，只是卡片里没有解读。**大模型不是链路的关键路径**——这保证了"AI 挂了，业务不挂"。

### 2.2 应用二：实时解读（按需，纯 API）

[`POST /api/insights`](../backend/app/api/insights.py#L12-L26)：给一个代码 → 取最新安全区间 → 实时生成一段解读。

**注意两处现状**：
- 前端目前**没有调用它的入口**（[api.js](../frontend/src/api.js) 里没有 insights 方法）→ 属于"**能力已具备、入口待接**"；
- 它把 `price` 参数传成了 `zone_low`（[L22-L23](../backend/app/api/insights.py#L22-L23)）——**现价示意值，不精确**（列入 §7 局限）。

### 2.3 应用三：对比研判（手动，本项目最有"AI 味"的功能）

在股池页勾选 2–8 只 → 点"开始分析" → `POST /api/compare/analyze` → [`compare.analyze`](../backend/app/services/compare.py#L114-L142)：

```
① 去重 + 截断：codes = list(dict.fromkeys(codes))[:MAX_CODES]        （MAX_CODES = 8）
② 按需实时抓资金流（一次请求，≤8 只），失败降级用已落库数据
③ build_metrics：本地拼出 17 列指标（现价/涨幅/区间三态/MA250乖离/得分/ROE/CAGR/PE分位/资金流…）
④ llm.compare(metrics)  ← **一次调用覆盖全部标的**，返回 {overall, items:[{code, view, reason, support, resistance, confidence}]}
⑤ **白名单回填**：以本地 metrics 为主循环，按 code 把 AI 结论合并进去
⑥ 落库 compare_analyses（items JSONB + ai_summary），返回 id 供前端跳转
```

**第 ④ 步是成本设计**：**8 只股票只发 1 次请求**（不是 8 次）——token 与延迟都最优。
**第 ⑤ 步是关键设计**：见 §4.3"白名单回填"。

**前端呈现**（[Compare.vue](../frontend/src/views/Compare.vue)）：
`ai_summary` 一句话总体结论（[L19](../frontend/src/views/Compare.vue#L19)、[L34](../frontend/src/views/Compare.vue#L34)）、
每只股票的 `view` 彩色标签（看多→红 / 看空→绿，[L139](../frontend/src/views/Compare.vue#L139)）、
`confidence` 置信度标签（[L99-L100](../frontend/src/views/Compare.vue#L99-L100)）、
`reason` / `support` / `resistance` 逐项展示。
**历史可回看**：`GET /api/compare/history` 与 `/api/compare/{id}`。

---

## 3. 怎么用（配置 → 调用 → 排错）

### 3.1 配置（三步）

```ini
# ① 复制模板
copy .env.example .env

# ② 填三行（其余 LLM 项保持默认即可）
LLM_ENABLED=true
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-********            ← 只填这里，不提交仓库

# ③ 重启后端让配置生效（配置在启动时读一次，且有 lru_cache 缓存）
docker compose up -d --build backend
```

**注意**：`get_settings()` 带 `@lru_cache`（[config.py L43-L45](../backend/app/config.py#L43-L45)），
**配置只在进程启动时读一次** → **改 `.env` 必须重启后端**，热改不生效。

### 3.2 手动调一次（先验证通路）

```sh
# 实时解读（PowerShell：JSON 建议写文件后 --data-binary 传，避免引号被吞）
curl.exe -s -X POST http://localhost:8000/api/insights -H "Content-Type: application/json" -d "{\"code\":\"600988\"}"
```
> 若返回 `503 llm disabled or unavailable`，说明**没配 key 或调用失败**（[insights.py L24-L25](../backend/app/api/insights.py#L24-L25)）。

**最容易上手的是走界面**：股池页勾 2–8 只 → 点"开始分析" → 自动跳转结果页 → 历史页可回看（无需手写请求）。

### 3.3 排错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 卡片/结果里**没有 AI 内容** | `LLM_ENABLED=false` 或 `LLM_API_KEY` 空 | 检查 `.env`，重启后端 |
| `503 llm disabled or unavailable` | `summarize` 返回 `None`（未配置或调用异常） | 看后端日志 `llm summarize failed` |
| 结果页显示"大模型未返回结论" | `compare` 返回 `None`，或返回内容**无法解析成 JSON** | 看日志 `llm compare failed`；可能模型没按 JSON 输出 |
| 请求超时 | `LLM_TIMEOUT=20` 太短（长输出场景） | 上调 `LLM_TIMEOUT` |
| 输出被截断（理由写到一半） | `max_tokens` 上限（提醒 300 / 对比 1600） | 上调 `LLM_MAX_TOKENS` 或收窄提示词要求 |
| 配置改了没生效 | `lru_cache` + 进程内读取 | **重启后端**（`up -d --build backend`） |

**日志关键字**（排错先看这两个）：

```
llm summarize failed: ...      ← 提醒解读失败
llm compare failed: ...        ← 对比研判失败
```

---

## 4. 提示词工程（本项目真正"怎么用模型"的部分）

### 4.1 提醒解读：短、有结构、带合规

见 §2.1 的提示词：**角色 + 事实（代码/名称/现价/区间）+ 任务（原因 + 风险）+ 字数上限 + 免责声明**。

**讲解要点**：这是一条"**填空式**"提示词——把所有**变量都来自数据库**，
模型没有"自由发挥"的空间，只在**表达**上有自由度。**风险最低、最可控。**

### 4.2 对比研判：本项目提示词设计最讲究的一处

[`COMPARE_PROMPT`](../backend/app/services/llm.py#L42-L56) 拆开看，有**四层设计**：

**第 1 层：角色 + 输入来源声明**
```
你是证券分析助手。以下是同一天的候选股票量化指标（来自日线/盘中快照、安全区间、基本面与资金流），
表头为列名、每行一只股票：
```
→ 明确"**数据从哪来**"，让模型知道自己在看**同一时点、同口径**的横向对比数据。

**第 2 层：口径声明（防幻觉的关键）**
```
注：资金流为东方财富按单笔成交金额分档推算的统计口径（主力净额 = 超大单 + 大单净额），并非真实席位数据；
表中资金流单位为万元，"当日主力净额"为当日累计值（盘中为开盘至今），"-" 表示该维度缺失、不要据此判断。
```
→ 三个信息缺一不可：
1. **数据本质**（推算口径，**不是真实席位数据**）——否则模型会当成"主力真实席位"来解读；
2. **单位**（万元）——否则会把 `1234.6` 当成"元"或"亿元"；
3. **缺失语义**（`-` 表示缺失，**不要据此判断**）——否则模型会**把缺失当 0** 来推理（典型幻觉来源）。

**第 3 层：硬性要求（6 条，逐条对应一个失败模式）**

| 要求 | 防的失败模式 |
|---|---|
| 只做定性判断，**禁止编造精确的上涨概率数字或百分比** | **伪精确**（本项目最重要的一条） |
| `view` 只能是「看多」「中性」「看空」之一 | 输出不受控、无法渲染与统计 |
| `reason` **不超过 60 字**，说明主要依据与风险 | 啰嗦、要点不清 |
| `support`/`resistance` 为数值，**无法判断填 `null`** | 编造点位（**给"可以不答"的出口**，比强迫回答更安全） |
| `confidence` 只能是「高」「中」「低」 | 同上，枚举约束 |
| `overall` 一句话 | 便于卡片/页面顶部展示 |

**第 4 层：输出格式约束**
```
严格只输出 JSON（不要 markdown 代码块、不要多余文字），格式：
{"overall": "...", "items": [{"code": "600988", "view": "看多", "reason": "...", "support": 32.5, "resistance": 38.0, "confidence": "中"}]}
```
→ **给了样例**（few-shot 的最简形态）+ **明确禁止 markdown 包裹**（虽然代码仍做了兼容，见 §4.4）。

**讲解要点**：**"6 条硬性要求分别对应 6 种已知的模型失控方式"**——这是本文最能体现深度的一句。
面试/答辩时能说出"每条约束背后是哪种失败模式"，比背提示词本身强得多。

### 4.3 白名单回填：不让模型"发明股票"

[compare.py L124-L132](../backend/app/services/compare.py#L124-L132)：

```python
ai_items = {it.get("code"): it for it in (ai or {}).get("items", []) if isinstance(it, dict)}
items = []
for m in metrics:                          # ← **以本地 metrics 为主循环**
    a = ai_items.get(m["code"], {})        # ← 按 code 取 AI 结论，取不到就是空字典
    items.append({**m, "view": a.get("view"), "reason": a.get("reason"),
                  "support": a.get("support"), "resistance": a.get("resistance"),
                  "confidence": a.get("confidence")})
```

**三层保护**
1. **标的以本地为准**：AI 返回了不存在的代码 → **被直接忽略**（不可能混进结果）；
2. **字段以本地为准**：所有数值指标（现价、区间、得分、资金流）**一律来自本地**，AI 只能补 5 个定性字段；
3. **AI 缺失不阻塞**：`ai=None`（未配置/失败）时，结果照样落库，只是定性字段为空（前端显示"未判断/未返回结论"）。

**讲解要点**：**"开源模型的输出要当'不可信输入'处理"**——就像对待任何外部接口一样。
这条设计让"模型胡说"的最坏后果被限制在"某只股票的 `view` 不准"，**而不是"结果里多出一只不存在的股票"**。

### 4.4 解析鲁棒性：兼容模型"不听话"

```python
def _extract_json(text):                      # [llm.py L83-L95]
    t = text.strip()
    start, end = t.find("{"), t.rfind("}")     # 取第一个 { 到最后一个 }
    obj = json.loads(t[start:end + 1])         # 解析失败 → 返回 None
    return obj if isinstance(obj, dict) else None
```
**为什么必须这么写**：即使提示词写了"不要 markdown 代码块"，模型**仍可能**输出
```` ```json ... ``` ```` 包裹或加一句解释。**"取首尾花括号"** 能兼容这些常见偏差。
**再失败就返回 `None`**（而不是抛异常）→ 走优雅降级路径。

### 4.5 输入侧的数据工程（把数字"喂得清楚"）

[`_format_table`](../backend/app/services/llm.py#L61-L75) 把 metrics 渲染成**一行一只股票的纯文本表**：

```
代码 名称 现价 涨幅% 区间下沿 区间上沿 位置 MA250乖离% 得分 ROE均值% 营收CAGR% 净利CAGR% PE5年分位% 主力净额万 主力净占比% 超大单净占比% 近5日主力净额万
```

三个细节（都可讲）：
1. **表头自带单位**（`%`、`万`）→ 减少模型对单位的猜测；
2. **缺失统一填 `-`**（`str(x if x is not None else "-")`）→ 与提示词里的"缺失语义"呼应；
3. **单位换算在本地做**：`_wan()`（[L78-L80](../backend/app/services/llm.py#L78-L80)）把"元 → 万元"，
   **不让模型做单位换算**（换算是确定性的活，交给代码）。
4. **位置用中文**：`_POS = {"above": "高于上沿", "in": "区间内", "below": "低于下沿"}`（[L58](../backend/app/services/llm.py#L58)）
   → **内部枚举值不直接暴露给模型**，避免"above 是什么意思"的歧义。

**讲解要点**：**"把模型当人看"——你会怎么给分析师递一张表，就该怎么组织提示词。**
单位、缺失、口径、中文语义，一个都不能少。

### 4.6 一句话总结提示词策略

> **输入给"干净的事实"，输出要"受限的枚举"，中间用"白名单回填"兜住。**

---

## 5. 产生了什么价值

### 5.1 四层价值（从"能用"到"有用"）

| 层次 | 价值 | 具体体现 |
|---|---|---|
| **① 可读性** | 把冰冷数字变成人话 | 提醒卡片里的一句话解读；对比页的"看多/中性/看空 + 60 字理由" |
| **② 决策辅助** | 把 **17 列量化指标**压缩成 **3 个判断 + 4 个关键点位** | 人不用自己对着表算"这 8 只谁强"，AI 给出相对强弱与理由 |
| **③ 留痕与复盘** | **结构化落库**（不是一段聊天记录） | `compare_analyses.items` 带 `view/reason/support/resistance/confidence`，历史页可回看、可比对、可统计 |
| **④ 工程示范** | 用"**别让 AI 做它做不好的事**"的方式用 AI | 数值决策全在本地、AI 只做表达；失败不阻塞主链路；输出不落地不确定的数字 |

### 5.2 价值要"可讲"：三个具体例子

**例 1（提醒链路）**：原本的提醒是"`600988 现价 32.10，区间 [30.00, 36.00]`"——**用户不知道该怎么理解**；
现在多了一句"**原因 + 风险 + 非投资建议**"，卡片**从通知变成了解读**。

**例 2（对比链路）**：8 只股票 × 17 列 = **136 个数字**要人肉比对；
AI 输出"谁看多、依据什么、支撑压力在哪、置信度多少"——**信息密度不变，认知负担骤降**。

**例 3（成本视角）**：**8 只股票只发 1 次请求**（[compare.py L123](../backend/app/services/compare.py#L123)）、
输出上限 1600 token（[`COMPARE_MAX_TOKENS`](../backend/app/services/llm.py#L40)）、
提醒解读上限 300 token 且**每只票每天最多 1 次**（Redis 去重，[monitor.py L25-L28](../backend/app/services/monitor.py#L25-L28)）
→ **成本被结构性限制在很低的水位**：它只在"有结论要解释"时才被调用，不做无意义的轮询。

### 5.3 反过来说：**如果不接大模型，会损失什么**

- 提醒只有数字，**没有"为什么/风险是什么"**；
- 对比页只有表，**没有相对强弱的结论**（人得自己归纳）；
- 没有可回看的**结构化研判记录**（AI 结论落库后能形成"历史观点库"）。

**讲解要点（加分项）**：**价值要能对上"没有它会怎样"**。
说"接了 AI 变智能了"是空话；说"把 136 个数字压缩成 3 个判断，且结论落库可复盘"才是价值。

---

## 6. 工程边界：LLM 的"四个不"

| 不 | 含义 | 实现位置 |
|---|---|---|
| **不阻塞** | 调用失败/未配置，主链照常跑（提醒照发、对比照存） | `summarize`/`compare` 返回 `None` + 调用侧判空 |
| **不失守** | 不输出精确概率数字；无法判断就填 `null` | `COMPARE_PROMPT` 硬性要求 1、4 |
| **不越权** | 数值指标一律本地算，AI 只补 5 个定性字段 | 白名单回填（§4.3） |
| **不联网** | 提示词里的所有事实**都来自本项目数据库**，不让模型自己找数据 | `_format_table` 只喂本地 metrics |

**讲解要点**：**"用大模型的正确姿势是先想清楚'它不许做什么'"。**
这四个"不"就是本项目的 AI 治理边界，比列一堆功能更能说明工程成熟度。

---

## 7. 已知局限（诚实清单）

| # | 局限 | 后果 | 位置 |
|---|---|---|---|
| 1 | **提示词硬编码在代码里** | 改提示词要改代码 + 重建镜像，无法热更新 | [llm.py L11-L56](../backend/app/services/llm.py#L11-L56) |
| 2 | **无调用日志与 token/成本统计** | 无法回答"这个月花了多少""哪次调用失败" | 仅 `log.warning` |
| 3 | **无重试机制** | 一次网络抖动即永久缺失（提醒因 Redis 当日去重，**当天不会再补**） | [L35-L37](../backend/app/services/llm.py#L35-L37) |
| 4 | **无幻觉校验**（`support`/`resistance` 未与真实均线/布林轨比对） | 点位可能是模型"编"的漂亮数字 | [compare.py L129-L131](../backend/app/services/compare.py#L129-L131) |
| 5 | **`/api/insights` 用区间下沿当现价** | 解读里的价格不真实 | [insights.py L22-L23](../backend/app/api/insights.py#L22-L23) |
| 6 | **对比历史未合并「同日多次分析」** | 同一天多次分析是独立记录，**无版本/去重概念** | `compare_analyses` |
| 7 | **单次同步调用**（无并发、无批量合并） | 提醒触发多只时是**逐只串行调用**，尾延迟叠加 | [monitor.py L73](../backend/app/services/monitor.py#L73) |
| 8 | **无质量评估** | 没有人工标注/打分，"AI 准不准"没有数据支撑 | 全局 |
| 9 | **输出可能被 `max_tokens` 截断** | 理由写一半 → JSON 解析失败 → 整条 AI 内容丢失 | `COMPARE_MAX_TOKENS=1600` |
| 10 | **前端未接 `/api/insights`** | 能力"有后端没入口" | [api.js](../frontend/src/api.js) |

### 改进优先级

| 优先级 | 动作 | 收益 |
|---|---|---|
| 1 | **加 `llm_calls` 日志表**（scene / model / prompt_tokens / completion_tokens / latency / ok / err） | 从"不知道花了多少、失败率多少"到**可观测**，是成本与质量治理的前提 |
| 2 | **提示词外置**（配置或数据库），配合 [飞书 Base 当配置中心](feishu.md) | 调提示词**不用发版**，可快速迭代 |
| 3 | **失败重试 + 提醒解读补推**（或允许"当日重试一次"） | 消除"网络抖动 → AI 内容永久缺失" |
| 4 | **点位校验**：`support`/`resistance` 与 MA20/MA250/布林轨比对，偏离过大则置 `null` | **直接降低幻觉可见度** |
| 5 | 支持 `response_format={"type":"json_object"}`（厂商支持时） | 结构化输出**从"兼容解析"变成"协议保证"** |
| 6 | `/api/insights` 接口传入**真实现价** + 前端补入口 | 修正不准确输入、让能力可达 |
| 7 | 多只提醒**合并成一次调用**（与 [飞书降噪卡片](feishu.md) 同理） | 降低延迟与成本 |

---

## 8. 参数与调用速查表

| 项 | 值 / 位置 |
|---|---|
| 模型 | `deepseek-chat`（[.env L17](../.env.example#L17)） |
| 接口 | OpenAI 兼容，`https://api.deepseek.com/v1` |
| 开关 | `LLM_ENABLED=true`（关闭则全部调用跳过、不报错） |
| 超时 | `LLM_TIMEOUT=20` 秒 |
| 输出上限（提醒解读） | `LLM_MAX_TOKENS=300` |
| 输出上限（对比研判） | `COMPARE_MAX_TOKENS=1600`（[llm.py L40](../backend/app/services/llm.py#L40)） |
| 单次对比标的数上限 | `MAX_CODES=8`（[compare.py L14](../backend/app/services/compare.py#L14)） |
| **对比分析的网络请求数** | **≤8 只仍只发 1 次**（[compare.py L123](../backend/app/services/compare.py#L123)） |
| 提醒解读调用频率 | **每只票每天最多 1 次**（Redis `SET NX EX 86400`，[monitor.py L25-L28](../backend/app/services/monitor.py#L25-L28)） |
| 解读字数约束 | ≤100 字（提示词内） |
| 研判字段 | `view`(看多/中性/看空) / `reason`(≤60 字) / `support` / `resistance`(可 null) / `confidence`(高/中/低) |
| 禁止输出 | **精确概率/百分比** |
| 落库位置 | `alert_logs.ai_summary`、`compare_analyses.items` + `ai_summary` |
| 失败兜底 | 返回 `None`，主链继续；前端提示"未配置 LLM_API_KEY" |

---

## 9. 十分钟讲解提纲

| 时长 | 内容 | 一句话开场 |
|---|---|---|
| 1 min | §0 职责分工图 | "**要精确的交给代码，要表达的交给模型**——它在本项目里是解说员，不是操盘手。" |
| 1 min | §1 用的是什么（DeepSeek + OpenAI 兼容） | "换厂商只改 `.env` 三行，代码零改动——**依赖协议，不依赖厂商**。" |
| 2 min | §2 三处应用 + 对比研判的完整链路 | "股池勾 2–8 只 → 一次请求 → 结构化结论落库 → 历史可回看。" |
| 3 min | §4 提示词设计（**重点**） | "6 条硬性要求，分别对应 6 种模型失控方式；口径声明的三个信息缺一不可。" |
| 1 min | §4.3 白名单回填 | "AI 的结论是**不可信输入**：标的以本地为准，它编的股票进不了结果。" |
| 1 min | §5 价值 | "把 136 个数字压缩成 3 个判断，而且**结论落库、可复盘**。" |
| 1 min | §6 四个"不" + §7 局限 | "先定义它不许做什么；再诚实说出它现在还做不好什么。" |

**讲 §4 时一定要用"失败模式 → 对应约束"的方式讲**：
"不给约束 → 它会给你一个 68% 的上涨概率，而这个数字毫无意义，却看起来很专业。"

---

## 10. 常见问答

| 提问 | 回答要点 |
|---|---|
| 用的什么模型？ | `deepseek-chat`，走 **OpenAI 兼容接口**（`base_url` 在 `.env`），可一键切厂商或本地模型 |
| 为什么不用 LangChain 之类框架？ | 需求只有"两次结构化调用"，**一个 SDK 调用就够**；引入框架增加依赖与调试成本（**遵循"不要过度设计"**） |
| 大模型参与选股/算区间吗？ | **不参与**。选股是硬门槛 + 加权打分，区间是 MA250 公式，全是**可复现的本地算法** |
| 为什么不预测"次日上涨概率"？ | **大模型无法可靠预测次日涨跌**；给百分数只会产生**伪精确**。所以只输出定性判断 + 理由 + 点位 + 置信度 |
| 输出格式怎么保证是 JSON？ | 提示词**硬性要求只输出 JSON 并给样例**；代码再用 `_extract_json` 兼容 markdown 包裹；再失败就返回 `None` 降级 |
| 模型胡说怎么办？ | 三层限制：**标的以本地为准**（编的股票进不来）、**数值字段全本地**、**AI 只补 5 个定性字段**；再加上点位未校验这一点如实承认（§7） |
| 提示词里的口径声明为什么要写那么多？ | 因为**不写就会出错**：不写"单位万元"它会当成元；不写"`-` 表示缺失"，它会**把缺失当 0** 推理 |
| AI 挂了会影响提醒吗？ | **不会**。`summarize` 失败返回 `None`，提醒照发、记录照存，只是卡片少了那句解读 |
| 成本高吗？ | 结构上很低：**8 只股票只发 1 次请求**；提醒**每只票每天最多 1 次**（Redis 去重）；输出有 300/1600 token 上限 |
| 能换成不用联网的模型吗？ | 能。`LLM_BASE_URL` 指向 vLLM/Ollama 等本地端点即可，**协议兼容就能无缝切换**（数据不出域） |
| 为什么改 `.env` 不生效？ | `get_settings()` 有 `lru_cache`，配置**只在进程启动时读一次** → **必须重启后端** |
| 前端怎么用这个功能？ | 股池页勾选 → "开始分析" → 结果页看 `view/reason/support/resistance/confidence` → 历史页回看 |
| `/api/insights` 前端能用吗？ | 后端能力在，**前端还没接入口**（属待办，见 §7）；它当前传入的"现价"用的是区间下沿，不精确 |
| token 用到多少？ | 提醒解读每次 ≤300 token、对比每次 ≤1600 token；**目前没有统计表**，无法给月度数字（这是 §7 局限 2 的改进项） |

---

## 11. 代码地图（讲的时候按这个顺序翻）

| 顺序 | 文件 | 讲什么 |
|---|---|---|
| 1 | [config.py L17-L23](../backend/app/config.py#L17-L23) | 模型/地址/超时/token 上限（**配置即切换厂商**） |
| 2 | [llm.py L15-L37](../backend/app/services/llm.py#L15-L37) | `_client` + `summarize`（提醒解读：短提示词 + 免责声明 + 失败返回 None） |
| 3 | [llm.py L40-L56](../backend/app/services/llm.py#L40-L56) | `COMPARE_PROMPT`（四层提示词设计：角色/口径/硬性要求/格式） |
| 4 | [llm.py L61-L95](../backend/app/services/llm.py#L61-L95) | `_format_table` / `_wan` / `_extract_json`（喂数据与解析鲁棒性） |
| 5 | [compare.py L114-L142](../backend/app/services/compare.py#L114-L142) | 对比链路：一次请求 + **白名单回填** + 落库 |
| 6 | [monitor.py L73-L82](../backend/app/services/monitor.py#L73-L82) | 提醒链路里 LLM 的位置（**失败不阻塞推送**） |
| 7 | [insights.py L12-L26](../backend/app/api/insights.py#L12-L26) | 按需实时解读（纯 API 能力） |
| 8 | [Compare.vue L19-L100](../frontend/src/views/Compare.vue#L19-L100) | 前端呈现（总体结论 / 标签 / 置信度 / 点位） |
