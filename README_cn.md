<div align="center">
  <img src="logo/hero2.webp" alt="awewarm" width="860">
  <h1>awewarm：订阅窗口保温器</h1>
  <p><strong>用一条最小请求，让 AI 编程套餐的窗口一直是热的。</strong></p>
  <p>接入一次；awewarm 自动检测你的 Claude Code / Codex 账号或订阅 endpoint 的能力，然后确保下一个用量窗口永远是已开启状态。</p>
  <p>
    <a href="./README.md">English</a> ·
    <strong>简体中文</strong>
  </p>
  <p>
    <a href="https://ko-fi.com/mugpeng"><img src="https://img.shields.io/badge/Ko--fi-Buy%20me%20a%20coffee-FF5E5B?style=flat-square&logo=ko-fi&logoColor=white" alt="Ko-fi"></a>
  </p>
  <p>
    <img src="https://img.shields.io/pypi/v/awewarm?style=flat-square&color=7C3AED" alt="Version">
    <img src="https://img.shields.io/badge/python-%E2%89%A5%203.9-0EA5E9?style=flat-square" alt="Python">
    <img src="https://img.shields.io/badge/license-MPL--2.0-22C55E?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-alpha-c96a3d?style=flat-square" alt="Status">
    <img src="https://img.shields.io/badge/install-pip-22C55E?style=flat-square" alt="pip install">
    <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-334155?style=flat-square" alt="Platform">
    <img src="https://img.shields.io/pepy/dt/awewarm?style=flat-square" alt="PyPI downloads">
    <img src="https://img.shields.io/github/stars/wehuman01/awewarm?style=flat-square" alt="GitHub stars">
  </p>
</div>

> 接入一次，之后不必再理解 5 小时重置：awewarm 会检测它能做什么，并只在支持的情况下让下一轮窗口自动开始。

awewarm 管理两类连接：

- **账号** —— 本机的 `claude` / `codex` CLI 登录。awewarm 复用它们的登录态，发送一条最小的无头请求（`Reply with exactly: ok`），不保存任何凭据。
- **订阅套餐** —— 任何 OpenAI Chat / OpenAI Responses / Anthropic 兼容的 endpoint（base URL + API key）。API key 存入 `~/.config/awewarm/secrets.json`（权限 600），供后台调度器随时读取。

