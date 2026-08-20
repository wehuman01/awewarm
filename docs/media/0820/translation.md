# awewarm：牛来，让你的订阅时刻热起来

![awewarm](../../../logo/hero2.webp)

所有 AI 编程订阅都有使用窗口期。就好像每个人的心里都有个牛来。Claude Max 从第一次请求起给你 5 小时，Codex 和第三方 token 计划也是如此。你早上 9 点开工，窗口到下午 2 点关闭。午休回来，下午 3 点再坐下，第一请求就会开启一个全新窗口——只为一段零散的会话烧掉配额。awewarm 解决这个问题：在正确的时间自动发送一次最小请求，让你坐下写代码时，订阅窗口已经提前打开。就好像牛来，或许你永远不会去电影院看它，但小某书、视频号，它的形象已经深入脑海。

和awesome 的其他工具一样，awewarm 也是面向ai 的。所以安装只需一句话："Read https://github.com/mugpeng/awewarm/blob/main/README.ai.md and follow it."（阅读这个 README 并照做。）有aweskill 的情况下，AI 智能体会自动安装包、向 launchd 注册调度器、从 macOS Keychain 读取你的 Claude Code 登录态，从 `~/.codex/auth.json` 读取 Codex 登录态，设置固定时间网格，并添加任何订阅连接。两分钟后，`awewarm status` 全部绿灯。工具的上手成本从你转移到了 AI 智能体身上。

