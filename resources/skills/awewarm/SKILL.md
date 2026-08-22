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
| Read-only — run freely | `awewarm status [<id>] [--remote|--local] [--json]`, `awewarm discover`, `awewarm config set <id>` (no flags = show settings), `awewarm config path`, `awewarm self-update --check` |
| Local changes — run on request | `awewarm config set <id> --times/--days/--mode/--on/--off/--anchor/--start/--window/--wake/--no-wake/--inherit-schedule`, `awewarm config remove <id>` (confirm first — deletes the stored API key), `awewarm remote push [<id>]`, `awewarm scheduler install [--wake]`, `awewarm scheduler uninstall`, `awewarm self-update` |
| Delegation — changes who ticks a connection; confirm intent first | `awewarm remote connect <url> [--token]` (single-user server) or `awewarm remote connect <url> --invite awi_...` (hub server), `awewarm config set <id> --remote` (only subscription connections; pushes config+key to the server), `awewarm config set <id> --local` (takeback: pulls server state), `awewarm remote disconnect` (refuses while delegations exist) |
| Real requests — prompts by default; `--force` skips the prompt | `awewarm run [<id>] [--reset-due] [--force]`. Errors with a clear message if called from a non-tty without `--force`. On delegated connections it fires on the server. |
| Scheduler-only — never call manually | `awewarm tick` (hidden). The background scheduler agent calls this once a minute. |
| Server-side — user runs on the 24/7 box | `awewarm serve [--data-dir/--bind/--port/--token]` (resident process; do not background it from an agent session). |
| Hub admin — operator runs on the hub box | `awewarm hub status [--details]` (read-only overview: tenants/connections against the caps, invite counts, serve liveness probe), `awewarm hub invite [--note/--expires-hours]` (side effect: writes tenants.json; the code is recoverable later), `awewarm hub list users [--api/--reveal/--json]` (tenant table; --api adds each connection's API endpoint, --reveal the joining invite code), `awewarm hub list invites [--reveal/--json]` (every minted code with pending/used/expired/revoked status; codes masked unless --reveal), `awewarm hub revoke <tenant>` (confirm first — kills that user's pairings and their delegated connections). All take `--data-dir` (default `~/.awewarm-server`). |

Commands from pre-0.3 releases (`add plan`, `times`, `enable`, `verify`, `anchor`, `activate`, `inspect`, ...) still work as hidden aliases that print their new spelling — prefer the new names. `remote status` folded into `status --remote` (hidden alias likewise). `awewarm update` was removed outright in v0.5.0 — use `awewarm self-update`.

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
| "Let the server keep it warm 24/7", "委托服务器保温" | Requires a paired server: `awewarm remote connect <url>` then `awewarm config set <id> --remote` (subscription connections only). Check `awewarm status --remote` afterwards (server health line + delegated connections). |
| "Share one server with my team", "多人共用一台服务器/hub" | Operator runs `awewarm serve --hub` and hands out `awewarm hub invite` codes; each user runs `awewarm remote connect <url> --invite awi_...`. Warn: hub users must trust the box's operator/root — their API keys pass through its RAM. |
| "Take it back local", "收回本地" | `awewarm config set <id> --local` — pulls server state first, local scheduling resumes. |
| "Server restarted / key missing", "服务器重启/缺钥" | Harmless by design: any local command or `awewarm remote push` re-claims and re-pushes; held slots fire late within catch-up. |
| "Remove this plan", "删掉这个连接" | Confirm, then `awewarm config remove <id>` |
| "Where are awewarm's files?", "配置在哪" | `awewarm config path` |
| "Is the scheduler installed?", "调度器装了吗" | `awewarm status` (last line) or `awewarm scheduler install` |
| "Wake the machine even with the lid closed", "合盖睡眠也要准时保温" | macOS/Windows only: `awewarm scheduler install --wake` (macOS prompts for sudo once to write /etc/sudoers.d/awewarm; Windows needs no grant). Verify with `awewarm status` — footer shows `Wake layer: enabled` and the next armed wake. Per-connection opt-out: `awewarm config set <id> --no-wake`. Linux cannot wake a suspended machine; suggest delegating to `awewarm serve` instead. |

## Config and State

- Config: `~/.config/awewarm/config.json` (override: `AWEWARM_CONFIG`)
- State: `~/.local/state/awewarm/state.json`; log: `~/.local/state/awewarm/awewarm.log` (override: `AWEWARM_STATE` / `AWEWARM_LOG`)

Users never hand-edit these files; commands mutate them. Read current state through `awewarm status` or `awewarm status <id> --json` (redacted).

Subscription API keys live in `secrets.json` next to the config (0600) — never in the config file, never echoed. `${ENV_VAR}` refs are no longer supported (background schedulers cannot read shell variables; the CLI rejects them), and legacy `keychain:` refs migrate into `secrets.json` on first load. Account connections store no credentials at all; they reuse local `claude` / `codex` logins.

## Scheduling Modes

| Mode | Fires when | Needs verified window |
|---|---|---|
| `fixed` | fixed local times (`weekday` or `every-day`), each slot may fire late within its catch-up window (default 30 min) | no |
| `interval` | `window duration + grace + jitter` after each success | yes |