调度分两种模式：`fixed` / `interval`，详见下文[调度模式](#调度模式)。interval 类续期在窗口语义已验证或用户确认前保持锁定；`fixed` 始终安全。

## 安装

需要 Python ≥ 3.9：

```bash
pip3 install awewarm
```

后台调度器支持 macOS（launchd）、Windows（任务计划程序）和 Linux（systemd 用户 timer —— 无桌面/SSH 账号先执行 `loginctl enable-linger $USER`）。没有 systemd 的环境可以用 cron 触发 tick：`* * * * * awewarm tick`。

所有密钥都保存在 `secrets.json` —— 环境变量引用方式已移除：后台调度器（launchd / systemd / 任务计划程序）读不到 shell 变量，会以 "API key unavailable" 静默失败。

## 快速开始

### 让 AI agent 代装

在 Claude Code、Codex 或其他编程 agent 里，对它说：

```text
阅读 https://github.com/wehuman01/awewarm/blob/main/README.ai.md 并按其指引安装和配置 awewarm。
```

Agent 会安装 CLI、只读扫描本机账号，并按你的要求调整调度。引导流程本身（`awewarm init`、`awewarm config add`）留在你的终端完成 —— 它需要交互式输入选项和 API key。装好之后可以直接问“下次保温是什么时候？”或“把 claude-code 改成 06:35 和 12:35”。

### 手动安装

```bash
awewarm init        # 扫描本机账号、选择调度、安装后台调度器
awewarm status      # 查看接下来会发生什么
```

无论账号还是 endpoint，添加时都会发一条测试请求 —— 模型名错误或 key 失效当场暴露，而不是等到早上六点。

接入订阅 endpoint：

```bash
awewarm config add
```

依次选择协议、输入 API base URL、API key 和模型；awewarm 会先用一条最小请求测试 endpoint，然后把 API key 存入 `secrets.json`（0600）。同一条命令也能重新添加之前删掉的 `claude` / `codex` 本机账号 —— 它会列出在这台机器上检测到的所有可管理项。

## 配套工具

awewarm 是 AI 编程 agent 工具家族的一员：

- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** —— Claude Code / Codex / OpenCode 的 agent profile 切换器。aweswitch 管理会话用哪个 provider 启动；awewarm 让该 provider 的订阅窗口在底下一直开着。如果你用 aweswitch 启动 coding-plan 套餐，awewarm 就是让这些 5 小时窗口夜里不凉掉的那一块拼图。
- **[aweskill](https://github.com/Webioinfo01/aweskill)** —— AI agent 的 CLI skill 包管理器（支持 47+ agent）。
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** —— Claude Code / Codex 的会话书签管理器。
- **[awerouter](https://github.com/mugpeng/awerouter)** —— 智能 LLM 路由器：按结构化信号切分 flash/pro。

## 调度模式

两种模式发送的都是同一条最小请求，区别只在触发时机。用 `awewarm config set <id> --mode fixed|interval` 切换；用 `awewarm status` 查看当前模式和下一次触发时间。

| 模式 | 触发时机 | 需要已验证窗口 | 适用场景 |
| --- | --- | --- | --- |
| `fixed` | 每天固定时间点 | 否 | 作息规律；窗口语义未验证的套餐 |
| `interval` | 每次成功后 窗口时长 + 余量 | 是 | 常开机器的全天候保温 |

### `fixed` —— 绝对时间，始终安全

在每个固定本地时间点（`weekday` 或 `every-day`）各发一条请求，每次命中开启一个新窗口。

- 时间点到了但机器在睡眠？在补跑窗口内（默认 45 分钟）仍会补发；超时则记为跳过。
- 距离上一次成功不足 30 分钟的时间点会自动跳过 —— 绝不重复为一个还热着的窗口买单。
- 唯一在窗口语义未知时也能用的模式，未验证的套餐因此从这里起步。
- 添加流程里窗口时长已知时，awewarm 会询问套餐每日配额的重置时间，并据此提供全天网格 —— 每个窗口一个时间点、间隔为窗口 + 5 分钟（例如重置 01:14 + 5 小时窗口 → 01:14, 06:19, 11:24, 16:29, 21:34）；拒绝则只保留你输入的时间。套餐选择 fixed 添加时会先问窗口时长（默认 300）—— 它决定网格间隔，并记录为 user-confirmed 窗口、解锁 interval 模式。

```bash
awewarm config set claude-code --times 06:35,11:40,16:45   # 间隔 5 小时 5 分钟：工作日窗口首尾相接
awewarm config set claude-code --mode fixed
```

**案例** —— 晚上合盖的笔记本：06:35 / 11:40 / 16:45 三个时间点让每个工作日从 06:35 到约 21:45 都有窗口开着，机器只需在每个时间点后 45 分钟内醒来。

### `interval` —— 滚动续期

每次成功后，下一条请求排在「窗口时长 + 余量」之后（默认 300 分钟 + 75 秒，另加最多 30 秒抖动）。余量加在旧窗口**关闭之后** —— 提前发只会落进旧窗口，什么也开启不了。还没有成功记录时，会立即发一条作为首个锚点 —— 也可以用 `--start HH:MM` 把这个起点推后：该时刻之前不发射任何请求（今天已过则顺延到明天），越过它的第一个 tick 开锚，首次成功后该门槛自动清除。

```bash
awewarm run my-plan                        # 1. 发一条最小请求并记下时间
# ...观察套餐配额何时重置，记下经过的分钟数...
awewarm config set my-plan --window 300    # 2. 记录窗口时长（解锁 interval）
awewarm config set my-plan --mode interval # 3. 滚动续期
```

手动 `run <id>` 不会移动续期链 —— 下次到期时刻保持原计划不变；加 `--reset-due` 才从本次请求重新起算。

**案例** —— 一台常开的机器，希望夜里和周末也持续保温。

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

以上所有固定时间点均间隔 5 小时 5 分钟 —— 等于订阅窗口（5 小时）加 5 分钟调度缓冲。四个时间点（`06:00, 11:05, 16:10, 21:15`）覆盖全天：上午、下午、晚间和深夜加班。三个时间点（`06:00, 11:05, 16:10`）覆盖标准工作日。`--days weekday` 限制只在工作日触发。两个时间点的链适合半天或间歇性使用。

### 请求失败时 —— 健康阶梯

两种模式共用同一条阶梯：`connected → failing → degraded → auto-disabled`。

```

connected ──节点首次失败──▶ failing ──连续 N 个节点丢失──▶ degraded ──再连续 N 个节点丢失──▶ auto-disabled
   ▲                          │                              │                              │
   └──────── 任意成功（节点/catch-up/手动 run）──────────────┘                              │
                                                                                             │
                                                                          └────── --on / run ──┘
```

- 失败的节点（fixed 的时间点，或 interval 的续期时刻）进入 **failing**，获得补跑重试 —— 默认 30 分钟内 5 次尝试，间隔约 5 分钟（默认值通过 `awewarm config settings` 调整；单个连接用 `--catchup-attempts` / `--catchup-minutes`）。
- 连续丢失 3 个节点（默认 3，通过 `awewarm config settings --degrade-after-nodes` 或单个连接的 `--degrade-after-nodes` 调整）将连接降为 **degraded**：每个节点只发一次，不再补跑；interval 每窗口探测一次，fixed 每个时间点只发一次。
- degraded 状态下再丢失同样次数，连接完全停止：**auto-disabled**，静默直到你用 `awewarm config set <id> --on` 恢复，或一次成功的 `run <id>` 重新激活。
- 任何成功 —— 节点尝试、补跑重试、手动 run —— 都会重置整条阶梯。手动尝试不计入节点，机器睡眠错过的时间点（零次尝试）不算丢失节点。
- `status` 显示当前层级和详情（`Health: failing — 1/3 nodes lost, catch-up attempt 2/5`），并在最近一次激活下方打印最后一次失败的完整错误信息。

### Sleeping Macs — calendar wake (macOS)

`scheduler install` 会在 launchd agent 里为每个 fixed 时间点写一条 `StartCalendarInterval` 日历条目。launchd 能把 Mac 从睡眠中唤醒 —— 合盖、深睡都能命中并准时执行 tick，无需 sudo，且每个时间点都受保护。条目每天都会触发，不受时间点的日期规则限制：tick 自身会判断今天是否该执行，因此周末触发一个仅工作日的条目是无害的空操作。修改时间或模式后条目立即更新，修改后的第一个 tick 会自动修复漂移。

每个连接可以设置 `schedule.wakeWhenAsleep: false` 选择退出（添加流程中询问；之后用 `awewarm config set <id> --no-wake` 修改）。被睡眠错过的时间点仍会在补跑窗口内（默认 45 分钟）补发；完全**关机**的 Mac 不会自行启动 —— 开机后第一个 tick 会补跑所有仍在补跑窗口内的 missed slots。

### Sleeping PCs — wake tasks (Windows)

macOS 设计的镜像版本：`scheduler install` 为每个 fixed 时间点注册一个额外的 Task Scheduler 任务 —— 在时间点触发日任务并启用 *Wake to run*，执行 `awewarm tick`。每分钟的 tick 任务本身不会唤醒机器（否则机器永远无法入睡）；只有时间点会唤醒。`schtasks.exe` 无法设置 *Wake to run*，因此这些任务通过 PowerShell 的 `Register-ScheduledTask` 注册。添加流程会询问 fixed 时间点是否允许唤醒机器（默认允许，与 macOS 相同），`awewarm config set <id> --no-wake` 可以让单个连接退出唤醒，install / uninstall / refresh / self-heal 都会保持任务集与配置同步。

### Always-on servers (Linux)

不需要唤醒机制，机器从不睡眠 —— `awewarm scheduler install` 直接设置 systemd user timer（每分钟 tick；`Persistent=true` 在开机时补跑错过的 tick）。把 `config.json` 和 `secrets.json` 复制过去（或重新运行 `init`），注意 CLI 连接需要在服务器上也安装对应 CLI。`loginctl enable-linger $USER` 在无桌面/SSH 账号上是必需的。Linux 本质上无法唤醒挂起的机器：添加流程不会询问唤醒偏好，连接默认 `wakeWhenAsleep: false`，错过的时间点在机器醒来后于补跑窗口内补发。

## 远程服务器 —— 委托给一台 24/7 在线的机器

合盖的笔记本只为 fixed 时间点醒来；interval 续期链在合盖期间会漂移。要全天候保温，把订阅连接委托给任意常开机器（VPS、NAS、树莓派）上的 `awewarm serve` 进程。CLI 账号连接无法委托 —— 登录态在你本机上，继续由本地调度。

服务器**磁盘上不保存任何秘密**：配对 token 和 API key 始终留在本地 `secrets.json`，需要时经网络推送；服务器只放在内存里。服务器重启后，本机在下次在线时自动重新认领并补推。缺 key 期间到期的时间点是*挂起*而非失败 —— key 回来后仍在补跑窗口内照常触发（和机器睡眠醒来的语义完全一致），过窗才记为 skip。

**搭建服务器（一次性）：**

```bash
ssh my-server
pip3 install awewarm
awewarm serve --data-dir ~/awewarm-server    # 监听 127.0.0.1:8790
```

用 systemd user unit 常驻（`~/.config/systemd/user/awewarm.service`）：

```ini
[Unit]
Description=awewarm serve
After=network-online.target

[Service]
ExecStart=awewarm serve --data-dir %h/awewarm-server
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user enable --now awewarm`（无桌面/SSH 环境先 `loginctl enable-linger $USER`）。用 cloudflared 隧道暴露 —— 免费 TLS、不开入站端口、隐藏源站 IP：

```bash
cloudflared tunnel create awewarm
cloudflared tunnel route dns awewarm warm.example.com
cloudflared tunnel run --url http://127.0.0.1:8790 awewarm
```

**从笔记本委托：**

```bash
awewarm remote connect https://warm.example.com   # 本地生成并保存 token，认领服务器
awewarm config set glm --remote                   # 服务器接管这条连接
awewarm status                                    # 合并视图：本地 + 委托真值
```

`--remote` 只有在服务器确认接收后才落盘，连接绝不会陷入"两边都没人 tick"的状态。已委托连接的一切照旧：`config set` 修改调度后自动推送（服务器不可达时改动留在本地并标记待推送，之后 `awewarm remote push` 对账）；`awewarm run glm` 在服务器上执行并回报结果；`awewarm config set glm --local` 收回连接（先拉回服务器状态，本地调度无缝接续）。`awewarm remote disconnect` 忘掉服务器，仍有委托连接时拒绝执行。fixed 时间按委托方机器的时区运行（时区随推送传递）；从不睡觉的服务器谈不上 wake。

## 配置

用户不需要手改配置；`init` / `config add` 会生成 `~/.config/awewarm/config.json`（状态在 `~/.local/state/awewarm/state.json`）。结构示例：

```json
{
  "version": 2,
  "connections": {
    "claude-code": {
      "label": "Claude Code",
      "cli": "/usr/local/bin/claude",
      "model": "haiku",
      "windowMinutes": 300,
      "mode": "fixed",
      "times": ["06:35"],
      "days": "weekday"
    },
    "glm": {
      "label": "glm",
      "url": "https://open.bigmodel.cn/api/coding/paas/v4",
      "protocol": "openai-chat",
      "apiKey": "file:glm",
      "model": "GLM-5-Turbo",
      "windowMinutes": 300,
      "mode": "fixed",
      "times": ["06:00"],
      "days": "every-day",
      "location": "remote"
    }
  },
  "remote": {
    "url": "https://warm.example.com",
    "tokenRef": "file:remote-token"
  }
}
```

有 `url` + `apiKey` 的是订阅连接，有 `cli` 的是本机账号。`apiKey` 为 `file:<id>`（粘贴的 key 存于 `~/.config/awewarm/secrets.json`，权限 600）。`location: "remote"`（缺省即 local）表示该连接由已配对的 `awewarm serve` 服务器调度，服务器地址和 token 引用存于顶层 `remote` 块。存在 `windowMinutes` 即视为窗口已验证/确认（解锁 interval 续期）。微调参数（catch-up、grace、jitter）默认不落盘，改动过才写入。v1 配置文件在首次加载时自动升级为本格式。

## 命令

```bash
awewarm init                          # 交互式引导：扫描账号、选择调度、安装后台调度器
awewarm discover                      # 纯读扫描本机 CLI 与登录态
awewarm config add                    # 添加连接：本机账号或订阅 endpoint
awewarm config set <id> [flags]       # 查看或修改设置：--times、--days、--mode、--on/--off、--anchor、--start、--window
awewarm config remove <id>            # 删除连接及其状态和存储的 API key
awewarm config show / edit            # 打印磁盘上的配置 / 用 $EDITOR 打开编辑（退出时校验）
awewarm config path                   # 配置 / 状态 / 日志路径
awewarm status [<id>] [--json]        # 摘要；单连接详情；脱敏机读输出
awewarm run                           # 立即触发所有启用的连接（无视调度计划）
awewarm run <id> [--reset-due]        # 立即触发单个连接（默认不动原计划，--reset-due 才重算下次到期）
awewarm scheduler install / uninstall # 后台调度器（launchd / 任务计划程序 / systemd）
awewarm serve                          # 运行常驻服务器，调度已委托的连接
awewarm remote connect <url>           # 与服务器配对（token 本地生成并保存）
awewarm remote status                  # 服务器视角：运行时长、上次 tick、委托连接
awewarm remote push [<id>]             # 向服务器重新同步委托连接（配置 + 密钥）
awewarm remote disconnect              # 忘掉服务器（仍有委托连接时拒绝）
awewarm update [--check]              # 升级到最新 PyPI 版本
```

0.3 之前版本的命令（`add plan`、`times`、`enable`、`disable`、`verify`、`anchor`、`activate`、`remove`、`install`、`uninstall`、`inspect`、`self-update`）仍作为隐藏别名可用 —— 执行时会提示新写法，v1.0 移除。

## 自动更新

awewarm 会在后台检查 PyPI —— 每天至多一次，且绝不在调度器 tick 里检查。有新版本时，交互式命令会在结束后向 stderr 打印一条提醒。

```bash
awewarm update            # 升级到最新版本
awewarm update --check    # 只看版本，不升级
```

关闭后台检查：

```bash
export AWEWARM_NO_UPDATE_CHECK=1
```

## 支持

如果 awewarm 帮你省下了配额，欢迎支持：

- ⭐ 给仓库点 Star —— 让更多人看到它。
- ☕ [Ko-fi](https://ko-fi.com/mugpeng) —— 请我喝杯咖啡。
- 💬 微信支付 —— 扫描下方二维码。

<p align="center">
  <img src="assets/images/wechat-pay.jpg" alt="WeChat Pay" width="240">
</p>

## 开发

```bash
pip install -e .
python3 -m unittest discover -s tests
```

工程规范见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)，版本历史见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。
