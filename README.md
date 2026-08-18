<div align="center">
  <h1>awewarm: Subscription Window Warmer</h1>
  <p><strong>Keep AI coding-plan windows warm with one minimal request.</strong></p>
  <p>Connect once; awewarm detects what your Claude Code / Codex account or subscription endpoint can do, then makes sure the next usage window is always already open.</p>
  <p>
    <strong>English</strong> ·
    <a href="./README_cn.md">简体中文</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/version-0.1.0-7C3AED?style=flat-square" alt="Version">
    <img src="https://img.shields.io/badge/python-%E2%89%A5%203.9-0EA5E9?style=flat-square" alt="Python">
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-alpha-c96a3d?style=flat-square" alt="Status">
    <img src="https://img.shields.io/badge/install-pip-22C55E?style=flat-square" alt="pip install">
    <img src="https://img.shields.io/badge/platform-terminal-334155?style=flat-square" alt="Platform">
  </p>
</div>

> Connect once, then never think about 5-hour resets again: awewarm detects what it can do and only then keeps the next window open.

awewarm manages two kinds of connections:

- **Account** — your local `claude` / `codex` CLI logins. awewarm reuses their login state and sends one minimal headless request (`Reply with exactly: ok`). No credentials are stored.
- **Subscription plan** — any OpenAI Chat / OpenAI Responses / Anthropic-compatible endpoint with a base URL + token. The token goes to the macOS Keychain, never to disk.

It schedules those requests in three modes — `fixed` (absolute times, e.g. 06:35 / 11:40 / 16:45), `interval` (renew 5 h + a grace margin after each success), and `hybrid` (fixed anchor + interval renewal, the recommended default). Interval mode stays locked until the window semantics are verified or user-confirmed; fixed mode is always safe.

## Install

Requires Python ≥ 3.9. Until the first PyPI release, install from source:

```bash
git clone <repo-url> && cd awewarm
pip install .
```

## Quick Start

```bash
awewarm init        # scan local accounts, pick a schedule, install the scheduler
awewarm status      # see what will happen next
```

For a subscription endpoint instead:

```bash
awewarm add plan
```

You will be asked for the API base URL, token, protocol, and model; awewarm tests the endpoint with one minimal request, then stores the token in the Keychain.

## Config

Users never hand-edit config; `init` / `add plan` generate it at `~/.config/awewarm/config.json` (state at `~/.local/state/awewarm/state.json`). The shape, for reference:

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

## Commands

```bash
awewarm init                     # interactive onboarding
awewarm discover                 # read-only scan of local CLIs and logins
awewarm add plan                 # add a subscription endpoint
awewarm status                   # human-readable summary
awewarm run [--dry-run]          # one scheduler tick (launchd calls this)
awewarm activate <id> --confirm  # send one real request now
awewarm verify <id> [--confirm] [--duration N --user-confirm]
awewarm enable <id> [--mode fixed|interval|hybrid]
awewarm disable <id>
awewarm remove <id>
awewarm install / uninstall      # launchd scheduler agent (macOS)
awewarm inspect [<id>] [--json]  # redacted capability dump
```

## Development

```bash
pip install -e .
python3 -m unittest discover -s tests
```

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for the engineering doctrine and [docs/CHANGELOG.md](docs/CHANGELOG.md) for release history.
