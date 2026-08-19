---
name: awewarm
description: "Use when helping users manage awewarm warm-up schedules — checking status, changing fixed times, switching fixed/interval modes, verifying plan windows, or installing the background scheduler. 中文触发词：保温、保活、订阅窗口、warm-up、awewarm、固定时间、调度模式、5小时窗口。"
---

# awewarm

This skill covers **managing** awewarm's warm-up schedules: status, fixed times, modes, window verification, and the scheduler install.

## Quota Rules (read this first)

Every activation sends one REAL request against the user's coding-plan quota:

- **Never run** `awewarm run` or `awewarm run <id>` unless the user explicitly asks for a request to be sent. They prompt for confirmation by default; `--force` skips the prompt (for scripting).
- `awewarm tick` is the background scheduler's own command (hidden). It is not for interactive use.
- `awewarm init` and `awewarm config add` are interactive (they prompt for choices and API keys, and send one test request per added connection). The user runs them in their own terminal.

## Command Safety

| Category | Commands |
|---|---|
| Read-only — run freely | `awewarm status [<id>] [--json]`, `awewarm discover`, `awewarm config set <id>` (no flags = show settings), `awewarm config path`, `awewarm update --check` |
| Local changes — run on request | `awewarm config set <id> --times/--days/--mode/--on/--off/--anchor/--start/--window`, `awewarm config remove <id>` (confirm first — deletes the stored API key), `awewarm scheduler install`, `awewarm scheduler uninstall`, `awewarm update` |
| Real requests — prompts by default; `--force` skips the prompt | `awewarm run [<id>] [--reset-due] [--force]`. Errors with a clear message if called from a non-tty without `--force`. |
| Scheduler-only — never call manually | `awewarm tick` (hidden). The background scheduler agent calls this once a minute. |

Commands from pre-0.3 releases (`add plan`, `times`, `enable`, `verify`, `anchor`, `activate`, `inspect`, `self-update`, ...) still work as hidden aliases that print their new spelling — prefer the new names.

## Intent Router

| User intent | Approach |
|---|---|
| "Keep my plan warm", "别让窗口过期" | Check `awewarm status` first, then tune modes/times below. |
| "When is the next warm-up?", "下次什么时候触发" | `awewarm status` |
| "What can awewarm see?", "检测一下本机" | `awewarm discover` |
| "Change warm-up times to 9:00 and 14:00", "改保温时间点" | `awewarm config set <id> --times 09:00,14:00` |
| "Switch to interval / fixed", "切换模式" | `awewarm config set <id> --mode <mode>`; interval needs a verified window |
| "Pause while I'm on vacation", "休假暂停" | `awewarm config set <id> --off` (resume with `--on`) |
| "Add my GLM subscription", "添加订阅套餐" | Tell the user to run `awewarm config add` in their terminal (interactive API key prompt). It also re-adds removed local accounts. |
| "Verify the window", "验证窗口时长" | Guide the 3-step verify flow below; only send the real request if the user asks. |
| "Remove this plan", "删掉这个连接" | Confirm, then `awewarm config remove <id>` |
| "Where are awewarm's files?", "配置在哪" | `awewarm config path` |
| "Is the scheduler installed?", "调度器装了吗" | `awewarm status` (last line) or `awewarm scheduler install` |

## Config and State

- Config: `~/.config/awewarm/config.json` (override: `AWEWARM_CONFIG`)
- State: `~/.local/state/awewarm/state.json`; log: `~/.local/state/awewarm/awewarm.log` (override: `AWEWARM_STATE` / `AWEWARM_LOG`)

Users never hand-edit these files; commands mutate them. Read current state through `awewarm status` or `awewarm status <id> --json` (redacted).

Subscription API keys live in `secrets.json` next to the config (0600) — never in the config file, never echoed. `${ENV_VAR}` refs are no longer supported (background schedulers cannot read shell variables; the CLI rejects them), and legacy `keychain:` refs migrate into `secrets.json` on first load. Account connections store no credentials at all; they reuse local `claude` / `codex` logins.

## Scheduling Modes

| Mode | Fires when | Needs verified window |
|---|---|---|
| `fixed` | fixed local times (`weekday` or `every-day`), each slot may fire late within its catch-up window (default 45 min) | no |
| `interval` | `window duration + grace + jitter` after each success | yes |

Gating rule: `interval` is locked until the window is verified or user-confirmed with `awewarm config set <id> --window <minutes>`.

## Workflows

### Check warmth status

```bash
awewarm status
```

Per connection: mode, schedule line (fixed times in fixed mode; window in interval mode), last activation, next due moment; last line shows whether the background scheduler is installed. `awewarm status <id>` shows one connection in detail (transport, evidence, plus the schedule info the summary omits). `degraded` means interval renewal auto-paused after 3 consecutive failures and will re-arm on the next success.

### Change fixed times

```bash
awewarm config set claude-code --times 06:35,11:40,16:45
```

Times are HH:MM (comma- or space-separated), sorted and de-duplicated on save. Slots 5 h + 5 min apart chain windows across a workday. `--days weekday|every-day` changes the day rule.

### Switch mode / pause / resume

```bash
awewarm config set claude-code --mode interval
awewarm config set claude-code --off     # pause (config and state kept)
awewarm config set claude-code --on      # resume
```

If a mode switch reports the window is not verified, guide the verify workflow first.

### Verify a plan's window (3 steps, user-paced)

1. `awewarm run <id>` — one real request, timestamped (user must ask for it). Prompts for confirmation; pass `--force` only if the user is running it scripted. By default the run does NOT move the next due moment; `--reset-due` restarts the interval chain from this run.
2. The user watches when the plan's quota/window resets and computes elapsed minutes
3. `awewarm config set <id> --window <minutes>` — unlocks interval

### Anchor an already-open window

```bash
awewarm config set <id> --anchor HH:MM
```

Tells awewarm when the current window closes; renewal starts right after it instead of firing inside it. No request is sent.

### Defer interval's first fire

```bash
awewarm config set <id> --start HH:MM
```

One-time gate: no request fires before that moment (today, or tomorrow if it has passed) — covers the first anchor and any stale chain due. The first tick past it opens the chain; the gate clears on the first success (`--anchor` clears it too). Requires interval mode; combine with the switch: `awewarm config set <id> --mode interval --start HH:MM`.

### Remove a connection

Confirm with the user first — this deletes the connection, its state, and its stored API key (secrets.json):

```bash
awewarm config remove <id>
```

## Core Rules

1. `awewarm run` fires immediately, ignoring the schedule — it sends real requests and consumes quota. `awewarm run` (no id) fires every enabled connection; `awewarm run <id>` fires one. Both prompt for confirmation by default; `--force` skips the prompt (for scripting). The scheduler never calls `awewarm run`; it calls `awewarm tick`.
2. `awewarm tick` is the scheduler's own command (hidden). It checks the schedule and only fires what's currently due. Never call it manually — use `awewarm run` for manual activations, `awewarm status` to preview what would fire.
3. `init` and `config add` belong in the user's terminal; they are interactive.
4. Read state through `status`; never hand-edit config.json or state.json.
5. API keys live in `secrets.json`; env-var refs are rejected by the CLI. Never ask the user to paste keys into chat; never echo them.
6. If a command fails, report the exact command and error. Do not silently retry.
