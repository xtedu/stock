# 飞书通知绑定指南

本文说明如何把本项目的安全区间提醒推送到飞书，属于 [technical-design.md](technical-design.md) 中「通知通道」一节的操作细则。

## 1. 方案定位：不需要企业认证

本项目的飞书提醒走**群自定义机器人 Webhook**，与飞书开放平台自建应用是两套完全不同的东西：

| 对比项 | 群自定义机器人 Webhook（本项目采用） | 开放平台自建应用（lark-cli / MCP） |
|---|---|---|
| 用途 | 单向推送消息到固定群 | 读写用户的飞书文档、消息、日历等 |
| 需要的账号 | 普通飞书账号即可（个人版也行） | 需要企业/团队，具备开放平台建应用权限 |
| 是否需要开发者认证 | **否** | 通常需要管理员在开发者后台创建应用 |
| 配置方式 | 群设置里复制一个 Webhook URL | `config init` + `auth login` 授权 |
| 代码入口 | `backend/app/services/notifier.py::send_feishu` | 不属于本项目代码 |

因此「绑定飞书」在本项目里就是**拿到一个 Webhook 地址并写进 `FEISHU_WEBHOOK`**，不需要任何企业认证。

## 2. 前置条件

- 一个飞书账号（个人版可用）。
- 一个可以添加机器人的群。可以只建一个自己的单人群，避免打扰他人。
- 项目已能正常启动（`docker compose up -d` 后后端容器运行中）。

## 3. 配置步骤

### 3.1 在飞书创建自定义机器人，获取 Webhook

1. 打开飞书，进入目标群（没有就新建一个，例如「股票提醒」）。
2. 点右上角「···」→ 群设置 → **群机器人** → 添加机器人 → **自定义机器人**。
3. 填写名称（例如「股票提醒」），可选头像，点击下一步。
4. 安全设置建议**三项全部留空**：
   - 「签名校验」**必须关闭**——后端未实现签名计算，开启后推送会失败。
   - 「自定义关键词」若开启，消息正文必须包含该关键词才会投递，容易静默丢消息。
   - 「IP 白名单」若开启，需要放行容器出口 IP，而该 IP 通常不固定。
5. 复制生成的 Webhook 地址，形如：

   ```
   https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```

### 3.2 写入 `.env`

在项目根目录 `.env` 中填写（可参考 `.env.example` 第 23 行）：

```
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

注意事项：

- `.env` 已在 `.gitignore` 中忽略（见 `.gitignore` 第 2 行），不会被提交入库。
- Webhook 地址等同于凭据，泄露后任何人都能往该群发消息，不要贴到公开场合或工单里。
- 后端读取配置的方式是 `backend/app/config.py::Settings.feishu_webhook`（pydantic-settings 读取 `.env`），**改完必须重启进程才会生效**。

### 3.3 重启后端加载配置

```powershell
docker compose up -d backend
```

### 3.4 建立提醒规则

飞书只是通道，必须存在**已启用**的提醒规则，监控任务才会推送。两种方式：

- 页面方式：「我的」页面创建提醒规则，通道勾选飞书（前端默认 `channels: ['feishu']`，见 `frontend/src/views/Mine.vue`）。
- 接口方式：`POST /api/alerts/rules`，请求体：

  ```json
  { "code": null, "trigger": "enter_zone", "channels": ["feishu"] }
  ```

  `code` 为 `null` 表示全股票池；`trigger` 字段目前仅落库，实际判定逻辑固定（见第 4 节）。

规则字段定义见 `backend/app/models.py` 的 `AlertRule`，接口实现见 `backend/app/api/alerts.py`。

## 4. 触发条件、去重与消息格式

实现位于 `backend/app/services/monitor.py::check_and_notify`：

| 项目 | 规则 |
|---|---|
| 触发条件 | `当前价 < 安全区间下沿`（即低估区），仅此一种 |
| 价格来源 | 当日盘中快照 `intraday_spot.price` 优先；否则取 `daily_quotes` 最近一条收盘价 |
| 去重 | Redis key `alert:{code}:{YYYY-MM-DD}`，`SET NX EX 86400`，单只股票当日仅推送一次 |
| 落库 | 每次推送写入一条 `alert_logs`（含 `ai_summary`） |

消息由 `backend/app/services/notifier.py::send_feishu` 发送，为 `interactive` 卡片：

- 标题：`{股票名}({代码}) 低于区间下沿，低估区可关注`
- 正文：`当前价 X，安全区间 [下沿, 上沿]，预警线 Y（低于区间下沿，低估区可关注）`，后接大模型生成的解读（`backend/app/services/llm.py`）。

## 5. 验证方式

### 5.1 直接验证 Webhook 通路（推荐先做）

用后端发送的同一份 payload 结构手动打一次，能收到卡片即说明地址与安全设置都没问题：

```powershell
$payload = '{"msg_type":"interactive","card":{"header":{"title":{"tag":"plain_text","content":"股票提醒绑定测试"}},"elements":[{"tag":"markdown","content":"收到本条即表示 FEISHU_WEBHOOK 配置成功。"}]}}'
Invoke-RestMethod -Uri "<把这里换成你的 WEBHOOK>" -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($payload))
```

返回 `{"code":0,...}` 或 `{"StatusCode":0,...}` 即为投递成功。

### 5.2 验证业务链路

在「任务」页面点「立即监控提醒」，或调用接口：

```
POST /api/jobs/monitor        # 仅监控提醒
POST /api/jobs/intraday       # 盘中链：快照 → 筛选 → 区间 → 监控
```

返回 `{"triggered": n}`。常见 `triggered=0` 的原因见下表。

## 6. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 完全收不到消息 | `FEISHU_WEBHOOK` 为空，或后端未重启加载新配置 | 检查 `.env`，执行 `docker compose up -d backend` |
| 报签名相关错误（如 `code 19021`） | 机器人开启了「签名校验」 | 关闭签名校验，或后续为 `send_feishu` 补签名实现 |
| 报关键词不匹配 | 开启了「自定义关键词」 | 关键词填 `安全区间`（卡片正文含该词），或直接留空 |
| 报 IP 不在白名单 | 开启了「IP 白名单」 | 关闭白名单（容器出口 IP 不固定） |
| 返回成功但 `triggered=0` | 无启用规则 / 当日已推送过 / 无当日安全区间数据 | 见 5.2 的排查顺序：先查 `GET /api/alerts/rules`，再查 Redis 去重键，最后确认 `safety_zones` 有当日数据 |
| 规则存在仍不推送 | `alert_rules.enabled=false`，或 `channels` 未包含 `feishu` | 修数据或重建规则 |
| 盘中不推、收盘才推 | 价格来源优先级导致 | 确认 `intraday_spot` 是否有当日快照，或规则只在收盘链触发 |

## 7. 其他通知通道

`backend/app/services/notifier.py` 的 `CHANNELS` 注册了三类通道，均为「配了才启用」：

| 通道 | 配置项 | 说明 |
|---|---|---|
| 飞书 | `FEISHU_WEBHOOK` | 交互式卡片，本文档主通道 |
| Server酱 | `SERVER_CHAN_SENDKEY` | 微信推送，填 SendKey 即可 |
| 邮件 | `SMTP_HOST/PORT/USER/PASSWORD/FROM/TO` | 需六项齐全，走 SMTP_SSL |

规则未指定 `channels`（或为空）时，`notify()` 会向**所有已配置**的通道投递；指定后只投递指定通道。
