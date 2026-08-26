#!/usr/bin/env python3
"""awewarm CLI: init, discover, config, status, run, scheduler, remote, serve,
self-update (plus tick, hidden). Older command names still work as hidden
aliases (removed in v1.0); the scheduler's `awewarm tick` invocation is fixed
because installed scheduler agents run it verbatim and self-heal if outdated."""
import copy
import io
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from . import __version__, credstore, discover, display_version, install, keystore, remote, running_from_checkout, schedule, transport
from .clickext import WrapGroup
from .flows import _add_account_flow, _config_add, _slots_proc
from .locking import LockBusy, local_process_lock
from .status import _show_status
from .update_check import check_async, get_pypi_latest, version_gte
from .config import (
    CONFIG_TEMPLATE,
    DEFAULT_CATCHUP_ATTEMPTS,
    DEFAULT_CATCHUP_MINUTES,
    DEFAULT_DEGRADE_AFTER_NODES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT,
    SCHEDULE_MODES,
    SLOT_RE,
    append_log,
    config_path,
    conn_state,
    connection_errors,
    default_schedule,
    default_settings,
    die,
    flatten_schedule,
    load_config,
    load_state,
    log_path,
    resolve_connection,
    save_config,
    save_state,
    state_path,
    timezone_for,
    timezone_name,
)


def log_event(message):
    """Append one line to the log; best-effort and never fatal."""
    append_log(log_path(), message)


def _tz(config):
    name = timezone_name(config)
    if not name:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except Exception as exc:
        die(f"unknown timezone: {name}\nfix: use an IANA name like Asia/Taipei\n{exc}")


def _now(config):
    return datetime.now(_tz(config))


def _next_occurrence(hhmm, now):
    """Next wall-clock occurrence of HH:MM: today when still ahead, else tomorrow."""
    moment = schedule.slot_datetime(now.date(), hhmm, now.tzinfo)
    if moment is not None and moment > now:
        return moment
    return schedule.slot_datetime(now.date() + timedelta(days=1), hhmm, now.tzinfo)


def _find_connection(config, conn_id):
    if conn_id in config["connections"]:
        return conn_id, config["connections"][conn_id]
    known = ", ".join(sorted(config["connections"])) or "none configured"
    die(f"unknown connection: {conn_id}\nknown connections: {known}\nrun: awewarm status")


def _resolve_api_key(conn):
    """Secret for subscription connections; None for local-CLI accounts."""
    ref = (conn.get("auth") or {}).get("apiKeyRef")
    if not ref:
        return None
    return keystore.load_api_key(ref)


def _execute_activation(conn, conn_id, cs, now, kind, slot=None, reset_due=True, node=None):
    """Send one real request and record the outcome in state.

    node ties the attempt to its scheduled node for ladder bookkeeping;
    manual/verify fires omit it so they never count as nodes.
    """
    schedule.record_attempt(cs, now)
    api_key = None
    if conn["kind"] == "subscription":
        api_key = _resolve_api_key(conn)
        if api_key is None:
            schedule.record_failure(cs, conn, now, kind, "API key unavailable (missing from secrets.json)", node=node)
            log_event(f"{conn_id} activation ({kind}) failed: API key unavailable")
            return {"ok": False, "detail": "API key unavailable (missing from secrets.json)"}
    result = transport.send_activation(conn, api_key)
    if result["ok"]:
        schedule.record_success(cs, conn, now, kind, slot, reset_due=reset_due)
        log_event(f"{conn_id} activation ({kind}) ok")
    else:
        schedule.record_failure(cs, conn, now, kind, result["detail"], node=node)
        log_event(f"{conn_id} activation ({kind}) failed: {result['detail']}")
    return result


def _fmt_moment(moment, now):
    if moment is None:
        return "never"
    if moment.date() == now.date():
        return f"today {moment.strftime('%H:%M')}"
    return moment.strftime("%Y-%m-%d %H:%M")


def _tick():
    """One scheduler pass over every enabled connection: fire what's due only.

    Calls install._maybe_self_heal_job() at the top so an old scheduler job
    (e.g. left over from a pre-`tick` version after a manual `pip install
    --upgrade`) is rewritten on the first tick and the second tick onward
    uses the current command line; on macOS it also heals stale calendar
    wake entries after schedule edits.

    This is the body of `awewarm tick`, called by the scheduler every minute.
    Distinct from `_fire_all`, which fires unconditionally regardless of due.
    The tail converges the RTC wake layer so lid-closed sleep still wakes at
    the next slot/renewal moment.
    """
    config = load_config()
    install._maybe_self_heal_job(config)
    state = load_state()
    now = _now(config)
    if not config["connections"]:
        click.echo("no connections configured")
        return
    activated = []
    skipped = 0
    for conn_id in sorted(config["connections"]):
        conn = config["connections"][conn_id]
        if not conn.get("enabled", True):
            continue
        if conn.get("location") == "remote":
            continue  # the remote server owns its schedule now
        errors = connection_errors(conn, conn_id)
        if errors:
            log_event(f"skipping {conn_id}: {errors[0]}")
            click.echo(f"skipping {conn_id}: {errors[0]}")
            continue
        cs = conn_state(state, conn_id)
        schedule.migrate_state(cs)

        def _activate(action, node):
            reason = action["reason"]
            slot_note = f", slot {action['slot']}" if action.get("slot") else ""
            result = _execute_activation(conn, conn_id, cs, now, reason, action.get("slot"), node=node)
            mark = "✓" if result["ok"] else "✗"
            suffix = f" — {result['detail']}" if result["detail"] else ""
            click.echo(f"{mark} activated {conn_id} ({reason}{slot_note}){suffix}")
            return result

        results, skipped_conn = schedule.dispatch_actions(conn, cs, now, _activate)
        activated.extend(result["ok"] for result in results)
        skipped += skipped_conn
    _maybe_sync_remote(config, state)
    save_state(state)
    _maybe_sync_wake(config, state)
    if activated or skipped:
        click.echo(f"{sum(activated)} activated, {len(activated) - sum(activated)} failed, {skipped} slots skipped")
    else:
        click.echo("nothing due")


def _plan_summary(connection):
    """Human description of what `awewarm run` is about to do, for the confirm prompt.

    Computed before the prompt so a user who runs `awewarm run` with no enabled
    connections doesn't get asked "Proceed? [y/N]" and then told "nothing to do".
    """
    if connection is not None:
        return f"Activate {connection}"
    config = load_config()
    enabled = [
        cid for cid, conn in config["connections"].items()
        if conn.get("enabled", True)
    ]
    if not enabled:
        return "No enabled connections"
    if len(enabled) == 1:
        return f"Fire the only enabled connection ({enabled[0]})"
    return f"Fire all {len(enabled)} enabled connections"


def _run_timeout(conn):
    """A delegated CLI account fires the real CLI (cap 120 s) inside the run
    round-trip; the client must outwait it instead of reporting a phantom
    failure while the activation is still in flight."""
    if (conn.get("transport") or {}).get("kind") in transport.CLI_TRANSPORT_KINDS:
        return remote.CLI_RUN_TIMEOUT_SECONDS
    return None


def _activate_now(target, reset_due=False):
    """Fire one connection immediately, outside its schedule.

    The schedule itself is untouched unless reset_due is set — a manual fire
    must not push the renewal cadence out by a full window. Delegated
    connections fire on the remote server, which owns their state.
    """
    config = load_config()
    conn_id, conn = _find_connection(config, target)
    if conn.get("location") == "remote":
        try:
            result = remote.run_connection(
                remote.remote_url(config), remote.load_token(), conn_id,
                reset_due=reset_due, allow_auto_disabled=True, timeout=_run_timeout(conn),
            )
        except remote.RemoteError as exc:
            die(f"remote activation failed:\n{exc}")
        if result.get("ok"):
            due_at = schedule.parse_ts(result.get("nextDue"))
            now = _now(config)
            note = f" — next due {_fmt_moment(due_at, now)}" if due_at else ""
            detail = f": {result['detail']}" if result.get("detail") else ""
            click.echo(f"✓ {conn_id} activated on the remote server{detail}{note}")
        else:
            die(f"activation failed: {result.get('detail')}")
        return
    state = load_state()
    cs = conn_state(state, conn_id)
    now = _now(config)
    result = _execute_activation(conn, conn_id, cs, now, "manual", reset_due=reset_due)
    save_state(state)
    if result["ok"]:
        due_at, _ = schedule.next_due(conn, cs, now)
        note = f" — next due {_fmt_moment(due_at, now)}" if due_at else ""
        click.echo(f"✓ {conn_id} activated{': ' + result['detail'] if result['detail'] else ''}{note}")
    else:
        die(f"activation failed: {result['detail']}")


def _fire_all():
    """Fire every enabled connection immediately, ignoring its schedule.

    Used by `awewarm run` (no id). Each connection gets one real request,
    recorded as kind="manual" with reset_due=False so the interval chain is
    not pushed forward. This is the user-facing "warm everything right now"
    verb — different from `tick`, which only fires what's due.
    """
    # A pre-`tick` scheduler agent still invokes `run --force` once a minute;
    # without this heal it would fire every connection on every pass and never
    # pick up the new job definition (self-heal otherwise lives in `tick`).
    if not sys.stdin.isatty():
        install._maybe_self_heal_job(load_config())
    config = load_config()
    state = load_state()
    now = _now(config)
    enabled = {
        cid: conn for cid, conn in config["connections"].items()
        if conn.get("enabled", True)
    }
    if not enabled:
        click.echo("No enabled connections")
        return
    ok = 0
    for conn_id in sorted(enabled):
        conn = enabled[conn_id]
        errors = connection_errors(conn, conn_id)
        if errors:
            log_event(f"skipping {conn_id}: {errors[0]}")
            click.echo(f"skipping {conn_id}: {errors[0]}")
            continue
        if conn.get("location") == "remote":
            try:
                result = remote.run_connection(
                    remote.remote_url(config), remote.load_token(), conn_id,
                    timeout=_run_timeout(conn),
                )
            except remote.RemoteError as exc:
                log_event(f"{conn_id} remote run failed: {exc}")
                click.echo(f"✗ activated {conn_id} — remote server unreachable")
                continue
            mark = "✓" if result["ok"] else "✗"
            suffix = f" — {result['detail']}" if result.get("detail") else ""
            click.echo(f"{mark} activated {conn_id} (on the server){suffix}")
            if result["ok"]:
                ok += 1
            continue
        cs = conn_state(state, conn_id)
        schedule.migrate_state(cs)
        if cs.get("autoDisabledAt"):
            click.echo(
                f"skipping {conn_id}: auto-disabled after repeated failures "
                f"(resume: awewarm config set {conn_id} --on)"
            )
            continue
        result = _execute_activation(conn, conn_id, cs, now, "manual", reset_due=False)
        mark = "✓" if result["ok"] else "✗"
        suffix = f" — {result['detail']}" if result["detail"] else ""
        click.echo(f"{mark} activated {conn_id}{suffix}")
        if result["ok"]:
            ok += 1
    save_state(state)
    click.echo(f"{ok} of {len(enabled)} activated")


