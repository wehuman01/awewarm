# Account Delegation to serve / hub — Design

- Date: 2026-08-25
- Status: draft, awaiting user review (nothing implemented yet)
- Scope: `awewarm` (this repo) plus coordinated updates in the separate `awewarm-hub` package

## Problem

Account connections (`claude-cli`, `codex-cli`) can only warm on the machine that
holds the CLI login. Subscription (API-key) connections can delegate to
`awewarm serve` / `awewarm-hub serve`. Goal: parity — a logged-in Claude Code or
Codex account can be delegated and warmed by a remote server.

Three gates currently forbid this, and all three change:

- `cli.py::_delegate_remote` — refuses non-subscription kinds
- `config.py::connection_errors` — `location == "remote"` + `kind == "account"` is invalid
- `server.py::WarmServer.put_connection` — server rejects non-subscription pushes

## Chosen model: push the credential (same as API keys)

The local machine reads its own login material and pushes it to the server
exactly the way an API key is pushed: RAM only by default, opt-in plaintext
persistence through the existing `persistKey` machinery. The server executes the
CLI subprocess with the credential injected per provider:

- **claude-cli** — `CLAUDE_CODE_OAUTH_TOKEN = <accessToken>` extracted from the
  credentials JSON. Fallback if the installed CLI does not honor the env var:
  materialize the credentials into a sandbox dir and point `CLAUDE_CONFIG_DIR`
  at it (same pattern as codex below).
- **codex-cli** — `CODEX_HOME = <data-dir>/codex-home/<conn_id>/` with
  `auth.json` re-written from the RAM credential before every fire. Any token
  refresh the CLI writes into the sandbox is discarded on the next
  re-materialization.

