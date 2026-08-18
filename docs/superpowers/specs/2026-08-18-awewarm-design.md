# awewarm v0.1 Design

Date: 2026-08-18
Status: approved (two revision rounds incorporated)

## Positioning

Connect once; awewarm detects what a local Claude Code / Codex account or a
subscription endpoint can do, then keeps the plan's usage window warm with
minimal scheduled requests. Users never need to understand 5-hour resets,
OAuth flows, or credential paths.

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Runtime | Python >= 3.9, click as the only dependency | Sibling of aweswitch; same stack, same style. HTTP via stdlib urllib, timezones via zoneinfo, Keychain via native `security`. |
| v0.1 scope | Doc's narrowed cut | Claude Code account (discover + fixed + interval), Codex account (discover + fixed), subscription endpoints (3 protocols, fixed, Keychain). No automatic third-party 5h-window detection. |
| Config format | JSON at `~/.config/awewarm/config.json`, state at `~/.local/state/awewarm/state.json` | Zero-dependency, tool-generated (users never hand-write), aweswitch-consistent. Both paths overridable via `AWEWARM_CONFIG` / `AWEWARM_STATE` / `AWEWARM_LOG` (also the test seam). |
| Token storage | macOS Keychain first (`security` CLI, token passed via `security -i` stdin so it never appears in `ps`), `${ENV_VAR}` reference fallback | No keyring dependency. Account mode stores nothing — it reuses local CLI logins. |

## Scheduler architecture

Tick model: launchd (`StartInterval: 60`) invokes `awewarm run` once a minute.
The process loads config + state, computes due actions with a pure function,
executes them, writes state back, exits. No daemon; missed ticks are covered
by fixed-mode catch-up; interval chaining is anchored in persisted state.

`schedule.plan_actions(connection, conn_state, now)` is pure: no network, no
disk, injected clock. It is the most heavily tested code in the project.

## Module layout

```
src/awewarm/
├── __init__.py    # __version__ only
├── cli.py         # click command layer; thin orchestration (run tick, onboarding)
├── config.py      # env-overridable paths, config/state load-save, validation, die()
├── discover.py    # local claude/codex detection; read-only; builtin window knowledge
├── transport.py   # minimal-request senders for 5 transports; pure builders + I/O senders
├── schedule.py    # pure time logic: fixed catch-up, interval renewal, state transitions
├── keychain.py    # token store: security CLI wrapper + ${ENV_VAR} fallback
├── install.py     # launchd install/uninstall (macOS-only in v0.1)
└── update_check.py  # background PyPI check for interactive commands (never on run ticks)
```

Data model follows aweswitch: plain dicts + isinstance validation, no
dataclasses/pydantic. A connection has five sections: auth / transport /
plan / window / activation / schedule.

## Schedule semantics (final)

Modes are first-class in config (`schedule.mode`): `fixed`, `interval`,
`hybrid`. The fourth state, **disabled**, is the connection-level
`enabled: false` flag (`awewarm disable <conn>`); manual `activate` still
works on a disabled connection's config but `run` skips it. Single source of
truth — no separate mode named "disabled" is stored.

| mode | Behavior | First anchor |
|---|---|---|
| fixed | Absolute time table; `at` supports multiple daily slots (e.g. 06:35 / 11:40 / 16:45 is a full-day warm pattern on its own) | none needed |
| interval | Renewal chains from last success | if no recorded success, the first tick after enabling fires once to establish the anchor |
| hybrid | fixed anchors, interval renews; default recommendation | fixed slots |

- **fixed**: per-slot catch-up window (default 45 min). A tick in
  `[T, T+catchup)` with the day's slot unmet fires; past the window the slot
  is recorded skipped and never fires late. `days: weekday | every-day`.
  Each slot fires at most once per day (`completedSlots` keyed by date).
- **interval**: next due = last success + `durationMinutes` + `graceSeconds`
  + jitter (uniform 0–`jitterSeconds`, drawn once at success time and
  persisted as `nextDueAt`). Grace is a safety margin so the old window has
  certainly closed — firing early would land inside the old window and waste
  the request. Example: success 07:05, 300 min window, grace 75 s → next due
  ≈ 12:06:15.
- **hybrid anti-double-fire**: every success (fixed or interval) refreshes
  the interval anchor; a fixed slot within `skipIfActivatedWithinMinutes`
  (default 30) of a recent success is marked satisfied instead of firing.
- **failure policy**: after 3 consecutive activation failures interval is
  auto-paused (`intervalDisabledAt` in state; hybrid degrades to fixed-only)
  and surfaced in `status`. Any later success re-arms it. Failed attempts are
  retried at most once per 5 minutes (no hammering a dead endpoint for the
  whole catch-up window).
- **window gating**: `fixed` is always allowed. `interval`/`hybrid` require
  `window.status ∈ {verified, user-confirmed}` and a `durationMinutes`.
  Builtin provider knowledge: claude-code → verified, 300 min,
  first-successful-request. Codex and all subscription endpoints → unknown.

## Commands (v0.1)

```
init        interactive onboarding (discover accounts, propose schedule)
discover    scan local claude/codex CLIs and login state (read-only, no network)
add plan    add a subscription endpoint (base URL + token + protocol + model)
status      human-readable summary
run         one scheduler tick (used by launchd); --dry-run prints plans only
activate    send one real request; requires --confirm
verify      show window evidence; optionally send one request and/or record a
            user-confirmed window duration (--duration N --user-confirm)
enable      enable a connection, optionally switching --mode
disable     stop scheduling a connection (config kept)
remove      delete a connection (config + state + keychain token)
install     install the launchd scheduler agent
uninstall   remove the launchd scheduler agent
inspect     redacted capability dump; --json for machine-readable
```

`-h/--help`, `-v` (bare version), `die()` with actionable multi-line errors —
aweswitch conventions throughout.

## Security invariants

- `discover` is purely read-only: no network, no activation, no secret values
  read into memory (credential existence checks only).
- Real requests happen only on explicit paths: onboarding confirmation,
  `activate --confirm`, `verify` confirmation, or an enabled schedule's due
  tick.
- Tokens never appear in logs, status, errors, or `inspect` (key-based
  `redact()`); Authorization headers are never echoed; Keychain writes pass
  the token through `security -i` stdin, not argv.
- Account mode stores zero credentials; it shells out to `claude` / `codex`
  which manage their own login state.
- Default mode is fixed; interval stays locked until the window is verified
  or user-confirmed.

## Testing

unittest + click CliRunner (aweswitch style), one test file per module.
`schedule.py` tests are the core: catch-up boundaries, multi-slot days,
weekday rules, anti-double-fire, grace direction, jitter bounds, degrade /
re-arm, DST-nonexistent slots. Transport tests cover pure request builders
(URL normalization for bases with/without `/v1`, headers, bodies) plus
mocked subprocess/urllib senders. CLI tests run full flows through CliRunner
with env-overridable paths, a fake transport, and the keychain disabled.

## Out of scope for v0.1 (interfaces already accommodate them)

Codex interval, third-party plan detectors (GLM / StepFun quota adapters),
systemd installer, `keepAliveAroundClock` / machine-activity gating,
automatic window observation via usage APIs.