# --- remote delegation: the local machine stays the owner of every secret ---


REMOTE_SYNC_EVERY = timedelta(minutes=30)


def _push_timezone(config):
    """This machine's zone as an IANA name — or a fixed UTC offset when the
    system offers no IANA name (Windows), which still runs fixed slots at the
    right wall-clock times, just without DST rules."""
    name = timezone_name(config)
    if name:
        return name
    tz = datetime.now().astimezone().tzinfo
    key = getattr(tz, "key", None)
    if key:
        return key
    offset = datetime.now(tz).utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _resolve_secret(conn, conn_id=None):
    """(secret, fingerprint) for delegation: a subscription's stored API key,
    or an local CLI account's login credential read fresh from the local login
    (the local machine stays the source of truth). fingerprint is set for
    accounts only — it rides the push so drift is detectable. Raises
    remote.RemoteError (actionable, secret-free) when unavailable."""
    if conn.get("kind") != "account":
        return _resolve_api_key(conn), None
    try:
        credential = credstore.read_credential(conn)
    except credstore.CredentialError as exc:
        raise remote.RemoteError(f"{conn_id or 'connection'}: {exc}")
    return credential.raw, credential.fingerprint


def _require_secret(conn, conn_id):
    secret, fingerprint = _resolve_secret(conn, conn_id)
    if secret is None:
        raise remote.RemoteError(
            f"{conn_id}: no API key stored locally\n"
            f"  fix: awewarm config set {conn_id} --api-key <key>"
        )
    return secret, fingerprint


PERSIST_KEY_ON_NOTICE = (
    "  its API key will be stored in PLAINTEXT on the server's disk\n"
    "  (keys.json, 0600) — readable by the hub operator and anyone with disk\n"
    "  access there. Worth it only if this machine is rarely online: without\n"
    "  it, a server restart while this machine is away holds the warm-ups\n"
    "  (skipped past the 30-minute catch-up window) until the machine returns."
)
PERSIST_KEY_ON_NOTICE_ACCOUNT = (
    "  its LOGIN CREDENTIAL — account-wide, not a scoped API key — will be\n"
    "  stored in PLAINTEXT on the server's disk (keys.json, 0600), readable by\n"
    "  the server operator and anyone with disk access there. Strongly\n"
    "  prefer RAM-only: a server restart while this machine is away merely\n"
    "  holds the warm-ups until the machine returns and re-pushes."
)
PERSIST_KEY_OFF_NOTICE = (
    "  the server deletes the key from its disk right away (the key stays in\n"
    "  secrets.json here and in the server's RAM — warm-ups continue), and\n"
    "  from then on a server restart while this machine is offline holds the\n"
    "  warm-ups (skipped past the 30-minute catch-up window) until it returns."
)


def _persist_on_notice(conn):
    return PERSIST_KEY_ON_NOTICE_ACCOUNT if conn.get("kind") == "account" else PERSIST_KEY_ON_NOTICE


def _provider_label(conn):
    return "Claude Code" if (conn.get("transport") or {}).get("kind") == "claude-cli" else "Codex"


def _stdin_is_interactive():
    """Separated so tests can flip it: the gates refuse to prompt a pipe."""
    return sys.stdin.isatty()


def _confirm_persist_change(conn_id, prompt, notice, default, assume_yes):
    """One persistence confirmation gate. Every user action that changes
    whether a key lives on a server's disk passes through here; background
    sync never does (it maintains choices already confirmed at a gate)."""
    click.echo(f"{conn_id}: {notice}")
    if assume_yes:
        return True
    if not _stdin_is_interactive():
        die("pass --yes to confirm this in non-interactive shells")
    return click.confirm(prompt, default=default)


def _sync_remote(config, state, force_ids=()):
    """Bring the server's copy back in line with local truth.

    Re-pushes edited or missing connections (their schedule changed, so the
    server state resets), re-sends secrets the server lost to a restart
    (its state on disk stays — only the RAM keyring was wiped), and re-pushes
    delegated accounts whose local credential rotated (fingerprint drift).
    Returns (pushed, rekeyed) connection ids.
    """
    url = remote.remote_url(config)
    token = remote.load_token()
    view = remote.ensure_session(config)
    have = view.get("connections") or {}
    pending = state.get("pendingPush") or {}
    tz = _push_timezone(config)
    pushed, rekeyed, keys = [], [], {}

    def _push(conn_id, conn):
        secret, fingerprint = _require_secret(conn, conn_id)
        remote.push_connection(
            url, token, conn_id, conn, secret, tz,
            persist=bool(conn.get("persistKey")), fingerprint=fingerprint,
        )
        pending.pop(conn_id, None)
        pushed.append(conn_id)

    for conn_id, conn in sorted(config["connections"].items()):
        if conn.get("location") != "remote":
            continue
        server = have.get(conn_id)
        if conn_id in force_ids or conn_id in pending or server is None:
            _push(conn_id, conn)
        elif server.get("keyMissing"):
            keys[conn_id] = _require_secret(conn, conn_id)[0]
            rekeyed.append(conn_id)
        elif conn.get("kind") == "account":
            # The local login is the source of truth: a fingerprint the server
            # no longer matches means the credential rotated — re-push it.
            try:
                _, fingerprint = _resolve_secret(conn, conn_id)
            except remote.RemoteError as exc:
                log_event(f"remote sync skipped {conn_id}: {exc}")
                continue
            if fingerprint and server.get("credentialFingerprint") != fingerprint:
                _push(conn_id, conn)
    if keys:
        remote.push_keys(url, token, keys)
    if state.get("pendingPush") != pending:
        state["pendingPush"] = pending
    return pushed, rekeyed


def _maybe_sync_remote(config, state):
    """Heal the server's RAM secrets while this machine is online, throttled.

    The server holds its token and API keys in memory only; after it restarts
    this is what re-claims and re-keys it. Runs from the local tick at most
    twice an hour; a failed attempt just waits for the next window.
    """
    if not any(c.get("location") == "remote" for c in config["connections"].values()):
        return
    if not remote.remote_url(config):
        return
    now = datetime.now().astimezone()
    last = schedule.parse_ts(state.get("lastRemoteSyncAt"))
    if last is not None and now - last < REMOTE_SYNC_EVERY:
        return
    state["lastRemoteSyncAt"] = schedule.iso(now)
    try:
        _sync_remote(config, state)
    except remote.RemoteError as exc:
        log_event(f"remote sync skipped: {exc}")


def _confirm_account_delegation(conn_id, url, conn, assume_yes):
    """The account-specific delegation gate: a login credential is
    account-wide (every subscription it holds), strictly more sensitive than
    a scoped API key — pushing it needs an explicit yes naming the server."""
    provider = _provider_label(conn)
    click.echo(
        f"{conn_id}: delegation reads your {provider} login credential and pushes it to\n"
        f"  {url} — it lives in that server's RAM (its disk only with --persist-key),\n"
        "  so the server operator can use the full account, not just warm it."
    )
    if assume_yes:
        return True
    if not _stdin_is_interactive():
        die("pass --yes to confirm account delegation in non-interactive shells")
    return click.confirm(f"Push this {provider} login credential to the server?", default=False)


def _delegate_remote(config, conn_id, conn, assume_yes=False):
    """Hand one connection to the remote server (`config set <id> --remote`).

    The flag only lands after the server accepted the push — otherwise neither
    side would tick the connection. The connection's effective schedule at
    handover (its own overrides, or whatever it inherited as a local
    connection) is pinned as its own settings: a delegated connection never
    follows the global schedule, so this is the one moment those values may
    carry over. A subscription needs its stored API key; an account reads its
    CLI login credential (behind the account delegation gate) and pushes it
    like a key — the server injects it into its own CLI runs, the local login
    stays the source of truth. A connection flagged persistKey gets its
    confirmation here — this is the moment the key would start living on the
    server's disk; declining downgrades to RAM-only and clears the flag.
    """
    if conn.get("location") == "remote":
        return
    kind = conn.get("kind")
    if kind not in ("subscription", "account"):
        die(f"{conn_id} is not a delegable connection (kind: {kind})")
    if not remote.remote_url(config):
        die("no remote server connected\nfix: awewarm remote connect <url>")
    url = remote.remote_url(config)
    if kind == "account" and not _confirm_account_delegation(conn_id, url, conn, assume_yes):
        click.echo(f"aborted — {conn_id} stays local (its login never left this machine)")
        return
    try:
        secret, fingerprint = _require_secret(conn, conn_id)
    except remote.RemoteError as exc:
        die(f"could not read the secret to delegate {conn_id}:\n{exc}")
    persist = bool(conn.get("persistKey"))
    if persist and not _confirm_persist_change(
        conn_id, "Store this key on the server's disk too?", _persist_on_notice(conn),
        default=False, assume_yes=assume_yes,
    ):
        conn.pop("persistKey", None)
        persist = False
        click.echo(f"✓ {conn_id} delegated with its key in server RAM only (persistence declined)")
    flattened = flatten_schedule(conn.get("schedule"))
    own = conn.setdefault("settings", {})
    own["schedule"] = {key: value for key, value in flattened.items() if key != "wakeWhenAsleep"}
    own["wakeWhenAsleep"] = flattened["wakeWhenAsleep"]
    resolve_connection(conn, config)
    try:
        remote.ensure_session(config)
        remote.push_connection(
            url, remote.load_token(), conn_id, conn, secret,
            _push_timezone(config), persist=persist, fingerprint=fingerprint,
        )
    except remote.RemoteError as exc:
        die(f"could not hand {conn_id} to the remote server — it stays local:\n{exc}")
    conn["location"] = "remote"
    resolve_connection(conn, config)
    if kind == "account":
        click.echo(
            f"✓ {conn_id} delegated — the server ticks it with its copy of your "
            f"{_provider_label(conn)} login (fingerprint {fingerprint})"
        )
        click.echo("  the local login stays the source of truth; a rotated credential re-pushes on the next sync")
    else:
        click.echo(f"✓ {conn_id} delegated — the server ticks it; the local scheduler skips it from now on")
    if persist:
        click.echo("  its key persists on the server (plaintext keys.json) — survives its restarts")


