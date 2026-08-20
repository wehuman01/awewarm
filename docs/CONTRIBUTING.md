# Contributing to awewarm

## Engineering Taste

- Simple: make the smallest change that solves the real problem.
- Clear: optimize for the next reader, not for cleverness.
- Decoupled: keep boundaries clean, but do not add abstractions without a real need.
- Honest: make complexity, state, side effects, assumptions, and failure modes visible; do not hide complexity or create extra complexity.
- Focused: preserve boundaries between modules, and keep top-level convenience commands minimal.
- Durable: choose behavior that is easy to maintain, test, and extend.
- First principles: identify the real problem, hard constraints, and known facts before reaching for patterns, abstractions, or prior solutions.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python3 -m unittest discover -s tests
```

The only runtime dependency is `click`. HTTP uses stdlib `urllib`, timezones use
`zoneinfo`, secrets live in `secrets.json` (legacy Keychain items migrate via
the native `security` CLI), launchd uses `plistlib`.
Do not add a dependency unless it clearly earns its cost.

## Stable design constraints

These are facts and rules, not suggestions. Changes here need a spec update
first.

- **Tick model**: the system scheduler (launchd on macOS, Task Scheduler on
  Windows, systemd user timer on Linux) invokes `awewarm run` once a minute.
  All scheduling state lives in `state.json`; the process is stateless
  between ticks. There is no daemon.
- **Pure planner**: `schedule.plan_actions(connection, conn_state, now)` does
  no I/O and takes an injected clock. Every scheduling rule lives there and is
  covered by tests. Do not put time decisions anywhere else.
- **Grace runs after the window**: interval renewal fires at
  `last success + durationMinutes + graceSeconds + jitter`. Firing early would
  land inside the old window and start nothing.
- **Window gating**: `fixed` is always allowed; `interval`/`hybrid` require
  `window.status ∈ {verified, user-confirmed}` plus a duration. Enforced by
  `config.connection_errors` on every save.
- **Mode is stored explicitly** (`fixed` / `interval` / `hybrid`); "disabled"
  is the connection-level `enabled: false` flag. Never store both.
- **Hybrid anti-double-fire**: any success re-anchors interval; a fixed slot
  within `skipIfActivatedWithinMinutes` of a recent success is marked satisfied
  instead of firing.
- **Failure policy**: a health ladder shared by both modes — `connected →
  failing → degraded → auto-disabled`. A failed node (slot or renewal moment)
  gets catch-up retries (`catchup.attempts` within `catchup.withinMinutes`,
  spaced by the 5-minute throttle); a lost node counts one rung.
  `degradeAfterNodes` consecutive lost nodes (default 3) drop to degraded
  (one shot per node, no catch-up); the same count again drops to
  auto-disabled (fully silent until `--on`). Any success resets the whole
  ladder; manual/verify fires never count as nodes; a slot the machine slept
  through (zero attempts) is not a lost node.
- **Security**: `discover` is read-only (no network, existence checks only);
  API keys live in `secrets.json` (0600, read-back verified on store; legacy
  `keychain:` refs migrate once via the `security` CLI); every display path
  goes through `transport.redact`;
  logs never contain API keys or auth headers.
- **CLI transports resolve to absolute paths** at send time — launchd runs
  with a minimal PATH.
- **Update checks never run on scheduler ticks**: `update_check.check_async`
  is wired into `main()` and skips `run` (and `self-update`/help/version),
  checks PyPI at most once a day, and backs off 6 h on network failure.
  Opt-out: `AWEWARM_NO_UPDATE_CHECK=1`.

## Code style

Follow the sibling project aweswitch: module-level functions with explicit
arguments (that is the test seam), plain dicts validated with `isinstance`,
`die()` with actionable multi-line errors, English docstrings that explain why.
Tests are `unittest` + `click.testing.CliRunner`, one file per module, with
env-overridable paths (`AWEWARM_CONFIG` etc.) for isolation.

When behavior changes, update README.md, README_cn.md, docs/CHANGELOG.md, and
the affected tests in the same change.

## Branch model and release

Work lands on `dev`, is promoted to `main`, then tagged `vX.Y.Z`. The release
workflow (`.github/workflows/release.yml`) verifies the tag matches
`pyproject.toml`, extracts the matching `## vX.Y.Z` section from
`docs/CHANGELOG.md` as release notes, and publishes to PyPI. Add a CHANGELOG
entry as part of any user-visible change.
