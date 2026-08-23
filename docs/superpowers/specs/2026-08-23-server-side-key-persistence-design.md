# Server-side API key persistence (opt-in, discouraged)

Date: 2026-08-23
Status: approved (revision incorporated: every default is OFF and the UX
actively discourages enabling; owner directive over the earlier
"hub default allow" choice)

## Problem

API keys live in server RAM only. A hub (or solo `awewarm serve`) restart
wipes them; each client re-pushes its keys on its next tick (throttled to
once per 30 minutes). If the owning machine is offline when the server
restarts, its connections' warm-ups are held and then skipped once past the
30-minute catch-up window. For users whose machines are rarely online, that
defeats the point of delegation.

## Goal / non-goals

Goal: a per-connection opt-in where the server persists the key on disk so
restarts never interrupt warm-ups.

Non-goals: encryption at rest (rejected below); changing the default trust
model for anyone who does not opt in; an operator switch on solo
`awewarm serve` (own box, own key — the per-connection flag is enough).

Related but separate: `awewarm config backup` / `config restore` (device
migration) was approved the same day as a bounded change and ships
alongside; it is not part of this spec.

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Storage | Plaintext `keys.json` inside the WarmServer workspace, 0600 | Same trust level as `tenants.json`, which already keeps invite codes and tenant tokens in the clear. Passphrase encryption breaks unattended restart (the feature's whole point); a local keybag beside the ciphertext is obfuscation, not security. Both rejected for violating honesty over complexity. |
| 0600 mechanics | Written via `_write_json` (mkstemp + os.replace) | mkstemp creates 0600 and rename preserves it — same construction secrets.json already relies on. A test pins it. |
| Who may enable | Per-connection user opt-in AND a hub operator global switch | The key's owner decides per connection; the disk's owner decides at all. |
| Defaults | OFF at every layer: client flag off, hub switch off | Owner directive 2026-08-23. The feature is a last resort, not a recommendation; the previous "hub default allow" choice was overruled. |
| UX stance | Actively discourage | Client turns it on only behind a confirmation gate (default No) that states the consequence; hub `--persist-keys on` prints a warning; docs frame it as "only if your machine is rarely online and you accept the key living on the server's disk". |
| Confirmation boundary | Gate every user action that changes persistence state — turning it on (default No) and turning it off (default Yes, but informed); never gate background maintenance | The confirm fires at the four on-entry points below plus `--persist-key off`, whose prompt states both consequences: the hub deletes the key from disk, and if the server restarts while this machine is offline the connection's warm-ups hold (then skip past the catch-up window) until the machine returns. The background sync / re-key / tick (up to every 30 minutes) maintains an already-confirmed choice and never prompts — a recurring prompt would train blind confirming. `remote connect` pushes no keys and does not prompt either. |
| Hub switch off | Purge every tenant's keys.json immediately | "No keys on my disk" must be true the moment it is said. Clients re-push keys to RAM on their next sync, so no warm-up is lost. |
| Revoke / delete invite / v1→v2 migration | Purge that tenant's keys.json; the workspace otherwise stays as today | A revoked tenant's keys must not linger on disk forever — a leak surface this feature creates, closed at every path that removes a tenant's authorization. |
| `/v1/keys` protocol | Unchanged | Persistence is server-side state declared at push time; re-key writes through for the already-persisted set. The flat `{id: key}` body must survive because old servers validate every top-level value as a string — any added top-level field would 400. |
| Wire additions | `PUT /v1/connections/<id>`: optional `persistKey` bool; `GET /v1/state`: per-connection `keyPersisted` bool | Optional fields only: old servers ignore `persistKey` (dict `.get`), old clients ignore `keyPersisted`. |

## Protocol

- `PUT /v1/connections/<id>` body gains optional `persistKey: true|false`
  (absent = false). Sent by delegation pushes, edit re-pushes, and sync
  re-pushes alike — always derived from the local connection flag, so the
  server's copy can never drift from the client's intent.
- `PUT /v1/keys` unchanged (`{connectionId: apiKey}`). The server updates
  `keys.json` for exactly those connections already in its persisted set —
  key rotation included.
- `GET /v1/state` per-connection entries gain `keyPersisted: bool`, so a new
  client can display the truth (and honestly show RAM-only when an old
  server ignored the request).

## Server (awewarm: WarmServer)

- `keys.json` in the workspace dir (solo serve: `~/.awewarm-server/keys.json`;
  hub: `tenants/<id>/keys.json` — automatic, WarmServer is per-workspace).
- `put_connection`: `persistKey` true → write key to keys.json; false or
  absent while previously persisted → remove from it. Re-push of a changed
  key overwrites (write-through).
- `put_keys`: update keys.json entries for the persisted set only.
- `delete_connection` (takeback): purge from keys.json.
- `WarmServer.__init__`: load keys.json into `self.keys` — a restart starts
  with every persisted key already usable. This is the feature.
- Log lines on persist / remove / purge name the connection, never the key.

## Client (awewarm)

- `conn["persistKey"] = true`, set via `awewarm config set <id>
  --persist-key on|off` (subscription connections only; a local connection
  errors with a hint).
- **Confirmation gates** — every user action that would start persistence
  asks first (`click.confirm`, default No; non-interactive shells must pass
  the same command's `--yes` or it dies with the same text). The prompt
  states: the key will live in plaintext on the server's disk (0600,
  readable by the operator and anyone with disk access) — only accept if
  the machine is rarely online. Four entry points:
  1. `config set <id> --persist-key on` — decline leaves the flag off.
  2. `config set <id> --remote` on a connection that already carries
     `persistKey: true` — decline still delegates, but downgrades that
     connection to RAM-only (flag cleared, push omits `persistKey`) and
     says so. Implemented as one gate inside `_delegate_remote`, so
  3. `config set <id> --duplicate --remote` (the copy inherits the flag)
     gets the same prompt and decline behavior for the copy.
  4. `config restore` when the archive's config.json contains
     persistKey-on connections — the prompt lists them by id; decline
     aborts the restore entirely (a migration moment, not a silent
     downgrade); re-run with `--yes` to accept.
  Turning **off** (`config set <id> --persist-key off`) confirms too —
  default Yes (off is the recommended state; friction points the other
  way), and the prompt states both consequences: the server deletes the
  key from its disk immediately (the key itself stays in local
  secrets.json and server RAM), and from then on a server restart while
  this machine is offline holds the connection's warm-ups — skipped past
  the 30-minute catch-up window — until the machine comes back and
  re-pushes. Decline is a no-op (flag stays on). Non-interactive shells
  pass `--yes` as with the on-gates.
- The background sync / re-key / tick never prompts: it maintains choices
  already confirmed at one of the four gates above.
- Both toggles trigger an immediate forced re-push (existing edit-push
  path); delegation (`--remote`) carries the flag at handover.
- `awewarm status`: connection line shows `key: server (persisted)` or
  `key: server (RAM)` from `keyPersisted`; when the field is absent (old
  server) it shows RAM — which is the truth.

## Hub (awewarm-hub)

- Serve record gains `persistKeys` (default false — absent means off).
  `awewarm-hub config --persist-keys on|off`; `on` prints a warning: tenant
  keys will land in plaintext on this box, and turning the switch off purges
  them again. Live adoption via `_refresh`, same as the caps.
- Gate in the `_put_connection` seam: `persistKey` requested while off →
  403 with actionable text ("the operator can allow it: awewarm-hub config
  --persist-keys on — or keep the key RAM-only by turning the flag off:
  awewarm config set <id> --persist-key off").
- Switch → off purges every tenant's keys.json and logs it.
- `revoke`, `revoke --delete`, and the v1→v2 registry migration purge the
  affected tenant's keys.json (workspace otherwise kept, as today).
- `awewarm-hub config` display gains a `persist keys: on/off` line
  (`the default` marker when unset).

## Compatibility

| | old server | new server |
|---|---|---|
| old client | unchanged | unchanged (no new fields sent) |
| new client | `persistKey` ignored; status honestly shows RAM | full behavior |

awewarm-hub pins awewarm's minor version; the WarmServer surface here is
additive (no renames), so the pin holds.

## Documentation

- awewarm README (Remote Server section; user-facing key promises): default
  RAM-only; opt-in persistence stores the key in plaintext on the server's
  disk (0600); not recommended unless the machine is rarely online.
- awewarm-hub README / README_cn / README.ai: same user-facing text, plus
  the operator switch (default off), purge-on-off and purge-on-revoke
  semantics, and the updated "never touch disk" promise wording.
- CHANGELOG entries in both repos.

## Testing

- WarmServer: persist on push (file exists, mode 0600); survives restart
  (fresh WarmServer over the dir has the key); flag-off push removes;
  takeback purges; re-key write-through updates the persisted copy only.
- Client: push bodies carry `persistKey`; confirm gates at all four entry
  points (toggle on, delegate a flagged connection, duplicate --remote,
  restore with flagged connections) with their decline behaviors
  (no-op / RAM-only downgrade / RAM-only downgrade on the copy / abort);
  the off-gate (default Yes, states hub-side deletion and the
  restart-while-offline hold risk, decline no-op); `--yes` non-tty escape;
  off-toggle removes server-side; status labels; background sync never
  prompts.
- Hub: default off → 403 with guidance; `config --persist-keys on` → push
  persists; switch off purges all tenants; revoke / delete purge the
  tenant's keys.json; live adoption while serve runs.
- Backup/restore (separate bounded feature): round-trip into redirected
  dirs, overwrite refusal without `--force`, archive path-traversal
  rejection, 0600 archive perms.
