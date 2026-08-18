<div align="center">
  <img src="logo/hero.png" alt="awewarm" width="860">
  <h1>awewarm: Subscription Window Warmer</h1>
  <p><strong>Keep AI coding-plan windows warm with one minimal request.</strong></p>
  <p>Connect once; awewarm detects what your Claude Code / Codex account or subscription endpoint can do, then makes sure the next usage window is always already open.</p>
  <p>
    <strong>English</strong> ·
    <a href="./README_cn.md">简体中文</a>
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

> Connect once, then never think about 5-hour resets again: awewarm detects what it can do and only then keeps the next window open.

awewarm manages two kinds of connections:

- **Account** — your local `claude` / `codex` CLI logins. awewarm reuses their login state and sends one minimal headless request (`Reply with exactly: ok`). No credentials are stored.
- **Subscription plan** — any OpenAI Chat / OpenAI Responses / Anthropic-compatible endpoint with a base URL + token. The token goes to the macOS Keychain, never to disk.

It schedules those requests in three modes — `fixed`, `interval`, and `hybrid` — explained in [Scheduling Modes](#scheduling-modes) below. Interval-style renewal stays locked until the window semantics are verified or user-confirmed; `fixed` is always safe.

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

## Scheduling Modes

All modes send the same one minimal request — what differs is *when* it fires. Switch with `awewarm enable <id> --mode fixed|interval|hybrid`; see the current mode and next due moment with `awewarm status`.

| Mode | Fires when | Needs a verified window | Best for |
| --- | --- | --- | --- |
| `fixed` | fixed times each day | no | predictable hours; unverified plans |
| `interval` | window + grace after each success | yes | 24/7 warmth on an always-on machine |
| `hybrid` | both of the above | yes | recommended default |

### `fixed` — absolute times, always safe

One request at each fixed local time (`weekday` or `every-day`); each hit opens a fresh window.

- If the machine was asleep at the slot time, the slot still fires late within the catch-up window (default 45 min); past that it is recorded as skipped.
- A slot landing within 30 min of a previous success is skipped — never pay for two windows at once.
- The only mode that works while window semantics are unknown, which is why unverified plans start here.

```bash
awewarm times claude-code 06:35 11:40 16:45   # 5 h + 5 min apart: windows chain across a workday
awewarm enable claude-code --mode fixed
```

**Example** — a laptop that sleeps at night: slots at 06:35 / 11:40 / 16:45 keep a window open from 06:35 to ~21:45 every weekday. The machine only needs to be awake within 45 min of each slot.

### `interval` — rolling renewal

After each success the next request is scheduled `window + grace` later (default 300 min + 75 s, plus up to 30 s jitter). The grace runs *after* the old window has closed — firing earlier would land inside the old window and start nothing. With no success recorded yet, one request fires immediately as the first anchor.

```bash
awewarm verify my-plan --confirm                      # 1. one minimal request, timestamped
# ...watch when the plan's quota resets, note the elapsed minutes...
awewarm verify my-plan --duration 300 --user-confirm  # 2. record the window (unlocks interval)
awewarm enable my-plan --mode interval                # 3. rolling renewal
```

**Example** — an always-on machine you want warm around the clock, nights and weekends included. After 3 consecutive failures renewal pauses itself (status shows `degraded`) and resumes on the next success.

### `hybrid` — fixed anchor + interval renewal (recommended)

Both engines run together: interval keeps the chain unbroken, while fixed slots re-anchor it at deterministic times — so each workday still starts where you expect even if the machine slept through the interval due. A slot within 30 min of a recent interval success is skipped automatically.

```bash
awewarm enable claude-code --mode hybrid   # window already verified at init
awewarm times claude-code 06:35            # one anchor per workday morning
```

**Example** — a verified account with a 06:35 weekday anchor: interval renews all day (renewal ignores the weekday rule and continues over the weekend), and Monday 06:35 re-anchors the chain no matter what happened overnight.

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
awewarm times <id> [HH:MM...]  # show or set fixed times, e.g. 06:35 11:40 16:45
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
