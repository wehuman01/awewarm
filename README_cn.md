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
- **订阅套餐** —— 任何 OpenAI Chat / OpenAI Responses / Anthropic 兼容的 endpoint（base URL + API key）。API key 存入 `~/.config/awewarm/secrets.json`（权限 600），也可以用环境变量引用、不落盘。

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

**案例** —— 一台常开的机器，希望夜里和周末也持续保温。连续失败 3 次后续期会自动暂停（status 显示 `degraded`），下次成功即恢复。

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
      "days": "every-day"
    }
  }
}
```

有 `url` + `apiKey` 的是订阅连接，有 `cli` 的是本机账号。`apiKey` 为 `file:<id>`（粘贴的 key 存于 `~/.config/awewarm/secrets.json`，权限 600）。存在 `windowMinutes` 即视为窗口已验证/确认（解锁 interval 续期）。微调参数（catch-up、grace、jitter）默认不落盘，改动过才写入。v1 配置文件在首次加载时自动升级为本格式。

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

## 开发

```bash
pip install -e .
python3 -m unittest discover -s tests
```

工程规范见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)，版本历史见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。
