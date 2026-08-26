"""Status rendering: per-connection blocks and the merged local+remote view.

Shared helpers (_now, _fmt_moment, _find_connection) are reached through a
call-time `from . import cli` (never a top-level import) so the modules stay
import-order-safe and tests can keep patching `awewarm.cli._now` — the same
seam the command bodies use.
"""
import json
import sys
from datetime import datetime

import click

from . import install, remote, schedule, transport
from .config import (
    DEFAULT_CATCHUP_ATTEMPTS,
    DEFAULT_DEGRADE_AFTER_NODES,
    conn_state,
    connection_errors,
    load_config,
    load_state,
    save_state,
)


def _status_block(conn_id, conn, state, now, detailed, where=None):
    from . import cli
    errors = connection_errors(conn, conn_id)
    cs = conn_state(state, conn_id)
    schedule.migrate_state(cs)
    enabled = conn.get("enabled", True)
    word = schedule.status_word(conn_id, conn, cs)
    click.echo(f"\n{conn.get('label', conn_id)} ({conn_id}) — {word}" + (f" · {where}" if where else ""))
    if errors:
        click.echo(f"  Problem: {errors[0]}")
        return
    window = conn["window"]
    window_line = window["status"] if window["status"] in ("verified", "user-confirmed") else "unknown"
    if window.get("durationMinutes"):
        window_line = f"{window['durationMinutes']} minutes, {window_line}"
    fixed = conn["schedule"].get("fixed") or {}
    times_line = f"{', '.join(fixed.get('at') or []) or 'none'} ({fixed.get('days', 'weekday')})"
    mode = conn["schedule"]["mode"]
    click.echo(f"  Mode: {mode}" + (" (single-shot after failures)" if word == "degraded" else ""))
    if mode == "fixed":
        click.echo(f"  Times: {times_line}")
    else:
        click.echo(f"  Window: {window_line}" + (f" (evidence: {window['evidence']})" if detailed else ""))
    if word in ("failing", "degraded", "auto-disabled"):
        threshold = conn.get("degradeAfterNodes", DEFAULT_DEGRADE_AFTER_NODES)
        if word == "failing":
            attempts_max = (conn.get("catchup") or {}).get("attempts", DEFAULT_CATCHUP_ATTEMPTS)
            if cs.get("nodeKey"):
                detail = f"catch-up attempt {cs.get('nodeAttempts', 0)}/{attempts_max}"
            else:
                detail = "waiting for the next node"
            click.echo(f"  Health: failing — {cs.get('failedNodes', 0)}/{threshold} nodes lost, {detail}")
        elif word == "degraded":
            click.echo(f"  Health: degraded — one shot per node ({cs.get('degradedFailedNodes', 0)}/{threshold} lost)")
        else:
            click.echo(f"  Health: stopped after repeated node failures — resume with: awewarm config set {conn_id} --on")
    if detailed:
        target = conn["transport"].get("baseUrl") or conn["transport"].get("cliCommand")
        click.echo(f"  Transport: {conn['transport']['kind']}" + (f" → {target}" if target else ""))
        click.echo(f"  Kind: {conn['kind']}, model: {conn['activation'].get('model') or 'cli default'}")
        if mode == "fixed":
            click.echo(f"  Window: {window_line} (evidence: {window['evidence']})")
        else:
            click.echo(f"  Fixed times: {times_line}")
    last = schedule.parse_ts(cs.get("lastActivationAt"))
    click.echo(f"  Last activation: {cli._fmt_moment(last, now)}")
    if cs.get("lastResult") == "failure":
        attempted = schedule.parse_ts(cs.get("lastAttemptAt"))
        detail = cs.get("lastError") or "unknown error"
        click.echo(f"  Last result: failure ({cli._fmt_moment(attempted, now)}) — {detail}")
    if not enabled:
        click.echo("  Next due: none (disabled)")
        return
    if cs.get("autoDisabledAt"):
        click.echo("  Next due: none (auto-disabled)")
        return
    due_at, due_kind = schedule.next_due(conn, cs, now)
    click.echo(f"  Next due: {cli._fmt_moment(due_at, now)}" + (f" ({due_kind})" if due_at else ""))


