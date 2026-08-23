# Changelog

## Unreleased

`--help` command listings now wrap long one-liners instead of truncating them with `...`, so a narrow terminal still reads every description in full (Click truncates by default; the new `awewarm.clickext.WrapGroup` hands the formatter the whole first help paragraph, which it wraps). Wording caught up with the hub split while at it: the `remote` group and `remote connect` name both servers (`awewarm serve` for your own box, `awewarm-hub serve` for a shared hub), `connect --invite` points at `awewarm-hub serve` instead of the removed `serve --hub` spelling, and the config validation error for `remote.url` accepts either server's name.

## v0.5.6

Every settings layer (global / `connections.local|remote` / a connection's own `settings`) now carries the full knob set — `windowMinutes`, `prompt`, and `maxTokens` joined the catch-up/degrade knobs — and each block's fields are split by semantics: the `schedule` block answers when a connection fires (`mode`, `times`, `days`, `skipIfActivatedMinutes`, `windowMinutes`, `graceSeconds`, `jitterSeconds`); the knobs answer how an activation behaves (`catchupMinutes`/`catchupAttempts`/`degradeAfterNodes`, `wakeWhenAsleep`, `prompt`, `maxTokens`). Two fields swapped homes for that split: `windowMinutes` moved from the knobs into the schedule block (it is the interval renewal clock), and `wakeWhenAsleep` moved from the schedule block out to the knobs (it is a machine behavior switch). `config settings` grew `--window-minutes`, `--prompt`, and `--max-tokens`; `config set <id> --window <minutes>` keeps its spelling but now writes the connection's own `settings.schedule.windowMinutes` instead of a top-level field. A layer's `windowMinutes` is the user vouching for that duration for every connection under it without its own record — it unlocks interval renewal exactly like a per-connection record, takes effect only while the resolved schedule mode is interval (fixed connections merely record it), and reaches delegated connections too: it is a fact about the plan, not about any machine's day, so it is the one schedule field exempt from the "delegated connections never follow the global schedule" rule. CLI accounts keep their builtin provider windows (Claude Code's verified 5 h), which a layer never overrides. `prompt`/`maxTokens`/`wakeWhenAsleep` materialize their code defaults into every saved global block, and the config template enumerates every knob and schedule field with its default value — the file documents what is configurable.

The on-disk format stays v3: older spellings — a top-level `windowMinutes` on a connection, a knob-position `windowMinutes`, a schedule-position `wakeWhenAsleep`, a per-connection `location` field, and a connection's overrides wrapped in a `settings` block — fold into the current positions on load and are never written again, so files migrate themselves on their next save. A connection's location is now carried by its group alone (`connections.local` / `connections.remote`); a `location` field that contradicts its group is a hand edit and refuses to load with a pointer at the stray field. A connection's own overrides sit directly on it — `schedule` plus any knob, no `settings` wrapper: the layers need the wrapper to share a dict with the connection ids, a connection does not. Mode is always visible: every saved global block names its schedule's `mode`, and every saved connection carries `mode` in its own `schedule` even when it matches the layers — the file shows fixed vs interval per connection without running `status`. That visibility is also a semantic choice: a layer's mode change never re-modes an existing connection (its pinned mode stands; switch explicitly), and an inherited interval whose window is unverified pins the fixed the connection actually runs on instead of the load-time fallback. Runtime behavior for existing configs is otherwise unchanged (window, schedule, and catch-up values round-trip identically; the resolved runtime shape the wake layer, status, and delegation pushes read is byte-identical).

## v0.5.5

