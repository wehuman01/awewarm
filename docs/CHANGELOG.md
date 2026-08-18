# Changelog

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