GitHub: [github.com/mugpeng/awewarm](https://github.com/mugpeng/awewarm)

## 两种模式：固定与间隔

### 固定模式：绝对时间，永远安全

固定模式是默认模式。你给它一组本地时间（`--times 06:35,11:40,16:45,21:50`）和日期（`weekday` 或 `every-day`）。每个时段都会开启一个全新窗口。如果机器处于睡眠状态，该时段会在补跑窗口（默认 30 分钟）内延迟触发；超过补跑窗口则跳过。距离上次成功不足 30 分钟的时段也会跳过，防止重复触发。

如果窗口时长已知（Claude Code 已确认为 5 小时），awewarm 会询问每日配额重置时间，然后计算全天网格：每个窗口一个时段，间隔为 `window + 5 分钟`。例如从 01:14 开始的 5 小时窗口，得到 `01:14, 06:19, 11:24, 16:29, 21:34`。未确认窗口时长的计划（比如 Codex）会先以固定模式启动，窗口确认后再切换到间隔模式。

### 间隔模式：滚动续期

间隔模式串联窗口。每次成功后，下一次请求会在 `window + grace + jitter` 之后调度（默认 300 分钟 + 75 秒 + 最多 30 秒抖动）。缓冲期（grace）**在旧窗口关闭后**才开始运行——提前触发会落在旧窗口内，什么也开启不了。在没有任何成功记录时，第一次请求会立即作为锚点触发（`--start HH:MM` 可以推迟）。手动 `run` 不会移动链，除非使用 `--reset-due`。间隔模式在窗口通过 `--window` 验证前**处于锁定状态**——错误时长的连锁调度比固定模式更危险。

## 健康阶梯：四级状态，一条恢复路径

大多数 cron 类工具只有两种状态：正常或故障。awewarm 有四种：

![health-ladder](../../media/images/health-ladder.png)

- **正常（Connected）** — 正常运行。时段按时触发，链按时续期。
- **故障中（Failing）** — 一个节点失败。补跑重试：30 分钟内 5 次尝试，间隔 5 分钟。任何成功都会重置阶梯。
- **降级（Degraded）** — `degradeAfterNodes`（默认 3）个连续节点丢失。补跑停止。每个节点只尝试一次。成功重置阶梯。
- **已自动禁用（Auto-disabled）** — 降级状态下再丢失 3 个连续节点。进入静默。只有 `--on` 或一次成功的手动 `run` 才能恢复。

不算作节点的情形：手动运行、机器睡眠错过的时段、故障中状态内的补跑重试。`awewarm status` 显示当前层级：`健康状态: 故障中 -- 已丢失 1/3 个节点，补跑尝试 2/5`。

## 账号与订阅连接

awewarm 同时支持 CLI 登录和 API key 订阅——五种传输方式，一种配置。

**Claude Code** — 从 macOS Keychain 或 `~/.claude/.credentials.json` 自动检测。5 小时窗口已确认。不存储凭证；awewarm 复用现有的登录状态。保温命令：`claude -p --model haiku "Reply with exactly: ok"`。

**Codex** — 从 `~/.codex/auth.json` 自动检测。窗口时长未知，以固定模式启动。保温命令：`codex exec -m <model> "Reply with exactly: ok"`。

**订阅计划** — 任何 OpenAI Chat / Responses / Anthropic-compatible 端点，只要提供 base URL + API key。支持协议：`openai-chat`、`openai-responses`、`anthropic-messages`。密钥存储在 `secrets.json`（权限 0600）中，供后台调度器读取。`plan.url` 字段存储在连接配置中。Claude Code 账号、Codex 账号、GLM token 计划、DeepSeek token 计划——全部由同一套配置管理，在同一调度器上运行。

## 架构设计：心跳调度，而非守护进程

没有守护进程（daemon）。没有常驻进程。系统调度器（launchd / Task Scheduler / systemd timer）每分钟调用一次 `awewarm tick`。心跳调度（tick）加载配置和状态，计算动作，发送到期请求，记录结果，保存状态，然后退出。即 `(config, state, now) → (actions, new_state)` 的纯函数——可测试、可检查、不会漂移。（**纯函数**：指输出仅由输入决定、无副作用的函数。）

在 macOS 上，launchd 会在时段到来时把机器从睡眠中唤醒（无需 sudo）。在 Windows 上，额外的 Task Scheduler 任务带有 *唤醒运行（Wake to run）* 设置。在 Linux 上，唤醒后的错失时段会在补跑窗口内自动补跑。每个连接都可以用 `--no-wake` 单独关闭唤醒。

## 技能可以触达的命令栈

| 你说 | 技能执行 |
|---|---|
| "查看 awewarm 状态。" | `awewarm status` |
| "把 Claude Code 的保温时间设为工作日 06:35、11:40、16:45、21:50。" | `awewarm config set claude-code --times 06:35,11:40,16:45,21:50 --days weekday` |
| "把 Claude Code 切换为间隔模式。" | `awewarm config set claude-code --mode interval`（在 `--window 300` 之后） |
| "添加我的 GLM 编程订阅。" | 交互式 `awewarm config add` |
| "这周暂停 Codex 的warm请求。" | `awewarm config set codex --off` |
| "恢复 Codex 并重置健康状态。" | `awewarm config set codex --on` |
| "为什么 Claude Code 停止保温了？" | `awewarm status claude-code` — 显示健康层级和最近失败 |
| "现在就触发一次 Claude Code 保温，我想测试一下。" | `awewarm run claude-code` |
| "把补跑窗口改为 45 分钟。" | `awewarm config settings --catchup-minutes 45` |



### 快速模板

常用调度模式速查：

```bash
# 标准工作日（上午 + 下午）
awewarm config set <id> --times 06:00,11:05,16:10

# 含晚间加班
awewarm config set <id> --times 06:00,11:05,16:10,21:15

# 仅工作日
awewarm config set <id> --times 08:00,13:05 --days weekday

# 滚动续期（已验证的 5 小时窗口）
awewarm config set <id> --mode interval --window 300 --anchor 11:05
```

以上所有固定时间点均间隔 5 小时 5 分钟 —— 等于订阅窗口（5 小时）加 5 分钟调度缓冲。四个时间点（`06:00, 11:05, 16:10, 21:15`）覆盖全天：上午、下午、晚间和深夜加班。三个时间点（`06:00, 11:05, 16:10`）覆盖标准工作日。`--days weekday` 限制只在工作日触发。两个时间点的链适合半天或间歇性使用。你可以参考我的:
```
awewarm status

doubao (doubao) — connected
  Mode: fixed
  Times: 06:35, 11:40, 16:45, 21:50 (weekday)
  Last activation: today 11:40
  Next due: today 16:45 (fixed)

glm (glm) — connected
  Mode: fixed
  Times: 05:50, 10:55, 16:00, 21:05 (every-day)
  Last activation: today 11:23
  Next due: today 16:00 (fixed)
```

妈妈再也不用担心我token 计划用的不开心了。



## 为什么这很重要

每个订阅都在变得越来越贵。token 计划不断缩减配额上限的同时提高费用。账很简单：如果你付了 5 小时窗口的钱，实际只用了 3/5，等于把 40% 的钱白白浪费在桌上。下午 3 点冷启动时，早上 9 点开的那个窗口早就废了——这不只是不方便，而是实打实地损耗了你已付费的配额。

awewarm 就是订阅的配额利用率优化器。它确保你付的每一个窗口在你需要时都能完整可用。没有冷启动，没有碎片窗口，没有浪费的配额。每次保温请求的边际成本几乎为零；而一个完整的 5 小时窗口，边际收益就是整个订阅。

三个设计决定让它经久耐用。健康阶梯是**渐进的，而非二元的**——一次失败是抖动，三次是模式，六次是事实。任何成功都会重置阶梯。心跳调度**没有守护进程**——无状态、透明、状态以 JSON 存在磁盘上。传输层是**统一的**——五种传输，一种配置格式。AI 智能体安装它，调度器运行它，你的订阅满额工作。

## 试用

告诉你的 AI 智能体：

> "Read https://github.com/mugpeng/awewarm/blob/main/README.ai.md and follow it."

然后检查状态：

```bash
awewarm status
```

接下来，问题都会变得很直接：

- "把 Claude Code 设为每 5 小时保温一次，从工作日 06:35 开始。"
- "添加我的 Codex 账号。"
- "为什么我的 GLM 订阅停止保温了？"
- "把 Claude Code 切换为间隔模式。"
- "周末暂停所有保温请求。"

AI 智能体本来就懂这些命令，只是你还没把 README 给它。

## mugpeng 的其他项目

awewarm 属于 aweteam 生态：

- **[aweskill](https://aweskill.webioinfo.top/)** — 面向 47+ AI 编程智能体的 CLI 优先技能包管理器
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — 为 Claude Code、Codex 和 OpenCode 设计的智能体配置切换器
- **[awerouter](https://github.com/mugpeng/awerouter)** — 根据结构信号将请求路由到 Flash 或 Pro 模型的智能 LLM 路由器
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — 支持配置感知恢复的 AI 编程会话管理器
- **[aweshare](https://github.com/wehuman01/aweshare)** — 本地优先的 AI 能力中继：共享 GPU 和 API key，但不暴露密钥本身
- **[awewarm](https://github.com/mugpeng/awewarm)** — 让 AI 编程订阅窗口保持稳定可用的订阅窗口保温器