def _config_duplicate(config, conn_id, conn, delegate, assume_yes=False):
    """Copy a connection under a fresh id (`config set <id> --duplicate`).

    The key is re-stored under the new id — a shared ref would let removing
    either connection delete the other's — and runtime state starts blank.
    With --remote the copy is delegated and the original disabled: one
    subscription, one ticker. The copy inherits persistKey, and with it the
    confirmation gate inside _delegate_remote — naming the copy's id."""
    new_id = f"{conn_id}-copy"
    suffix = 2
    while new_id in config["connections"]:
        new_id = f"{conn_id}-copy{suffix}"
        suffix += 1
    clone = copy.deepcopy(conn)
    clone.pop("location", None)  # the copy starts local; --remote delegates it below
    api_key = _resolve_api_key(conn)
    if api_key is not None:
        clone.setdefault("auth", {})["apiKeyRef"] = keystore.store_api_key(new_id, api_key)
    config["connections"][new_id] = clone
    save_config(config)
    click.echo(f"✓ {conn_id} duplicated as {new_id}")
    if not delegate:
        click.echo(f"  tweak it with: awewarm config set {new_id} ...")
        return
    _delegate_remote(config, new_id, clone, assume_yes=assume_yes)  # on failure its die() guides; the copy stays local
    conn["enabled"] = False
    save_config(config)
    if conn.get("location") == "remote":  # a delegated original keeps ticking server-side
        _push_edits_to_remote(config, load_state(), conn_id)
    click.echo(f"✓ {conn_id} disabled — the remote copy ticks it now")
    click.echo(f"  rollback: awewarm config set {conn_id} --on, then drop the copy: awewarm config remove {new_id}")


def _takeback_remote(config, state, conn_id, conn):
    """Resume local scheduling (`config set <id> --local`), pulling server truth."""
    if conn.get("location") != "remote":
        return
    url = remote.remote_url(config)
    try:
        view = remote.ensure_session(config)
        server_state = (view.get("connections") or {}).get(conn_id, {}).get("state")
        remote.delete_connection(url, remote.load_token(), conn_id)
    except remote.RemoteError as exc:
        die(f"could not take {conn_id} back — it stays delegated:\n{exc}")
    if server_state:
        local = conn_state(state, conn_id)
        local.clear()
        local.update(server_state)
    conn.pop("location", None)
    resolve_connection(conn, config)  # local again: unpinned fields follow global/local defaults
    (state.get("pendingPush") or {}).pop(conn_id, None)
    click.echo(f"✓ {conn_id} back on local scheduling (server state pulled)")


def _show_settings(config, conn_id, conn):
    fixed = conn["schedule"].get("fixed") or {}
    window = conn["window"]
    duration = f"{window['durationMinutes']} minutes, {window['status']}" if window.get("durationMinutes") else "unknown"
    wake = conn["schedule"].get("wakeWhenAsleep", False)
    location = conn.get("location", "local")
    where = f" ({remote.remote_url(config)})" if location == "remote" else ""
    own_schedule = (conn.get("settings") or {}).get("schedule") or {}
    # mode is always written on every connection (self-documenting file) —
    # only the fields beyond it count as real own overrides
    own_fields = sorted(set(own_schedule) - {"mode"})
    if own_fields:
        source = f"own overrides ({', '.join(own_fields)})"
    elif location == "remote":
        source = "remote defaults (a delegated connection never follows the global schedule)"
    else:
        source = "local defaults → global"
    click.echo(f"Settings for {conn_id}:")
    click.echo(f"  enabled: {'true' if conn.get('enabled', True) else 'false'}")
    click.echo(f"  hidden from status: {'true' if conn.get('hide') else 'false'}")
    click.echo(f"  location: {location}{where}")
    click.echo(f"  mode: {conn['schedule']['mode']}")
    click.echo(f"  fixed times: {', '.join(fixed.get('at') or []) or 'none'} ({fixed.get('days', 'weekday')})")
    click.echo(f"  schedule source: {source}" + (f" — inherit everything with: awewarm config set {conn_id} --inherit-schedule" if own_fields else ""))
    click.echo(f"  window: {duration}")
    catchup = conn.get("catchup") or {}
    click.echo(
        f"  catch-up: {catchup.get('attempts', DEFAULT_CATCHUP_ATTEMPTS)} attempts within "
        f"{catchup.get('withinMinutes', DEFAULT_CATCHUP_MINUTES)} minutes"
    )
    click.echo(f"  degrade after nodes: {conn.get('degradeAfterNodes', DEFAULT_DEGRADE_AFTER_NODES)}")
    click.echo(f"  wake when asleep: {'true' if wake else 'false'} (macOS/Windows only; Linux cannot wake)")
    click.echo(f"change with: awewarm config set {conn_id} --times 06:35,11:40 --mode fixed --no-wake")


def _moved(old, new):
    click.echo(f"note: `awewarm {old}` moved to `awewarm {new}` (legacy alias, removed in v1.0)", err=True)


def _version_callback(ctx, _param, value):
    """What `version_option` does, except the version is computed lazily —
    the editable/git marking must not cost a subprocess on every command."""
    if not value or ctx.resilient_parsing:
        return
    click.echo(display_version())
    ctx.exit()


@click.group(cls=WrapGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--version", is_flag=True, expose_value=False, is_eager=True,
              callback=_version_callback, help="Show the version and exit.")
def cli():
    """Keep AI coding-plan subscription windows warm with minimal requests."""


@cli.command("init")
def init_command():
    """Interactive onboarding: accounts + scheduler."""
    click.echo("Welcome to awewarm.\n")
    click.echo("Scanning local coding accounts...")
    findings = discover.discover_accounts()
    for finding in findings:
        for line in discover.describe_finding(finding):
            click.echo(line)
    click.echo()
    config = load_config()
    state = load_state()
    added = []
    state_changed = False
    for finding in findings:
        if not finding["installed"]:
            continue
        line, anchored = _add_account_flow(config, state, finding)
        if line:
            added.append(line)
            state_changed = state_changed or anchored
    if not added and not config["connections"]:
        click.echo("No manageable local accounts found.")
        click.echo("Add a subscription endpoint instead: awewarm config add")
        return
    save_config(config)
    if state_changed:
        save_state(state)
    for line in added:
        click.echo(line)
    if click.confirm("\nInstall the background scheduler now (runs `awewarm tick` every minute)?", default=True):
        _scheduler_install()
    else:
        click.echo("Scheduler not installed — start it later with: awewarm scheduler install")
    click.echo("\nRun `awewarm status` anytime to see the plan.")


@cli.command("discover")
def discover_command():
    """Scan local CLIs and login state (read-only).

Detects Claude Code / Codex installs and logins."""
    for finding in discover.discover_accounts():
        for line in discover.describe_finding(finding):
            click.echo(line)


@cli.group(cls=WrapGroup)
def config():
    """Manage connections: add, set, remove, show, edit."""


@config.command("add")
def config_add():
    """Add a connection (account or plan).

Offers detected local accounts plus a manual subscription endpoint."""
    _config_add()


class _SetOptions:
    """Flags of one `config set <id>` invocation; None means "leave unchanged".

    Replaces a 14-parameter signature whose legacy call sites passed runs of
    positional Nones — a typo'd field name now raises instead of misbinding.
    """

    FIELDS = (
        "times", "days", "mode", "enabled", "hide", "anchor_hhmm", "start_hhmm",
        "window_minutes", "api_key", "wake", "catchup_minutes",
        "catchup_attempts", "degrade_after_nodes", "location",
        "inherit_schedule", "duplicate", "persist_key",
    )

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self.FIELDS)
        if unknown:
            raise TypeError(f"unknown config set field(s): {', '.join(sorted(unknown))}")
        for field in self.FIELDS:
            setattr(self, field, kwargs.get(field))

    def any(self):
        return any(getattr(self, field) is not None for field in self.FIELDS)

    def overrides(self):
        """Fields that land in the connection's own settings block."""
        return (
            self.times, self.days, self.mode, self.wake, self.inherit_schedule,
            self.window_minutes,
            self.catchup_minutes, self.catchup_attempts, self.degrade_after_nodes,
        )


