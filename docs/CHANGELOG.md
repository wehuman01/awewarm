# Changelog

## v0.3.5

`v0.3.5` removes the legacy `hybrid` scheduling mode, adds a full-day fixed-slot grid at setup, and removes the macOS pmset wake fallback in favor of pure launchd calendar entries.

### hybrid mode removed — fixed and interval only

Three scheduling modes collapsed to two. The combination mode was the source of the subtlest failures: two engines shared one anchor, a fixed slot landing inside a still-open interval window wasted a request and polluted the renewal chain, and status displayed a "next due" neither engine owned. A `fixed` grid spaced one window apart (see the setup grid below) already chains windows across the whole day and adds calendar wake coverage that interval cannot have; `interval` remains the always-on-machine choice. Existing `hybrid` configs (v1 and v2) migrate to `fixed` on first load — times and days are preserved. `config set --mode` now accepts only `fixed|interval`; anchoring (`--anchor`) is interval-only; the account/plan setup menus offer two choices; calendar wake entries are written for fixed connections only.

### Full-day slot grid offered at setup

`config add` / `init` now know that one fixed time rarely covers a day. When the window duration is known (verified built-in accounts, or a plan whose window you just recorded), the fixed-times prompt asks for the plan's daily quota reset time and offers a full-day grid — one slot per window, spaced window + 5 min apart, anchored on the reset time so drift stays minimal (e.g. reset 01:14 + 300-min window → 01:14, 06:19, 11:24, 16:29, 21:34). Accepting the grid defaults days to every-day; declining keeps the single entered time with the usual weekday default. Windows under 2 h offer no grid (interval mode fits those better).

### pmset wake removal (macOS)

