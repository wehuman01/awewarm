---
name: awewarm
description: "Use when helping users manage awewarm warm-up schedules — checking status, changing fixed times, switching fixed/interval/hybrid modes, verifying plan windows, or installing the background scheduler. 中文触发词：保温、保活、订阅窗口、warm-up、awewarm、固定时间、调度模式、5小时窗口。"
---

# awewarm

This skill covers **managing** awewarm's warm-up schedules: status, fixed times, modes, window verification, and the scheduler install.

## Quota Rules (read this first)

Every activation sends one REAL request against the user's coding-plan quota:

- **Never run** `awewarm activate --confirm`, `awewarm verify --confirm`, or bare `awewarm run` unless the user explicitly asks for a request to be sent. `awewarm run --dry-run` is always safe.
- `awewarm init` and `awewarm add plan` are interactive (they prompt for choices and tokens). The user runs them in their own terminal.

## Command Safety

| Category | Commands |
|---|---|
| Read-only — run freely | `awewarm status`, `awewarm discover`, `awewarm times <id>`, `awewarm inspect [<id>] [--json]`, `awewarm config path`, `awewarm self-update --check` |
| Local changes — run on request | `awewarm times <id> HH:MM...`, `awewarm enable <id> [--mode ...]`, `awewarm disable <id>`, `awewarm install`, `awewarm uninstall`, `awewarm remove <id>` (confirm first — deletes the stored token), `awewarm self-update` |
| Real requests — user must explicitly ask | `awewarm activate <id> --confirm`, `awewarm verify <id> --confirm`, `awewarm run` |
| Interactive — user's terminal only | `awewarm init`, `awewarm add plan` |

## Intent Router

| User intent | Approach |
|---|---|
| "Keep my plan warm", "别让窗口过期" | Check `awewarm status` first, then tune modes/times below. |
| "When is the next warm-up?", "下次什么时候触发" | `awewarm status` |
| "What can awewarm see?", "检测一下本机" | `awewarm discover` |
| "Change warm-up times to 9:00 and 14:00", "改保温时间点" | `awewarm times <id> 09:00 14:00` |
| "Switch to hybrid / interval / fixed", "切换模式" | `awewarm enable <id> --mode <mode>`; interval/hybrid need a verified window |
| "Pause while I'm on vacation", "休假暂停" | `awewarm disable <id>` (resume with `awewarm enable <id>`) |
| "Add my GLM subscription", "添加订阅套餐" | Tell the user to run `awewarm add plan` in their terminal (interactive token prompt). |
| "Verify the window", "验证窗口时长" | Guide the 3-step verify flow below; only send the `--confirm` request if the user asks. |
| "Remove this plan", "删掉这个连接" | Confirm, then `awewarm remove <id>` |
| "Where are awewarm's files?", "配置在哪" | `awewarm config path` |
| "Is the scheduler installed?", "调度器装了吗" | `awewarm status` (last line) or `awewarm install` |

## Config and State

- Config: `~/.config/awewarm/config.json` (override: `AWEWARM_CONFIG`)
- State: `~/.local/state/awewarm/state.json`; log: `~/.local/state/awewarm/awewarm.log` (override: `AWEWARM_STATE` / `AWEWARM_LOG`)

Users never hand-edit these files; commands mutate them. Read current state through `awewarm status` or `awewarm inspect <id> --json` (redacted).

Subscription tokens live in the macOS Keychain (service `awewarm/<id>`) or as `${ENV_VAR}` references — never in the config file, never echoed. On Windows there is no Keychain, so tokens are always `${ENV_VAR}` refs (persist with `setx`, which scheduler tasks inherit). Account connections store no credentials at all; they reuse local `claude` / `codex` logins.

## Scheduling Modes

| Mode | Fires when | Needs verified window |
|---|---|---|
| `fixed` | fixed local times (`weekday` or `every-day`), each slot may fire late within its catch-up window (default 45 min) | no |
| `interval` | `window duration + grace + jitter` after each success | yes |
| `hybrid` | both — fixed anchors re-anchor the interval chain | yes |

Gating rule: `interval`/`hybrid` are locked until the window is verified or user-confirmed with `awewarm verify <id> --duration <minutes> --user-confirm`.

## Workflows

### Check warmth status

```bash
awewarm status
```

Per connection: mode, window, last activation, next due moment; last line shows whether the background scheduler is installed. `degraded` means interval renewal auto-paused after 3 consecutive failures and will re-arm on the next success.

### Change fixed times

```bash
awewarm times claude-code 06:35 11:40 16:45
```

Times are HH:MM, sorted and de-duplicated on save. Slots 5 h + 5 min apart chain windows across a workday.

### Switch mode

```bash
awewarm enable claude-code --mode hybrid
```

If the command reports the window is not verified, guide the verify workflow first.

### Verify a plan's window (3 steps, user-paced)

1. `awewarm verify <id> --confirm` — one real request, timestamped (user must ask for it)
2. The user watches when the plan's quota/window resets and computes elapsed minutes
3. `awewarm verify <id> --duration <minutes> --user-confirm` — unlocks interval/hybrid

### Pause for vacation

```bash
awewarm disable claude-code
```

Config and state are kept; `awewarm enable <id>` resumes.

### Remove a connection

Confirm with the user first — this deletes the connection, its state, and its stored Keychain token:

```bash
awewarm remove <id>
```

## Core Rules

1. Never send real requests (`activate --confirm`, `verify --confirm`, bare `run`) without an explicit user request — they consume plan quota.
2. `init` and `add plan` belong in the user's terminal; they are interactive.
3. Read state through `status` / `inspect`; never hand-edit config.json or state.json.
4. Tokens are Keychain-only or `${ENV_VAR}` refs. Never ask the user to paste tokens into chat; never echo them.
5. `awewarm run` is the scheduler tick (once a minute, launchd on macOS / Task Scheduler on Windows). Don't run it manually to "test" — use `awewarm run --dry-run`.
6. If a command fails, report the exact command and error. Do not silently retry.