def _config_set(connection, opts):
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    if opts.duplicate:
        for field in _SetOptions.FIELDS:
            if field not in ("duplicate", "location") and getattr(opts, field) is not None:
                die(f"--duplicate combines only with --remote/--local — tweak the copy itself with: awewarm config set <new-id> ...")
        _config_duplicate(config, conn_id, conn, delegate=opts.location is True, assume_yes=opts.assume_yes)
        return
    slots = []
    if opts.times:
        try:
            slots = _slots_proc(opts.times)
        except ValueError as exc:
            die(str(exc))
    if not opts.any():
        _show_settings(config, conn_id, conn)
        return
    if conn.get("location") == "remote" and opts.anchor_hhmm is not None:
        die(f"{conn_id} is delegated — its state lives on the server\n"
            f"  fix: take it back first: awewarm config set {conn_id} --local")
    if conn.get("location") == "remote" and opts.start_hhmm is not None:
        die(f"{conn_id} is delegated — its state lives on the server\n"
            f"  fix: take it back first: awewarm config set {conn_id} --local")
    if opts.api_key is not None:
        if not opts.api_key.strip() or "\n" in opts.api_key:
            die("--api-key must be a single non-empty line")
        conn.setdefault("auth", {})["apiKeyRef"] = keystore.store_api_key(conn_id, opts.api_key.strip())
    state = load_state()
    state_changed = False
    anchor_now = None
    window_notice = None

    # Schedule and knob edits land in the connection's own settings block,
    # then the resolved fields are recomputed from it and the layers above.
    own = conn.setdefault("settings", {})
    own.setdefault("schedule", {})  # present-even-empty marks "no own overrides"
    if opts.inherit_schedule:
        own["schedule"] = {}
    if slots:
        own["schedule"]["times"] = slots
    if opts.days:
        own["schedule"]["days"] = opts.days
    if opts.mode:
        own["schedule"]["mode"] = opts.mode
    if opts.wake is not None:
        own["wakeWhenAsleep"] = opts.wake
    if opts.catchup_minutes is not None:
        if not 5 <= opts.catchup_minutes <= 240:
            die("--catchup-minutes must be between 5 and 240")
        own["catchupMinutes"] = opts.catchup_minutes
    if opts.catchup_attempts is not None:
        if not 1 <= opts.catchup_attempts <= 10:
            die("--catchup-attempts must be between 1 and 10")
        own["catchupAttempts"] = opts.catchup_attempts
    if opts.degrade_after_nodes is not None:
        if not 1 <= opts.degrade_after_nodes <= 10:
            die("--degrade-after-nodes must be between 1 and 10")
        own["degradeAfterNodes"] = opts.degrade_after_nodes
    if opts.window_minutes is not None:
        if opts.window_minutes <= 0:
            die("--window needs the duration in minutes you verified (greater than 0)")
        window_notice = schedule.window_override_notice(conn["window"], opts.window_minutes)
        own["schedule"]["windowMinutes"] = opts.window_minutes
    if any(value is not None for value in opts.overrides()):
        resolve_connection(conn, config)
    if slots and conn["schedule"]["mode"] != "fixed":
        click.echo(
            f"note: {conn_id} is in {conn['schedule']['mode']} mode — "
            f"these times apply after: awewarm config set {conn_id} --mode fixed"
        )
    if opts.enabled is not None:
        conn["enabled"] = opts.enabled
        if opts.enabled:
            # Resuming is a conscious fresh start: drop the failure ladder,
            # keep schedule memory (anchor, chain, completed slots).
            schedule.migrate_state(conn_state(state, conn_id))
            schedule.reset_ladder(conn_state(state, conn_id))
            state_changed = True
    if opts.hide is not None:
        # Display-only: hidden connections keep their schedule and keep warming;
        # status listings omit them, asking by id still shows them.
        conn["hide"] = opts.hide
    persist_applied = None  # "on"/"off" once a gate approved an actual change
    if opts.persist_key is not None:
        wanted = opts.persist_key == "on"
        if wanted != bool(conn.get("persistKey")):
            prompt, notice, default = (
                ("Store this key on the server's disk?", _persist_on_notice(conn), False)
                if wanted else
                ("Stop storing the key on the server's disk?", PERSIST_KEY_OFF_NOTICE, True)
            )
            if _confirm_persist_change(conn_id, prompt, notice, default, opts.assume_yes):
                if wanted:
                    conn["persistKey"] = True
                else:
                    conn.pop("persistKey", None)
                persist_applied = opts.persist_key
            else:
                click.echo(
                    f"✓ {conn_id} unchanged — its key stays "
                    f"{'on' if conn.get('persistKey') else 'off'} the server's disk"
                )
    if opts.anchor_hhmm is not None:
        window = conn["window"]
        if window.get("status") not in ("verified", "user-confirmed") or not window.get("durationMinutes"):
            die(f"{conn_id}: anchoring needs a known window duration\n"
                f"  fix: run: awewarm config set {conn_id} --window <minutes>")
        if conn["schedule"]["mode"] != "interval":
            die(f"{conn_id}: anchoring only affects interval renewal\n"
                f"  fix: run: awewarm config set {conn_id} --mode interval")
        if not SLOT_RE.match(opts.anchor_hhmm):
            die("use HH:MM, e.g. 13:27")
        anchor_now = _now(config)
        reset_at = schedule.slot_datetime(anchor_now.date(), opts.anchor_hhmm, anchor_now.tzinfo)
        if reset_at is None or reset_at <= anchor_now:
            die("that time already passed today — enter a later time today")
        schedule.apply_user_anchor(conn_state(state, conn_id), conn, reset_at)
        state_changed = True
    if opts.start_hhmm is not None:
        if not SLOT_RE.match(opts.start_hhmm):
            die("use HH:MM, e.g. 08:00")
        start_now = _now(config)
        conn_state(state, conn_id)["deferUntil"] = schedule.iso(
            _next_occurrence(opts.start_hhmm, start_now)
        )
        state_changed = True

    if opts.location is not None:
        if opts.location:
            _delegate_remote(config, conn_id, conn, assume_yes=opts.assume_yes)
        else:
            _takeback_remote(config, state, conn_id, conn)
            state_changed = True

    save_config(config)
    if state_changed:
        save_state(state)
    if slots:
        click.echo(f"✓ Fixed times for {conn_id}: {', '.join(conn['schedule']['fixed']['at'])}")
    if opts.days:
        click.echo(f"✓ Days for {conn_id}: {opts.days}")
    if opts.mode:
        click.echo(f"✓ Mode for {conn_id}: {opts.mode}")
    if opts.enabled is True:
        click.echo(f"✓ {conn_id} enabled (mode: {conn['schedule']['mode']}, failure counters reset)")
    if opts.enabled is False:
        click.echo(f"✓ {conn_id} disabled — resume with: awewarm config set {conn_id} --on")
    if opts.hide is True:
        click.echo(f"✓ {conn_id} hidden from status (warm-ups continue) — unhide with: awewarm config set {conn_id} --show")
    if opts.hide is False:
        click.echo(f"✓ {conn_id} visible in status again")
    if opts.catchup_minutes is not None or opts.catchup_attempts is not None:
        block = conn.get("catchup") or {}
        click.echo(
            f"✓ Catch-up for {conn_id}: {block.get('attempts', DEFAULT_CATCHUP_ATTEMPTS)} attempts within "
            f"{block.get('withinMinutes', DEFAULT_CATCHUP_MINUTES)} minutes"
        )
    if opts.degrade_after_nodes is not None:
        click.echo(f"✓ Degrade after {conn['degradeAfterNodes']} consecutive lost nodes (both rungs)")
    if opts.anchor_hhmm is not None:
        next_due = schedule.parse_ts(conn_state(state, conn_id)["nextDueAt"])
        click.echo(f"✓ {conn_id} anchored — next request at {_fmt_moment(next_due, anchor_now)} (interval)")
    if opts.start_hhmm is not None:
        defer = schedule.parse_ts(conn_state(state, conn_id)["deferUntil"])
        scope = "interval activation" if conn["schedule"]["mode"] == "interval" else "fixed slots"
        click.echo(f"✓ {conn_id} deferred until {_fmt_moment(defer, start_now)} — no {scope} fire before then (clears on first success)")
    if opts.window_minutes is not None:
        click.echo(f"✓ Window recorded as {opts.window_minutes} minutes, user-confirmed.")
        if window_notice:
            click.echo(window_notice)
        click.echo(f"Interval renewal is unlocked — switch modes with: awewarm config set {conn_id} --mode interval")
    if opts.api_key is not None:
        click.echo(f"✓ API key for {conn_id} stored in {keystore.secrets_path()}")
    if opts.wake is not None:
        if opts.wake:
            click.echo(f"✓ {conn_id} may wake a sleeping machine at its fixed slots")
            if sys.platform not in ("darwin", "win32"):
                click.echo("  note: this platform cannot wake a suspended machine — the flag has no effect here")
        else:
            click.echo(f"✓ {conn_id} will not wake a sleeping machine (missed slots catch up on next wake)")
    if opts.inherit_schedule:
        location = conn.get("location", "local")
        layers = "remote defaults" if location == "remote" else ("local" if (config.get("connectionDefaults") or {}).get("local") else "global") + " defaults"
        click.echo(f"✓ {conn_id} dropped its own schedule overrides — it follows {layers}")
    if persist_applied is not None:
        state_word = "persist on the server (plaintext keys.json; survives its restarts)" if persist_applied == "on" \
            else "no longer persist on the server (a restart while this machine is offline holds its warm-ups)"
        click.echo(f"✓ {conn_id}'s key will {state_word}")
    if conn.get("location") == "remote" and opts.location is not True and (
        persist_applied is not None or any(value is not None for value in (
            opts.times, opts.days, opts.mode, opts.enabled, opts.window_minutes, opts.api_key, opts.wake,
            opts.catchup_minutes, opts.catchup_attempts, opts.degrade_after_nodes, opts.inherit_schedule,
        ))
    ):
        _push_edits_to_remote(config, state, conn_id)
    if any(value is not None for value in (opts.times, opts.days, opts.mode, opts.enabled, opts.wake, opts.start_hhmm, opts.inherit_schedule)):
        _refresh_wake_after_edit()


def _push_edits_to_remote(config, state, conn_id):
    """After editing a delegated connection, bring the server's copy along.

    Unreachable server: the edit stays local and marked pending; the server
    keeps running its old schedule meanwhile — never a guess, never both
    sides ticking.
    """
    try:
        _sync_remote(config, state, force_ids={conn_id})
        click.echo("✓ Pushed to the remote server")
    except remote.RemoteError as exc:
        state.setdefault("pendingPush", {})[conn_id] = schedule.iso(datetime.now().astimezone())
        save_state(state)
        click.echo(
            "⚠ Saved locally, but the remote server is unreachable — it keeps the old schedule\n"
            f"  {exc}\n  rerun when online: awewarm remote push"
        )


@config.command("set")
@click.argument("connection")
@click.option("--times", "times", default=None, metavar="HH:MM,...", help="Fixed activation times, comma- or space-separated, e.g. 06:35,11:40.")
@click.option("--days", type=click.Choice(["weekday", "every-day"]), default=None, help="Which days the fixed times fire.")
@click.option("--mode", type=click.Choice(SCHEDULE_MODES), default=None, help="Switch schedule mode.")
@click.option("--on/--off", "enabled", default=None, help="Enable or disable the connection (--on also resets failure counters).")
@click.option("--hide/--show", "hide", default=None, help="Hide this connection from status listings (its warm-ups continue).")
@click.option("--anchor", "anchor_hhmm", default=None, metavar="HH:MM", help="Anchor renewal to a window open now (its close time today).")
@click.option("--start", "start_hhmm", default=None, metavar="HH:MM", help="Defer activation until this time — today, or tomorrow if passed (fixed and interval).")
@click.option("--window", "window_minutes", type=int, default=None, metavar="MINUTES", help="Record the window duration you verified (unlocks interval).")
@click.option("--api-key", "api_key", default=None, help="Store a new API key in awewarm's secrets file.")
@click.option("--wake/--no-wake", "wake", default=None, help="Let fixed slots wake a sleeping machine (macOS/Windows).")
@click.option("--catchup-minutes", "catchup_minutes", type=int, default=None, metavar="MINUTES", help="Catch-up window after a failed node — overrides the global default (30).")
@click.option("--catchup-attempts", "catchup_attempts", type=int, default=None, metavar="N", help="Max attempts per failed node — overrides the global default (5).")
@click.option("--degrade-after-nodes", "degrade_after_nodes", type=int, default=None, metavar="N", help="Lost nodes before degraded, and again before auto-disabled — overrides the global default (3).")
@click.option("--remote/--local", "location", default=None, help="Delegate this connection to the remote server (--remote) or resume local scheduling (--local).")
@click.option("--inherit-schedule", "inherit_schedule", is_flag=True, default=None, help="Drop this connection's own schedule overrides; it follows the location/global defaults instead.")
@click.option("--duplicate", is_flag=True, default=False, help="Copy this connection under a fresh id (<id>-copy) instead of changing it; with --remote the copy is delegated and this one disabled.")
@click.option("--persist-key", "persist_key", type=click.Choice(["on", "off"]), default=None,
              help="Also store this connection's API key on the server's disk, surviving its restarts (asks to confirm; off is the default and recommended).")
