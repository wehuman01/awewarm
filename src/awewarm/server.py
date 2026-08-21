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

Wire protocol (JSON over HTTP, Bearer token):
  GET    /healthz                    no auth; {ok, version, claimed}
  POST   /v1/claim                   {token} → claim an unclaimed server
  POST   /v1/release                 give up the claim (authed; disconnect)
  PUT    /v1/connections/<id>        {connection, apiKey, timezone} → take over
  DELETE /v1/connections/<id>        drop a connection (takeback)
  PUT    /v1/keys                    {id: key, ...} → re-key after a restart
  GET    /v1/state                   server truth for `awewarm status`
  POST   /v1/connections/<id>/run    fire one now (manual semantics)
"""
import hmac
import json
import re
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, schedule, transport
from .config import append_log, conn_state, connection_errors, default_conn_state, timezone_for

TOKEN_RE = re.compile(r"^awt_[A-Za-z0-9_-]{20,128}$")
BODY_LIMIT_BYTES = 256 * 1024
# The tick and run_now hold the server lock while sending, so one activation
# must not stall every API call behind it: cap HTTP far below the 60 s the
# local CLI allows (delegated connections are always HTTP subscriptions).
ACTIVATION_TIMEOUT_SECONDS = 15


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
        path = Path(path)
        path.write_text(json.dumps(data, indent=2) + "\n")
        path.chmod(0o600)

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


class _Handler(BaseHTTPRequestHandler):
    server_version = "awewarm-server"
    protocol_version = "HTTP/1.1"
    warm = None  # bound by make_server

    def log_message(self, fmt, *args):
        self.warm.log(f"{self.address_string()} {fmt % args}")

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

    def _authed(self):
        header = self.headers.get("Authorization") or ""
        token = header[7:] if header.startswith("Bearer ") else ""
        if not self.warm.check_token(token):
            raise ApiError(401, "missing or invalid token (awewarm remote connect <url>)")

    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/healthz":
                if method != "GET":
                    raise ApiError(405, "GET only")
                return self._send(200, {"ok": True, "version": __version__, "claimed": self.warm.claimed})
            if path == "/v1/claim":
                if method != "POST":
                    raise ApiError(405, "POST only")
                return self._send(200, self.warm.claim(self._body().get("token")))
            self._authed()
            if path == "/v1/release":
                if method != "POST":
                    raise ApiError(405, "POST only")
                return self._send(200, self.warm.release())
            parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
            if parts[:2] == ["v1", "state"] and len(parts) == 2:
                if method != "GET":
                    raise ApiError(405, "GET only")
                return self._send(200, self.warm.view())
            if parts[:2] == ["v1", "keys"] and len(parts) == 2:
                if method != "PUT":
                    raise ApiError(405, "PUT only")
                return self._send(200, self.warm.put_keys(self._body()))
            if parts[:2] == ["v1", "connections"] and len(parts) in (3, 4):
                conn_id = parts[2]
                verb = parts[3] if len(parts) == 4 else None
                if verb == "run" and method == "POST":
                    body = self._body()
                    return self._send(200, self.warm.run_now(
                        conn_id, bool(body.get("resetDue")), bool(body.get("allowAutoDisabled"))
                    ))
                if verb is not None:
                    raise ApiError(404, f"no such endpoint: {path}")
                if method == "PUT":
                    return self._send(200, self.warm.put_connection(conn_id, self._body()))
                if method == "DELETE":
                    return self._send(200, self.warm.delete_connection(conn_id))
                raise ApiError(405, "PUT or DELETE only")
            raise ApiError(404, f"no such endpoint: {path}")
        except ApiError as exc:
            return self._send(exc.status, {"error": exc.message})
        except Exception as exc:  # never leak a traceback to the wire
            self.warm.log(f"internal error on {method} {path}: {exc!r}")
            return self._send(500, {"error": "internal server error (see the server log)"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def make_server(data_dir, bind="127.0.0.1", port=8790, fixed_token=None):
    """Build the WarmServer plus its HTTP server (port 0 picks a free one)."""
    warm = WarmServer(data_dir, fixed_token=fixed_token)
    handler = type("BoundHandler", (_Handler,), {"warm": warm})
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    return warm, httpd


def run(data_dir, bind="127.0.0.1", port=8790, fixed_token=None, tick_seconds=60):
    """Serve forever: API in the main thread, the tick loop beside it."""
    warm, httpd = make_server(data_dir, bind=bind, port=port, fixed_token=fixed_token)
    actual = httpd.server_address[1]

    def _loop():
        while True:
            time.sleep(tick_seconds)
            try:
                warm.tick()
            except Exception as exc:  # the loop must outlive any single tick
                warm.log(f"tick crashed: {exc!r}")

    threading.Thread(target=_loop, daemon=True, name="awewarm-tick").start()
    host, port_str = (bind, str(actual))
    print(f"awewarm serve {__version__}")
    print(f"  data dir: {warm.data_dir}  (config/state/log — no secrets ever written to disk)")
    print(f"  listening: http://{host}:{port_str}")
    if fixed_token is not None:
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
