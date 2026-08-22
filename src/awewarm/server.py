"""awewarm serve: the always-on half of remote delegation.

One resident process that (a) serves an authenticated sync API to the local
machine and (b) ticks the delegated connections once a minute with the same
pure planner (`schedule.plan_actions`) and transports the local CLI uses.

Nothing secret is ever written to disk here. The access token and connection
API keys live in RAM only, pushed by the local machine, which owns them in its
own secrets.json. A restart therefore loses them: the local side re-claims and
re-pushes automatically whenever it is online, and any slot that came due
while its key was missing is held (not failed) — it still fires inside the
catch-up window once the key returns; past it, it is recorded as skipped.
That is exactly how the local tick already treats a machine that was asleep.

`--hub` swaps the single-pairing model for many users behind one process:
each tenant gets a private WarmServer workspace and pairs through a one-time
invite (`awewarm hub invite`) exchanged at /v1/join for a personal token.
tenants.json stores SHA-256 hashes of tenant tokens plus the invite codes
themselves — the operator can re-read a sent code (`awewarm hub list
invites --reveal`) — and a serve record with the launch's caps and endpoint
for `awewarm hub status`; hub pairings survive a restart without every user
re-claiming; API keys remain the only secret that never touches disk.

Wire protocol (JSON over HTTP, Bearer token):
  GET    /healthz                    no auth; {ok, version, claimed[, hub]}
  POST   /v1/claim                   {token} → claim an unclaimed server
  POST   /v1/join                    hub only; {invite} → {token, tenantId}
  POST   /v1/release                 give up the claim (authed; disconnect)
  PUT    /v1/connections/<id>        {connection, apiKey, timezone} → take over
  DELETE /v1/connections/<id>        drop a connection (takeback)
  PUT    /v1/keys                    {id: key, ...} → re-key after a restart
  GET    /v1/state                   server truth for `awewarm status`
  POST   /v1/connections/<id>/run    fire one now (manual semantics)
"""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.parse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, schedule, transport
from .config import append_log, conn_state, connection_errors, default_conn_state, timezone_for, _write_json
from .locking import LockBusy, process_lock

TOKEN_RE = re.compile(r"^awt_[A-Za-z0-9_-]{20,128}$")
INVITE_RE = re.compile(r"^awi_[A-Za-z0-9_-]{16,128}$")
BODY_LIMIT_BYTES = 256 * 1024
# The tick and run_now hold the server lock while sending, so one activation
# must not stall every API call behind it: cap HTTP far below the 60 s the
# local CLI allows (delegated connections are always HTTP subscriptions).
ACTIVATION_TIMEOUT_SECONDS = 15