@click.option("--yes", "assume_yes", is_flag=True, default=False, help="Skip the persist-key confirmation prompts (for non-interactive shells).")
def config_set(connection, times, days, mode, enabled, hide, anchor_hhmm, start_hhmm, window_minutes, api_key, wake,
               catchup_minutes, catchup_attempts, degrade_after_nodes, location, inherit_schedule, duplicate,
               persist_key, assume_yes):
    """Show or change one connection's settings.

    With no flags, prints the current settings."""
    options = _SetOptions(
        times=times, days=days, mode=mode, enabled=enabled, hide=hide, anchor_hhmm=anchor_hhmm,
        start_hhmm=start_hhmm, window_minutes=window_minutes, api_key=api_key, wake=wake,
        catchup_minutes=catchup_minutes, catchup_attempts=catchup_attempts,
        degrade_after_nodes=degrade_after_nodes, location=location, inherit_schedule=inherit_schedule,
        duplicate=duplicate or None,  # is_flag defaults to False; _SetOptions speaks None
        persist_key=persist_key,
    )
    options.assume_yes = assume_yes  # a modifier, not an edit: never counts in any()
    _config_set(connection, options)


def _settings_scope_block(config, scope):
    """The settings block one scope edits: the global one, or a location's."""
    if scope == "global":
        return config["settings"]
    return config.setdefault("connectionDefaults", {}).setdefault(scope, {})


def _describe_schedule(block):
    """One line describing a settings block's schedule (what it sets, defaults filled)."""
    schedule = (block or {}).get("schedule") or {}
    filled = {**default_schedule(), **schedule}
    if filled["mode"] == "fixed":
        core = f"fixed at {', '.join(filled['times'])} ({filled['days']})"
    else:
        core = f"interval (window + {filled['graceSeconds']}s grace, {filled['jitterSeconds']}s jitter)"
    wake = (block or {}).get("wakeWhenAsleep", False)
    return f"{core}, wake when asleep: {'true' if wake else 'false'}"


def _show_settings_scope(config, scope):
    """Print the settings layers: all three at once, or just the asked-for scope."""
    defaults = config.get("connectionDefaults") or {}
    s = config["settings"]
    window = (s.get("schedule") or {}).get("windowMinutes")
    window_bits = f"{window} minutes" if window else "unknown (interval locked)"
    knobs_line = (
        f"catch-up: {s['catchupAttempts']} attempts within {s['catchupMinutes']} minutes, "
        f"degrade after nodes: {s['degradeAfterNodes']}\n"
        f"  window: {window_bits}, "
        f"prompt: {s.get('prompt', DEFAULT_PROMPT)!r}, maxTokens: {s.get('maxTokens', DEFAULT_MAX_TOKENS)}\n"
        f"  wake when asleep: {'true' if s.get('wakeWhenAsleep', False) else 'false'}"
    )

    def show(name, block, note):
        click.echo(f"{name}:")
        click.echo(f"  {knobs_line if name == 'Global' else 'knobs: ' + (', '.join(f'{k}={v}' for k, v in (block or {}).items() if k not in ('schedule', 'wakeWhenAsleep')) or 'none set (inherit global)')}")
        if name != "Global" and isinstance((block or {}).get("wakeWhenAsleep"), bool):
            click.echo(f"  wake when asleep: {'true' if block['wakeWhenAsleep'] else 'false'}")
        schedule = (block or {}).get("schedule")
        click.echo(f"  schedule: {_describe_schedule(block) if schedule else 'none set (code defaults: fixed at 06:35, weekday)'}")
        click.echo(f"  {note}")

    if scope in ("local", "remote"):
        note = (
            "applies to delegated connections; the schedule never falls back to the global block"
            if scope == "remote"
            else "overrides the global block for local connections"
        )
        show(scope.capitalize(), defaults.get(scope), note)
        return
    show("Global", config["settings"],
         "every connection inherits the knobs; the schedule reaches local connections only — delegated ones never follow it")
    show("Local", defaults.get("local"), "overrides global for local connections")
    show("Remote", defaults.get("remote"), "delegated connections; the schedule never falls back to the global block")
    click.echo("change with: awewarm config settings --times 06:35,11:40")
    click.echo("            awewarm config settings local|remote --times 09:00 --catchup-minutes 45")


def _after_settings_scope_edit(config, scope, timing_edited, wake_edited, knobs_edited, window_edited):
    """Side effects a settings-layer edit may owe: re-push delegated
    connections whose effective values changed, refresh the wake layer.

    Wake is filed as a knob but is machine-local: it refreshes the wake layer
    and never re-pushes. windowMinutes sits in the schedule block but is a
    plan fact that inherits globally even for delegated connections, so
    editing it re-pushes like a knob."""
    if (timing_edited or wake_edited) and scope in ("global", "local"):
        _refresh_wake_after_edit()
    # Knob edits reach delegated connections wherever they were made; timing
    # edits reach them only through the remote layer — the global schedule is
    # deliberately not part of a delegated connection's chain.
    if scope == "remote" or (scope == "global" and (knobs_edited or window_edited)):
        delegated = sorted(
            cid for cid, conn in config["connections"].items() if conn.get("location") == "remote"
        )
        if delegated:
            state = load_state()
            now = datetime.now().astimezone()
            state.setdefault("pendingPush", {}).update({cid: schedule.iso(now) for cid in delegated})
            save_state(state)
            click.echo(f"✓ Marked {', '.join(delegated)} for re-push: awewarm remote push")


def _config_settings(scope, catchup_minutes, catchup_attempts, degrade_after_nodes,
                     window_minutes, prompt, max_tokens, times, days, mode, wake, reset):
    config = load_config()
    scope = scope or "global"
    if reset:
        if scope == "global":
            config["settings"] = default_settings()
        else:
            config.setdefault("connectionDefaults", {}).pop(scope, None)
        save_config(config)
        click.echo(f"✓ {scope} settings cleared (knobs back to code defaults)" if scope == "global"
                   else f"✓ {scope} settings cleared — those connections inherit the global block")
        _after_settings_scope_edit(
            config, scope,
            timing_edited=True, wake_edited=True, knobs_edited=True, window_edited=True,
        )
        return
    slots = None
    if times:
        try:
            slots = _slots_proc(times)
        except ValueError as exc:
            die(str(exc))
    knobs = (catchup_minutes, catchup_attempts, degrade_after_nodes, prompt, max_tokens)
    schedule_fields = (slots, days, mode, window_minutes)
    if all(value is None for value in knobs + schedule_fields) and wake is None:
        _show_settings_scope(config, scope)
        return
    block = _settings_scope_block(config, scope)
    if catchup_minutes is not None:
        if not 5 <= catchup_minutes <= 240:
            die("--catchup-minutes must be between 5 and 240")
        block["catchupMinutes"] = catchup_minutes
    if catchup_attempts is not None:
        if not 1 <= catchup_attempts <= 10:
            die("--catchup-attempts must be between 1 and 10")
        block["catchupAttempts"] = catchup_attempts
    if degrade_after_nodes is not None:
        if not 1 <= degrade_after_nodes <= 10:
            die("--degrade-after-nodes must be between 1 and 10")
        block["degradeAfterNodes"] = degrade_after_nodes
    if window_minutes is not None:
        if window_minutes <= 0:
            die("--window-minutes must be greater than 0")
        block.setdefault("schedule", {})["windowMinutes"] = window_minutes
    if prompt is not None:
        if not prompt.strip() or "\n" in prompt:
            die("--prompt must be a single non-empty line")
        block["prompt"] = prompt
    if max_tokens is not None:
        if not 1 <= max_tokens <= 1024:
            die("--max-tokens must be between 1 and 1024")
        block["maxTokens"] = max_tokens
    if wake is not None:
        block["wakeWhenAsleep"] = wake
    if any(value is not None for value in schedule_fields):
        sched = block.setdefault("schedule", {})
        if slots is not None:
            sched["times"] = slots
        if days:
            sched["days"] = days
        if mode:
            sched["mode"] = mode
    save_config(config)
    if any(value is not None for value in knobs) or wake is not None or window_minutes is not None:
        if scope == "global":
            click.echo(
                f"✓ {scope} knob defaults: {config['settings']['catchupAttempts']} attempts within "
                f"{config['settings']['catchupMinutes']} minutes, degrade after "
                f"{config['settings']['degradeAfterNodes']} nodes"
            )
        else:
            set_bits = ", ".join(
                f"{k}={v}" for k, v in block.items() if k not in ("schedule", "wakeWhenAsleep")
            )
            if set_bits:
                click.echo(f"✓ {scope} knobs set ({set_bits})")
        if wake is not None:
            click.echo(f"  wake when asleep: {'true' if wake else 'false'}")
        if window_minutes is not None:
            click.echo(
                f"  every connection under {scope} now vouches for a {window_minutes}-minute window — "
                "interval renewal is unlocked for the ones without their own record"
            )
    if any(value is not None for value in schedule_fields):
        click.echo(f"✓ {scope} schedule: {_describe_schedule(block)}")
        if scope == "global":
            click.echo("  reaches local connections only — delegated ones never follow the global schedule")
        if (block.get("schedule") or {}).get("mode") == "interval":
            click.echo("  note: connections without a verified window stay on fixed")
    _after_settings_scope_edit(
        config, scope,
        timing_edited=any(value is not None for value in (slots, days, mode)),
        wake_edited=wake is not None,
        knobs_edited=any(value is not None for value in knobs),
        window_edited=window_minutes is not None,
    )


@config.command("settings")
@click.argument("scope", required=False, type=click.Choice(["global", "local", "remote"]))
@click.option("--catchup-minutes", "catchup_minutes", type=int, default=None, metavar="MINUTES", help="Catch-up window after a failed node (default 30).")
@click.option("--catchup-attempts", "catchup_attempts", type=int, default=None, metavar="N", help="Max attempts per failed node (default 5).")
@click.option("--degrade-after-nodes", "degrade_after_nodes", type=int, default=None, metavar="N", help="Lost nodes before degraded, and again before auto-disabled (default 3).")
@click.option("--window-minutes", "window_minutes", type=int, default=None, metavar="MINUTES", help="Subscription window every connection under this layer vouches for (default: unset — unknown windows keep interval mode locked).")
@click.option("--prompt", "prompt", default=None, help="Warm-up prompt sent to the model (default 'Reply with exactly: ok').")
@click.option("--max-tokens", "max_tokens", type=int, default=None, metavar="N", help="Max tokens per warm-up request (default 4).")
@click.option("--times", default=None, metavar="HH:MM,...", help="Default fixed activation times, comma- or space-separated.")
@click.option("--days", type=click.Choice(["weekday", "every-day"]), default=None, help="Which days the default fixed times fire.")
@click.option("--mode", type=click.Choice(SCHEDULE_MODES), default=None, help="Default schedule mode.")
@click.option("--wake/--no-wake", "wake", default=None, help="Default wake-when-asleep behavior for fixed slots (macOS/Windows).")
@click.option("--reset", is_flag=True, help="Clear this scope's settings (global falls back to code defaults).")
def config_settings(scope, catchup_minutes, catchup_attempts, degrade_after_nodes, window_minutes,
                    prompt, max_tokens, times, days, mode, wake, reset):
    """Show or change the settings layers every connection inherits.

    \b
      awewarm config settings               # show all three layers
      awewarm config settings --times 06:35 # the global layer (knobs reach every
                                            #   connection; the schedule: local only)
      awewarm config settings local ...     # defaults for local connections
      awewarm config settings remote ...    # defaults for delegated connections
                                            #   (their schedule never falls back
                                            #   to the global layer)

    A connection's own settings (`awewarm config set <id> ...`) still win over
    every layer."""
    _config_settings(scope, catchup_minutes, catchup_attempts, degrade_after_nodes,
                     window_minutes, prompt, max_tokens, times, days, mode, wake, reset)


