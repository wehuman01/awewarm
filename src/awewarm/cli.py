#!/usr/bin/env python3
"""awewarm CLI: interactive onboarding plus the scheduler tick."""
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from . import __version__, discover, install, keychain, schedule, transport
from .update_check import check_async, get_pypi_latest, version_gte
from .config import (
    DEFAULT_CATCHUP_MINUTES,
    DEFAULT_FIXED_AT,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_JITTER_SECONDS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT,
    DEFAULT_SKIP_IF_ACTIVATED_MINUTES,
    SCHEDULE_MODES,
    SLOT_RE,
    config_path,
    conn_state,
    connection_errors,
    die,
    load_config,
    load_state,
    log_path,
    save_config,
    save_state,
    state_path,
    timezone_name,
    unique_connection_id,
)

LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_KEEP_BYTES = 512 * 1024

PROTOCOL_CHOICES = {
    "1": "openai-chat",
    "2": "openai-responses",
    "3": "anthropic-messages",
}


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


def log_event(message):
    """Append one line to the log; best-effort and never fatal."""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            path.write_bytes(path.read_bytes()[-LOG_KEEP_BYTES:])
        with open(path, "a") as handle:
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _find_connection(config, conn_id):
    if conn_id in config["connections"]:
        return conn_id, config["connections"][conn_id]
    known = ", ".join(sorted(config["connections"])) or "none configured"
    die(f"unknown connection: {conn_id}\nknown connections: {known}\nrun: awewarm status")


def _resolve_token(conn):
    """Secret for subscription connections; None for local-CLI accounts."""
    ref = (conn.get("auth") or {}).get("tokenRef")
    if not ref:
        return None
    return keychain.load_token(ref)


def _execute_activation(conn, conn_id, cs, now, kind, slot=None):
    """Send one real request and record the outcome in state."""
    schedule.record_attempt(cs, now)
    token = None
    if conn["kind"] == "subscription":
        token = _resolve_token(conn)
        if token is None:
            schedule.record_failure(cs, now, kind, "token unavailable (keychain or env)")
            log_event(f"{conn_id} activation ({kind}) failed: token unavailable")
            return {"ok": False, "detail": "token unavailable (keychain or env)"}
    result = transport.send_activation(conn, token)
    if result["ok"]:
        schedule.record_success(cs, conn, now, kind, slot)
        log_event(f"{conn_id} activation ({kind}) ok")
    else:
        schedule.record_failure(cs, now, kind, result["detail"])
        log_event(f"{conn_id} activation ({kind}) failed: {result['detail']}")
    return result


def _fmt_moment(moment, now):
    if moment is None:
        return "never"
    if moment.date() == now.date():
        return f"today {moment.strftime('%H:%M')}"
    return moment.strftime("%Y-%m-%d %H:%M")


def _slots_proc(value):
    """Parse one or more comma/space-separated HH:MM times into a sorted list."""
    slots = []
    for part in str(value).replace(",", " ").split():
        if not SLOT_RE.match(part):
            raise ValueError(f"times must look like 06:35 (got {part})")
        if part not in slots:
            slots.append(part)
    if not slots:
        raise ValueError("enter at least one time like 06:35")
    return sorted(slots)


def _nonempty_proc(value):
    text = str(value).strip()
    if not text:
        raise ValueError("enter a value")
    return text