# Hub-mode knobs (`awewarm serve --hub`); the two caps are also serve flags,
# stamped into the registry at launch so `hub status` can report them.
DEFAULT_MAX_TENANTS = 10
DEFAULT_MAX_CONNS_PER_TENANT = 5
INVITE_TTL_HOURS = 48
# Generous for honest clients (status + sync make a handful of calls an hour)
# while still stopping a looping client from monopolizing the process.
HUB_RATE_PER_MINUTE = 60
# Persisting lastSeen on every request would rewrite tenants.json constantly;
# refreshing it at most this often keeps `hub list users` honest to a small window.
HUB_SEEN_PRECISION = timedelta(minutes=10)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class WarmServer:
    """All server state: config + state on disk, token + keys in RAM."""

    def __init__(self, data_dir, fixed_token=None):
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "config.json"
        self.state_path = self.data_dir / "state.json"
        self.log_path = self.data_dir / "awewarm-server.log"
        self.lock = threading.RLock()
        self.fixed_token = fixed_token
        self.claimed_token = fixed_token  # RAM only; lost on restart (by design)
        self.keys = {}  # conn_id → API key, RAM only
        self.config = self._load(self.config_path, {"version": 2, "connections": {}})
        self.state = self._load(self.state_path, {"version": 1, "connections": {}})
        self.started_at = datetime.now().astimezone()
        self.last_tick_at = None

    # --- storage (config/state shapes match the local files; keys never land here) ---

    def _load(self, path, default):
        try:
            return json.loads(Path(path).read_text())
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"awewarm serve: cannot read {path}\n{exc}\n"
                "fix: delete the file — the local machine re-pushes everything"
            )

    def _save(self, path, data):
        _write_json(path, data)

    def log(self, message):
        append_log(self.log_path, message)

    # --- auth ---

    @property
    def claimed(self):
        return self.claimed_token is not None

    def check_token(self, candidate):
        return self.claimed and bool(candidate) and hmac.compare_digest(candidate, self.claimed_token)

    def claim(self, token):
        with self.lock:
            if self.fixed_token is not None:
                if hmac.compare_digest(token or "", self.fixed_token):
                    return {"ok": True, "claimed": True}
                raise ApiError(403, "this server was started with a fixed token — it does not match")
            if self.claimed_token is None:
                if not TOKEN_RE.match(token or ""):
                    raise ApiError(400, "token must look like awt_<random> — generate one with: awewarm remote connect <url>")
                self.claimed_token = token
                self.log("server claimed")
                return {"ok": True, "claimed": True}
            if hmac.compare_digest(token or "", self.claimed_token):
                return {"ok": True, "claimed": True}  # re-claim after the local side restarted
            raise ApiError(
                403,
                "this server is already claimed by another token "
                "(disconnect from the machine that claimed it, or restart the server)",
            )

    def release(self):
        """Give up the claim so a different token can pair (authed by the old one)."""
        with self.lock:
            if self.fixed_token is not None:
                return {"ok": True, "released": False}  # claim pinned by --token
            if self.claimed_token is None:
                return {"ok": True, "released": False}
            self.claimed_token = None
            self.log("server released")
            return {"ok": True, "released": True}

    # --- connections ---

    def put_connection(self, conn_id, payload):
        if not conn_id or not isinstance(conn_id, str):
            raise ApiError(400, "connection id required")
        conn = payload.get("connection")
        api_key = payload.get("apiKey")
        tz_name = payload.get("timezone")
        if not isinstance(conn, dict):
            raise ApiError(400, "body must be {{connection, apiKey, timezone}}")
        if conn.get("kind") != "subscription":
            raise ApiError(
                400,
                "only subscription (API-key) connections can be delegated — "
                "CLI accounts live on the machine they logged in on",
            )
        if not isinstance(api_key, str) or not api_key.strip():
            raise ApiError(400, "apiKey is required (the server fires requests with it)")
        if not isinstance(tz_name, str):
            raise ApiError(400, "timezone is required (an IANA name, e.g. Asia/Shanghai)")
        try:
            timezone_for(tz_name)
        except Exception:
            raise ApiError(400, f"unknown timezone: {tz_name}")
        with self.lock:
            conn = json.loads(json.dumps(conn))  # private copy, no shared mutable state
            conn.setdefault("auth", {})["apiKeyRef"] = None  # the key lives in RAM, not as a ref
            conn["timezone"] = tz_name
            conn["location"] = "remote"
            errors = connection_errors(conn, conn_id)
            if errors:
                raise ApiError(400, "; ".join(errors))
            replaced = conn_id in self.config["connections"]
            self.config["connections"][conn_id] = conn
            self.state["connections"][conn_id] = default_conn_state()
            self.keys[conn_id] = api_key
            self._save(self.config_path, self.config)
            self._save(self.state_path, self.state)
            due_at, _ = schedule.next_due(conn, self.state["connections"][conn_id], self._now(conn))
            self.log(f"{conn_id} pushed ({'replaced' if replaced else 'new'})")
            return {"ok": True, "replaced": replaced, "nextDue": schedule.iso(due_at) if due_at else None}

    def delete_connection(self, conn_id):
        with self.lock:
            if conn_id not in self.config["connections"]:
                raise ApiError(404, f"no such connection: {conn_id}")
            del self.config["connections"][conn_id]
            self.state["connections"].pop(conn_id, None)
            self.keys.pop(conn_id, None)
            self._save(self.config_path, self.config)
            self._save(self.state_path, self.state)
            self.log(f"{conn_id} removed")
            return {"ok": True}

    def put_keys(self, mapping):
        if not isinstance(mapping, dict) or not mapping:
            raise ApiError(400, "body must be {connectionId: apiKey}")
        with self.lock:
            unknown = [cid for cid in mapping if cid not in self.config["connections"]]
            if unknown:
                raise ApiError(400, f"unknown connections: {', '.join(sorted(unknown))}")
            for conn_id, key in mapping.items():
                if not isinstance(key, str) or not key.strip():
                    raise ApiError(400, f"empty key for {conn_id}")
                self.keys[conn_id] = key
            self.log(f"keys restored: {', '.join(sorted(mapping))}")
            return {"ok": True, "missing": self.missing_keys()}

    def missing_keys(self):
        with self.lock:
            return sorted(
                cid for cid, conn in self.config["connections"].items()
                if conn.get("kind") == "subscription" and not self.keys.get(cid)
            )

    def view(self):
        """Server truth for status display — no secrets by construction."""
        with self.lock:
            missing = set(self.missing_keys())
            return {
                "version": __version__,
                "startedAt": schedule.iso(self.started_at),
                "lastTickAt": self.last_tick_at,
                "connections": {
                    cid: {
                        "config": conn,
                        "state": self.state["connections"].get(cid) or default_conn_state(),
                        "keyMissing": cid in missing,
                    }
                    for cid, conn in sorted(self.config["connections"].items())
                },
            }

    def run_now(self, conn_id, reset_due=False, allow_auto_disabled=False):
        with self.lock:
            conn = self.config["connections"].get(conn_id)
            if conn is None:
                raise ApiError(404, f"no such connection: {conn_id}")
            cs = conn_state(self.state, conn_id)
            if cs.get("autoDisabledAt") and not allow_auto_disabled:
                # Bulk `run` skips auto-disabled connections, same as the local
                # fire-all; an explicit `run <id>` passes allow_auto_disabled
                # and a success clears the ladder, same as the local single run.
                return {
                    "ok": False,
                    "detail": "auto-disabled after repeated failures (resume: awewarm config set --on, or fire one: awewarm run <id>)",
                }
            now = self._now(conn)
            key = self.keys.get(conn_id)
            if not key:
                return {"ok": False, "detail": "API key not pushed yet — run: awewarm remote push"}
            result = self._execute(conn, conn_id, cs, now, "manual", None, None, reset_due=reset_due)
            due_at, _ = schedule.next_due(conn, cs, now)
            result["nextDue"] = schedule.iso(due_at) if due_at else None
            self._save(self.state_path, self.state)
            return result

    # --- the tick ---

    def _now(self, conn):
        name = conn.get("timezone")
        if name:
            try:
                return datetime.now(timezone_for(name))
            except Exception:
                pass
        return datetime.now().astimezone()

    def _execute(self, conn, conn_id, cs, now, kind, slot, node, reset_due=True):
        schedule.record_attempt(cs, now)
        result = transport.send_activation(
            conn, self.keys.get(conn_id), timeout_seconds=ACTIVATION_TIMEOUT_SECONDS
        )
        if result["ok"]:
            schedule.record_success(cs, conn, now, kind, slot, reset_due=reset_due)
        else:
            schedule.record_failure(cs, conn, now, kind, result["detail"], node=node)
        self.log(f"{conn_id} activation ({kind}) " + ("ok" if result["ok"] else f"failed: {result['detail']}"))
        return result

    def tick(self, now_fn=None):
        """One scheduling pass over every delegated connection.

        The server-side twin of the local `awewarm tick`. now_fn overrides the
        clock per connection (tests); it takes the connection and returns now.
        """
        with self.lock:
            fired, held = 0, []
            for conn_id in sorted(self.config["connections"]):
                conn = self.config["connections"][conn_id]
                if not conn.get("enabled", True):
                    continue
                errors = connection_errors(conn, conn_id)
                if errors:
                    self.log(f"skipping {conn_id}: {errors[0]}")
                    continue
                now = now_fn(conn) if now_fn else self._now(conn)
                cs = conn_state(self.state, conn_id)
                schedule.migrate_state(cs)

                def activate(action, node):
                    if not self.keys.get(conn_id):
                        # Hold, don't fail: the key lives in RAM and a restart
                        # wiped it. Catch-up still fires the slot once the local
                        # machine re-pushes; past the window it is skipped.
                        self.log(f"{conn_id}: activation held — API key missing (server restarted?)")
                        if conn_id not in held:
                            held.append(conn_id)
                        return None
                    return self._execute(conn, conn_id, cs, now, action["reason"], action.get("slot"), node)

                results, _skipped = schedule.dispatch_actions(conn, cs, now, activate)
                fired += len(results)
            self.last_tick_at = schedule.iso(datetime.now().astimezone())
            self._save(self.state_path, self.state)
            return {"fired": fired, "held": held}