def _fetch_remote_view(config, state):
    """Server truth for delegated connections, cached for offline display.

    Returns (view or None, note or None); the note explains stale or missing
    data. Never fatal — status works offline off the last successful sync.
    """
    from . import cli
    try:
        view = remote.ensure_session(config)
        state["remoteCache"] = {"fetchedAt": schedule.iso(datetime.now().astimezone()), "server": view}
        return view, None
    except remote.RemoteError as exc:
        cache = state.get("remoteCache") or {}
        if cache.get("server"):
            fetched = schedule.parse_ts(cache.get("fetchedAt"))
            when = cli._fmt_moment(fetched, datetime.now().astimezone()) if fetched else "an unknown time"
            return cache["server"], f"server unreachable, showing the last sync from {when}"
        return None, f"server unreachable ({exc})"


def _show_status(connection, as_json, location=None):
    """Render status; `location` filters — True shows only delegated
    connections, False only locally scheduled ones, None shows both."""
    from . import cli
    config = load_config()
    if connection:
        # An explicit ask always shows the connection, hidden or not.
        conn = cli._find_connection(config, connection)[1]
        if location is not None and (conn.get("location") == "remote") != location:
            where = "remote" if location else "local"
            cli.die(
                f"{connection} is not {where}\n"
                f"fix: awewarm config set {connection} --{where}"
            )
        conns = {connection: config["connections"][connection]}
    else:
        conns = {
            cid: conn for cid, conn in config["connections"].items()
            if not conn.get("hide")
        }
        if location is not None:
            conns = {
                cid: conn for cid, conn in conns.items()
                if (conn.get("location") == "remote") == location
            }
    state = load_state()
    remote_view, remote_note = (None, None)
    if remote.remote_url(config) and any(c.get("location") == "remote" for c in conns.values()):
        cached_before = state.get("remoteCache")
        remote_view, remote_note = _fetch_remote_view(config, state)
        if state.get("remoteCache") != cached_before:
            save_state(state)  # persist the sync cache for offline status runs
    if as_json:
        view = {
            "config": {"version": config["version"], "connections": conns},
            "state": {"connections": {k: state["connections"].get(k) for k in conns}},
            "scheduler": {
                "installed": install.scheduler_installed(),
                "wake": {
                    "granted": sys.platform == "darwin" and install.wake_grant_installed(),
                    "events": [
                        moment.strftime("%Y-%m-%d %H:%M")
                        for moment in install.armed_wake_moments(state)
                    ],
                },
            },
            "remote": {"url": remote.remote_url(config), "server": remote_view, "note": remote_note},
        }
        click.echo(json.dumps(transport.redact(view), indent=2))
        return
    if not conns:
        if location:
            if not remote.remote_url(config):
                click.echo("No remote server paired — pair with: awewarm remote connect <url>")
            elif config["connections"]:
                click.echo("No delegated connections — delegate one with: awewarm config set <id> --remote")
            else:
                click.echo("No connections yet.\nrun: awewarm init\n or: awewarm config add")
        elif location is False:
            if any(c.get("location") == "remote" for c in config["connections"].values()):
                click.echo("No local connections — every connection is delegated (view them: awewarm status --remote)")
            elif config["connections"]:
                click.echo(
                    "No visible connections — all are hidden from status.\n"
                    "unhide with: awewarm config set <id> --show"
                )
            else:
                click.echo("No connections yet.\nrun: awewarm init\n or: awewarm config add")
        elif config["connections"]:
            click.echo(
                "No visible connections — all are hidden from status.\n"
                "unhide with: awewarm config set <id> --show"
            )
        else:
            click.echo("No connections yet.\nrun: awewarm init\n or: awewarm config add")
        return
    now = cli._now(config)
    if location and remote_view:
        # Server stamps arrive in the server's timezone; the health line sits
        # next to viewer-local times, so render both in the viewer's zone —
        # one screen, one time basis.
        started = schedule.parse_ts(remote_view.get("startedAt"))
        if started is not None:
            started = started.astimezone(now.tzinfo)
        ticked = schedule.parse_ts(remote_view.get("lastTickAt"))
        if ticked is not None:
            ticked = ticked.astimezone(now.tzinfo)
        hub_version = remote_view.get("hubVersion")
        engine_version = remote_view.get("version")
        what = (
            f"awewarm-hub {hub_version} (engine awewarm {engine_version})"
            if hub_version
            else f"awewarm server {engine_version}"
        )
        click.echo(
            f"{what} at {remote.remote_url(config)} — "
            f"up since {cli._fmt_moment(started, now)}, last tick {cli._fmt_moment(ticked, now)}"
        )
    for conn_id in sorted(conns):
        conn = conns[conn_id]
        if conn.get("location") == "remote" and remote_view:
            entry = (remote_view.get("connections") or {}).get(conn_id)
            if entry:
                server_state = {"connections": {conn_id: entry.get("state") or {}}}
                _status_block(
                    conn_id, entry.get("config") or conn, server_state, now,
                    detailed=bool(connection), where=remote.remote_url(config),
                )
                noun = "credential" if (entry.get("config") or {}).get("kind") == "account" else "key"
                if entry.get("keyMissing"):
                    click.echo(f"  ⚠ the server lost its {noun} (restarted?) — rerun: awewarm remote push")
                elif connection:
                    # Key location is a static fact, not news — the summary only
                    # speaks up when a key is missing.
                    if entry.get("keyPersisted"):
                        click.echo(f"  {noun}: stored on the server (you opted in; plaintext in its keys.json)")
                    else:
                        click.echo(f"  {noun}: server RAM only")
                fingerprint = entry.get("credentialFingerprint")
                if connection and noun == "credential":
                    note = f"fingerprint {fingerprint}" if fingerprint else "fingerprint unknown (re-push: awewarm remote push)"
                    click.echo(f"  credential {note} — the local login is the source of truth")
                    native = ((entry.get("config") or {}).get("transport") or {}).get("exec") == "native"
                    click.echo(
                        "  warmed natively over HTTPS (no CLI installed on the server)"
                        if native else "  warmed by the server's own CLI copy"
                    )
                continue
            # Reachable server, but the connection is missing from its view
            # (wiped data dir, never-healed pending push): show the local copy
            # labeled as delegated, with the warning that nothing fires it now.
            _status_block(
                conn_id, conn, state, now,
                detailed=bool(connection), where=remote.remote_url(config),
            )
            click.echo("  ⚠ missing on the server — rerun: awewarm remote push")
            continue
        _status_block(conn_id, conn, state, now, detailed=bool(connection))
    footer = f"\nScheduler: {'enabled' if install.scheduler_installed() else 'not installed — run: awewarm scheduler install'}"
    if sys.platform == "darwin":
        if install.wake_grant_installed():
            moments = install.armed_wake_moments(state)
            next_note = f", next {moments[0].strftime('%m-%d %H:%M')}" if moments else ""
            footer += f"\nWake layer: enabled — {len(moments)} RTC wake(s) armed{next_note}"
        else:
            footer += "\nWake layer: off — lid-closed sleep fires late (enable: awewarm scheduler install --wake)"
    if remote.remote_url(config):
        delegated = sum(1 for c in config["connections"].values() if c.get("location") == "remote")
        cached = state.get("remoteCache") or {}
        synced = cli._fmt_moment(schedule.parse_ts(cached.get("fetchedAt")), now) if cached.get("fetchedAt") else None
        footer += f"\nRemote: {remote.remote_url(config)} ({delegated} delegated"
        footer += f", last sync {synced}" if synced else ""
        footer += ")"
        if remote_note:
            footer += f" — {remote_note}"
    click.echo(footer)
