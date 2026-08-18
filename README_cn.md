<div align="center">
  <img src="logo/hero.png" alt="awewarm" width="860">
  <h1>awewarm：订阅窗口保温器</h1>
  <p><strong>用一条最小请求，让 AI 编程套餐的窗口一直是热的。</strong></p>
  <p>接入一次；awewarm 自动检测你的 Claude Code / Codex 账号或订阅 endpoint 的能力，然后确保下一个用量窗口永远是已开启状态。</p>
  <p>
    <a href="./README.md">English</a> ·
    <strong>简体中文</strong>
  </p>
  <p>
    <img src="https://img.shields.io/badge/version-0.1.5-7C3AED?style=flat-square" alt="Version">
    <img src="https://img.shields.io/badge/python-%E2%89%A5%203.9-0EA5E9?style=flat-square" alt="Python">
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-alpha-c96a3d?style=flat-square" alt="Status">
    <img src="https://img.shields.io/badge/install-pip-22C55E?style=flat-square" alt="pip install">
    <img src="https://img.shields.io/badge/platform-terminal-334155?style=flat-square" alt="Platform">
  </p>
</div>

> 接入一次，之后不必再理解 5 小时重置：awewarm 会检测它能做什么，并只在支持的情况下让下一轮窗口自动开始。

awewarm 管理两类连接：

- **账号** —— 本机的 `claude` / `codex` CLI 登录。awewarm 复用它们的登录态，发送一条最小的无头请求（`Reply with exactly: ok`），不保存任何凭据。
- **订阅套餐** —— 任何 OpenAI Chat / OpenAI Responses / Anthropic 兼容的 endpoint（base URL + token）。token 存入 macOS 钥匙串，绝不落盘。

调度分三种模式：`fixed`（绝对时间表，如 06:35 / 11:40 / 16:45）、`interval`（每次成功后 5 小时 + 安全余量续期）、`hybrid`（fixed 锚定 + interval 续期，推荐默认）。interval 只有在窗口语义已验证或用户确认后才解锁；fixed 模式始终安全。

## 安装

需要 Python ≥ 3.9。首个 PyPI 版本发布前，从源码安装：

```bash
git clone <repo-url> && cd awewarm
pip install .
```

## 快速开始

```bash
awewarm init        # 扫描本机账号、选择调度、安装后台调度器
awewarm status      # 查看接下来会发生什么
```

接入订阅 endpoint：

```bash
awewarm add plan
```

依次输入 API base URL、token、协议和模型；awewarm 会先用一条最小请求测试 endpoint，然后把 token 存入钥匙串。

## 配置

用户不需要手改配置；`init` / `add plan` 会生成 `~/.config/awewarm/config.json`（状态在 `~/.local/state/awewarm/state.json`）。结构示例：

```json
{
  "version": 1,
  "connections": {
    "claude-code": {
      "kind": "account",
      "enabled": true,
      "transport": {"kind": "claude-cli", "cliCommand": "claude"},
      "window": {"status": "verified", "durationMinutes": 300},
      "schedule": {
        "mode": "hybrid",
        "fixed": {"at": ["06:35"], "days": "weekday", "catchUpWindowMinutes": 45},
        "interval": {"graceSeconds": 75, "jitterSeconds": 30}
      }
    }
  }
}
```

## 命令

```bash
awewarm init                     # 交互式引导
awewarm discover                 # 纯读扫描本机 CLI 与登录态
awewarm add plan                 # 添加订阅 endpoint
awewarm status                   # 人读摘要
awewarm run [--dry-run]          # 一次调度 tick（launchd 每分钟调用）
awewarm activate <id> --confirm  # 立即发送一条真实请求
awewarm verify <id> [--confirm] [--duration N --user-confirm]
awewarm enable <id> [--mode fixed|interval|hybrid]
awewarm times <id> [HH:MM...]  # 查看或设置 fixed 时间点，如 06:35 11:40 16:45
awewarm disable <id>
awewarm remove <id>
awewarm install / uninstall      # launchd 调度器（macOS）
awewarm inspect [<id>] [--json]  # 脱敏能力信息
```

## 开发

```bash
pip install -e .
python3 -m unittest discover -s tests
```

工程规范见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)，版本历史见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。