The launchd `StartCalendarInterval` entries cover every fixed slot at its exact time with no sudo; the pmset `wakeorpoweron` fallback covered only the earliest slot, needed sudo, and duplicated schedule state. It is removed. `awewarm update`, `scheduler install`, and `scheduler uninstall` cancel a pmset repeat left behind by earlier versions — only if it is still the one awewarm set, and a failed cancel (no sudo password) is retried by the next of those commands. If the state record was already lost, cancel manually: `sudo pmset repeat cancel` (safe when awewarm's is the only repeating event). Calendar entries now fire every day regardless of the slot's day rule; the tick applies the day rule, so a weekend wake for a weekday-only slot is a no-op. `schedule.wakeLeadMinutes` and `scheduler install --wake/--no-wake` are gone. A fully shut-down Mac no longer auto-boots; the first tick after power-on still catches up slots inside the catch-up window.

## v0.3.6

`v0.3.6` replaces the attempt-count failure pause with a node-based health ladder shared by both modes (`connected → failing → degraded → auto-disabled`), unifies and makes catch-up configurable, shows the last activation failure in `status`, removes the grid generator's 8-slot cap so short-window fixed grids span the full day, asks for the window duration in fixed-mode plan setup so the full-day grid is offered there too, brings Windows wake-from-sleep to parity with macOS and prompts for it at setup, retunes `status` to show the active schedule line, hardens the tick's self-heal so a failed heal can no longer abort the whole tick, adds `config set --start HH:MM` to defer interval activation, and switches `__version__` to dynamic versioning via setuptools.

### Failure handling rebuilt as a health ladder

Both modes now share one ladder. A failed node — a fixed slot or an interval renewal moment — enters `failing`: catch-up retries at most `--catchup-attempts` (default 5) within `--catchup-minutes` (default 30), spaced by the 5-minute throttle; any success returns to `connected`. After `--degrade-after-nodes` (default 3) consecutive lost nodes the connection is `degraded` — single shot per node, no catch-up: interval probes once per window, fixed fires each slot exactly once. The same count again while degraded goes `auto-disabled`: fully silent until `--on` (or a successful manual `run <id>`), both of which reset the ladder but keep schedule memory. Any success resets everything; manual and verify attempts never count as nodes; a slot the machine slept through (zero attempts) is a skip, not a lost node. This replaces the old behavior where three failed *attempts* (about 10 minutes) auto-paused interval only, fixed never paused but showed nothing, and a fixed connection that later switched to interval could inherit a phantom degraded state. `status` prints the rung with details (`Health: failing — 1/3 nodes lost, catch-up attempt 2/5`), and old `intervalDisabledAt`/`consecutiveFailures` state migrates on first read. The fixed-mode catch-up default changes from 45 to 30 minutes; configs that recorded an explicit `catchupMinutes` keep their value.

### Tuning knobs live in a layered `settings` block

config.json gains a top-level `settings` object holding the catch-up/degrade knobs (`catchupMinutes`, `catchupAttempts`, `degradeAfterNodes`), always written with its effective values so the file documents the defaults at a glance. `awewarm config settings` shows or changes them. A connection can override any knob in its own `settings` — set with the existing `config set <id> --catchup-*` / `--degrade-after-nodes` flags — and anything it leaves out falls back to the top level, the same layering the schedule fields use. Overrides persist only while they differ from the global block, so retuning a global value absorbs a matching override. Knob keys written flat on a connection by earlier v0.3.6 builds migrate into that connection's `settings` on first load.

### Full-day slot grid cap removed

Fixed after the v0.3.5 release: the grid generator capped out at 8 slots, so windows under ~3 h were silently cut off mid-day (a 120-min plan got 16.6 h of coverage, not 24); the cap is gone and short-window grids now span the full day.

### Setup asks for the window in fixed mode

Adding a plan and choosing fixed mode now asks for the window duration first (default 300 — most coding plans use 5-hour windows). The answer drives the full-day grid spacing, gets the grid offered right away instead of a single time, and is recorded as a user-confirmed window that unlocks interval mode. Local accounts keep using their builtin window knowledge — the question only appears where nothing else knows the duration.

### Status shows the active schedule

`awewarm status` now prints the schedule line that actually drives the connection. Fixed mode shows `Times: 06:19, 11:24, 16:29, 21:34 (every-day)` — the window said nothing about when fixed mode fires. Interval mode keeps `Window: 300 minutes, user-confirmed`, since the window is its renewal clock. The detailed view (`status <id>`) still shows the other one, with evidence. Disabled connections print `Next due: none (disabled)` instead of a moment the tick would never fire.

### Status shows the last failure

`status` used to print only the last successful activation, so a connection failing every retry still read as healthy. When the most recent attempt failed, the block now adds `Last result: failure (<time>) — <error>` right under `Last activation` — in both the summary and the detailed view.

### Tick self-heal can no longer abort the tick

The tick's opening self-heal pass (rewrites a stale scheduler job) called paths that `die()` on failure — e.g. `awewarm` missing from launchd's sparse PATH, or a failed `launchctl bootstrap`. `die()` raises `SystemExit`, which the pass's error filter didn't catch, so a failed heal aborted the whole tick and skipped that minute's due activations. `SystemExit` is now caught alongside the I/O errors, restoring the intended behavior: the old job keeps running, the tick proceeds, and the next tick retries the heal.

### Wake-from-sleep: Windows parity, prompted at setup

`wakeWhenAsleep` now does something on Windows too. `scheduler install` registers one extra Task Scheduler task per fixed slot — a daily trigger at the slot time with *Wake to run* enabled, running `awewarm tick` — the same shape as the macOS launchd calendar entries (`schtasks.exe` cannot set the flag, so registration goes through PowerShell's `Register-ScheduledTask`). The per-minute tick itself never wakes the machine; only slot times do. Uninstall removes the tasks, config edits refresh them, and the tick's self-heal repairs drift, all mirroring the launchd lifecycle.

The add flows now ask whether fixed slots may wake a sleeping machine on macOS and Windows (default yes); `config set <id> --wake/--no-wake` changes it later, `config set` with no flags shows it, and a wake-affecting edit refreshes the installed entries/tasks immediately. Linux cannot wake a suspended machine at all: the setup flow never asks, new connections record `wakeWhenAsleep: false`, `--wake` there prints a no-effect note, and `scheduler install` says missed slots catch up after the next wake.

### Interval start gate (`--start`)

`config set <id> --start HH:MM` defers interval activation: no request fires before that moment — not the first anchor of a fresh connection, not a renewal whose due has passed, and not a stale `nextDueAt` left over from mode switches. The time resolves to the next occurrence (today when still ahead, otherwise tomorrow), the first tick past it opens the chain, and the gate clears on the first success. `--anchor` clears it too, since anchoring seeds the whole chain explicitly. `--start` requires interval mode (the effective mode after any `--mode` flag in the same call), matching `--anchor`'s strictness; `status` shows the deferred moment as the next due.

### Dynamic versioning

`pyproject.toml` now uses `setuptools` dynamic versioning (`version = {attr = "awewarm.__version__"}`) instead of a static `version` string, and `__version__` is exposed from `awewarm.__init__`.

## v0.3.1

`v0.3.1` replaces the macOS Keychain with a cross-platform `secrets.json` store, collapses the CLI surface to seven commands with legacy aliases, simplifies the manual-fire path to `run <id>`, and adds a macOS wake schedule so fixed-time warm-ups fire with the lid closed.

### API key storage (no Keychain)

Pasted keys now go to `~/.config/awewarm/secrets.json` (created with `chmod 600`). Env-var references in either `$GLM_API_KEY` or `${GLM_API_KEY}` form are first-class citizens: `config add` accepts them inline, and `config set --api-key-env VAR` manages them non-interactively. Legacy `keychain:` refs migrate into `secrets.json` on first load. The Keychain code is removed — its `security -i` write path was truncating stored keys to a single character, and the truncation went unnoticed because activations surfaced only as HTTP 401.

### Flat v2 config format

`config.json` connections collapse from a 4-level nested shape (`kind` / `auth` / `transport` / `plan` / `window` / `activation` / `schedule`) to one flat level: `label`, `url` + `apiKey` + `protocol` (subscription) or `cli` (local account), `model`, `windowMinutes`, `mode`, `times`, `days`. Presence of `url` + `apiKey` marks a subscription; `windowMinutes` present means the window is verified / user-confirmed. Tuning knobs (catch-up, skip-if-activated, grace, jitter) stay at code defaults and land on disk only when changed. v1 files upgrade in place on first load.

### `run <id>` simplification

`awewarm run --now <id> --confirm` collapses to `awewarm run <id>`: the positional connection id fires that one connection immediately, no `--now`, no `--confirm`. Bare `awewarm run` stays the scheduler tick the background agents invoke.

### Manual fires no longer shift the schedule

A successful `run <id>` used to push the interval chain's next due a full window out; it now leaves `nextDueAt` untouched, so a manual test run doesn't move the renewal cadence (fixed-slot skip logic still sees the fresh success). `run <id> --reset-due` opts back into the old behavior. Scheduled activations (fixed / interval / first-anchor) keep rolling the chain as before.

### Activation test during setup

Every added connection — local accounts included — gets one minimal test request through its configured transport and model before being saved. On failure the detail is shown and the user chooses whether to keep the connection (endpoints already had this; accounts did not, which let a broken model name surface only as repeated 6 a.m. activation failures).

### macOS wake schedule

On macOS, `scheduler install` can now register a `pmset repeat wakeorpoweron` event for the earliest fixed slot across all enabled connections, so fixed-time warm-ups fire on schedule even with the lid closed. `scheduler uninstall` cancels it (only if the live schedule still matches what awewarm set). The choice is remembered in `global.wakeWhenAsleep` and can be overridden per-install with `--wake` / `--no-wake`.

### CLI polish

- `config show` / `config edit` — print or edit `config.json` in `$VISUAL` / `$EDITOR` / `nano`; `edit` validates on exit.
- `scheduler install --wake/--no-wake` — macOS wake schedule control.
- Every pre-0.3 command name still works as a hidden alias that prints its new spelling; they are removed in v1.0.

### Highlights

- **Change: Keychain → secrets.json** — cross-platform API key storage with `chmod 600`, env-var refs, and automatic migration from legacy `keychain:` entries.
- **Change: flat v2 config format** — one-level connections, v1 auto-upgrade on load.
- **Change: `run <id>`** — positional immediate fire, no `--now` / `--confirm`.
- **Change: manual fires preserve `nextDueAt`** — test runs no longer shift the renewal chain; `--reset-due` restores old behavior.
- **Add: setup activation test** — every added connection is exercised once before save.
- **Add: macOS wake schedule** — `pmset repeat wakeorpoweron` for fixed slots with the lid closed.
- **Add: `config show` / `config edit`** — inspect or edit config with editor validation.
- **Add: WeChat support** — donation QR code in README Support section.

## v0.3.0

`v0.3.0` redesigns the CLI surface down to seven visible commands. It also ships the window anchoring and versioned-base-URL fixes drafted in the unreleased v0.2.8 section below.

### Seven commands

The seventeen-command surface collapses into `init`, `discover`, `config`, `status`, `run`, `scheduler`, and `update`. Reads go through `status`, changes through `config set` flags, immediate fires through `run --now`. `awewarm run` keeps its name — installed launchd / Task Scheduler / systemd agents invoke it verbatim and keep working without reinstall.

- `config add` is the single add entry: it lists detected local accounts plus "Subscription endpoint (API key)", so a removed `claude` / `codex` account can be re-added without re-running `init`.
- `config set <id> [--times] [--days] [--mode] [--on/--off] [--anchor] [--window]` replaces `times`, `enable`, `disable`, `anchor`, and `verify --user-confirm`; with no flags it prints the current settings.
- `status [<id>] [--json]` absorbs `inspect`; `status <id>` shows one connection in detail (transport, window evidence, fixed times).
- `run --now <id> --confirm` replaces `activate` (and `verify --confirm`).
- `scheduler install` / `scheduler uninstall` replace `install` / `uninstall`.
- `update` replaces `self-update`.

### Legacy aliases

Every pre-0.3 command name still works as a hidden alias that prints its new spelling (for example `awewarm times <id> 06:35` suggests `awewarm config set <id> --times ...`). Alias coverage keeps scripts and older docs functional; they are removed in v1.0.

### Highlights

- **Breaking: seven-command CLI surface** — init, discover, config, status, run, scheduler, update.
- **Add: `config add` menu** — re-add removed local accounts or add a subscription endpoint in one flow.
- **Add: `config set`** — one mutation point for times, days, mode, enable/disable, anchor, and window duration.
- **Add: `--days` on `config set`** — the weekday/every-day rule is changeable without re-onboarding.
- **Keep: legacy aliases** — pre-0.3 command names work through v0.x and print their replacement.

## v0.2.8

`v0.2.8` adds the `awewarm anchor` command so users can tell awewarm when a window they opened by hand is expected to close, and improves base URL handling for versioned API paths.

### Window anchoring

`awewarm anchor <id> --reset HH:MM` seeds the renewal chain from a user-reported close time, so the next request lands just after the current window instead of burning an immediate anchor inside it. `awewarm init` and `awewarm add plan` also prompt for an optional reset time when the user confirms the window is already open.

### Versioned base URL support

Transport URL construction now uses `endpoint_url()`, which appends endpoint paths to bases ending in a version segment (`/v1`, `/v4`, ...) directly, and only adds `/v1` to bare hosts. This fixes mis-routed requests for backends whose published base URL already includes a version.

### Prompt UX

Protocol selection now uses the same numbered-choice prompt as the rest of the CLI, and `add plan` asks for "API / plan URL" with protocol-specific examples.

### Highlights

- **Add: `awewarm anchor`** — anchor interval/hybrid renewal to a user-reported window close without sending a request.
- **Add: `apply_user_anchor()`** — seeds `lastActivationAt` as if a success happened at `reset_at - window_duration`, so `compute_next_due()` starts just after the open window.
- **Fix: endpoint URL construction** — `endpoint_url()` handles versioned base URLs (`/v1`, `/v4`, ...) correctly; previously only `/v1` was special-cased.
- **Improve: CLI prompts** — numbered-choice helper with visible default, protocol-first `add plan` flow, and base URL examples per protocol.

## v0.2.7

`v0.2.7` renames subscription secrets from "token" to "API key" across the codebase and docs, adds a cold-gap warning when a user-confirmed duration is shorter than the verified window, and improves `add plan` UX with protocol-first prompting and base URL examples.

### Terminology: token → API key

All user-facing and config references to subscription secrets now say "API key": prompts, error messages, env-var references (`AWEWARM_API_KEY_*`), and the JSON key (`apiKeyRef`).

### Cold-gap warning on `verify --user-confirm`

When the recorded duration is shorter than the previously verified window (and not covered by grace), awewarm now warns that renewal will fire inside the still-open window, leaving a cold gap each cycle.

### `add plan` UX

Protocol selection now comes first (defaulting to OpenAI Chat Completions), and the base URL prompt shows protocol-specific examples.

### Highlights

- **Change: terminology rename** — `token` → `API key` across CLI, keychain, transport, tests, README, and CHANGELOG.
- **Add: cold-gap warning** — `window_override_notice()` detects when a shorter user-confirmed duration leaves a gap inside the old verified window.
- **Improve: `add plan` prompts** — protocol first with default 1 (OpenAI Chat), base URL examples per protocol.
- **Add: hero2 logo** — compressed WebP hero image and original PNG asset.

## v0.2.6

`v0.2.6` adds Linux support for the background scheduler, completing platform coverage (macOS, Windows, Linux).

### Linux scheduler (systemd user timer)

`awewarm install` on Linux writes `~/.config/systemd/user/awewarm.{service,timer}` and enables a per-minute user timer via `systemctl --user` — no root required. Like the launchd plist, `AWEWARM_*` and `PATH` are baked into the service unit because the user manager's environment is sparser than a login shell. On SSH-only/headless accounts where the user manager is not running, the installer's error suggests `loginctl enable-linger $USER`; systems without systemd get the cron fallback hint (`* * * * * awewarm run`). Missed ticks are recovered from `state.json` catch-up windows, so the timer needs no `Persistent=true`.

### Highlights

- **Add: Linux scheduler** — systemd user timer (oneshot service + 60 s `OnUnitActiveSec`, 5 s `AccuracySec`), install/uninstall/installed-detection.
- Actionable `enable-linger` hint when `systemctl --user` cannot reach the bus.
- Platform badge and docs updated to `macOS | Windows | Linux` across READMEs, README.ai.md, SKILL.md, CONTRIBUTING, and the design spec.
- 9 new tests (unit file contents, env propagation, enable/disable flows, linger hint, cron fallback); suite now at 171 tests.

## v0.2.5

`v0.2.5` adds Windows support for the background scheduler, makes PyPI the advertised install path (v0.1.5 is already published), and adds agent-facing docs, update reminders, and self-update.

### Windows support

`awewarm install` now works on Windows: it registers a per-minute Task Scheduler task via `schtasks` (user-level, no admin required; all scheduling state stays in `state.json`, so the task itself is static — Task Scheduler tasks inherit user env vars, which is also how `${ENV_VAR}` token refs reach ticks after `setx`). CLI transports resolve `.ps1`-installed CLIs (PowerShell installs of Claude Code and friends) through `powershell -ExecutionPolicy Bypass -File`, since `CreateProcess` cannot execute scripts directly; `.exe`/`.cmd` resolve as-is. Without a Keychain on Windows, subscription tokens use `${ENV_VAR}` references and `awewarm add plan` prints a `setx` hint instead of an `export` hint. The Windows CI matrix (green since the v0.1.5 test fixes) now covers real platform paths.

### PyPI install as the default path

README install steps now say `pip3 install awewarm` (the old text still pointed at a source install), and badges switched to PyPI (dynamic version badge, downloads, stars). The platform badge states `macOS | Windows`; Linux users can cron `awewarm run`.

### Highlights

- **Add: Windows scheduler** — `schtasks`-registered per-minute tick, install/uninstall/query, no admin rights needed.
- **Add: `.ps1` CLI routing** through PowerShell for script-installed agent CLIs on Windows.
- **Add: `awewarm self-update`** upgrades to the latest PyPI release (`--check` to preview), detecting pipx installs via `sys.prefix`.
- **Add: background update reminder.** Interactive commands check PyPI at most once a day (cached next to the config; network failures back off 6 h) and print a reminder to stderr. Scheduler ticks (`awewarm run`) never check. Opt out with `AWEWARM_NO_UPDATE_CHECK=1`.
- **Add: `awewarm config path`** prints config, state, and log locations.
- **Add: AI agent bootstrap guide (`README.ai.md`) and bundled skill (`resources/skills/awewarm/SKILL.md`)** with explicit quota-safety boundaries: agents manage schedules locally but never send real requests (`activate`/`verify`/bare `run`) or run the interactive `init`/`add plan`.
- READMEs gained a "let an agent set it up" quick start, a Companion Tools section (aweswitch pairing), a Self-Update section, and a Support section; added `.github/FUNDING.yml`.

## v0.1.5

Fixes and configuration improvements over v0.1.0.

### Highlights

- **Fix: scheduler ticks couldn't find local CLIs.** launchd runs with a
  minimal `PATH` that lacks user-local install dirs, so every tick failed
  with "claude not found in PATH" and interval renewal degraded after three
  failures. Discovery now stores each CLI's absolute path in the connection,
  and `awewarm install` propagates the installing shell's `PATH` as a
  fallback for existing configs (re-run `awewarm install` to pick it up).
- **Fix: `awewarm status` advertised skipped slots as next due.** Slots
  skipped as "recently-activated" are now excluded, and slot lists are
  evaluated chronologically instead of in config order.
- **Add: multi-slot fixed times are now configurable.** The interactive
  prompts accept comma-separated times (e.g. `06:35, 11:40, 16:45`), and
  `awewarm times <id> [HH:MM...]` shows or replaces them after onboarding.

## v0.1.0

Initial release: connect once, keep AI coding-plan subscription windows warm.

### Highlights

- **Account connections**: automatic discovery of local Claude Code and Codex
  CLIs with their login state (read-only scan, no network); Claude Code's
  5-hour session window is recognized as verified, Codex stays unverified by
  default.
- **Subscription connections**: any OpenAI Chat / OpenAI Responses /
  Anthropic-compatible endpoint added via `awewarm add plan` (base URL + token
  + protocol + model), tested with one minimal request before saving.
- **Three schedule modes** with explicit semantics: `fixed` (multi-slot daily
  times with a catch-up window), `interval` (renewal at window duration +
  grace + jitter after each success), `hybrid` (fixed anchor + interval
  renewal, default recommendation). Interval stays locked until the window is
  verified or user-confirmed via `awewarm verify`.
- **Failure policy**: 3 consecutive failures auto-pause interval renewal and
  surface in `awewarm status`; any success re-arms it.
- **launchd scheduler**: `awewarm install` registers a per-minute tick agent
  (macOS); all scheduling state persists in `state.json`, so nothing is lost
  across reboots or sleep.
- **Token safety**: subscription tokens go to the macOS Keychain (fed via
  `security -i` stdin, never visible in `ps`) with `${ENV_VAR}` reference
  fallback; account connections store nothing. Every display path redacts
  secret-looking fields.
- **Zero-dependency core**: `click` only — stdlib `urllib` for HTTP,
  `zoneinfo` for timezones, `plistlib` for launchd.
- 127 unit tests covering the scheduling core (catch-up boundaries, weekday
  rules, grace direction, jitter bounds, degrade/re-arm, DST edges), transport
  request builders, keychain handling, install, and full CLI flows.