Gating rule: `interval` is locked until the window is verified or user-confirmed with `awewarm config set <id> --window <minutes>`.

## Workflows

### Check warmth status

```bash
awewarm status
```

Per connection: mode, schedule line (fixed times in fixed mode; window in interval mode), last activation, next due moment; last line shows whether the background scheduler is installed (on macOS, a second footer line shows the wake layer: `Wake layer: enabled — N RTC wake(s) armed` or the `--wake` enable hint). `awewarm status <id>` shows one connection in detail (transport, evidence, plus the schedule info the summary omits). `--remote` filters to delegated connections and leads with the server health line (version, uptime, last tick); `--local` shows only locally scheduled connections. `degraded` means interval renewal auto-paused after 3 consecutive failures and will re-arm on the next success.

### Change fixed times

```bash
awewarm config set claude-code --times 06:35,11:40,16:45
```

Times are HH:MM (comma- or space-separated), sorted and de-duplicated on save. Slots 5 h + 5 min apart chain windows across a workday. `--days weekday|every-day` changes the day rule.

**Keep the chain (confirm first)**: when the user changes one slot (e.g., "把第一个时间改成 05:50"), tell the user the planned recalculation, wait for confirmation, then treat the user-supplied time as the anchor and recalculate the rest at 5 h + 5 min intervals from that anchor. Do not silently change the slot count unless the user asks.

**Respect explicit irregularity (confirm first)**: if the user clearly intends custom spacing (e.g., "05:50, 10:55, 16:00, 21:05"), show the final list to the user, wait for confirmation, then pass the times through as-is — do not re-space them.

### Quick Templates

Common scheduling patterns as starting points.

```bash
# Standard workday (morning + afternoon)
awewarm config set <id> --times 06:00,11:05,16:10

# With evening overtime
awewarm config set <id> --times 06:00,11:05,16:10,21:15

# Weekday only
awewarm config set <id> --times 08:00,13:05 --days weekday

# Interval (verified 5h window)
awewarm config set <id> --mode interval --window 300 --anchor 11:05
```

Slots are 5 h 5 min apart — the subscription window (5 h) plus a 5 min buffer. Each slot fires once and opens a fresh window. With `06:00,11:05,16:10,21:15` you get morning, afternoon, evening, and late-night coverage; three slots (`06:00,11:05,16:10`) cover a standard workday. `--days weekday` limits fires to weekdays. Two-slot chains are fine for half-day or intermittent use.

### Switch mode / pause / resume

```bash
awewarm config set claude-code --mode interval
awewarm config set claude-code --off     # pause (config and state kept)
awewarm config set claude-code --on      # resume
```

If a mode switch reports the window is not verified, guide the verify workflow first.

**Preserve anchor on fixed → interval (confirm first)**: when switching from fixed to interval, check `awewarm status <id>` first, derive `--anchor` from the existing schedule's next-due or last-activation time, show the planned anchor to the user, wait for confirmation, then switch and set anchor in the same command:

```bash
awewarm config set claude-code --mode interval --anchor HH:MM
```

This avoids a gap or duplicate fire caused by losing the existing schedule's position in time.

### Verify a plan's window (3 steps, user-paced)

1. `awewarm run <id>` — one real request, timestamped (user must ask for it). Prompts for confirmation; pass `--force` only if the user is running it scripted. By default the run does NOT move the next due moment; `--reset-due` restarts the interval chain from this run.
2. The user watches when the plan's quota/window resets and computes elapsed minutes
3. `awewarm config set <id> --window <minutes>` — unlocks interval

### Anchor an already-open window

```bash
awewarm config set <id> --anchor HH:MM
```

Tells awewarm when the current window closes; renewal starts right after it instead of firing inside it. No request is sent.

### Defer the next fire (both modes)

```bash
awewarm config set <id> --start HH:MM
```

One-time gate: no request fires before that moment (today, or tomorrow if it has passed). In interval mode it covers the first anchor and any stale chain due; in fixed mode a held slot fires right after the gate lifts while still inside its catch-up window — `--start 16:05` turns today's 16:00 slot into 16:05 without touching the times list (a gate past a slot's catch-up end skips that slot). The gate clears on the first success (`--anchor` clears it too).

### Enable RTC wake for lid-closed sleep

```bash
awewarm scheduler install --wake
```

Two layers on macOS: calendar entries fire the tick exactly while awake (default); `--wake` additionally arms `pmset` RTC wake events so a lid-closed sleeping Mac wakes at every slot/renewal moment (asks for sudo once — writes a scoped `/etc/sudoers.d/awewarm` grant; the unattended tick re-arms events as schedules drift). Windows needs no grant: fixed slots get Wake-to-run tasks, interval renewals get one-shot wake tasks. Verify with `awewarm status` (wake footer) or `pmset -g sched`. Battery standby (RAM off) cannot be woken on schedule — for that, delegate to an always-on `awewarm serve`. `awewarm config set <id> --no-wake` opts one connection out of both layers; `scheduler uninstall` cancels all armed events and removes the grant.

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
