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
- **Subscription plan** — any OpenAI Chat / OpenAI Responses / Anthropic-compatible endpoint with a base URL + API key. The key is stored in `~/.config/awewarm/secrets.json` (chmod 600) so the background scheduler can always read it.

It schedules those requests in two modes — `fixed` and `interval` — explained in [Scheduling Modes](#scheduling-modes) below. Interval-style renewal stays locked until the window semantics are verified or user-confirmed; `fixed` is always safe.

## Install

Requires Python ≥ 3.9:

```bash
pip3 install awewarm
```

The background scheduler installs on macOS (launchd), Windows (Task Scheduler), and Linux (systemd user timer — `loginctl enable-linger $USER` first on headless/SSH accounts). Where systemd is unavailable, cron the tick: `* * * * * awewarm tick`.

All keys live in `secrets.json` — env-var references were removed because the background scheduler (launchd/systemd/Task Scheduler) cannot read shell variables and would silently fail with "API key unavailable".

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

You will be asked for the protocol, API base URL, API key, and model; awewarm tests the endpoint with one minimal request, then stores the key in `secrets.json`. The same command also re-adds a local `claude` / `codex` account you removed earlier — it lists whatever is detected on this machine.

## Companion Tools

awewarm is part of a small tool family for AI coding agents:

- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — agent profile switcher for Claude Code, Codex, and OpenCode. aweswitch manages which provider a session launches with; awewarm keeps that provider's subscription window open underneath. If you launch coding-plan profiles with aweswitch, awewarm is the piece that keeps those 5-hour windows from going cold overnight.
- **[aweskill](https://github.com/Webioinfo01/aweskill)** — CLI skill package manager for AI agents (47+ agents).
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — session bookmark manager for Claude Code and Codex.
- **[awerouter](https://github.com/mugpeng/awerouter)** — smart LLM router: flash/pro split by structural signals.

## Scheduling Modes

Both modes send the same one minimal request — what differs is *when* it fires. Switch with `awewarm config set <id> --mode fixed|interval`; see the current mode and next due moment with `awewarm status`. The old `hybrid` mode was removed — a fixed grid spaced one window apart already keeps windows chained all day, with calendar wake coverage interval cannot offer. Existing hybrid configs migrate to `fixed` on first load.

| Mode | Fires when | Needs a verified window | Best for |
| --- | --- | --- | --- |
| `fixed` | fixed times each day | no | predictable hours; unverified plans; sleeping Macs |
| `interval` | window + grace after each success | yes | 24/7 warmth on an always-on machine |

### `fixed` — absolute times, always safe

One request at each fixed local time (`weekday` or `every-day`); each hit opens a fresh window.

- If the machine was asleep at the slot time, the slot still fires late within the catch-up window (default 45 min); past that it is recorded as skipped.
- A slot landing within 30 min of a previous success is skipped — never pay for two windows at once.
- The only mode that works while window semantics are unknown, which is why unverified plans start here.
- During setup, when the window duration is known, awewarm asks for the plan's daily quota reset time and offers a full-day grid anchored on it — one slot per window, spaced window + 5 min apart (e.g. reset 01:14 + a 5 h window → 01:14, 06:19, 11:24, 16:29, 21:34). Declining keeps just the time you entered. Plans added in fixed mode are asked for the window duration first (default 300) — it spaces the grid and is recorded as a user-confirmed window that unlocks interval mode.

```bash
awewarm config set claude-code --times 06:35,11:40,16:45   # 5 h + 5 min apart: windows chain across a workday
awewarm config set claude-code --mode fixed
```

**Example** — a laptop that sleeps at night: slots at 06:35 / 11:40 / 16:45 keep a window open from 06:35 to ~21:45 every weekday. The machine only needs to be awake within 30 min of each slot.

### `interval` — rolling renewal

After each success the next request is scheduled `window + grace` later (default 300 min + 75 s, plus up to 30 s jitter). The grace runs *after* the old window has closed — firing earlier would land inside the old window and start nothing. With no success recorded yet, one request fires immediately as the first anchor — unless you defer that start with `--start HH:MM`: no request fires before that moment (today, or tomorrow if it has passed), the first tick after it opens the chain, and the gate clears on the first success.

```bash
awewarm run my-plan                        # 1. one minimal request, timestamped
# ...watch when the plan's quota resets, note the elapsed minutes...
awewarm config set my-plan --window 300    # 2. record the window (unlocks interval)
awewarm config set my-plan --mode interval # 3. rolling renewal
```

A manual `run <id>` never shifts the renewal chain — the next due moment stays as scheduled. Add `--reset-due` to restart the chain from this run instead.

**Example** — an always-on machine you want warm around the clock, nights and weekends included: no wake machinery needed, renewal just keeps rolling.

### When requests fail — the health ladder

Both modes share one ladder: `connected → failing → degraded → auto-disabled`.

- A failed node (a fixed slot, or an interval renewal moment) enters **failing** and gets catch-up retries — by default 5 attempts within 30 minutes, spaced ~5 minutes apart (defaults via `awewarm config settings`; one connection via `--catchup-attempts` / `--catchup-minutes`).
- 3 consecutive lost nodes (default 3, via `awewarm config settings --degrade-after-nodes` or a per-connection `--degrade-after-nodes`) drop the connection to **degraded**: single shot per node, no more catch-up. interval probes once per window; fixed fires each slot exactly once.
- The same count again while degraded stops it entirely: **auto-disabled**, silent until you resume with `awewarm config set <id> --on` (or a manual `run <id>` that succeeds).
- Any success — node attempt, catch-up retry, manual run — resets the whole ladder. Manual attempts never count as nodes, and a slot the machine slept through (zero attempts) is not a lost node.
- `status` shows the rung plus details (`Health: failing — 1/3 nodes lost, catch-up attempt 2/5`), and prints the last failure with its error right under the last activation.

### Sleeping Macs — calendar wake (macOS)

`scheduler install` writes one `StartCalendarInterval` entry per fixed slot into the launchd agent. launchd wakes the Mac from sleep — lid closed and deep sleep included — and runs the tick at the exact slot time. No sudo, and *every* slot is protected. Entries fire every day regardless of the slot's day rule: the tick itself decides whether today is an active day, so a weekend wake for a weekday-only slot is a harmless no-op. Editing times/mode updates the entries immediately; the first tick after any edit heals drift automatically.

Per connection, `schedule.wakeWhenAsleep: false` opts out (asked during setup; change later with `awewarm config set <id> --no-wake`). Missed slots still fire late within the catch-up window once the machine wakes. A fully *shut down* Mac stays off — power it on and the first tick catches up anything still inside the catch-up window.

### Sleeping PCs — wake tasks (Windows)

The macOS design, mirrored: `scheduler install` registers one extra Task Scheduler task per fixed slot — a daily trigger at the slot time with *Wake to run* enabled, running `awewarm tick`. The per-minute tick task itself never wakes the machine (a waking tick would keep it from ever staying asleep); only slot times do. `schtasks.exe` cannot set *Wake to run*, so the tasks are registered through PowerShell's `Register-ScheduledTask`. The setup flow asks whether fixed slots may wake the machine (same prompt as macOS), `awewarm config set <id> --no-wake` opts a connection out, and install/uninstall/refresh/self-heal keep the task set in sync with the config.

### Always-on servers (Linux)

No wake machinery exists or is needed on a machine that never sleeps — `awewarm scheduler install` sets up the systemd user timer directly (tick every minute; `Persistent=true` fires a missed tick at boot). Copy `config.json` and `secrets.json` over (or re-run `init`), and note that CLI-based connections need their CLI installed on the server. `loginctl enable-linger $USER` first on headless/SSH accounts. Linux simply cannot wake a suspended machine: the setup flow never asks, connections default to `wakeWhenAsleep: false`, and missed slots catch up within their catch-up windows once the machine wakes.

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

A connection with `url` + `apiKey` is a subscription; one with `cli` is a local account. `apiKey` is `file:<id>` — the pasted key lives in `~/.config/awewarm/secrets.json` (chmod 600), readable by the background scheduler. `windowMinutes` present means the window is verified/user-confirmed (interval renewal unlocked). Tuning knobs (catch-up, grace, jitter) stay at code defaults unless changed. v1 config files upgrade to this format automatically on first load.

## Commands

```bash
awewarm init                          # interactive onboarding: scan accounts, pick schedules, install scheduler
awewarm discover                      # read-only scan of local CLIs and logins
awewarm config add                    # add a connection: a detected account or a subscription endpoint
awewarm config set <id> [flags]       # show or change settings: --times, --days, --mode, --on/--off, --anchor, --start, --window
awewarm config remove <id>            # delete a connection, its state, and its stored API key
awewarm config show / edit            # print the on-disk config / open it in $EDITOR (validated on exit)
awewarm config path                   # config / state / log locations
awewarm status [<id>] [--json]        # summary; one connection in detail; redacted machine-readable dump
awewarm run                           # fire every enabled connection now, ignoring the schedule
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

## Development

```bash
pip install -e .
python3 -m unittest discover -s tests
```

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for the engineering doctrine and [docs/CHANGELOG.md](docs/CHANGELOG.md) for release history.

## Support

If awewarm saves your quota, consider supporting it:

- ⭐ Star the repo — it helps others find it.
- ☕ [Ko-fi](https://ko-fi.com/mugpeng) — buy me a coffee.
- 💬 WeChat — scan the QR code below.

<p align="center">
  <img src="assets/images/wechat-pay.jpg" alt="WeChat Pay" width="240">
</p>

> awewarm is free and open source. Sponsors keep it maintained — thank you.