class Tenant:
    """One paired hub user: registry bookkeeping plus a private workspace.

    The workspace is that tenant's own WarmServer over `tenants/<id>/` — its
    connections, state, and RAM keyring are invisible to other tenants by
    construction. It loads lazily so `awewarm hub invite` / `list` never pay
    for spinning up every tenant's files.
    """

    def __init__(self, tenant_id, record, tenants_root):
        self.id = tenant_id
        self.record = record  # the registry entry: tokenHash, note, createdAt, lastSeenAt, usage
        self.workspace_dir = Path(tenants_root) / tenant_id
        self.requests = deque()  # monotonic timestamps, the rate-limit window
        self._warm = None

    @property
    def token_hash(self):
        return self.record.get("tokenHash") or ""

    @property
    def note(self):
        return self.record.get("note") or ""

    @property
    def warm(self):
        if self._warm is None:
            self._warm = WarmServer(self.workspace_dir)
        return self._warm


class Hub:
    """The multi-tenant engine behind `awewarm serve --hub`.

    Pairing flows through one-time invites minted by the operator
    (`awewarm hub invite`); /v1/join burns one and returns a personal token.
    tenants.json stores SHA-256 hashes of tenant tokens; invite codes are kept
    in plaintext so the operator can recover one they already sent
    (`awewarm hub list invites --token`). API keys still never touch disk, and
    unlike single-tenant mode the pairings survive a restart without waiting
    for every user to come back online.
    """

    def __init__(self, data_dir, max_tenants=None, max_conns_per_tenant=None):
        self.data_dir = Path(data_dir).expanduser()
        self.registry_path = self.data_dir / "tenants.json"
        self.registry_lock_path = self.data_dir / "tenants.lock"
        self.log_path = self.data_dir / "awewarm-hub.log"
        self.lock = threading.RLock()
        self.registry = self._load()
        # Serve always passes its flag values; one-shot CLI processes (hub
        # invite / status) pass None and adopt what the running serve stamped
        # into the registry — so `hub status` reports the live caps, not the
        # defaults. Nothing recorded yet falls back to the code defaults.
        self.serve_record = self.registry.get("serve") or {}
        self.max_tenants = (
            max_tenants if max_tenants is not None
            else self.serve_record.get("maxTenants", DEFAULT_MAX_TENANTS)
        )
        self.max_conns_per_tenant = (
            max_conns_per_tenant if max_conns_per_tenant is not None
            else self.serve_record.get("maxConnsPerTenant", DEFAULT_MAX_CONNS_PER_TENANT)
        )
        self._registry_stamp = self._stamp()
        self.tenants = {
            tenant_id: Tenant(tenant_id, record, self.data_dir / "tenants")
            for tenant_id, record in self.registry["tenants"].items()
        }

    # --- registry (hashes only; no secret ever lands here) ---

    def _load(self):
        try:
            data = json.loads(self.registry_path.read_text())
        except FileNotFoundError:
            return {"version": 1, "tenants": {}, "invites": {}}
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"awewarm serve: cannot read {self.registry_path}\n{exc}\n"
                "fix: delete the file — tenants must re-join with fresh invites"
            )
        if not isinstance(data, dict) or not isinstance(data.get("tenants"), dict):
            raise SystemExit(
                f"awewarm serve: {self.registry_path} is malformed\n"
                "fix: delete the file — tenants must re-join with fresh invites"
            )
        data.setdefault("invites", {})
        return data

    def _stamp(self):
        """Identity of tenants.json on disk; any write — ours or another
        process's — changes it."""
        try:
            stat = self.registry_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _save(self):
        _write_json(self.registry_path, self.registry)
        self._registry_stamp = self._stamp()

    @contextmanager
    def _registry_transaction(self):
        """Serialize refresh + mutation + save across serve and hub CLI processes."""
        with self.lock:
            try:
                with process_lock(self.registry_lock_path, timeout_seconds=5):
                    self._refresh()
                    yield
            except LockBusy:
                raise ApiError(503, "hub registry is busy — retry this request")

    def _refresh(self):
        """Adopt tenants.json changes made by other processes since our last look.

        `hub invite` and `hub revoke` are one-shot CLI processes writing the
        same file; without this a long-lived serve would 403 every join with
        an invite minted after it started and keep honoring revoked tokens.
        Disk wins for persisted tenant records because every mutation saves
        synchronously under the registry transaction lock. Existing Tenant
        objects stay alive so their RAM keyrings and rate-limit queues survive."""
        with self.lock:
            stamp = self._stamp()
            if stamp is None or stamp == self._registry_stamp:
                return
            try:
                fresh = self._load()
            except SystemExit as exc:
                # a bad file mid-request must not kill the serve thread;
                # keep answering with what we have and say so in the log
                self.log(f"registry reload skipped: {exc}")
                return
            self.registry = fresh
            for tenant_id in list(self.tenants):
                if tenant_id in fresh["tenants"]:
                    self.tenants[tenant_id].record = fresh["tenants"][tenant_id]
                else:  # revoked by the operator in another process
                    self.tenants.pop(tenant_id, None)
            for tenant_id, record in fresh["tenants"].items():
                if tenant_id not in self.tenants:
                    self.tenants[tenant_id] = Tenant(tenant_id, record, self.data_dir / "tenants")
            self._registry_stamp = stamp

    def log(self, message):
        append_log(self.log_path, message)

    def record_launch(self, bind, port):
        """Stamp the effective knobs into the registry so `hub status` (a
        separate process) can report caps and find the live endpoint. Last
        launch wins; nothing here is secret."""
        record = {
            "version": __version__,
            "startedAt": schedule.iso(datetime.now().astimezone()),
            "bind": bind,
            "port": port,
            "maxTenants": self.max_tenants,
            "maxConnsPerTenant": self.max_conns_per_tenant,
        }
        with self._registry_transaction():
            self.registry["serve"] = record
            self._save()
        self.serve_record = record
        self.log(f"serve up on {bind}:{port} — caps {self.max_tenants} tenants, {self.max_conns_per_tenant} conns each")

    # --- pairing ---

    def mint_invite(self, note=None, ttl_hours=INVITE_TTL_HOURS):
        """One-time pairing code; the code itself is kept so `hub list invites`
        can recover it (tenant tokens, in contrast, are stored hashed only)."""
        invite = "awi_" + secrets.token_urlsafe(16)
        now = datetime.now().astimezone()
        with self._registry_transaction():
            self.registry["invites"][_hash_secret(invite)] = {
                "code": invite,
                "note": note,
                "createdAt": schedule.iso(now),
                "expiresAt": schedule.iso(now + timedelta(hours=ttl_hours)),
            }
            self._save()
        return invite

    def list_invites(self):
        """Rows for `awewarm hub list invites` — every minted code and its fate."""
        now = datetime.now().astimezone()
        rows = []
        for entry in self.registry["invites"].values():
            expires = schedule.parse_ts(entry.get("expiresAt"))
            used_by = entry.get("usedBy")
            if used_by:
                tenant_record = self.registry["tenants"].get(used_by)
                status = "suspended" if tenant_record and tenant_record.get("suspendedAt") else "used"
            elif entry.get("revokedAt"):
                status = "revoked"
            elif expires is not None and expires <= now:
                status = "expired"
            else:
                status = "pending"
            rows.append({
                # absent on invites minted before codes were kept on disk
                "code": entry.get("code"),
                "note": entry.get("note"),
                "createdAt": entry.get("createdAt"),
                "expiresAt": entry.get("expiresAt"),
                "usedBy": used_by,
                "usedAt": entry.get("usedAt"),
                "status": status,
            })
        rows.sort(key=lambda row: row["createdAt"] or "")
        return rows

    def join(self, invite):
        """Burn one invite, create the tenant, and return its token exactly once."""
        with self._registry_transaction():
            if not INVITE_RE.match(invite or ""):
                raise ApiError(400, "invite must look like awi_<code> — get one from the hub operator")
            digest = _hash_secret(invite)
            entry = self.registry["invites"].get(digest)
            if entry is None or entry.get("usedBy"):
                raise ApiError(403, "unknown or already-used invite — ask the hub operator for a fresh one")
            if entry.get("revokedAt"):
                raise ApiError(403, "invite revoked by the operator — ask them to restore it before reusing the code")
            now = datetime.now().astimezone()
            if schedule.parse_ts(entry.get("expiresAt")) <= now:
                del self.registry["invites"][digest]
                self._save()
                raise ApiError(403, "invite expired — ask the hub operator for a fresh one")
            self._require_capacity()
            tenant_id = "t_" + secrets.token_hex(4)
            while tenant_id in self.registry["tenants"]:
                tenant_id = "t_" + secrets.token_hex(4)
            token = "awt_" + secrets.token_urlsafe(32)
            self.registry["tenants"][tenant_id] = {
                "tokenHash": _hash_secret(token),
                "note": entry.get("note"),
                "createdAt": schedule.iso(now),
                "lastSeenAt": None,
                "usage": {"day": None, "today": 0, "total": 0},
            }
            self.tenants[tenant_id] = Tenant(tenant_id, self.registry["tenants"][tenant_id], self.data_dir / "tenants")
            entry["usedBy"] = tenant_id
            entry["usedAt"] = schedule.iso(now)
            self._save()
            self.log(f"{tenant_id} joined ({self.tenants[tenant_id].note or 'no note'})")
            return {"ok": True, "token": token, "tenantId": tenant_id}

    def _require_capacity(self):
        """Suspended tenants free their slot; taking one back needs room."""
        active = sum(1 for r in self.registry["tenants"].values() if not r.get("suspendedAt"))
        if active >= self.max_tenants:
            raise ApiError(
                403,
                f"hub is full ({self.max_tenants} active tenants) — "
                "the operator must suspend one first: awewarm hub revoke <tenant>",
            )

    def _invite_of(self, tenant_id):
        """The registry entry of the invite that produced this tenant, if any."""
        for entry in self.registry["invites"].values():
            if entry.get("usedBy") == tenant_id:
                return entry
        return None

    def revoke(self, tenant_id):
        """Suspend a tenant (`hub revoke t_...`): its token stops authenticating
        and its connections stop ticking, but everything stays on disk —
        reversible with `hub restore`. A suspended tenant frees its slot."""
        with self._registry_transaction():
            record = self.registry["tenants"].get(tenant_id)
            if record is None:
                raise ApiError(404, f"no such tenant: {tenant_id} (see: awewarm hub list users)")
            if record.get("suspendedAt"):
                raise ApiError(403, f"{tenant_id} is already suspended — restore it instead: awewarm hub restore {tenant_id}")
            record["suspendedAt"] = schedule.iso(datetime.now().astimezone())
            invite = self._invite_of(tenant_id)
            if invite is not None:
                invite["revokedAt"] = record["suspendedAt"]
            self._save()
        self.log(f"{tenant_id} suspended")
        return {"ok": True}

    def restore(self, tenant_id):
        """Reverse `revoke`: the tenant's token works again, capacity permitting."""
        with self._registry_transaction():
            record = self.registry["tenants"].get(tenant_id)
            if record is None:
                raise ApiError(404, f"no such tenant: {tenant_id} (see: awewarm hub list users)")
            if not record.get("suspendedAt"):
                raise ApiError(403, f"{tenant_id} is not suspended — nothing to restore")
            self._require_capacity()
            record.pop("suspendedAt", None)
            invite = self._invite_of(tenant_id)
            if invite is not None:
                invite.pop("revokedAt", None)
            self._save()
        self.log(f"{tenant_id} restored")
        return {"ok": True}

    def revoke_invite(self, code):
        """Kill an invite now instead of at its expiry (`hub revoke awi_...`).

        A pending code stops pairing on the spot; a used one suspends the
        tenant it produced (its token dies with it). Both are reversible via
        `hub restore`; nothing is deleted — the ledger keeps every row.
        """
        with self._registry_transaction():
            digest = _hash_secret(code)
            entry = self.registry["invites"].get(digest)
            if entry is None:
                raise ApiError(404, f"no such invite: {code}\nfix: list codes with: awewarm hub list invites --reveal")
            if entry.get("revokedAt"):
                raise ApiError(403, f"invite already revoked — restore it instead: awewarm hub restore {code}")
            now_text = schedule.iso(datetime.now().astimezone())
            used_by = entry.get("usedBy")
            tenant_record = self.registry["tenants"].get(used_by) if used_by else None
            if used_by and tenant_record is None:
                raise ApiError(404, f"invite was used by {used_by}, which no longer exists — nothing to suspend")
            expires = schedule.parse_ts(entry.get("expiresAt"))
            was_expired = expires is not None and expires <= datetime.now().astimezone()
            entry["revokedAt"] = now_text
            if tenant_record is not None and not tenant_record.get("suspendedAt"):
                tenant_record["suspendedAt"] = now_text
            self._save()
        self.log(f"invite revoked ({entry.get('note') or 'no note'})")
        status = "used" if used_by else ("expired" if was_expired else "pending")
        return {"ok": True, "status": status, "tenant": used_by, "note": entry.get("note")}

    def restore_invite(self, code):
        """Reverse `revoke_invite`: a pending code pairs again; a used one
        brings its tenant back, capacity permitting."""
        with self._registry_transaction():
            digest = _hash_secret(code)
            entry = self.registry["invites"].get(digest)
            if entry is None:
                raise ApiError(404, f"no such invite: {code}\nfix: list codes with: awewarm hub list invites --reveal")
            if not entry.get("revokedAt"):
                raise ApiError(403, f"invite is not revoked — nothing to restore")
            used_by = entry.get("usedBy")
            if used_by:
                tenant_record = self.registry["tenants"].get(used_by)
                if tenant_record is None:
                    raise ApiError(404, f"invite was used by {used_by}, which no longer exists — nothing to restore")
                self._require_capacity()
                tenant_record.pop("suspendedAt", None)
            entry.pop("revokedAt", None)
            self._save()
        self.log(f"invite restored ({entry.get('note') or 'no note'})")
        return {"ok": True, "tenant": used_by}

    def auth(self, bearer):
        """Bearer token → tenant, behind the per-tenant rate-limit gate."""
        with self.lock:
            self._refresh()  # revocations happen in other processes (awewarm hub revoke)
            digest = _hash_secret(bearer)
            tenant = next(
                (t for t in self.tenants.values() if hmac.compare_digest(t.token_hash, digest)),
                None,
            )
            if tenant is None:
                raise ApiError(
                    401,
                    "invalid hub token — re-pair with an invite, or reuse a saved token: awewarm remote connect <url> --token <saved>",
                )
            if tenant.record.get("suspendedAt"):
                raise ApiError(
                    401,
                    f"hub token suspended by the operator — ask them to restore it: awewarm hub restore {tenant.id}",
                )
            now = time.monotonic()
            while tenant.requests and now - tenant.requests[0] >= 60:
                tenant.requests.popleft()
            if len(tenant.requests) >= HUB_RATE_PER_MINUTE:
                raise ApiError(429, "too many requests from this tenant — is a client looping?")
            tenant.requests.append(now)
            self._refresh_seen(tenant)
            return tenant

    def _refresh_seen(self, tenant):
        now = datetime.now().astimezone()
        seen = schedule.parse_ts(tenant.record.get("lastSeenAt"))
        if seen is not None and now - seen < HUB_SEEN_PRECISION:
            return
        with self._registry_transaction():
            if tenant.id not in self.tenants:
                raise ApiError(401, "hub token was revoked during this request")
            tenant.record["lastSeenAt"] = schedule.iso(now)
            self._save()

    # --- quotas and usage ---

    def check_conn_quota(self, tenant, conn_id):
        """Per-tenant connection cap; replacing an existing id never counts."""
        with tenant.warm.lock:
            conns = tenant.warm.config["connections"]
            if conn_id not in conns and len(conns) >= self.max_conns_per_tenant:
                raise ApiError(
                    403,
                    f"connection quota reached ({self.max_conns_per_tenant} per tenant on this hub)",
                )

    def _bump_usage(self, tenant, count):
        with self._registry_transaction():
            if tenant.id not in self.tenants:
                return
            today = datetime.now().astimezone().date().isoformat()
            usage = tenant.record.setdefault("usage", {})
            if usage.get("day") != today:
                usage["day"] = today
                usage["today"] = 0
            usage["today"] = usage.get("today", 0) + count
            usage["total"] = usage.get("total", 0) + count
            self._save()

    def summarize(self):
        """Rows for `hub list users` — no secrets by construction."""
        joined_with = {
            entry.get("usedBy"): entry.get("code")
            for entry in self.registry["invites"].values()
            if entry.get("usedBy")
        }
        rows = []
        for tenant_id in sorted(self.tenants):
            tenant = self.tenants[tenant_id]
            warm = tenant.warm
            connections = []
            for cid in sorted(warm.config["connections"]):
                conn = warm.config["connections"][cid]
                cs = conn_state(warm.state, cid)
                transport = conn.get("transport") or {}
                connections.append({
                    "id": cid,
                    "status": schedule.status_word(cid, conn, cs),
                    "mode": (conn.get("schedule") or {}).get("mode", "fixed"),
                    "api": transport.get("baseUrl"),
                    "protocol": transport.get("kind"),
                    "model": (conn.get("activation") or {}).get("model"),
                    "enabled": conn.get("enabled", True),
                    "timezone": conn.get("timezone"),
                    "nextDueAt": cs.get("nextDueAt"),
                })
            rows.append({
                "tenant": tenant_id,
                "note": tenant.note,
                "invite": joined_with.get(tenant_id),
                "suspended": bool(tenant.record.get("suspendedAt")),
                "createdAt": tenant.record.get("createdAt"),
                "lastSeenAt": tenant.record.get("lastSeenAt"),
                "connections": connections,
                "usage": dict(tenant.record.get("usage") or {}),
            })
        return rows

    # --- the tick: every tenant's workspace in one pass ---

    def tick(self, now_fn=None):
        fired, held = 0, []
        with self._registry_transaction():
            tenants = [
                self.tenants[tenant_id] for tenant_id in sorted(self.tenants)
                if not self.tenants[tenant_id].record.get("suspendedAt")
            ]
        for tenant in tenants:
            result = tenant.warm.tick(now_fn=now_fn)
            fired += result["fired"]
            held.extend(result["held"])
            if result["fired"]:
                self._bump_usage(tenant, result["fired"])
        return {"fired": fired, "held": held}

    def run_now(self, tenant, conn_id, reset_due=False, allow_auto_disabled=False):
        result = tenant.warm.run_now(
            conn_id, reset_due=reset_due, allow_auto_disabled=allow_auto_disabled
        )
        if result.get("ok"):
            self._bump_usage(tenant, 1)
        return result