def _config_remove(connection):
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    if not click.confirm(f"Remove '{conn.get('label', conn_id)}' and its stored API key?", default=False):
        click.echo("aborted — nothing removed")
        return
    if conn.get("location") == "remote":
        try:
            remote.ensure_session(config)
            remote.delete_connection(remote.remote_url(config), remote.load_token(), conn_id)
        except remote.RemoteError as exc:
            die(
                f"{conn_id} is delegated and the remote server could not be reached —\n"
                "removing only the local copy would leave it ticking there unmanaged.\n"
                f"{exc}\n"
                f"fix: retry when online, or take it back first: awewarm config set {conn_id} --local"
            )
    del config["connections"][conn_id]
    save_config(config)
    state = load_state()
    state["connections"].pop(conn_id, None)
    save_state(state)
    keystore.delete_api_key(conn_id, (conn.get("auth") or {}).get("apiKeyRef"))
    click.echo(f"✓ {conn_id} removed")
    _refresh_wake_after_edit()


@config.command("remove")
@click.argument("connection")
def config_remove(connection):
    """Delete a connection and its stored key.

Also removes its scheduler state."""
    _config_remove(connection)


@config.command("path")
def config_path_command():
    """Print config, state, and log paths."""
    click.echo(f"config: {config_path()}")
    click.echo(f"secrets: {keystore.secrets_path()}")
    click.echo(f"state:  {state_path()}")
    click.echo(f"log:    {log_path()}")


@config.command("show")
def config_show_command():
    """Print the on-disk config (secrets never live there)."""
    path = config_path()
    if not path.exists():
        die(f"no config at {path} yet\nfix: run: awewarm init")
    click.echo(path.read_text(), nl=False)


@config.command("template")
def config_template_command():
    """Print the reference config shape.

    Use this as a template when adjusting a hand-edited or pre-v3 config by hand."""
    click.echo(CONFIG_TEMPLATE, nl=False)


@config.command("edit")
def config_edit_command():
    """Open config.json in $VISUAL, $EDITOR, or nano."""
    path = config_path()
    if not path.exists():
        die(f"no config at {path} yet\nfix: run: awewarm init")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    if shutil.which(editor.split()[0]) is None:
        click.echo(f"note: editor '{editor}' not found; set $EDITOR to your preferred one")
    click.edit(filename=str(path), editor=editor)
    config = load_config()
    errors = []
    for conn_id, conn in config["connections"].items():
        errors.extend(connection_errors(conn, conn_id))
    if errors:
        click.echo("⚠ config has problems after editing:")
        for error in errors:
            click.echo(f"  {error}")
    else:
        click.echo("✓ config is valid")


# --- backup/restore: everything a new machine needs to become this machine ---

BACKUP_FORMAT = 1
BACKUP_FILES = ("config.json", "secrets.json", "machine-id", "state.json")


def _backup_members():
    """(archive name, target path) for everything a restore needs, in this
    machine's terms — honors the AWEWARM_* path overrides, so a backup (and
    a restore) lands exactly where this awewarm reads and writes."""
    return [
        ("config.json", config_path()),
        ("secrets.json", keystore.secrets_path()),
        ("machine-id", config_path().parent / "machine-id"),
        ("state.json", state_path()),
    ]


@config.command("backup")
@click.option("--output", "output_path", default=None, metavar="PATH",
              help="Where to write the archive (default: awewarm-backup-<timestamp>.tar.gz in the current directory).")