def _positive_int_proc(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("enter a whole number")
    if number <= 0:
        raise ValueError("enter a number greater than 0")
    return number


def _prompt_fixed_settings():
    fixed_at = click.prompt(
        "Fixed activation times (one or more, comma-separated)",
        default=DEFAULT_FIXED_AT, value_proc=_slots_proc,
    )
    days_choice = click.prompt(
        "Days\n  1. weekday (Mon-Fri)\n  2. every day",
        type=click.Choice(["1", "2"]), default="1", show_choices=False,
    )
    return fixed_at, "weekday" if days_choice == "1" else "every-day"


def _fixed_block(fixed_at, days):
    return {
        "at": list(fixed_at),
        "days": days,
        "catchUpWindowMinutes": DEFAULT_CATCHUP_MINUTES,
        "skipIfActivatedWithinMinutes": DEFAULT_SKIP_IF_ACTIVATED_MINUTES,
    }


def _interval_block():
    return {"graceSeconds": DEFAULT_GRACE_SECONDS, "jitterSeconds": DEFAULT_JITTER_SECONDS}


def _account_connection(conn_id, finding, mode, fixed_at, days):
    provider = finding["provider"]
    window = dict(finding["builtinWindow"])
    auth_status = "valid" if finding["authFound"] else "unknown"
    return {
        "label": finding["label"],
        "kind": "account",
        "enabled": True,
        "auth": {"type": "local-cli", "status": auth_status, "tokenRef": None},
        "transport": {
            "kind": discover.PROVIDER_TRANSPORTS[provider],
            "baseUrl": None,
            "cliCommand": finding.get("cliPath") or finding["cliCommand"],
        },
        "plan": {"url": None, "label": None},
        "window": window,
        "activation": {
            "model": discover.PROVIDER_MODELS[provider],
            "prompt": DEFAULT_PROMPT,
            "maxTokens": DEFAULT_MAX_TOKENS,
        },
        "schedule": {
            "mode": mode,
            "fixed": _fixed_block(fixed_at, days),
            "interval": _interval_block(),
        },
    }


def _plan_connection(conn_id, label, base_url, token_ref, plan_url, transport_kind, model, mode, window, fixed_at, days):
    return {
        "label": label,
        "kind": "subscription",
        "enabled": True,
        "auth": {"type": "api-token", "status": "valid", "tokenRef": token_ref},
        "transport": {"kind": transport_kind, "baseUrl": base_url, "cliCommand": None},
        "plan": {"url": plan_url or None, "label": label},
        "window": window,
        "activation": {
            "model": model,
            "prompt": DEFAULT_PROMPT,
            "maxTokens": DEFAULT_MAX_TOKENS,
        },
        "schedule": {
            "mode": mode,
            "fixed": _fixed_block(fixed_at, days),
            "interval": _interval_block(),
        },
    }


def _unknown_window():
    return {
        "status": "unknown",
        "startRule": "unknown",
        "durationMinutes": None,
        "evidence": "none",
    }


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-v", "--version", message="%(version)s")
def cli():
    """Keep AI coding-plan subscription windows warm with minimal requests."""


@cli.command()
def init():
    """Interactive onboarding: detect local accounts and enable a schedule."""
    click.echo("Welcome to awewarm.\n")
    click.echo("Scanning local coding accounts...")
    findings = discover.discover_accounts()
    for finding in findings:
        for line in discover.describe_finding(finding):
            click.echo(line)
    click.echo()
    config = load_config()
    added = []
    for finding in findings:
        if not finding["installed"]:
            continue
        provider = finding["provider"]
        if not finding["authFound"]:
            hint = "claude auth login" if provider == "claude-code" else "codex login"
            click.echo(f"? {finding['label']} has no login yet — run `{hint}` first, then re-run: awewarm init")
            continue
        if not click.confirm(f"Manage {finding['label']} with awewarm?", default=True):
            continue
        verified = finding["builtinWindow"]["status"] == "verified"
        proposed = "hybrid" if verified else "fixed"
        mode_choice = click.prompt(
            f"{finding['label']} warm-up mode\n"
            "  1. hybrid — fixed anchor + interval renewal (recommended)\n"
            "  2. fixed — scheduled times only\n"
            "  3. interval — renew continuously from last success",
            type=click.Choice(["1", "2", "3"]),
            default="1" if verified else "2",
            show_choices=False,
        )
        mode = {"1": "hybrid", "2": "fixed", "3": "interval"}[mode_choice]
        if mode in ("fixed", "hybrid"):
            fixed_at, days = _prompt_fixed_settings()
        else:
            fixed_at, days = [DEFAULT_FIXED_AT], "weekday"
        conn_id = unique_connection_id(config, finding["label"])
        config["connections"][conn_id] = _account_connection(conn_id, finding, mode, fixed_at, days)
        added.append(f"✓ {finding['label']} added — mode {mode}, fixed {', '.join(fixed_at)} {days}")
    if not added and not config["connections"]:
        click.echo("No manageable local accounts found.")
        click.echo("Add a subscription endpoint instead: awewarm add plan")
        return
    save_config(config)
    for line in added:
        click.echo(line)
    if click.confirm("\nInstall the background scheduler now (runs `awewarm run` every minute)?", default=True):
        plist = install.install_scheduler()
        click.echo(f"✓ Scheduler installed: {plist}")
    else:
        click.echo("Scheduler not installed — start it later with: awewarm install")
    click.echo("\nRun `awewarm status` anytime to see the plan.")


@cli.command("discover")
def discover_command():
    """Scan local Claude Code / Codex CLIs and their login state (read-only)."""
    for finding in discover.discover_accounts():
        for line in discover.describe_finding(finding):
            click.echo(line)


@cli.group()
def add():
    """Add a new connection."""


@add.command()
def plan():
    """Add a subscription endpoint (API base URL + token + protocol)."""
    label = click.prompt("Plan name")
    base_url = click.prompt("API base URL").strip()
    if not base_url.startswith(("http://", "https://")):
        die("API base URL must start with http:// or https://")
    token = click.prompt("Token", hide_input=True).strip()
    if not token:
        die("token must not be empty")
    plan_url = click.prompt("Plan URL (optional, kept as evidence)", default="", show_default=False)
    click.echo(
        "Protocol:\n  1. OpenAI Chat Completions\n  2. OpenAI Responses\n  3. Anthropic Messages"
    )
    protocol_choice = click.prompt(
        "Select", type=click.Choice(["1", "2", "3"]), default="3", show_choices=False
    )
    transport_kind = PROTOCOL_CHOICES[protocol_choice]
    model = click.prompt("Model for warm-up requests", value_proc=_nonempty_proc, show_default=False)

    draft = _plan_connection(
        "draft", label, base_url, None, plan_url, transport_kind, model,
        "fixed", _unknown_window(), DEFAULT_FIXED_AT, "weekday",
    )
    click.echo("\nTesting endpoint...")
    result = transport.send_activation(draft, token)
    if result["ok"]:
        click.echo("✓ Authentication accepted, minimal request supported")
    else:
        click.echo(f"✗ Endpoint test failed: {result['detail']}")
        if not click.confirm("Save this plan anyway?", default=False):
            die("aborted — nothing was saved")

    click.echo(
        "\nSelect warm-up mode:\n"
        "  1. Fixed activation only — safe default\n"
        "  2. Verify interval renewal — send one request and confirm the window manually\n"
        "  3. Configure interval manually — you already know the window duration"
    )
    mode_choice = click.prompt("Select", type=click.Choice(["1", "2", "3"]), default="1", show_choices=False)
    window = _unknown_window()
    mode = "fixed"
    fixed_at, days = [DEFAULT_FIXED_AT], "weekday"
    config = load_config()
    conn_id = unique_connection_id(config, label)
    state = load_state()
    now = _now(config)

    if mode_choice == "2":
        if result["ok"]:
            # The endpoint test already sent one real request; reuse it as
            # the verification anchor instead of sending a second one.
            cs = conn_state(state, conn_id)
            schedule.record_attempt(cs, now)
            schedule.record_success(cs, draft, now, "verify")
            save_state(state)
            click.echo(f"✓ Verification request recorded at {_fmt_moment(now, now)}")
        elif click.confirm("Send the verification request now?", default=True):
            verify_result = transport.send_activation(draft, token)
            if verify_result["ok"]:
                cs = conn_state(state, conn_id)
                schedule.record_attempt(cs, now)
                schedule.record_success(cs, draft, now, "verify")
                save_state(state)
                click.echo(f"✓ Verification request recorded at {_fmt_moment(now, now)}")
            else:
                click.echo(f"✗ Verification request failed: {verify_result['detail']}")
        click.echo(
            "When your plan's window/quota resets, note the elapsed minutes since that\n"
            f"request, then unlock interval renewal with:\n"
            f"  awewarm verify {conn_id} --duration <minutes> --user-confirm"
        )
    elif mode_choice == "3":
        duration = click.prompt("Window duration in minutes", default=300, value_proc=_positive_int_proc, show_default=True)
        window = {
            "status": "user-confirmed",
            "startRule": "unknown",
            "durationMinutes": duration,
            "evidence": "user-confirmed",
        }
        mode_choice_2 = click.prompt(
            "Mode\n  1. hybrid (recommended)\n  2. interval only",
            type=click.Choice(["1", "2"]), default="1", show_choices=False,
        )
        mode = "hybrid" if mode_choice_2 == "1" else "interval"
        if mode == "hybrid":
            fixed_at, days = _prompt_fixed_settings()
    else:
        fixed_at, days = _prompt_fixed_settings()

    token_ref = keychain.store_token(conn_id, token)
    if token_ref.startswith("keychain:"):
        click.echo(f"✓ Token stored in Keychain ({token_ref.split(':', 1)[1]})")
    else:
        token_var = token_ref[2:-1]
        if sys.platform == "win32":
            click.echo(
                "⚠ Keychain unavailable on Windows — token NOT stored on disk.\n"
                f"  Persist it as a user env var (scheduler tasks inherit it):\n"
                f"  setx {token_var} <your-token>"
            )
        else:
            click.echo(
                "⚠ Keychain unavailable — token NOT stored on disk.\n"
                f"  Export it before awewarm runs:\n  export {token_var}=<your-token>"
            )
    config["connections"][conn_id] = _plan_connection(
        conn_id, label, base_url, token_ref, plan_url, transport_kind, model,
        mode, window, fixed_at, days,
    )
    save_config(config)
    click.echo(f"\n✓ {label} added ({conn_id}) in {mode} mode.")
    if install.scheduler_installed():
        click.echo("Scheduler already installed — it will pick this plan up automatically.")
    else:
        click.echo("Start the scheduler with: awewarm install")


@cli.command()
def status():
    """Show a human-readable summary of every connection."""
    config = load_config()
    state = load_state()
    now = _now(config)
    if not config["connections"]:
        click.echo("No connections yet.\nrun: awewarm init\n or: awewarm add plan")
        return
    for conn_id in sorted(config["connections"]):
        conn = config["connections"][conn_id]
        errors = connection_errors(conn, conn_id)
        if not conn.get("enabled", True):
            word = "disabled"
        elif errors:
            word = "invalid"
        else:
            word = "connected"
        cs = state["connections"].get(conn_id) or {}
        degraded = cs.get("intervalDisabledAt") and conn["schedule"]["mode"] in ("interval", "hybrid")
        if degraded and word == "connected":
            word = "degraded"
        click.echo(f"\n{conn.get('label', conn_id)} ({conn_id}) — {word}")
        if errors:
            click.echo(f"  Problem: {errors[0]}")
            continue
        window = conn["window"]
        window_line = window["status"] if window["status"] in ("verified", "user-confirmed") else "unknown"
        if window.get("durationMinutes"):
            window_line = f"{window['durationMinutes']} minutes, {window_line}"
        click.echo(f"  Mode: {conn['schedule']['mode']}" + (" (interval paused after failures)" if degraded else ""))
        click.echo(f"  Window: {window_line}")
        last = schedule.parse_ts(cs.get("lastActivationAt"))
        click.echo(f"  Last activation: {_fmt_moment(last, now)}")
        due_at, due_kind = schedule.next_due(conn, cs, now)
        click.echo(f"  Next due: {_fmt_moment(due_at, now)}" + (f" ({due_kind})" if due_at else ""))
    click.echo(f"\nScheduler: {'enabled' if install.scheduler_installed() else 'not installed — run: awewarm install'}")


@cli.command()
@click.option("--dry-run", "dry_run", is_flag=True, help="Print planned actions without sending anything.")
def run(dry_run):
    """One scheduler tick: fire whatever is due right now (used by the background scheduler)."""
    config = load_config()
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
        errors = connection_errors(conn, conn_id)
        if errors:
            log_event(f"skipping {conn_id}: {errors[0]}")
            click.echo(f"skipping {conn_id}: {errors[0]}")
            continue
        cs = conn_state(state, conn_id)
        for action in schedule.plan_actions(conn, cs, now):
            if action["type"] == "skip-slot":
                if dry_run:
                    click.echo(f"[dry-run] would mark slot {action['slot']} skipped ({action['why']}) for {conn_id}")
                    continue
                schedule.record_skip(cs, now, action["slot"], action["why"])
                skipped += 1
                continue
            reason = action["reason"]
            slot_note = f", slot {action['slot']}" if action.get("slot") else ""
            if dry_run:
                click.echo(f"[dry-run] would activate {conn_id} ({reason}{slot_note})")
                continue
            result = _execute_activation(conn, conn_id, cs, now, reason, action.get("slot"))
            mark = "✓" if result["ok"] else "✗"
            suffix = f" — {result['detail']}" if result["detail"] else ""
            click.echo(f"{mark} activated {conn_id} ({reason}{slot_note}){suffix}")
            activated.append(result["ok"])
        schedule.prune_state(cs, now)
    if not dry_run:
        save_state(state)
    if activated or skipped:
        click.echo(f"{sum(activated)} activated, {len(activated) - sum(activated)} failed, {skipped} slots skipped")
    else:
        click.echo("nothing due")


@cli.command()
@click.argument("connection")
@click.option("--confirm", is_flag=True, help="Actually send the request (it consumes plan quota).")
def activate(connection, confirm):
    """Send one real activation request now."""
    if not confirm:
        die("activation sends a real request that consumes plan quota\nre-run with --confirm to proceed")
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    state = load_state()
    cs = conn_state(state, conn_id)
    result = _execute_activation(conn, conn_id, cs, _now(config), "manual")
    save_state(state)
    if result["ok"]:
        click.echo(f"✓ {conn_id} activated{': ' + result['detail'] if result['detail'] else ''}")
    else:
        die(f"activation failed: {result['detail']}")


@cli.command()
@click.argument("connection")
@click.option("--confirm", is_flag=True, help="Send one real request and record its time.")
@click.option("--duration", type=int, default=None, help="Window duration in minutes you verified by hand.")
@click.option("--user-confirm", "user_confirm", is_flag=True, help="Mark the window as user-confirmed (unlocks interval).")
def verify(connection, confirm, duration, user_confirm):
    """Show window evidence; optionally measure or confirm the window."""
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    window = conn["window"]
    click.echo(f"Window status: {window['status']} (evidence: {window['evidence']})")
    if window.get("durationMinutes"):
        click.echo(f"Duration: {window['durationMinutes']} minutes, start rule: {window['startRule']}")
    if user_confirm:
        if not duration or duration <= 0:
            die("--user-confirm needs --duration <minutes> (the window length you verified)")
        conn["window"] = {
            "status": "user-confirmed",
            "startRule": window.get("startRule", "unknown"),
            "durationMinutes": duration,
            "evidence": "user-confirmed",
        }
        save_config(config)
        click.echo(f"✓ Window recorded as {duration} minutes, user-confirmed.")
        click.echo(f"Interval renewal is unlocked — switch modes with: awewarm enable {conn_id} --mode hybrid")
        return
    if confirm:
        state = load_state()
        cs = conn_state(state, conn_id)
        now = _now(config)
        result = _execute_activation(conn, conn_id, cs, now, "verify")
        save_state(state)
        if result["ok"]:
            click.echo(f"✓ Request sent at {_fmt_moment(now, now)} — recorded.")
        else:
            die(f"request failed: {result['detail']}")
        click.echo(
            "Watch when your plan's window/quota resets, compute the elapsed minutes since\n"
            f"that request, then record it:\n  awewarm verify {conn_id} --duration <minutes> --user-confirm"
        )
        return
    click.echo(
        "\nTo verify a window manually:\n"
        f"  1. awewarm verify {conn_id} --confirm   (sends one minimal request)\n"
        "  2. note when the plan's window/quota resets relative to that request\n"
        f"  3. awewarm verify {conn_id} --duration <minutes> --user-confirm"
    )


@cli.command()
@click.argument("connection")
@click.option("--mode", type=click.Choice(SCHEDULE_MODES), default=None, help="Switch schedule mode.")
def enable(connection, mode):
    """Enable a connection, optionally switching its schedule mode."""
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    conn["enabled"] = True
    if mode:
        conn["schedule"]["mode"] = mode
    save_config(config)
    click.echo(f"✓ {conn_id} enabled (mode: {conn['schedule']['mode']})")


@cli.command()
@click.argument("connection")
def disable(connection):
    """Stop scheduling a connection (config and state are kept)."""
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    conn["enabled"] = False
    save_config(config)
    click.echo(f"✓ {conn_id} disabled — run: awewarm enable {conn_id}")


@cli.command()
@click.argument("connection")
@click.argument("times", nargs=-1)
def times(connection, times):
    """Show or set the fixed activation times, e.g. 06:35 11:40 16:45."""
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    fixed = conn["schedule"].get("fixed") or {}
    if not times:
        current = ", ".join(fixed.get("at") or []) or "none"
        click.echo(f"Fixed times for {conn_id}: {current} ({fixed.get('days', 'weekday')})")
        click.echo(f"Set them with: awewarm times {conn_id} 06:35 11:40 16:45")
        return
    slots = []
    for value in times:
        if not SLOT_RE.match(value):
            die(f"time must look like 06:35 (got {value})")
        if value not in slots:
            slots.append(value)
    if conn["schedule"]["mode"] not in ("fixed", "hybrid"):
        click.echo(
            f"note: {conn_id} is in {conn['schedule']['mode']} mode — "
            f"these times apply after: awewarm enable {conn_id} --mode fixed|hybrid"
        )
    fixed = conn["schedule"].setdefault("fixed", {})
    fixed.setdefault("days", "weekday")
    fixed.setdefault("catchUpWindowMinutes", DEFAULT_CATCHUP_MINUTES)
    fixed.setdefault("skipIfActivatedWithinMinutes", DEFAULT_SKIP_IF_ACTIVATED_MINUTES)
    fixed["at"] = sorted(slots)
    save_config(config)
    click.echo(f"✓ Fixed times for {conn_id}: {', '.join(fixed['at'])}")


@cli.command()
@click.argument("connection")
def remove(connection):
    """Delete a connection, its state, and its stored token."""
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    if not click.confirm(f"Remove '{conn.get('label', conn_id)}' and its stored token?", default=False):
        click.echo("aborted — nothing removed")
        return
    del config["connections"][conn_id]
    save_config(config)
    state = load_state()
    state["connections"].pop(conn_id, None)
    save_state(state)
    keychain.delete_token(conn_id)
    click.echo(f"✓ {conn_id} removed")


@cli.command("install")
def install_cmd():
    """Install the background scheduler agent (tick every minute)."""
    target = install.install_scheduler()
    click.echo(f"✓ Scheduler installed: {target}")
    click.echo(f"  Tick: every {install.TICK_SECONDS}s — log: {log_path()}")


@cli.command()
def uninstall():
    """Remove the background scheduler agent."""
    if install.uninstall_scheduler():
        click.echo("✓ Scheduler removed")
    else:
        click.echo("Scheduler was not installed")


@cli.command("inspect")
@click.argument("connection", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON (still redacted).")
def inspect(connection, as_json):
    """Show detected capabilities and state (secrets never included)."""
    config = load_config()
    if connection:
        _find_connection(config, connection)
        conns = {connection: config["connections"][connection]}
    else:
        conns = config["connections"]
    state = load_state()
    view = {
        "config": {"version": config["version"], "connections": conns},
        "state": {"connections": {k: state["connections"].get(k) for k in conns}},
        "scheduler": {"installed": install.scheduler_installed()},
    }
    if as_json:
        click.echo(json.dumps(transport.redact(view), indent=2))
        return
    for conn_id, conn in conns.items():
        cs = view["state"]["connections"][conn_id] or {}
        click.echo(f"\n{conn.get('label', conn_id)} ({conn_id})")
        click.echo(f"  kind: {conn['kind']}, enabled: {conn.get('enabled', True)}")
        click.echo(f"  transport: {conn['transport']['kind']}" + (f" → {conn['transport'].get('baseUrl')}" if conn['transport'].get("baseUrl") else ""))
        click.echo(f"  window: {conn['window']['status']}, duration: {conn['window'].get('durationMinutes') or 'unknown'}")
        click.echo(f"  mode: {conn['schedule']['mode']}, model: {conn['activation'].get('model') or 'cli default'}")
        click.echo(f"  last activation: {cs.get('lastActivationAt') or 'never'}")
    click.echo(f"\nscheduler: {'installed' if install.scheduler_installed() else 'not installed'}")


@cli.group()
def config():
    """Show where awewarm keeps its files."""


@config.command("path")
def config_path_command():
    """Print config, state, and log paths."""
    click.echo(f"config: {config_path()}")
    click.echo(f"state:  {state_path()}")
    click.echo(f"log:    {log_path()}")


@cli.command("self-update")
@click.option("--check", "check_only", is_flag=True, help="Show versions without updating.")
def self_update_command(check_only):
    """Update awewarm to the latest PyPI release."""
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

    if Path(sys.prefix, "pyvenv.cfg").exists() and "pipx" in sys.prefix:
        cmd = [shutil.which("pipx") or "pipx", "upgrade", "awewarm"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "awewarm"]

    click.echo(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode == 0:
        click.echo("Done. The scheduler tick picks up the new version on its next run.")
    else:
        raise SystemExit(result.returncode)


def main(argv=None):
    """Console entry point; prints an update reminder after interactive commands."""
    get_reminder = check_async(sys.argv[1:] if argv is None else argv)
    try:
        return cli.main(args=argv, prog_name="awewarm")
    finally:
        reminder = get_reminder()
        if reminder:
            click.echo(f"⚠  {reminder}", err=True)