Why not the alternatives: a server-local login ("log in on the server, warm with
the operator's account") cannot serve hub tenants — it warms the operator's
account, not the tenant's — and a hybrid means two delegation semantics. One
push model is true API parity and one mechanism to maintain.

## Non-goals

- No server→local credential writeback. The local login is the source of truth;
  server-side refresh results are thrown away. Rotation flows local→server only.
- No auto-install of CLIs on the server. Operator prerequisite; the push
  validates and errors actionably.
- Local (non-delegated) account firing is unchanged — no env injection locally.

## Local credential reading — new `credstore.py`

- claude: macOS `security find-generic-password -s "Claude Code-credentials" -w`;
  otherwise read `~/.claude/.credentials.json`.
- codex: read `~/.codex/auth.json`.
- Returns the raw JSON string plus a sha256 fingerprint (16 hex chars, for
  display and drift detection). Read failures produce actionable errors
  (Keychain ACL prompt, missing file → "log in first, then re-push").
- `discover.py` keeps its contract: existence checks only, secrets never read.
  Credential reads happen exclusively inside delegation / re-push flows.

## Protocol changes

Wire field `apiKey` keeps its name but is documented as "the connection's
secret" (API key or login-token JSON) — changing the field name would break the
hub's pinned dependency for no gain.

- `push_connection` body gains `credentialFingerprint` (always sent).
- `put_connection` (server):
  - accepts `kind == "account"` with a CLI transport;
  - still requires the secret string;
  - resolves the CLI binary server-side: exact `transport.cliCommand` path →
    `shutil.which(basename)` → `400` with an install hint; the resolved absolute
    path is stored in the server's private copy of the connection;
  - records the fingerprint in the server config entry.
- `connection_errors`: the remote+account rejection is removed; remote accounts
  keep the normal per-kind checks.
- `view()`: exposes per-connection `credentialFingerprint` alongside the
  existing `keyMissing` / `keyPersisted`.
- `put_keys` (re-key after restart): unchanged — credential-agnostic.
- `run_connection` client timeout: raised for CLI transports (CLI cap 120 s →
  client waits ~135 s). Flagged risk: long round-trips through proxies.

## Server execution model

`transport.py`:

- new `activation_env(connection, credential)` → dict injected into the
  subprocess environment (claude token extraction is defensive: unrecognized
  JSON shape → activation failure with "credential format not recognized —
  re-push from the local machine");
- `_send_cli` gains an `env` overlay parameter (merged over `os.environ`).

`server.py`:

- `tick()` no longer holds the global lock across activations. Per connection:
  plan + fire under a **per-connection mutex** (prevents tick / `run_now`
  overlap on one connection), executed on a bounded thread pool (4 workers),
  global lock re-acquired once at the end to persist state. API calls
  (`/v1/state`, pushes) stop blocking behind a slow activation.
- CLI transports use `CLI_TIMEOUT_SECONDS` (120 s); HTTP stays at 15 s.
- `run_now` uses the same execution path.
- Codex sandbox dirs live under the engine's data dir (per-tenant on a hub,
  since each tenant warm owns its own data dir), files 0600, cleaned on
  `delete_connection`.

## Freshness sync — local is the source of truth

`_maybe_sync_remote` (the existing 30-minute throttled sync) additionally
compares the local credential fingerprint of each delegated account against the
server view's fingerprint; mismatch → re-push with a freshly read credential
(`remote push` does the same on demand). This rides existing machinery — no new
endpoints, no background loop.

## Security

- An account credential is account-wide — strictly more sensitive than a scoped
  API key. Delegating an account therefore adds an explicit confirmation naming
  the target server URL ("your <provider> login credential will live in this
  server's RAM"). Hub trust-rule copy updated to match ("your login token passes
  through the hub's RAM").
- `persistKey` remains allowed for accounts (the keys.json machinery is
  credential-agnostic) with a harder-worded notice.
- Secrets still never logged; redaction paths unchanged.

## CLI / UX

- `awewarm config set <account-id> --remote`: read credential → confirm →
  resolve + push → mark remote. The local tick already skips remote connections.
- `awewarm remote push` re-reads the current credential each time.
- `awewarm status --remote`: account connections render like subscriptions;
  `keyMissing` reads as "credential missing (server restarted?) — rerun:
  awewarm remote push".
- `awewarm run <account-id>` on a delegated account fires the CLI on the server.

## Files touched (awewarm)

- `src/awewarm/credstore.py` (new) — credential read + fingerprint
- `src/awewarm/transport.py` — `activation_env`, env overlay in `_send_cli`
- `src/awewarm/server.py` — `put_connection` validation, fingerprint, threaded
  tick, per-connection mutex, sandbox lifecycle
- `src/awewarm/remote.py` — fingerprint in push body, run timeout for CLIs
- `src/awewarm/config.py` — `connection_errors` change
- `src/awewarm/cli.py` — `_delegate_remote` account path + confirm,
  `_require_api_key` → secret resolution per kind, `_sync_remote` fingerprint
  drift, status wording
- `README.md` / `README.ai.md` / skill copy — delegation of accounts, safety
  rules
- `awewarm-hub` (separate repo) — trust-rule notices, tests, version pin bump

## Staging

0. **Spike (throwaway scripts, gates everything):** verify on a real box that
   (a) `CLAUDE_CODE_OAUTH_TOKEN=... claude -p "Reply with exactly: ok"` fires
   headless without a local login; (b) `CODEX_HOME=<dir> codex exec
   --skip-git-repo-check "..."` fires with only a materialized `auth.json`;
   (c) `security find-generic-password -s "Claude Code-credentials" -w` returns
   the JSON without an interactive prompt (or documents the prompt behavior).
1. **Server side:** transport injection + `put_connection` + threaded tick + tests.
2. **Local side:** delegation flow, fingerprint re-push, status UX + tests.
3. **Hub + docs:** awewarm-hub notices/tests/pin, README and skill updates.

## Testing

- Unit: env extraction (token parse failures actionable), codex sandbox write +
  re-materialize, `put_connection` accept/reject matrix (wrong kind, missing CLI
  binary), `connection_errors` for remote accounts, fingerprint drift triggers
  re-push.
- Engine: tick with fake CLI scripts on PATH (existing `test_server.py`
  patterns), per-connection mutex behavior, state persisted after a pass.
- End-to-end (local fake serve): delegate a fake account connection, assert the
  server fires the env-injected CLI and records success.

## Risks / open items

- Env-var and `CODEX_HOME` support depend on installed CLI versions — stage 0
  verifies before anything is built; the `CLAUDE_CONFIG_DIR` sandbox is the
  fallback.
- macOS Keychain read may prompt depending on the item's ACL — error path tells
  the user to re-run in their terminal.
- Codex rotates tokens on refresh; the server stays fresh within ~30 min of any
  local sync while the local machine is online (same availability contract as
  re-keying subscriptions).
- A 120 s CLI activation inside an HTTP `run` round-trip is long for proxied
  setups; accepted for v1, noted in docs.
- Hub trust surface grows: account tokens in hub RAM. Notices must say this
  plainly; `persistKey` for accounts is opt-in with the stronger warning.