**Breaking:** hub mode — one server warming connections for many invited users — moved out of this package into **awewarm-hub**, a separate open-source package (same MPL-2.0, at [wehuman01/awewarm-hub](https://github.com/wehuman01/awewarm-hub)) on PyPI. The operator installs it on the box (`pip install awewarm-hub`, then `awewarm-hub serve` / `invite` / `list` / `revoke` / `restore` / `config`); an existing `~/.awewarm-server` data dir carries over unchanged. Everything hub *users* need stays here and works exactly as before: pairing (`awewarm remote connect <url> --invite awi_...`) and delegated-connection sync. Solo `awewarm serve` is untouched, and `server._Handler` grew overridable seams — the semi-public extension surface awewarm-hub builds on. The old spellings now die with a tombstone naming their replacement: `awewarm serve --hub` → `awewarm-hub serve`, `awewarm hub ...` → `awewarm-hub ...`; `awewarm serve` also no longer reads a data dir persisted by `hub config --data-dir` (the mechanism moved with the hub commands — pass `--data-dir` instead). Hub-side changes since v0.5.0 (per-token machine pairing with `--max-machines` and earlier hub entries below) never shipped in a release of this package; they ship in awewarm-hub, whose changelog carries them from here on.

`scheduler install` now asks before installing on a machine with nothing to schedule locally — every connection delegated, or none configured yet. The server's `serve` ticks delegated connections itself, so a local scheduler there is dead weight (typically hit on the hub box, where installing awewarm-hub also brings this CLI). The question comes before `--wake`'s sudo prompt; scripted (non-tty) installs proceed with a notice.

The two packages cross-reference each other in docs and metadata — this README's Companion Tools, README.ai.md's Next Steps, the packaged skill, and `pyproject.toml`'s URLs point to awewarm-hub, whose README/README_cn/README.ai/CONTRIBUTING/skill suite mirrors this one and points back — and their version numbers run in lockstep from here on: both ship v0.5.5, with the hub's engine pin at `awewarm>=0.5.5,<0.6`.

## v0.5.0

`awewarm status` gains `--remote` / `--local` filters: the merged view keeps showing everything by default, `--remote` narrows it to delegated connections and leads with the server health line (version, uptime, last tick) that used to live in `remote status`, `--local` shows only locally scheduled connections. A filter that names a connection of the other kind dies with the matching `config set <id> --remote/--local` fix instead of rendering it; `--remote` with no server paired or nothing delegated prints a friendly pointer instead of failing. `awewarm remote status` is now a hidden alias for `status --remote` (migration note, removed in v1.0), same treatment as the pre-0.3 names.

`awewarm hub status` gives the operator a one-glance dashboard: active/suspended tenants against the max, delegated connections against the per-tenant cap, invite counts by fate, the data dir, and a best-effort liveness probe of the local `serve` (its recorded endpoint, short timeout, "NOT reachable" is information, not an error). `--details` appends every delegated connection across tenants with its mode, next due moment, and timezone; without it a hint points there. The numbers come from a `serve` record `tenants.json` now carries: at launch the server stamps its effective caps (max-tenants, max-conns-per-tenant), bind address, port, version, and start time; one-shot hub CLI processes adopt those caps when they construct a Hub, and a data dir whose serve never launched says "caps unknown" instead of guessing.

Local commands now share one cross-process transaction lock, preventing the local scheduler tick and an interactive edit/run from overwriting each other's `state.json` updates. A busy background tick exits for the next minute to catch up; an interactive command waits up to five seconds and then reports the conflict. `awewarm serve` keeps its separate server data directory and coordination model.

`secrets.json` writes now use the same atomic replace path as config and state. Malformed, unreadable, or non-object secret files are refused with a repair hint instead of being treated as empty and overwritten.

HTTP integration tests now close their listening sockets and join server threads, removing Python 3.13 resource warnings and cutting their shutdown time. The old `awewarm update` alias is removed outright; use `awewarm self-update`.

Hub registry mutations now hold a cross-process transaction lock shared by the resident server and operator CLI. A stale usage/last-seen write can no longer overwrite a concurrent revoke and revive the tenant's token.

Hub revocation is now suspension, not deletion, and works from either handle. `awewarm hub revoke` accepts a tenant (`t_...`) or an invite code (`awi_...`): a pending code stops pairing on the spot, a used one suspends the tenant it produced — its token stops authenticating and its connections stop ticking, while everything stays on disk. The new `awewarm hub restore` undoes either. A suspended tenant frees its capacity slot; restoring re-takes one and refuses when the hub is full. `--max-tenants` defaults to 10 (was 50), counting active tenants only. `hub list users` marks suspended tenants and `hub list invites` shows revoked codes — a used code reads `suspended` while its tenant is suspended.

`awewarm hub list users` gains an INVITE column with the code each tenant joined with (masked by default, full text with `--reveal`, mirroring `hub list invites`) — no more cross-referencing USED BY to map a tenant back to its code.

`awewarm remote connect --invite` now prints the personal token once at join (it is auto-saved to `secrets.json`): the invite is spent, so a saved copy — reused with `remote connect <url> --token <token>` — is the only way back in without a fresh invite.

## v0.4.7

Orphan pmset wake events no longer accumulate. `sync_wake_events` now reads the live `pmset -g sched` output with creator attribution on every pass (not just when the ledger is stale), so `wakeorpoweron` events armed by the pmset command line that no ledger entry tracks are identified and cancelled. A failed cancel adopts the orphan into the ledger so the normal retry path owns it. `teardown_wake_layer` sweeps the same orphans at uninstall time — with the scheduler gone, nothing else would ever cancel them.

`config set <id> --duplicate` copies a connection under a fresh id (`<id>-copy`, then `<id>-copy2` if that exists). The API key is re-stored under the new id so removing either connection cannot clobber the other's secret. With `--remote` the copy is delegated and the original disabled — one subscription, one ticker. `--duplicate` rejects any other flag combination.

`hub list invites --token` is now `--reveal` (the old flag is rejected outright). `awewarm -v` on a source checkout now says `editable, git <describe>` instead of a bare number; `awewarm update` refuses on a checkout and tells you to `git pull && pip install -e .` instead of running pip.

## v0.4.6

`hub list` splits into `hub list users` (paired tenants) and `hub list invites` (every minted code with pending / used / expired status, codes masked by default; `--token` reveals them). Invite codes are kept on disk in the clear so the operator can recover one already sent (`hub list invites --token`) — anyone who can read the data dir can use a pending invite, so guard it accordingly. The server now reloads `tenants.json` when it changes underneath a long-lived `serve`, so invites minted or tenants revoked by separate CLI processes take effect without a restart, while in-memory usage / lastSeen for already-connected tenants survive the reload. `join` marks an invite as used (`usedBy` / `usedAt`) instead of deleting it, so the operator can see who joined with which code. `tenants.json` still stores only SHA-256 hashes of tenant tokens (pairings survive restarts), and API keys remain the only secret that never touches disk.

## v0.4.5

Config format v3: settings are layered three deep and the schedule moved into them. Every settings block — the top-level `settings` (global), `connections.local.settings` / `connections.remote.settings` (the connections layer), and each connection's own `settings` (the profile) — carries the same knobs plus a `schedule` block, and every field resolves own-overrides first, then the location's defaults, then the global block. One deliberate asymmetry: a delegated connection never follows the global schedule — it describes the delegating machine's day, so remote connections resolve their schedule from their own settings and `connections.remote.settings` only (knobs still inherit globally). An inherited interval mode never breaks a connection whose window is unverified (it stays fixed until the window is recorded); an explicit own override surfaces the usual gating error instead. Delegating freezes the then-effective schedule as the connection's own settings, so handover never changes what fires. `awewarm config settings [global|local|remote]` now sets every layer (`--times`, `--days`, `--mode`, `--wake/--no-wake`, the catch-up/degrade knobs, `--reset`); edits that reach delegated connections mark them for re-push. `awewarm config set <id>` gained `--inherit-schedule` to drop a connection's own schedule overrides and follow the layers again, and its `--times/--days/--mode/--wake` edits now land in the connection's own settings block. v1/v2 files upgrade in place on first load (v2 per-connection schedule fields become that connection's own overrides, values unchanged).

The server data dir no longer needs a flag: `awewarm serve` and every `awewarm hub` command default to `~/.awewarm-server`, and `awewarm hub config --data-dir <path>` persists a different default on the hub machine (the flag still overrides once; `--unset` clears it). `hub config` with no flags shows the resolved dir and where it comes from.

Remote clients identify themselves: `remote.py`'s HTTP layer now sends `User-Agent: awewarm/<version>` instead of urllib's default `Python-urllib/3.x`. Cloudflare Bot Fight Mode in front of a delegated server banned that default UA outright (`error code: 1010`, HTTP 403 on the very first `/healthz`), so `remote connect` died before pairing even though the server itself was healthy. A 403 whose body carries no awewarm error detail now adds a hint that a proxy/WAF in front of the server may be blocking the client, instead of a bare "HTTP 403".

`config set --start HH:MM` now works in fixed mode too, giving fixed schedules the one-time defer interval always had. The gate is the same `deferUntil` state: no request fires before the moment (today, or tomorrow if passed), and a held slot fires right after the gate lifts while still inside its catch-up window — `--start 16:05` turns today's 16:00 slot into 16:05 without touching the times list. A gate set past a slot's catch-up end skips that slot (same as a slept-through slot); the gate clears on the first success, and `status` shows the deferred moment as the next due (`fixed (deferred)`). Previously `--start` died with "--start only defers interval activation"; the flag's success message and help text now name both modes, and setting a gate refreshes the RTC wake layer's armed events.

Hub mode: one `awewarm serve` box can now warm connections for many users. `awewarm serve --hub` replaces the first-token claim with one-time invites — the operator mints them on the server (`awewarm hub invite --note alice`, one use, 48 h) and each user pairs with `awewarm remote connect <url> --invite awi_...`. Every tenant gets a private workspace (their `glm` and yours never collide; connections, state, and RAM keys are invisible across tenants), and everything from single-user delegation works unchanged: edits push, `run` fires remotely, `--local` takes back, fixed times follow the user's own timezone. Administration lives on the hub machine: `hub list` renders a tenant table (worst health rung, connection count, activations today/total, last seen, paired date; the health word is the same ladder `status` uses, now one shared helper) and `--api` appends one row per delegated connection with its endpoint, protocol, model, and next-due; `hub revoke` drops a tenant's token, connections, and state. Capacities are flags (`--max-tenants`, `--max-conns-per-tenant`) and a light per-tenant rate limit (60 requests/minute) stops a looping client. Two rules differ from single-user mode, both deliberate: `tenants.json` stores only SHA-256 hashes of tenant tokens so hub pairings survive a restart without waiting for every user to re-claim (plaintext secrets still never touch disk; RAM API keys are lost and re-pushed as always), and `remote disconnect` does not free a hub slot — the kept token re-pairs on reconnect, and freeing capacity is the operator's `hub revoke`. The README's hub section also states the trust rule plainly: the hub fires requests with its users' API keys, so plaintext keys pass through its RAM — hub for people who trust the machine's operator, not strangers on a shared VPS. Single-user `serve` keeps its exact previous behavior.

## v0.4.3

Sleeping Macs now really wake. The previous calendar layer only fires the tick at the exact slot time while the machine is awake — launchd does not wake a sleeping Mac, so a lid-closed machine fired late (minutes, thanks to maintenance dark wakes, but late). `awewarm scheduler install --wake` adds an RTC wake layer on top: `pmset schedule wakeorpoweron` one-shot events for every moment the schedules need the machine — all fixed slots for today and tomorrow plus each interval connection's next renewal, including drifted ones. The tail of every tick recomputes the needed moments and converges the armed events to them (at most 16 within a 2-day horizon), reconciling its ledger against `pmset -g sched` so fired events drop out and lost ones re-track instead of double-arming. Arming needs root, so the install writes a one-line sudoers grant (`/etc/sudoers.d/awewarm`, scoped to scheduling/cancelling wake events only, visudo-validated before it lands, 0440 root:wheel); the unattended tick then arms events password-free. The machine wakes into a screen-off dark wake, ticks within seconds, and sleeps again — no prevent-sleep assertion anywhere. Battery standby (RAM powered off) still cannot be woken on schedule; that regime belongs to `awewarm serve`. `status` shows the layer (`Wake layer: enabled — N RTC wake(s) armed`), `scheduler uninstall` cancels every armed event and removes the grant, and `--no-wake` per connection opts out of both layers.

Windows interval renewals get the same coverage without any grant: one-shot `-Once` WakeToRun tasks, named after their moment and converged by the same tick tail (fixed slots keep their static daily wake tasks). The README's sleep sections were rewritten around the two-layer split — the old text claimed calendar entries wake a sleeping Mac outright.

Documentation aligned with the code: the missed-slot catch-up window is 30 minutes, not the 45 the docs had carried since the default changed in v0.1-era development (English, Chinese, and the packaged agent skill all said 45; the ladder section already said 30). README.ai.md described bare `awewarm run` as the scheduler tick — it is the user-facing fire-now verb that prompts (the scheduler calls the hidden `awewarm tick`) — and knew nothing of `--wake`; both fixed. The README command list now includes `config settings` and the full `config set` flag set, and the agent skill documents the wake layer with its intent-router entry.

## v0.4.2

`config set` gains `--hide/--show`: a hidden connection disappears from `awewarm status` listings (text and `--json`) while its schedule keeps firing — hiding is display-only, unlike `--off` which stops the warm-ups. Asking for it by id (`status <id>`) still shows it, and `config set <id>` with no flags prints its hidden state.

Remote delegation fixes, pairing and firing. `remote disconnect` now releases the server's claim through a new `/v1/release` endpoint and keeps the pairing token in `secrets.json` — previously it deleted the token locally while the server kept the claim in RAM, so a later `remote connect` died on 403 "already claimed by another token" until the server process was restarted. The token also moved to a namespaced `remote:token` secrets key (old `remote-token` entries migrate on first read), so a connection literally named `remote-token` can no longer clobber it. And a 200 answer that is not awewarm's JSON — a tunnel pointed at the wrong origin — fails as a clear RemoteError instead of a raw `JSONDecodeError` traceback.

Delegated runs honor the ladder contract and outwait the server: `run <id>` on a delegated connection now fires and, on success, clears an auto-disabled ladder — same as the local CLI has always documented — while bulk `run` keeps skipping those connections; the run call also waits 30 s instead of 5, because the server sends the real request before answering (its own per-activation cap is 15 s), so slow endpoints used to read as "unreachable" here while the request had actually gone out, inviting a duplicate retry. Delegation from machines whose local zone has no IANA name (Windows) now pushes the UTC offset (`UTC+08:00`) instead of silently UTC, keeping fixed slots on the right wall clock. `status` also warns when a delegated connection is missing from a reachable server's view instead of silently rendering the stale local schedule.

## v0.4.1

`remote connect` now asks for confirmation before pairing over plain `http://` with a non-local host — the pairing token and any delegated API keys would otherwise cross the network unencrypted. Loopback, private-range, and `.local` addresses do not prompt; https never does. The README's remote-server section also documents the claim model explicitly: an unclaimed server trusts the first token that reaches it, so keep the URL private or pin one ahead of time with `serve --token`.

The delegation server caps each activation request at 15 seconds (the local CLI keeps its 60). The server's tick and `run` hold its lock while sending, so a dead endpoint could previously queue every API call behind retries for minutes; a short per-request timeout bounds that. Delegated connections are always HTTP subscriptions, so the tighter cap costs nothing in practice.

Hardening and internals: `secrets.json` is reset to mode 600 on every write (a hand-relaxed mode no longer persists), the "API key unavailable" failure message no longer mentions the removed env-var refs, the token generator's dead duplicate in `server.py` is gone, and log append/rotation is one shared helper instead of two copies. The two tick engines now share one action dispatcher (`schedule.dispatch_actions`), so skip bookkeeping, node closure, and pruning stay identical by construction. `cli.py` splits its interactive setup wizards into `flows.py` and status rendering into `status.py` (1379 lines, down from 1927), and `config set`'s internal flag passing moved from fourteen positional parameters to a guarded options object — a typo'd field name now raises instead of silently misbinding.

Account (CLI) connections: codex warm-ups now run with `--skip-git-repo-check` — `codex exec` refuses to run outside a git repository, and every scheduler tick is outside one (launchd starts at `/`, systemd at the home directory, Task Scheduler at System32), so scheduled codex activations always failed even when the add-time test, run from a git checkout, passed. CLI activations also detach stdin now: both CLIs append piped stdin to the prompt, so an open pipe could hold a headless tick until its timeout. And a Claude Code account's builtin verified window survives a config save/load round trip instead of being downgraded to user-confirmed, keeping `status` output and the `--window` override warning consistent across restarts.

## v0.4.0

The unreleased entry adds remote delegation: an always-on `awewarm serve` process can tick subscription connections for a machine that sleeps, pairing over a cloudflared tunnel and keeping every secret on the local machine.

### Remote delegation: `serve`, `remote`, `--remote`

Any 24/7 box (VPS, NAS, Raspberry Pi) runs `awewarm serve` — one resident process that serves an authenticated sync API and ticks delegated connections once a minute with the same pure planner and transports as the local CLI. The local machine pairs with `awewarm remote connect <url>` (a token is generated locally, stored in `secrets.json`, and used to claim the server; `serve --token` fixes one ahead of time instead) and hands connections over with `awewarm config set <id> --remote`; `--local` takes them back, pulling server state first so local scheduling resumes where the server left off. Only subscription connections can be delegated — CLI-account logins physically cannot move. The flag only lands after the server accepted the push, so a connection is never left with nobody ticking it; the local tick and wake entries skip delegated connections from then on.

### The server holds no secrets on disk

The pairing token and delegated API keys stay in the local `secrets.json` and are pushed over the wire; the server keeps them in RAM only. Restarting it therefore loses them by design: the local side re-claims and re-pushes automatically whenever it is online (interactive commands immediately, the local scheduler tick at most twice an hour). A slot that came due while its key was missing is *held*, not failed — no health-ladder impact — and still fires inside its catch-up window once the key returns, exactly like a machine that was asleep; past the window it is recorded as skipped. `awewarm remote push` reconciles manually; `config set` pushes schedule edits to delegated connections automatically and, when the server is unreachable, keeps them local under a visible pending marker.

### Status shows one merged truth

`awewarm status` fetches the server's view for delegated connections and renders them from server state (config + ladder + next due), caching the last successful sync so an offline server degrades to a labeled stale view instead of a blank one. `awewarm run <id>` on a delegated connection fires on the server and reports back; `awewarm remote status` shows the server's own uptime, last tick, and per-connection state; `awewarm remote disconnect` refuses while connections are still delegated, so nothing keeps ticking unmanaged. Fixed times run in the delegating machine's timezone — the IANA name travels with the push.

## v0.3.7

`v0.3.7` replaces the attempt-count failure pause with a node-based health ladder shared by both modes (`connected → failing → degraded → auto-disabled`), unifies and makes catch-up configurable, shows the last activation failure in `status`, asks for the window duration in fixed-mode plan setup so the full-day grid is offered there too, brings Windows wake-from-sleep to parity with macOS and prompts for it at setup, and moves tuning knobs to a layered top-level `settings` block.

### Failure handling rebuilt as a health ladder

Both modes now share one ladder. A failed node — a fixed slot or an interval renewal moment — enters `failing`: catch-up retries at most `--catchup-attempts` (default 5) within `--catchup-minutes` (default 30), spaced by the 5-minute throttle; any success returns to `connected`. After `--degrade-after-nodes` (default 3) consecutive lost nodes the connection is `degraded` — single shot per node, no catch-up: interval probes once per window, fixed fires each slot exactly once. The same count again while degraded goes `auto-disabled`: fully silent until `--on` (or a successful manual `run <id>`), both of which reset the ladder but keep schedule memory. Any success resets everything; manual and verify attempts never count as nodes; a slot the machine slept through (zero attempts) is a skip, not a lost node. This replaces the old behavior where three failed *attempts* (about 10 minutes) auto-paused interval only, fixed never paused but showed nothing, and a fixed connection that later switched to interval could inherit a phantom degraded state. `status` prints the rung with details (`Health: failing — 1/3 nodes lost, catch-up attempt 2/5`), and old `intervalDisabledAt`/`consecutiveFailures` state migrates on first read. The fixed-mode catch-up default changes from 45 to 30 minutes; configs that recorded an explicit `catchupMinutes` keep their value.

### Tuning knobs live in a layered `settings` block

config.json gains a top-level `settings` object holding the catch-up/degrade knobs (`catchupMinutes`, `catchupAttempts`, `degradeAfterNodes`), always written with its effective values so the file documents the defaults at a glance. `awewarm config settings` shows or changes them. A connection can override any knob in its own `settings` — set with the existing `config set <id> --catchup-*` / `--degrade-after-nodes` flags — and anything it leaves out falls back to the top level, the same layering the schedule fields use. Overrides persist only while they differ from the global block, so retuning a global value absorbs a matching override. Knob keys written flat on a connection by earlier v0.3.6 builds migrate into that connection's `settings` on first load.

### Setup asks for the window in fixed mode

Adding a plan and choosing fixed mode now asks for the window duration first (default 300 — most coding plans use 5-hour windows). The answer drives the full-day grid spacing, gets the grid offered right away instead of a single time, and is recorded as a user-confirmed window that unlocks interval mode. Local accounts keep using their builtin window knowledge — the question only appears where nothing else knows the duration.

### Status shows the last failure

`status` used to print only the last successful activation, so a connection failing every retry still read as healthy. When the most recent attempt failed, the block now adds `Last result: failure (<time>) — <error>` right under `Last activation` — in both the summary and the detailed view.

### Wake-from-sleep: Windows parity, prompted at setup

`wakeWhenAsleep` now does something on Windows too. `scheduler install` registers one extra Task Scheduler task per fixed slot — a daily trigger at the slot time with *Wake to run* enabled, running `awewarm tick` — the same shape as the macOS launchd calendar entries (`schtasks.exe` cannot set the flag, so registration goes through PowerShell's `Register-ScheduledTask`). The per-minute tick itself never wakes the machine; only slot times do. Uninstall removes the tasks, config edits refresh them, and the tick's self-heal repairs drift, all mirroring the launchd lifecycle.

The add flows now ask whether fixed slots may wake a sleeping machine on macOS and Windows (default yes); `config set <id> --wake/--no-wake` changes it later, `config set` with no flags shows it, and a wake-affecting edit refreshes the installed entries/tasks immediately. Linux cannot wake a suspended machine at all: the setup flow never asks, new connections record `wakeWhenAsleep: false`, `--wake` there prints a no-effect note, and `scheduler install` says missed slots catch up after the next wake.

## v0.3.6

`v0.3.6` removes the grid generator's 8-slot cap so short-window fixed grids span the full day, retunes `status` to show the active schedule line, hardens the tick's self-heal so a failed heal can no longer abort the whole tick, adds `config set --start HH:MM` to defer interval activation, and switches `__version__` to dynamic versioning via setuptools.

### Full-day slot grid cap removed

Fixed after the v0.3.5 release: the grid generator capped out at 8 slots, so windows under ~3 h were silently cut off mid-day (a 120-min plan got 16.6 h of coverage, not 24); the cap is gone and short-window grids now span the full day.

### Status shows the active schedule

`awewarm status` now prints the schedule line that actually drives the connection. Fixed mode shows `Times: 06:19, 11:24, 16:29, 21:34 (every-day)` — the window said nothing about when fixed mode fires. Interval mode keeps `Window: 300 minutes, user-confirmed`, since the window is its renewal clock. The detailed view (`status <id>`) still shows the other one, with evidence. Disabled connections print `Next due: none (disabled)` instead of a moment the tick would never fire.

### Tick self-heal can no longer abort the tick

The tick's opening self-heal pass (rewrites a stale scheduler job) called paths that `die()` on failure — e.g. `awewarm` missing from launchd's sparse PATH, or a failed `launchctl bootstrap`. `die()` raises `SystemExit`, which the pass's error filter didn't catch, so a failed heal aborted the whole tick and skipped that minute's due activations. `SystemExit` is now caught alongside the I/O errors, restoring the intended behavior: the old job keeps running, the tick proceeds, and the next tick retries the heal.

### Interval start gate (`--start`)

`config set <id> --start HH:MM` defers interval activation: no request fires before that moment — not the first anchor of a fresh connection, not a renewal whose due has passed, and not a stale `nextDueAt` left over from mode switches. The time resolves to the next occurrence (today when still ahead, otherwise tomorrow), the first tick past it opens the chain, and the gate clears on the first success. `--anchor` clears it too, since anchoring seeds the whole chain explicitly. `--start` requires interval mode (the effective mode after any `--mode` flag in the same call), matching `--anchor`'s strictness; `status` shows the deferred moment as the next due.

### Dynamic versioning

`pyproject.toml` now uses `setuptools` dynamic versioning (`version = {attr = "awewarm.__version__"}`) instead of a static `version` string, and `__version__` is exposed from `awewarm.__init__`.

## v0.3.5

`v0.3.5` removes the legacy `hybrid` scheduling mode, adds a full-day fixed-slot grid at setup, and removes the macOS pmset wake fallback in favor of pure launchd calendar entries.

### hybrid mode removed — fixed and interval only

Three scheduling modes collapsed to two. The combination mode was the source of the subtlest failures: two engines shared one anchor, a fixed slot landing inside a still-open interval window wasted a request and polluted the renewal chain, and status displayed a "next due" neither engine owned. A `fixed` grid spaced one window apart (see the setup grid below) already chains windows across the whole day and adds calendar wake coverage that interval cannot have; `interval` remains the always-on-machine choice. Existing `hybrid` configs (v1 and v2) migrate to `fixed` on first load — times and days are preserved. `config set --mode` now accepts only `fixed|interval`; anchoring (`--anchor`) is interval-only; the account/plan setup menus offer two choices; calendar wake entries are written for fixed connections only.

### Full-day slot grid offered at setup

`config add` / `init` now know that one fixed time rarely covers a day. When the window duration is known (verified built-in accounts, or a plan whose window you just recorded), the fixed-times prompt asks for the plan's daily quota reset time and offers a full-day grid — one slot per window, spaced window + 5 min apart, anchored on the reset time so drift stays minimal (e.g. reset 01:14 + 300-min window → 01:14, 06:19, 11:24, 16:29, 21:34). Accepting the grid defaults days to every-day; declining keeps the single entered time with the usual weekday default. Windows under 2 h offer no grid (interval mode fits those better).

### pmset wake removal (macOS)

The launchd `StartCalendarInterval` entries cover every fixed slot at its exact time with no sudo; the pmset `wakeorpoweron` fallback covered only the earliest slot, needed sudo, and duplicated schedule state. It is removed. `awewarm update`, `scheduler install`, and `scheduler uninstall` cancel a pmset repeat left behind by earlier versions — only if it is still the one awewarm set, and a failed cancel (no sudo password) is retried by the next of those commands. If the state record was already lost, cancel manually: `sudo pmset repeat cancel` (safe when awewarm's is the only repeating event). Calendar entries now fire every day regardless of the slot's day rule; the tick applies the day rule, so a weekend wake for a weekday-only slot is a no-op. `schedule.wakeLeadMinutes` and `scheduler install --wake/--no-wake` are gone. A fully shut-down Mac no longer auto-boots; the first tick after power-on still catches up slots inside the catch-up window.

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