def config_backup_command(output_path):
    """Bundle config, secrets, machine-id, and state into one tar.gz.

    \b
      awewarm config backup                     # ./awewarm-backup-<ts>.tar.gz
      awewarm config backup --output /safe/dir/awewarm.tar.gz

    The archive carries your machine identity (machine-id), so a restore on
    a new machine is the SAME machine to any hub — no new pairing slot. It
    holds API keys and the remote token in plaintext (the file is 0600);
    encrypt it yourself for transit (e.g. gpg)."""
    members = [(name, Path(path)) for name, path in _backup_members() if Path(path).exists()]
    if not any(name == "config.json" for name, _ in members):
        die(f"no config to back up at {config_path()}\nfix: run: awewarm init")
    target = Path(output_path).expanduser() if output_path else Path(
        f"awewarm-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    )
    manifest = {
        "format": BACKUP_FORMAT,
        "createdAt": schedule.iso(datetime.now().astimezone()),
        "awewarmVersion": __version__,
        "files": sorted(name for name, _ in members),
    }
    try:
        with tarfile.open(target, "w:gz") as archive:
            for name, path in members:
                archive.add(path, arcname=name)
            info = tarfile.TarInfo("manifest.json")
            payload = (json.dumps(manifest, indent=2) + "\n").encode()
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    except OSError as exc:
        die(f"could not write {target}\n{exc}")
    os.chmod(target, 0o600)
    click.echo(f"✓ Backup written to {target} (0600)")
    click.echo(f"  contents: {', '.join(name for name, _ in members)}")
    click.echo("  ⚠ it contains your API keys and remote token in PLAINTEXT —")
    click.echo("    keep it somewhere safe; encrypt it yourself for transit (e.g. gpg)")
    click.echo(f"  restore it with: awewarm config restore {target}")


@config.command("restore")
@click.argument("backup_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--force", is_flag=True, default=False, help="Overwrite existing config/secrets/machine-id/state files.")
@click.option("--yes", "assume_yes", is_flag=True, default=False, help="Accept the persisted-key notice without its prompt.")
def config_restore_command(backup_path, force, assume_yes):
    """Unpack a `config backup` archive onto this machine.

    Refuses to touch existing files unless --force. If the backup contains
    connections flagged --persist-key on, restoring re-establishes key
    storage on their servers — that asks (default No; --yes accepts)."""
    _config_restore(Path(backup_path), force, assume_yes)


def _config_restore(backup_path, force, assume_yes):
    try:
        archive = tarfile.open(backup_path, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        die(f"could not read {backup_path}\n{exc}")
    with archive:
        files = {member.name: member for member in archive.getmembers() if member.isfile()}
        allowed = set(BACKUP_FILES) | {"manifest.json"}
        unknown = sorted(set(files) - allowed)
        if unknown:
            die(f"unexpected file(s) in the archive: {', '.join(unknown)}\nthis does not look like an awewarm backup")
        if "manifest.json" not in files:
            die("no manifest inside — not an awewarm backup\nfix: make one with: awewarm config backup")
        try:
            manifest = json.loads(archive.extractfile(files["manifest.json"]).read().decode())
        except ValueError:
            die("the manifest is corrupt — remake the backup with: awewarm config backup")
        if manifest.get("format") != BACKUP_FORMAT:
            die(f"backup format {manifest.get('format')!r} is not what this awewarm understands ({BACKUP_FORMAT})")
        existing = sorted(name for name, path in _backup_members() if Path(path).exists() and name in files)
        if existing and not force:
            die("refusing to overwrite: " + ", ".join(existing) + "\npass --force to overwrite them")
        if "config.json" in files:
            try:
                data = json.loads(archive.extractfile(files["config.json"]).read().decode())
            except ValueError:
                die("the backup's config.json is corrupt — remake the backup")
            flagged = []
            for key, value in (data.get("connections") or {}).items():
                if key in ("local", "remote"):  # v3 nests connections by location
                    flagged.extend(
                        cid for cid, conn in (value or {}).items()
                        if (conn or {}).get("persistKey")
                    )
                elif (value or {}).get("persistKey"):  # a hand-flattened file
                    flagged.append(key)
            if flagged:
                click.echo("this backup persists keys on servers for: " + ", ".join(flagged))
                click.echo(PERSIST_KEY_ON_NOTICE)
                if not assume_yes:
                    if not _stdin_is_interactive():
                        die("pass --yes to accept restoring the persisted-key connections")
                    if not click.confirm("Restore with these keys stored on their servers?", default=False):
                        die("aborted — nothing restored\n(the flag is per connection in the backup's config.json)")
        for name, path in _backup_members():
            if name not in files:
                continue
            payload = archive.extractfile(files[name]).read()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(payload)
            if name == "secrets.json":
                os.chmod(path, 0o600)
    restored = ", ".join(sorted(set(files) - {"manifest.json"}))
    click.echo(f"✓ Restored {restored}")
    if "machine-id" in files:
        click.echo("  machine-id restored — a hub sees this machine as the same one (no new pairing slot)")
    click.echo("  arm the background tick on a new machine: awewarm scheduler install")
    click.echo("  then verify with: awewarm status")


@cli.command("status")
@click.argument("connection", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON (still redacted).")
@click.option("--remote/--local", "location", default=None,
              help="Show only delegated (--remote) or only locally scheduled (--local) connections.")
def status_command(connection, as_json, location):
    """Show connections and what fires next.

    \\b
      awewarm status               everything: local + delegated (server truth)
      awewarm status --remote      delegated only, with the server health line
      awewarm status --local       locally scheduled only
    """
    _show_status(connection, as_json, location)


@cli.command("tick", hidden=True)
def tick_command():
    """One scheduler tick: fire what's currently due, ignore the rest.

Hidden — called by the background scheduler agent (launchd on macOS, Task
Scheduler on Windows, systemd user timer on Linux) once a minute. Not
intended for interactive use; use `awewarm run` to fire manually.
    """
    _tick()


@cli.command("run")
@click.argument("connection", required=False)
@click.option("--force", is_flag=True,
              help="Skip the confirmation prompt.")
@click.option("--reset-due", "reset_due", is_flag=True,
              help="With CONNECTION: reset the interval chain from this run.")
def run_command(connection, force, reset_due):
    """Fire connections immediately, ignoring the schedule.

`awewarm run` is the user-facing "fire now" verb. It does NOT check whether
anything is due — it fires unconditionally. The scheduler tick is a separate
command: `awewarm tick` (called by the background agent every minute, hidden).

By default, prompts for confirmation before sending any real request.
Use --force to skip the prompt (for scripting).

\b
  awewarm run                fire all enabled connections (prompts)
  awewarm run --force        fire all, no prompt
  awewarm run <id>           fire one connection (prompts)
  awewarm run <id> --force   fire one connection, no prompt
  awewarm run <id> --reset-due --force   fire one and reset its interval chain
    """
    if not sys.stdin.isatty() and not force:
        die(
            "awewarm run requires --force when stdin is not a terminal."
        )
    summary = _plan_summary(connection)
    if not force:
        if not click.confirm(f"{summary}. Proceed?", default=False):
            click.echo("Cancelled.")
            return
    if connection is not None:
        _activate_now(connection, reset_due=reset_due)
        return
    _fire_all()


def _maybe_sync_wake(config, state=None):
    """Converge the RTC wake layer (tick tail & post-edit); never fatal.

    A broken wake layer must not break scheduling — the same contract as
    install._maybe_self_heal_job; the next tick retries.
    """
    try:
        install.sync_wake_events(
            config, state if state is not None else load_state(), _now(config)
        )
    except (OSError, subprocess.SubprocessError, ValueError, SystemExit):
        return


def _refresh_wake_after_edit():
    """Keep the installed wake schedule in sync after schedule edits.

    Rewrites the launchd calendar entries / Windows wake tasks when they
    drifted, then converges the RTC wake layer so new times arm before the
    next sleep.
    """
    if sys.platform not in ("darwin", "win32") or not install.scheduler_installed():
        return
    config = load_config()
    if install.refresh_wake(config):
        where = "launchd" if sys.platform == "darwin" else "Task Scheduler"
        click.echo(f"✓ Wake schedule updated ({where})")
    _maybe_sync_wake(config)


def _legacy_pmset_cleanup():
    """Cancel a pmset repeat wake left behind by awewarm < 0.4, if any.

    The calendar entries replaced it; this runs after scheduler
    install/uninstall and after `awewarm self-update`, and is a no-op once the
    state key is gone. A failed cancel keeps the key, so the next of those
    commands retries.
    """
    if sys.platform != "darwin":
        return
    status, spec = install.cancel_wake_schedule()
    if status == "cancelled":
        click.echo("✓ Legacy pmset wake cancelled (superseded by calendar wake)")
    elif status == "failed":
        click.echo(
            "  could not cancel the legacy pmset wake — run manually:\n"
            f"  sudo pmset repeat cancel {install.WAKE_TYPE} {spec['days']} {spec['time']}"
        )


def _stdin_is_interactive():
    """True when a human can be asked (tests patch this to try both paths)."""
    return sys.stdin.isatty()


def _scheduler_install(wake=False):
    local = [
        cid for cid, conn in load_config()["connections"].items()
        if conn.get("enabled", True) and conn.get("location") != "remote"
    ]
    if not local:
        # A machine that delegated everything (or has no connections yet) has
        # nothing for a local scheduler to tick — the remote server does that.
        # Ask before --wake's sudo prompt so the gate is always seen first.
        notice = (
            "no connections for a local scheduler to tick — everything is\n"
            "delegated to the server (or none are configured); the remote\n"
            "server ticks delegated connections itself"
        )
        if not _stdin_is_interactive():
            click.echo(f"note: {notice}")
        elif not click.confirm(f"{notice}\nInstall the scheduler anyway?", default=False):
            click.echo("aborted — nothing installed (re-run when a connection is scheduled locally)")
            return
    target = install.install_scheduler()
    click.echo(f"✓ Scheduler installed: {target}")
    entries = install.calendar_entries(load_config())
    if sys.platform == "darwin":
        if entries:
            times = ", ".join(f"{e['Hour']:02d}:{e['Minute']:02d}" for e in entries)
            click.echo(f"  Calendar wake at {times} — exact-time fire while awake, no sudo")
        if wake:
            install.install_wake_grant()
            _maybe_sync_wake(load_config())
            armed = install.armed_wake_moments(load_state())
            next_note = f", next {armed[0].strftime('%m-%d %H:%M')}" if armed else ""
            click.echo(
                f"  ✓ RTC wakes enabled — the lid-closed machine wakes at every "
                f"slot/renewal ({len(armed)} armed{next_note})"
            )
        elif not install.wake_grant_installed():
            click.echo("  lid-closed sleep: not covered — rerun with --wake (one sudo) to arm RTC wakes")
    elif sys.platform == "win32":
        if entries:
            times = ", ".join(f"{e['Hour']:02d}:{e['Minute']:02d}" for e in entries)
            click.echo(f"  Wake tasks at {times} — fire with the lid closed (Task Scheduler)")
        if wake:
            _maybe_sync_wake(load_config())
            click.echo("  ✓ interval renewals now wake the machine too (one-shot wake tasks)")
    elif sys.platform.startswith("linux"):
        click.echo("  note: Linux cannot wake a suspended machine — missed slots catch up on the next wake")
    click.echo(f"  Tick: every {install.TICK_SECONDS}s — log: {log_path()}")
    _legacy_pmset_cleanup()


def _scheduler_uninstall():
    if install.uninstall_scheduler():
        click.echo("✓ Scheduler removed")
    else:
        click.echo("Scheduler was not installed")
    if sys.platform == "darwin":
        cancelled, total, grant = install.teardown_wake_layer()
        if total:
            mark = "✓" if cancelled == total else "⚠"
            click.echo(f"{mark} RTC wake events cancelled ({cancelled}/{total})")
        if grant:
            click.echo("✓ Wake grant removed (/etc/sudoers.d/awewarm)")
        elif install.wake_grant_installed():
            click.echo("⚠ could not remove the wake grant — run: sudo rm /etc/sudoers.d/awewarm")
    _legacy_pmset_cleanup()


@cli.group(cls=WrapGroup)
def scheduler():
    """Install/uninstall the background scheduler.

The installed agent ticks once a minute."""


@scheduler.command("install")
@click.option(
    "--wake/--no-wake", default=False,
    help="Also wake the lid-closed machine at slot/renewal moments "
         "(macOS: one sudo to grant pmset wake arming; Windows: needs no grant).",
)
def scheduler_install(wake):
    """Install the background scheduler agent."""
    _scheduler_install(wake)


@scheduler.command("uninstall")
def scheduler_uninstall():
    """Remove the background scheduler agent."""
    _scheduler_uninstall()


@cli.group("remote", cls=WrapGroup)
def remote_group():
    """Manage the always-on server that ticks delegated connections.

    The server runs `awewarm serve` (your own box) or `awewarm-hub serve`
    (a shared hub, paired by invite) on any 24/7 machine; this machine
    owns every secret and pushes keys over the wire (the server keeps
    them in RAM only). Delegate per connection with: awewarm config set
    <id> --remote."""


def _plaintext_http_host(url):
    """Hostname when url is plain http://, else None (https is exempt)."""
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname if parsed.scheme == "http" else None


def _host_is_local(host):
    """Loopback, link-local, private-range, or LAN-style name — safe for http."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith(".local")
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _confirm_plaintext_http(url):
    """Refuse silent plain-HTTP pairing with a non-local host unless confirmed.

    The pairing token and every delegated API key cross this connection; over
    public http:// they would be readable on the wire.
    """
    host = _plaintext_http_host(url)
    if host is None or _host_is_local(host):
        return
    if not click.confirm(
        f"{url} uses plain HTTP — the pairing token and any delegated API keys"
        " would cross the network unencrypted.\nContinue anyway?",
        default=False,
    ):
        die("refusing to pair over plain HTTP\nfix: use https:// (e.g. via a cloudflared tunnel), or an http:// address on your LAN")


def _remote_connect(url, token_opt, invite_opt=None):
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        die("server URL must start with http:// or https://")
    _confirm_plaintext_http(url)
    try:
        health = remote.healthz(url)
    except remote.RemoteError as exc:
        die(str(exc))
    if not health.get("ok"):
        die(f"{url} answered, but is not an awewarm server")
    if health.get("hub"):
        _remote_connect_hub(url, token_opt, invite_opt, health)
        return
    token = token_opt or remote.load_token() or remote.generate_token()
    try:
        remote.claim(url, token)
    except remote.RemoteError as exc:
        die(str(exc))
    remote.store_token(token)
    config = load_config()
    config["remote"] = {"url": url, "tokenRef": f"file:{remote.TOKEN_SECRET_ID}"}
    save_config(config)
    click.echo(f"✓ Connected to awewarm {health.get('version')} at {url}")
    click.echo("  Delegate a connection with: awewarm config set <id> --remote")


def _remote_connect_hub(url, token_opt, invite_opt, health):
    """Pair with a multi-tenant hub: reuse a working token or burn an invite."""
    token = token_opt or remote.load_token()
    if token:
        try:
            remote.fetch_state(url, token)
        except remote.RemoteError as exc:
            if "401" not in str(exc):
                die(str(exc))
            click.echo("the stored token was rejected by the hub — pairing with a new invite")
        else:
            remote.store_token(token)
            config = load_config()
            config["remote"] = {"url": url, "tokenRef": f"file:{remote.TOKEN_SECRET_ID}"}
            save_config(config)
            click.echo(f"✓ Connected to awewarm hub {health.get('version')} at {url} (already paired)")
            click.echo("  Delegate a connection with: awewarm config set <id> --remote")
            return
    invite = invite_opt
    if invite is None:
        invite = click.prompt("Invite code from the hub operator").strip()
    try:
        joined = remote.join(url, invite)
    except remote.RemoteError as exc:
        die(str(exc))
    remote.store_token(joined["token"])
    config = load_config()
    config["remote"] = {"url": url, "tokenRef": f"file:{remote.TOKEN_SECRET_ID}"}
    save_config(config)
    click.echo(f"✓ Joined awewarm hub {health.get('version')} at {url} (tenant {joined['tenantId']})")
    click.echo(f"  your token (auto-saved to secrets.json — keep a copy yourself, it is the only way back in):")
    click.echo(f"  {joined['token']}")
    click.echo(f"  reuse a saved token with: awewarm remote connect {url} --token <token>")
    click.echo("  Delegate a connection with: awewarm config set <id> --remote")


@remote_group.command("connect")
@click.argument("url")
@click.option("--token", "token_opt", default=None, help="Pair with this token: claim an `awewarm serve --token` server, or reuse a saved hub token.")
@click.option("--invite", "invite_opt", default=None, help="One-time invite code when the server runs `awewarm-hub serve`.")
def remote_connect_command(url, token_opt, invite_opt):
    """Pair with a server: `awewarm serve` (your own) or `awewarm-hub serve`.

    A shared hub pairs with --invite; the URL and token are stored locally."""
    _remote_connect(url, token_opt, invite_opt)


@remote_group.command("status", hidden=True)
def remote_status_command():
    """Legacy alias: status --remote (removed in v1.0)."""
    _moved("remote status", "status --remote")
    _show_status(None, False, location=True)


def _remote_push(connection):
    config = load_config()
    if not remote.remote_url(config):
        die("no remote server connected\nfix: awewarm remote connect <url>")
    state = load_state()
    force = ()
    if connection:
        conn_id, conn = _find_connection(config, connection)
        if conn.get("location") != "remote":
            die(f"{conn_id} is not delegated\nfix: awewarm config set {conn_id} --remote")
        force = {conn_id}
    if not any(c.get("location") == "remote" for c in config["connections"].values()):
        click.echo("No delegated connections — delegate one with: awewarm config set <id> --remote")
        return
    try:
        pushed, rekeyed = _sync_remote(config, state, force_ids=force)
    except remote.RemoteError as exc:
        die(str(exc))
    save_state(state)
    if pushed:
        click.echo(f"✓ Pushed {', '.join(pushed)} (schedule restarted on the server)")
    if rekeyed:
        click.echo(f"✓ Re-keyed {', '.join(rekeyed)} (keys live in server RAM only)")
    if not pushed and not rekeyed:
        click.echo("✓ Server already in sync")


@remote_group.command("push")
@click.argument("connection", required=False)
def remote_push_command(connection):
    """Re-sync delegated connections to the server (config + keys)."""
    _remote_push(connection)


def _remote_disconnect():
    config = load_config()
    delegated = sorted(cid for cid, c in config["connections"].items() if c.get("location") == "remote")
    if delegated:
        die(
            "still delegated: " + ", ".join(delegated) + "\n"
            "take them back first: awewarm config set <id> --local"
        )
    if not config.get("remote"):
        click.echo("No remote server connected")
        return
    url = remote.remote_url(config)
    token = remote.load_token()
    config.pop("remote", None)
    save_config(config)
    # Best-effort: release the server's claim so another machine can pair. The
    # token stays in secrets.json — it is this machine's pairing identity and
    # makes reconnect work even against a server that kept the old claim.
    released = False
    if url and token:
        try:
            released = bool(remote.release(url, token).get("released"))
        except remote.RemoteError:
            released = False
    if released:
        click.echo("✓ Remote server disconnected — claim released, the pairing token kept for reconnect")
    else:
        click.echo(
            "✓ Remote server disconnected — it kept its claim (offline or an older awewarm),\n"
            "  but the kept pairing token re-pairs it on reconnect"
        )


@remote_group.command("disconnect")
def remote_disconnect_command():
    """Forget the server (refuses while connections are delegated)."""
    _remote_disconnect()


# --- serve: the single-tenant always-on server (hub moved to awewarm-hub) ---

DEFAULT_SERVER_DATA_DIR = "~/.awewarm-server"


@cli.command("hub", hidden=True, context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def hub_command(args):
    """Moved to the separate awewarm-hub package."""
    die(
        "hub commands moved to the separate awewarm-hub package\n"
        "fix: pip install awewarm-hub, then run: awewarm-hub " + " ".join(args)
    )


@cli.command("serve")
@click.option("--data-dir", default=None, show_default="~/.awewarm-server", help="Directory for server config/state/log (never secrets). Defaults to ~/.awewarm-server.")
@click.option("--bind", default="127.0.0.1", show_default=True, help="Address to listen on.")
@click.option("--port", default=8790, show_default=True, type=int, help="Port to listen on (0 picks a free one).")
@click.option("--token", "fixed_token", default=None, help="Require exactly this token instead of the first-connect claim.")
@click.option("--hub", is_flag=True, hidden=True, help="Moved to the separate awewarm-hub package.")
@click.option("--tick-seconds", default=60, show_default=True, type=int, help="Seconds between scheduling passes.")
def serve_command(data_dir, bind, port, fixed_token, hub, tick_seconds):
    """Run the always-on server that ticks delegated connections.

    \b
  awewarm serve                    # token claimed by the first remote connect
  awewarm serve --token awt_...    # fixed token (RAM only)
  awewarm serve --data-dir /data   # keep config/state/log in one place

Expose it safely with a cloudflared tunnel (README → Remote server).
Nothing secret is ever written to disk: API keys live in server RAM and are
re-pushed by the local machine after a restart. Multi-tenant serving (one
server, many invited users) is the separate awewarm-hub package.
    """
    if hub:
        die(
            "multi-tenant serving moved to the separate awewarm-hub package\n"
            "fix: pip install awewarm-hub, then run: awewarm-hub serve"
        )
    from . import server
    server.run(
        data_dir or DEFAULT_SERVER_DATA_DIR, bind=bind, port=port,
        fixed_token=fixed_token, tick_seconds=tick_seconds,
    )


def _self_update(check_only):
    try:
        latest = get_pypi_latest()
    except Exception as exc:
        die(f"failed to check PyPI: {exc}")
    if version_gte(__version__, latest):
        click.echo(f"awewarm is up to date ({__version__}).")
        return
    click.echo(f"Current: {__version__}  Latest: {latest}")
    if check_only:
        return
    if running_from_checkout():
        die("this awewarm runs from a source checkout (pip install -e .) — "
            "update it with: git pull && pip install -e .")

    if Path(sys.prefix, "pyvenv.cfg").exists() and "pipx" in sys.prefix:
        cmd = [shutil.which("pipx") or "pipx", "upgrade", "awewarm"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "awewarm"]

    click.echo(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode == 0:
        if install.scheduler_installed():
            install.install_scheduler()
            click.echo("Scheduler job refreshed to match the new command line.")
        _legacy_pmset_cleanup()
        click.echo("Done. The scheduler picks up the new version on its next tick.")
    else:
        raise SystemExit(result.returncode)


@cli.command("self-update")
@click.option("--check", "check_only", is_flag=True, help="Show versions without updating.")
def self_update_command(check_only):
    """Update awewarm to the latest PyPI release."""
    _self_update(check_only)


# --- Hidden legacy aliases (pre-0.3 command names; removed in v1.0). ---


@cli.command("add", hidden=True)
@click.argument("kind", required=False)
def legacy_add(kind):
    """Legacy alias for `awewarm config add`."""
    _moved("add plan", "config add")
    if kind not in (None, "plan"):
        die(f"unknown add target: {kind}")
    _config_add()


@cli.command("activate", hidden=True)
@click.argument("connection")
def legacy_activate(connection):
    """Legacy alias: run <id>."""
    _moved(f"activate {connection}", f"run {connection}")
    _activate_now(connection)


@cli.command("verify", hidden=True)
@click.argument("connection")
@click.option("--confirm", is_flag=True, help="Send one real request and record its time.")
@click.option("--duration", type=int, default=None, help="Window duration in minutes you verified by hand.")
@click.option("--user-confirm", "user_confirm", is_flag=True, help="Mark the window as user-confirmed (unlocks interval).")
def legacy_verify(connection, confirm, duration, user_confirm):
    """Legacy alias: status <id> / config set --window."""
    _moved("verify", f"status {connection} / config set {connection} --window <minutes>")
    if user_confirm:
        if not duration or duration <= 0:
            die("--user-confirm needs --duration <minutes> (the window length you verified)")
        _config_set(connection, _SetOptions(window_minutes=duration))
        return
    if confirm:
        _activate_now(connection, reset_due=True)
        click.echo(
            "Watch when your plan's window/quota resets, compute the elapsed minutes since\n"
            f"that request, then record it:\n  awewarm config set {connection} --window <minutes>"
        )
        return
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    window = conn["window"]
    click.echo(f"Window status: {window['status']} (evidence: {window['evidence']})")
    if window.get("durationMinutes"):
        click.echo(f"Duration: {window['durationMinutes']} minutes, start rule: {window['startRule']}")
    click.echo(
        "\nTo verify a window manually:\n"
        f"  1. awewarm run {conn_id}   (sends one minimal request)\n"
        "  2. note when the plan's window/quota resets relative to that request\n"
        f"  3. awewarm config set {conn_id} --window <minutes>"
    )


@cli.command("enable", hidden=True)
@click.argument("connection")
@click.option("--mode", type=click.Choice(SCHEDULE_MODES), default=None, help="Switch schedule mode.")
def legacy_enable(connection, mode):
    """Legacy alias: config set <id> --on [--mode M]."""
    _moved(f"enable {connection}", f"config set {connection} --on")
    _config_set(connection, _SetOptions(mode=mode, enabled=True))


@cli.command("anchor", hidden=True)
@click.argument("connection")
@click.option("--reset", "reset_hhmm", required=True, help="HH:MM today when the currently-open window closes.")
def legacy_anchor(connection, reset_hhmm):
    """Legacy alias: config set <id> --anchor HH:MM."""
    _moved(f"anchor {connection}", f"config set {connection} --anchor {reset_hhmm}")
    _config_set(connection, _SetOptions(anchor_hhmm=reset_hhmm))


@cli.command("disable", hidden=True)
@click.argument("connection")
def legacy_disable(connection):
    """Legacy alias: config set <id> --off."""
    _moved(f"disable {connection}", f"config set {connection} --off")
    _config_set(connection, _SetOptions(enabled=False))


@cli.command("times", hidden=True)
@click.argument("connection")
@click.argument("times", nargs=-1)
def legacy_times(connection, times):
    """Legacy alias: config set <id> --times HH:MM...."""
    _moved(f"times {connection}", f"config set {connection} --times HH:MM...")
    _config_set(connection, _SetOptions(times=" ".join(times) if times else None))


@cli.command("remove", hidden=True)
@click.argument("connection")
def legacy_remove(connection):
    """Legacy alias: config remove <id>."""
    _moved(f"remove {connection}", f"config remove {connection}")
    _config_remove(connection)


@cli.command("install", hidden=True)
def legacy_install():
    """Legacy alias for `awewarm scheduler install`."""
    _moved("install", "scheduler install")
    _scheduler_install()


@cli.command("uninstall", hidden=True)
def legacy_uninstall():
    """Legacy alias: scheduler uninstall."""
    _moved("uninstall", "scheduler uninstall")
    _scheduler_uninstall()


@cli.command("inspect", hidden=True)
@click.argument("connection", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON (still redacted).")
def legacy_inspect(connection, as_json):
    """Legacy alias: status [<id>] [--json]."""
    _moved("inspect", "status")
    _show_status(connection, as_json)


def main(argv=None):
    """Console entry point; prints an update reminder after interactive commands."""
    args = list(sys.argv[1:] if argv is None else argv)
    get_reminder = check_async(args)
    # The root group takes no options, so args[0] is the subcommand name when
    # one is given; help/version flags may sit at any position.
    command = args[0] if args else None
    bypass_lock = command == "serve" or any(
        arg in ("-h", "--help", "-v", "--version") for arg in args
    )
    guard = nullcontext() if bypass_lock else local_process_lock(timeout_seconds=0 if command == "tick" else 5)
    try:
        try:
            with guard:
                return cli.main(args=args, prog_name="awewarm")
        except LockBusy:
            if command == "tick":
                return None
            raise SystemExit(
                "awewarm: another awewarm command is still running\n"
                "fix: wait for it to finish, then retry"
            )
    finally:
        reminder = get_reminder()
        if reminder:
            click.echo(f"⚠  {reminder}", err=True)
