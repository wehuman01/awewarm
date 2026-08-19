<div align="center">
  <img src="logo/hero2.webp" alt="awewarm" width="860">
  <h1>awewarm: Subscription Window Warmer</h1>
  <p><strong>Keep AI coding-plan windows warm with one minimal request.</strong></p>
  <p>Connect once; awewarm detects what your Claude Code / Codex account or subscription endpoint can do, then makes sure the next usage window is always already open.</p>
  <p>
    <strong>English</strong> ·
    <a href="./README_cn.md">简体中文</a>
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

> Connect once, then never think about 5-hour resets again: awewarm detects what it can do and only then keeps the next window open.

awewarm manages two kinds of connections:

- **Account** — your local `claude` / `codex` CLI logins. awewarm reuses their login state and sends one minimal headless request (`Reply with exactly: ok`). No credentials are stored.
- **Subscription plan** — any OpenAI Chat / OpenAI Responses / Anthropic-compatible endpoint with a base URL + API key. The key is stored in `~/.config/awewarm/secrets.json` (chmod 600), or referenced from an env var without being stored.

It schedules those requests in three modes — `fixed`, `interval`, and `hybrid` — explained in [Scheduling Modes](#scheduling-modes) below. Interval-style renewal stays locked until the window semantics are verified or user-confirmed; `fixed` is always safe.

## Install

Requires Python ≥ 3.9:

```bash
pip3 install awewarm
```

The background scheduler installs on macOS (launchd), Windows (Task Scheduler), and Linux (systemd user timer — `loginctl enable-linger $USER` first on headless/SSH accounts). Where systemd is unavailable, cron the tick: `* * * * * awewarm run`.

Env-var keys: paste an env ref like `${GLM_API_KEY}` instead of the key itself and awewarm stores only the reference (aweswitch convention). Note the background scheduler only sees variables from the shell that installed it — re-install the scheduler from a shell where the variable is set. On Windows, persist such vars with `setx` so scheduler tasks inherit them.

## Quick Start

### Let an AI agent set it up

Working in Claude Code, Codex, or another coding agent? Tell it:

```text
Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm.
```

The agent installs the CLI, scans your local accounts (read-only), and tunes schedules on request. Onboarding itself (`awewarm init`, `awewarm config add`) stays in your terminal — it prompts for choices and API keys. After setup you can ask things like "when is the next warm-up?" or "set claude-code to 06:35 and 12:35".

### Manual setup

```bash
awewarm init        # scan local accounts, pick a schedule, install the scheduler
awewarm status      # see what will happen next
```

Every added connection — account or endpoint — gets one test request during setup, so a broken model or bad key surfaces immediately instead of at 6 a.m.

For a subscription endpoint instead:

```bash
awewarm config add
```

You will be asked for the protocol, API base URL, API key, and model; awewarm tests the endpoint with one minimal request, then stores the key in `secrets.json` — or as an env reference if you typed `${ENV_VAR}`. The same command also re-adds a local `claude` / `codex` account you removed earlier — it lists whatever is detected on this machine.

## Companion Tools

awewarm is part of a small tool family for AI coding agents:

- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — agent profile switcher for Claude Code, Codex, and OpenCode. aweswitch manages which provider a session launches with; awewarm keeps that provider's subscription window open underneath. If you launch coding-plan profiles with aweswitch, awewarm is the piece that keeps those 5-hour windows from going cold overnight.
- **[aweskill](https://github.com/Webioinfo01/aweskill)** — CLI skill package manager for AI agents (47+ agents).
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — session bookmark manager for Claude Code and Codex.
- **[awerouter](https://github.com/mugpeng/awerouter)** — smart LLM router: flash/pro split by structural signals.

## Scheduling Modes

All modes send the same one minimal request — what differs is *when* it fires. Switch with `awewarm config set <id> --mode fixed|interval|hybrid`; see the current mode and next due moment with `awewarm status`.

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
awewarm config set claude-code --times 06:35,11:40,16:45   # 5 h + 5 min apart: windows chain across a workday
awewarm config set claude-code --mode fixed
```

**Example** — a laptop that sleeps at night: slots at 06:35 / 11:40 / 16:45 keep a window open from 06:35 to ~21:45 every weekday. The machine only needs to be awake within 45 min of each slot.

### `interval` — rolling renewal

After each success the next request is scheduled `window + grace` later (default 300 min + 75 s, plus up to 30 s jitter). The grace runs *after* the old window has closed — firing earlier would land inside the old window and start nothing. With no success recorded yet, one request fires immediately as the first anchor.

```bash
awewarm run my-plan                        # 1. one minimal request, timestamped
# ...watch when the plan's quota resets, note the elapsed minutes...
awewarm config set my-plan --window 300    # 2. record the window (unlocks interval)
awewarm config set my-plan --mode interval # 3. rolling renewal
```

A manual `run <id>` never shifts the renewal chain — the next due moment stays as scheduled. Add `--reset-due` to restart the chain from this run instead.

**Example** — an always-on machine you want warm around the clock, nights and weekends included. After 3 consecutive failures renewal pauses itself (status shows `degraded`) and resumes on the next success.

### `hybrid` — fixed anchor + interval renewal (recommended)

Both engines run together: interval keeps the chain unbroken, while fixed slots re-anchor it at deterministic times — so each workday still starts where you expect even if the machine slept through the interval due. A slot within 30 min of a recent interval success is skipped automatically.

```bash
awewarm config set claude-code --mode hybrid   # window already verified at init
awewarm config set claude-code --times 06:35   # one anchor per workday morning
```

**Example** — a verified account with a 06:35 weekday anchor: interval renews all day (renewal ignores the weekday rule and continues over the weekend), and Monday 06:35 re-anchors the chain no matter what happened overnight.

## Config

Users never hand-edit config; `init` / `config add` generate it at `~/.config/awewarm/config.json` (state at `~/.local/state/awewarm/state.json`). The shape, for reference:

```json
{
  "version": 2,
  "connections": {
    "claude-code": {
      "label": "Claude Code",
      "cli": "/usr/local/bin/claude",
      "model": "haiku",
      "windowMinutes": 300,
      "mode": "hybrid",
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
      "mode": "hybrid",
      "times": ["06:00"],
      "days": "every-day"
    }
  }
}
```

A connection with `url` + `apiKey` is a subscription; one with `cli` is a local account. `apiKey` is `file:<id>` (the pasted key lives in `~/.config/awewarm/secrets.json`, chmod 600) or an env reference like `$GLM_API_KEY` / `${GLM_API_KEY}`. `windowMinutes` present means the window is verified/user-confirmed (interval renewal unlocked). Tuning knobs (catch-up, grace, jitter) stay at code defaults unless changed. v1 config files upgrade to this format automatically on first load.

## Commands

```bash
awewarm init                          # interactive onboarding: scan accounts, pick schedules, install scheduler
awewarm discover                      # read-only scan of local CLIs and logins
awewarm config add                    # add a connection: a detected account or a subscription endpoint
awewarm config set <id> [flags]       # show or change settings: --times, --days, --mode, --on/--off, --anchor, --window
awewarm config remove <id>            # delete a connection, its state, and its stored API key
awewarm config show / edit            # print the on-disk config / open it in $EDITOR (validated on exit)
awewarm config path                   # config / state / log locations
awewarm status [<id>] [--json]        # summary; one connection in detail; redacted machine-readable dump
awewarm run [--dry-run]               # one scheduler tick (the background scheduler calls this)
awewarm run <id> [--reset-due]        # fire one connection now (schedule untouched unless --reset-due)
awewarm scheduler install / uninstall # background scheduler (launchd / Task Scheduler / systemd)
awewarm update [--check]              # upgrade to the latest PyPI release
```

Commands from pre-0.3 releases (`add plan`, `times`, `enable`, `disable`, `verify`, `anchor`, `activate`, `remove`, `install`, `uninstall`, `inspect`, `self-update`) still work as hidden aliases; they print their new spelling and will be removed in v1.0.

## Self-Update

awewarm checks PyPI in the background — at most once a day, and never during scheduler ticks. When a newer release exists, interactive commands print a reminder to stderr.

```bash
awewarm update            # upgrade to the latest release
awewarm update --check    # show versions only
```

To disable the background check:

```bash
export AWEWARM_NO_UPDATE_CHECK=1
```

## Support

If awewarm saves your quota, consider supporting it:

- ⭐ Star the repo — it helps others find it.
- ☕ [Ko-fi](https://ko-fi.com/mugpeng) — buy me a coffee.

## Development

```bash
pip install -e .
python3 -m unittest discover -s tests
```

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for the engineering doctrine and [docs/CHANGELOG.md](docs/CHANGELOG.md) for release history.
