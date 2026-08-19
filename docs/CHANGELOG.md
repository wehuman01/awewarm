# Changelog

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