def _hash_secret(value):
    return hashlib.sha256((value or "").encode()).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    server_version = "awewarm-server"
    protocol_version = "HTTP/1.1"
    warm = None  # bound by make_server (single-tenant)
    hub = None   # bound by make_server (hub mode)

    def log_message(self, fmt, *args):
        engine = self.hub or self.warm
        engine.log(f"{self.address_string()} {fmt % args}")

    # --- plumbing ---

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > BODY_LIMIT_BYTES:
            raise ApiError(413, "body too large")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except ValueError:
            raise ApiError(400, "body must be JSON")

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[7:] if header.startswith("Bearer ") else ""

    def _authed(self):
        if not self.warm.check_token(self._bearer()):
            raise ApiError(401, "missing or invalid token (awewarm remote connect <url>)")

    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/healthz":
                if method != "GET":
                    raise ApiError(405, "GET only")
                if self.hub is not None:
                    payload = {"ok": True, "version": __version__, "claimed": True, "hub": True}
                else:
                    payload = {"ok": True, "version": __version__, "claimed": self.warm.claimed}
                return self._send(200, payload)
            if path == "/v1/join":
                if self.hub is None:
                    raise ApiError(404, "this server is single-tenant — pair with: awewarm remote connect <url>")
                if method != "POST":
                    raise ApiError(405, "POST only")
                return self._send(200, self.hub.join(self._body().get("invite")))
            if path == "/v1/claim":
                if self.hub is not None:
                    raise ApiError(403, "this is a hub server — pair with an invite: awewarm remote connect <url>")
                if method != "POST":
                    raise ApiError(405, "POST only")
                return self._send(200, self.warm.claim(self._body().get("token")))
            if self.hub is not None:
                tenant = self.hub.auth(self._bearer())
                warm, tenant_id = tenant.warm, tenant.id
            else:
                self._authed()
                warm, tenant_id = self.warm, None
            if path == "/v1/release":
                if method != "POST":
                    raise ApiError(405, "POST only")
                if self.hub is not None:
                    # The pairing outlives a disconnect — the kept token re-pairs
                    # on reconnect; freeing the slot is the operator's call
                    # (`awewarm hub revoke`), not any single client's.
                    return self._send(200, {"ok": True, "released": False})
                return self._send(200, warm.release())
            parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
            if parts[:2] == ["v1", "state"] and len(parts) == 2:
                if method != "GET":
                    raise ApiError(405, "GET only")
                view = warm.view()
                if tenant_id is not None:
                    view["tenant"] = tenant_id
                return self._send(200, view)
            if parts[:2] == ["v1", "keys"] and len(parts) == 2:
                if method != "PUT":
                    raise ApiError(405, "PUT only")
                return self._send(200, warm.put_keys(self._body()))
            if parts[:2] == ["v1", "connections"] and len(parts) in (3, 4):
                conn_id = parts[2]
                verb = parts[3] if len(parts) == 4 else None
                if verb == "run" and method == "POST":
                    body = self._body()
                    if self.hub is not None:
                        result = self.hub.run_now(
                            tenant, conn_id, bool(body.get("resetDue")), bool(body.get("allowAutoDisabled"))
                        )
                    else:
                        result = warm.run_now(
                            conn_id, bool(body.get("resetDue")), bool(body.get("allowAutoDisabled"))
                        )
                    return self._send(200, result)
                if verb is not None:
                    raise ApiError(404, f"no such endpoint: {path}")
                if method == "PUT":
                    if self.hub is not None:
                        self.hub.check_conn_quota(tenant, conn_id)
                    return self._send(200, warm.put_connection(conn_id, self._body()))
                if method == "DELETE":
                    return self._send(200, warm.delete_connection(conn_id))
                raise ApiError(405, "PUT or DELETE only")
            raise ApiError(404, f"no such endpoint: {path}")
        except ApiError as exc:
            return self._send(exc.status, {"error": exc.message})
        except Exception as exc:  # never leak a traceback to the wire
            engine = self.hub or self.warm
            engine.log(f"internal error on {method} {path}: {exc!r}")
            return self._send(500, {"error": "internal server error (see the server log)"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def make_server(data_dir, bind="127.0.0.1", port=8790, fixed_token=None, hub=False,
                max_tenants=DEFAULT_MAX_TENANTS, max_conns_per_tenant=DEFAULT_MAX_CONNS_PER_TENANT):
    """Build the engine (one WarmServer, or a Hub of them) plus its HTTP server.

    Port 0 picks a free one."""
    if hub:
        engine = Hub(data_dir, max_tenants=max_tenants, max_conns_per_tenant=max_conns_per_tenant)
        handler = type("BoundHandler", (_Handler,), {"hub": engine})
    else:
        engine = WarmServer(data_dir, fixed_token=fixed_token)
        handler = type("BoundHandler", (_Handler,), {"warm": engine})
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    return engine, httpd


def run(data_dir, bind="127.0.0.1", port=8790, fixed_token=None, tick_seconds=60, hub=False,
        max_tenants=DEFAULT_MAX_TENANTS, max_conns_per_tenant=DEFAULT_MAX_CONNS_PER_TENANT):
    """Serve forever: API in the main thread, the tick loop beside it."""
    engine, httpd = make_server(
        data_dir, bind=bind, port=port, fixed_token=fixed_token, hub=hub,
        max_tenants=max_tenants, max_conns_per_tenant=max_conns_per_tenant,
    )
    actual = httpd.server_address[1]
    if hub:
        engine.record_launch(bind, actual)

    def _loop():
        while True:
            time.sleep(tick_seconds)
            try:
                engine.tick()
            except Exception as exc:  # the loop must outlive any single tick
                engine.log(f"tick crashed: {exc!r}")

    threading.Thread(target=_loop, daemon=True, name="awewarm-tick").start()
    host, port_str = (bind, str(actual))
    print(f"awewarm serve {__version__}")
    print(f"  data dir: {engine.data_dir}  (config/state/log — no secrets ever written to disk)")
    print(f"  listening: http://{host}:{port_str}")
    if hub:
        print(f"  hub mode: {len(engine.tenants)} of max {engine.max_tenants} tenants, "
              f"{engine.max_conns_per_tenant} connections each")
        print("  auth: per-tenant tokens (hashes in tenants.json); pair by invite: awewarm hub invite")
    elif fixed_token is not None:
        print("  auth: fixed token from --token (RAM only)")
    else:
        print("  auth: claimed by the first `awewarm remote connect` (token lives in RAM)")
        print("  expose it safely with a cloudflared tunnel; see the README server section")
    print("  Ctrl-C stops the server")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
