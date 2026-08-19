#!/usr/bin/env python3
"""awewarm CLI: seven visible commands — init, discover, config, status, run,
scheduler, update. Older command names still work as hidden aliases (removed
in v1.0); the scheduler's `awewarm run --force` invocation is fixed because
installed scheduler agents run it verbatim and self-heal if it's outdated."""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from . import __version__, discover, install, keystore, schedule, transport
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

BASE_URL_EXAMPLES = {
    "openai-chat": "e.g. https://api.openai.com/v1",
    "openai-responses": "e.g. https://api.openai.com/v1",
    "anthropic-messages": "e.g. https://api.anthropic.com",
}


def _base_url_example(transport_kind):
    return BASE_URL_EXAMPLES.get(transport_kind, "e.g. https://your-endpoint/v1")


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


def _resolve_api_key(conn):
    """Secret for subscription connections; None for local-CLI accounts."""
    ref = (conn.get("auth") or {}).get("apiKeyRef")
    if not ref:
        return None
    return keystore.load_api_key(ref)


def _execute_activation(conn, conn_id, cs, now, kind, slot=None, reset_due=True):
    """Send one real request and record the outcome in state."""
    schedule.record_attempt(cs, now)
    api_key = None
    if conn["kind"] == "subscription":
        api_key = _resolve_api_key(conn)
        if api_key is None:
            schedule.record_failure(cs, now, kind, "API key unavailable (secrets file or env)")
            log_event(f"{conn_id} activation ({kind}) failed: API key unavailable")
            return {"ok": False, "detail": "API key unavailable (secrets file or env)"}
    result = transport.send_activation(conn, api_key)
    if result["ok"]:
        schedule.record_success(cs, conn, now, kind, slot, reset_due=reset_due)
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


def _choice_prompt(label, choices, default):
    """Numbered-choice prompt: empty input accepts the default, shown as [default N]."""
    suffix = f"\n[default {default}]" if "\n" in label else f" [default {default}]"
    return click.prompt(
        f"{label}{suffix}",
        type=click.Choice(choices),
        default=default,
        show_default=False,
        show_choices=False,
    )


def _prompt_window_reset(config):
    """Optional HH:MM today when the currently-open window closes (None if not open).

    Lets the chain anchor past a window the user already opened by hand,
    instead of burning an immediate first anchor inside it.
    """
    now = _now(config)

    def parse(value):
        value = value.strip()
        if not value:
            return None
        if not SLOT_RE.match(value):
            raise click.BadParameter("use HH:MM, e.g. 13:27")
        moment = schedule.slot_datetime(now.date(), value, now.tzinfo)
        if moment <= now:
            raise click.BadParameter("that time already passed today — enter a later time, or leave empty")
        return moment

    return click.prompt(
        "Current window closes at (optional, HH:MM — empty if no window is open)",
        default="", show_default=False, value_proc=parse,
    )


def _prompt_fixed_settings():
    fixed_at = click.prompt(
        "Fixed activation times (one or more, comma-separated)",
        default=DEFAULT_FIXED_AT, value_proc=_slots_proc,
    )
    days_choice = _choice_prompt(
        "Select days\n  1. weekday (Mon-Fri)\n  2. every day", ["1", "2"], "1"
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
        "auth": {"type": "local-cli", "status": auth_status, "apiKeyRef": None},
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


def _plan_connection(conn_id, label, base_url, api_key_ref, plan_url, transport_kind, model, mode, window, fixed_at, days):
    return {
        "label": label,
        "kind": "subscription",
        "enabled": True,
        "auth": {"type": "api-key", "status": "valid", "apiKeyRef": api_key_ref},
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


def _scheduler_hint():
    if install.scheduler_installed():
        click.echo("Scheduler already installed — it will pick this up automatically.")
    else:
        click.echo("Start the scheduler with: awewarm scheduler install")


def _add_account_flow(config, state, finding, confirm_first=True):
    """Interactive prompts that turn one discovered local account into a connection.

    Returns (summary line or None when declined, whether renewal was anchored).
    """
    if not finding["authFound"]:
        hint = "claude auth login" if finding["provider"] == "claude-code" else "codex login"
        click.echo(f"? {finding['label']} has no login yet — run `{hint}` first, then re-run: awewarm config add")
        return None, False
    if confirm_first and not click.confirm(f"Manage {finding['label']} with awewarm?", default=True):
        return None, False
    verified = finding["builtinWindow"]["status"] == "verified"
    mode_choice = _choice_prompt(
        f"Select {finding['label']} warm-up mode\n"
        "  1. hybrid — fixed anchor + interval renewal (recommended)\n"
        "  2. fixed — scheduled times only\n"
        "  3. interval — renew continuously from last success",
        ["1", "2", "3"],
        "1" if verified else "2",
    )
    mode = {"1": "hybrid", "2": "fixed", "3": "interval"}[mode_choice]
    if mode in ("fixed", "hybrid"):
        fixed_at, days = _prompt_fixed_settings()
    else:
        fixed_at, days = [DEFAULT_FIXED_AT], "weekday"
    conn_id = unique_connection_id(config, finding["label"])
    conn = _account_connection(conn_id, finding, mode, fixed_at, days)
    click.echo(f"\nTesting {finding['label']} warm-up (one minimal request)...")
    test = transport.send_activation(conn)
    if test["ok"]:
        click.echo("✓ Activation test passed")
    else:
        click.echo(f"✗ Activation test failed: {test['detail']}")
        if not click.confirm("Save this connection anyway?", default=False):
            click.echo("aborted — nothing was saved")
            return None, False
    config["connections"][conn_id] = conn
    anchored = False
    if mode in ("hybrid", "interval") and verified and click.confirm(
        f"Is {finding['label']}'s window already open right now?", default=False
    ):
        reset_at = _prompt_window_reset(config)
        if reset_at is not None:
            schedule.apply_user_anchor(conn_state(state, conn_id), conn, reset_at)
            anchored = True
            click.echo(f"✓ Renewal anchored: next request after {reset_at.strftime('%H:%M')}")
    return f"✓ {finding['label']} added — mode {mode}, fixed {', '.join(fixed_at)} {days}", anchored


def _add_plan_flow():
    """Interactive flow for a manual subscription endpoint (protocol + URL + key)."""
    label = click.prompt("Plan name")
    click.echo(
        "Protocol:\n  1. OpenAI Chat Completions\n  2. OpenAI Responses\n  3. Anthropic Messages"
    )
    protocol_choice = _choice_prompt("Select protocol", ["1", "2", "3"], "1")
    transport_kind = PROTOCOL_CHOICES[protocol_choice]
    base_url = click.prompt(f"API / plan URL ({_base_url_example(transport_kind)})").strip()
    if not base_url.startswith(("http://", "https://")):
        die("API base URL must start with http:// or https://")
    api_key_input = click.prompt(
        "API key (paste the key, or an env ref like $GLM_API_KEY)", hide_input=True
    ).strip()
    env_ref = keystore.normalize_env_ref(api_key_input)
    if env_ref:
        api_key_ref = env_ref
        api_key = keystore.load_api_key(env_ref)
        if api_key is None:
            die(f"env var {env_ref[2:-1]} is not set in this shell\n"
                f"  fix: export it first, or paste the key itself")
    else:
        if not api_key_input:
            die("API key must not be empty")
        api_key = api_key_input
    model = click.prompt("Model for warm-up requests", value_proc=_nonempty_proc, show_default=False)

    draft = _plan_connection(
        "draft", label, base_url, None, base_url, transport_kind, model,
        "fixed", _unknown_window(), DEFAULT_FIXED_AT, "weekday",
    )
    click.echo("\nTesting endpoint...")
    result = transport.send_activation(draft, api_key)
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
    mode_choice = _choice_prompt("Select warm-up mode", ["1", "2", "3"], "1")
    window = _unknown_window()
    mode = "fixed"
    reset_at = None
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
            verify_result = transport.send_activation(draft, api_key)
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
            f"  awewarm config set {conn_id} --window <minutes>"
        )
    elif mode_choice == "3":
        duration = click.prompt("Window duration in minutes", default=300, value_proc=_positive_int_proc, show_default=True)
        window = {
            "status": "user-confirmed",
            "startRule": "unknown",
            "durationMinutes": duration,
            "evidence": "user-confirmed",
        }
        mode_choice_2 = _choice_prompt(
            "Select mode\n  1. hybrid (recommended)\n  2. interval only", ["1", "2"], "1"
        )
        mode = "hybrid" if mode_choice_2 == "1" else "interval"
        if mode == "hybrid":
            fixed_at, days = _prompt_fixed_settings()
        if click.confirm("Is this plan's window already open right now?", default=False):
            reset_at = _prompt_window_reset(config)
    else:
        fixed_at, days = _prompt_fixed_settings()

    if not env_ref:
        api_key_ref = keystore.store_api_key(conn_id, api_key)
        click.echo(f"✓ API key stored in {keystore.secrets_path()} (chmod 600)")
    else:
        click.echo(
            f"✓ API key referenced from environment ({api_key_ref})\n"
            "  note: the background scheduler only sees variables from the shell that\n"
            "  installed it; re-install the scheduler from a shell where this var is set."
        )
    config["connections"][conn_id] = _plan_connection(
        conn_id, label, base_url, api_key_ref, base_url, transport_kind, model,
        mode, window, fixed_at, days,
    )
    save_config(config)
    if reset_at is not None:
        conn = config["connections"][conn_id]
        schedule.apply_user_anchor(conn_state(state, conn_id), conn, reset_at)
        save_state(state)
        click.echo(f"✓ Renewal anchored: next request after {reset_at.strftime('%H:%M')}")
    click.echo(f"\n✓ {label} added ({conn_id}) in {mode} mode.")
    _refresh_wake_after_edit()
    _scheduler_hint()


def _tick():
    """One scheduler pass over every enabled connection.

    Calls install._maybe_self_heal_job() at the top so an old scheduler job
    (e.g. left over from a pre-`--force` version after a manual `pip install
    --upgrade`) is rewritten on the first tick and the second tick onward
    uses the current command line; on macOS it also heals stale calendar
    wake entries after schedule edits.
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
        errors = connection_errors(conn, conn_id)
        if errors:
            log_event(f"skipping {conn_id}: {errors[0]}")
            click.echo(f"skipping {conn_id}: {errors[0]}")
            continue
        cs = conn_state(state, conn_id)
        for action in schedule.plan_actions(conn, cs, now):
            if action["type"] == "skip-slot":
                schedule.record_skip(cs, now, action["slot"], action["why"])
                skipped += 1
                continue
            reason = action["reason"]
            slot_note = f", slot {action['slot']}" if action.get("slot") else ""
            result = _execute_activation(conn, conn_id, cs, now, reason, action.get("slot"))
            mark = "✓" if result["ok"] else "✗"
            suffix = f" — {result['detail']}" if result["detail"] else ""
            click.echo(f"{mark} activated {conn_id} ({reason}{slot_note}){suffix}")
            activated.append(result["ok"])
        schedule.prune_state(cs, now)
    save_state(state)
    if activated or skipped:
        click.echo(f"{sum(activated)} activated, {len(activated) - sum(activated)} failed, {skipped} slots skipped")
    else:
        click.echo("nothing due")


def _plan_summary(connection):
    """Human description of what `awewarm run` is about to do, for the confirm prompt.

    Computed before the prompt so a user who runs `awewarm run` with nothing due
    doesn't get asked "Proceed? [y/N]" and then told "nothing to do".
    """
    if connection is not None:
        return f"Activate {connection}"
    config = load_config()
    enabled = {
        cid: conn for cid, conn in config["connections"].items()
        if conn.get("enabled", True)
    }
    if not enabled:
        return "No enabled connections"
    state = load_state()
    now = _now(config)
    due = []
    for cid in sorted(enabled):
        cs = conn_state(state, cid)
        actions = schedule.plan_actions(enabled[cid], cs, now)
        if any(a["type"] == "activate" for a in actions):
            due.append(cid)
    if not due:
        return f"No connections due right now (checked {len(enabled)} enabled)"
    if len(due) == 1:
        return f"Activate {due[0]}"
    return f"Activate {', '.join(due)} ({len(due)} connections)"


def _activate_now(target, reset_due=False):
    """Fire one connection immediately, outside its schedule.

    The schedule itself is untouched unless reset_due is set — a manual fire
    must not push the renewal cadence out by a full window.
    """
    config = load_config()
    conn_id, conn = _find_connection(config, target)
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


def _ensure_fixed(conn):
    fixed = conn["schedule"].setdefault("fixed", {})
    fixed.setdefault("days", "weekday")
    fixed.setdefault("catchUpWindowMinutes", DEFAULT_CATCHUP_MINUTES)
    fixed.setdefault("skipIfActivatedWithinMinutes", DEFAULT_SKIP_IF_ACTIVATED_MINUTES)
    return fixed


def _show_settings(conn_id, conn):
    fixed = conn["schedule"].get("fixed") or {}
    window = conn["window"]
    duration = f"{window['durationMinutes']} minutes, {window['status']}" if window.get("durationMinutes") else "unknown"
    click.echo(f"Settings for {conn_id}:")
    click.echo(f"  enabled: {'true' if conn.get('enabled', True) else 'false'}")
    click.echo(f"  mode: {conn['schedule']['mode']}")
    click.echo(f"  fixed times: {', '.join(fixed.get('at') or []) or 'none'} ({fixed.get('days', 'weekday')})")
    click.echo(f"  window: {duration}")
    click.echo(f"change with: awewarm config set {conn_id} --times 06:35 11:40 --mode hybrid")


def _status_block(conn_id, conn, state, now, detailed):
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
        return
    window = conn["window"]
    window_line = window["status"] if window["status"] in ("verified", "user-confirmed") else "unknown"
    if window.get("durationMinutes"):
        window_line = f"{window['durationMinutes']} minutes, {window_line}"
    click.echo(f"  Mode: {conn['schedule']['mode']}" + (" (interval paused after failures)" if degraded else ""))
    click.echo(f"  Window: {window_line}" + (f" (evidence: {window['evidence']})" if detailed else ""))
    if detailed:
        target = conn["transport"].get("baseUrl") or conn["transport"].get("cliCommand")
        click.echo(f"  Transport: {conn['transport']['kind']}" + (f" → {target}" if target else ""))
        click.echo(f"  Kind: {conn['kind']}, model: {conn['activation'].get('model') or 'cli default'}")
        fixed = conn["schedule"].get("fixed") or {}
        click.echo(f"  Fixed times: {', '.join(fixed.get('at') or []) or 'none'} ({fixed.get('days', 'weekday')})")
    last = schedule.parse_ts(cs.get("lastActivationAt"))
    click.echo(f"  Last activation: {_fmt_moment(last, now)}")
    due_at, due_kind = schedule.next_due(conn, cs, now)
    click.echo(f"  Next due: {_fmt_moment(due_at, now)}" + (f" ({due_kind})" if due_at else ""))


def _moved(old, new):
    click.echo(f"note: `awewarm {old}` moved to `awewarm {new}` (legacy alias, removed in v1.0)", err=True)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-v", "--version", message="%(version)s")
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
    if click.confirm("\nInstall the background scheduler now (runs `awewarm run --force` every minute)?", default=True):
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


@cli.group()
def config():
    """Manage connections: add, set, remove, show, edit."""


def _config_add():
    """Interactive add: pick a discovered local account or enter an endpoint."""
    click.echo("Scanning local coding accounts...")
    findings = discover.discover_accounts()
    config = load_config()
    state = load_state()
    candidates = []
    for finding in findings:
        if not finding["installed"]:
            continue
        if not finding["authFound"]:
            hint = "claude auth login" if finding["provider"] == "claude-code" else "codex login"
            click.echo(f"? {finding['label']} has no login yet — run `{hint}` first, then re-run: awewarm config add")
            continue
        candidates.append(finding)
    if not candidates:
        click.echo("No logged-in local accounts found — adding a subscription endpoint.\n")
        _add_plan_flow()
        return
    managed = {conn.get("label") for conn in config["connections"].values()}
    lines = ["Add what?"]
    for index, finding in enumerate(candidates, 1):
        note = " (already managed)" if finding["label"] in managed else ""
        lines.append(f"  {index}. {finding['label']}{note}")
    lines.append(f"  {len(candidates) + 1}. Subscription endpoint (API key)")
    click.echo("\n".join(lines))
    choice = _choice_prompt("Select connection", [str(i) for i in range(1, len(candidates) + 2)], "1")
    if int(choice) <= len(candidates):
        line, anchored = _add_account_flow(config, state, candidates[int(choice) - 1], confirm_first=False)
        if line is None:
            return
        save_config(config)
        if anchored:
            save_state(state)
        click.echo(line)
        _scheduler_hint()
        return
    _add_plan_flow()


@config.command("add")
def config_add():
    """Add a connection (account or plan).

Offers detected local accounts plus a manual subscription endpoint."""
    _config_add()


def _config_set(connection, times, days, mode, enabled, anchor_hhmm, window_minutes, api_key, api_key_env):
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    slots = []
    if times:
        try:
            slots = _slots_proc(times)
        except ValueError as exc:
            die(str(exc))
    if api_key and api_key_env:
        die("use either --api-key or --api-key-env, not both")
    if all(value is None for value in (times, days, mode, enabled, anchor_hhmm, window_minutes, api_key, api_key_env)):
        _show_settings(conn_id, conn)
        return
    if api_key_env:
        env_ref = (
            keystore.normalize_env_ref(api_key_env)
            or (re.fullmatch(r"[A-Z0-9_]+", api_key_env) and "${" + api_key_env + "}")
        )
        if not env_ref:
            die("--api-key-env takes a var name (GLM_API_KEY), $VAR, or ${VAR}")
        conn.setdefault("auth", {})["apiKeyRef"] = env_ref
        api_key_env = env_ref[2:-1]
    if api_key is not None:
        if not api_key.strip() or "\n" in api_key:
            die("--api-key must be a single non-empty line")
        conn.setdefault("auth", {})["apiKeyRef"] = keystore.store_api_key(conn_id, api_key.strip())
    state = load_state()
    state_changed = False
    anchor_now = None
    window_notice = None

    if slots:
        if conn["schedule"]["mode"] not in ("fixed", "hybrid"):
            click.echo(
                f"note: {conn_id} is in {conn['schedule']['mode']} mode — "
                f"these times apply after: awewarm config set {conn_id} --mode fixed|hybrid"
            )
        _ensure_fixed(conn)["at"] = slots
    if days:
        _ensure_fixed(conn)["days"] = days
    if mode:
        conn["schedule"]["mode"] = mode
    if enabled is not None:
        conn["enabled"] = enabled
    if anchor_hhmm is not None:
        window = conn["window"]
        if window.get("status") not in ("verified", "user-confirmed") or not window.get("durationMinutes"):
            die(f"{conn_id}: anchoring needs a known window duration\n"
                f"  fix: run: awewarm config set {conn_id} --window <minutes>")
        if conn["schedule"]["mode"] not in ("interval", "hybrid"):
            die(f"{conn_id}: anchoring only affects interval renewal\n"
                f"  fix: run: awewarm config set {conn_id} --mode hybrid")
        if not SLOT_RE.match(anchor_hhmm):
            die("use HH:MM, e.g. 13:27")
        anchor_now = _now(config)
        reset_at = schedule.slot_datetime(anchor_now.date(), anchor_hhmm, anchor_now.tzinfo)
        if reset_at is None or reset_at <= anchor_now:
            die("that time already passed today — enter a later time today")
        schedule.apply_user_anchor(conn_state(state, conn_id), conn, reset_at)
        state_changed = True
    if window_minutes is not None:
        if window_minutes <= 0:
            die("--window needs the duration in minutes you verified (greater than 0)")
        window_notice = schedule.window_override_notice(conn["window"], window_minutes)
        conn["window"] = {
            "status": "user-confirmed",
            "startRule": conn["window"].get("startRule", "unknown"),
            "durationMinutes": window_minutes,
            "evidence": "user-confirmed",
        }

    save_config(config)
    if state_changed:
        save_state(state)
    if slots:
        click.echo(f"✓ Fixed times for {conn_id}: {', '.join(conn['schedule']['fixed']['at'])}")
    if days:
        click.echo(f"✓ Days for {conn_id}: {days}")
    if mode:
        click.echo(f"✓ Mode for {conn_id}: {mode}")
    if enabled is True:
        click.echo(f"✓ {conn_id} enabled (mode: {conn['schedule']['mode']})")
    if enabled is False:
        click.echo(f"✓ {conn_id} disabled — resume with: awewarm config set {conn_id} --on")
    if anchor_hhmm is not None:
        next_due = schedule.parse_ts(conn_state(state, conn_id)["nextDueAt"])
        click.echo(f"✓ {conn_id} anchored — next request at {_fmt_moment(next_due, anchor_now)} (interval)")
    if window_minutes is not None:
        click.echo(f"✓ Window recorded as {window_minutes} minutes, user-confirmed.")
        if window_notice:
            click.echo(window_notice)
        click.echo(f"Interval renewal is unlocked — switch modes with: awewarm config set {conn_id} --mode hybrid")
    if api_key_env:
        click.echo(f"✓ API key for {conn_id} now referenced from ${{{api_key_env}}}")
    if api_key is not None:
        click.echo(f"✓ API key for {conn_id} stored in {keystore.secrets_path()}")
    if any(value is not None for value in (times, days, mode, enabled)):
        _refresh_wake_after_edit()


@config.command("set")
@click.argument("connection")
@click.option("--times", "times", default=None, metavar="HH:MM,...", help="Fixed activation times, comma- or space-separated, e.g. 06:35,11:40.")
@click.option("--days", type=click.Choice(["weekday", "every-day"]), default=None, help="Which days the fixed times fire.")
@click.option("--mode", type=click.Choice(SCHEDULE_MODES), default=None, help="Switch schedule mode.")
@click.option("--on/--off", "enabled", default=None, help="Enable or disable the connection.")
@click.option("--anchor", "anchor_hhmm", default=None, metavar="HH:MM", help="Anchor renewal to a window open now (its close time today).")
@click.option("--window", "window_minutes", type=int, default=None, metavar="MINUTES", help="Record the window duration you verified (unlocks interval).")
@click.option("--api-key", "api_key", default=None, help="Store a new API key in awewarm's secrets file.")
@click.option("--api-key-env", "api_key_env", default=None, metavar="VAR", help="Reference an env var (e.g. GLM_API_KEY) instead of storing the key.")
def config_set(connection, times, days, mode, enabled, anchor_hhmm, window_minutes, api_key, api_key_env):
    """Show or change one connection's settings.

With no flags, prints the current settings."""
    _config_set(connection, times, days, mode, enabled, anchor_hhmm, window_minutes, api_key, api_key_env)


def _config_remove(connection):
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    if not click.confirm(f"Remove '{conn.get('label', conn_id)}' and its stored API key?", default=False):
        click.echo("aborted — nothing removed")
        return
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


def _show_status(connection, as_json):
    config = load_config()
    if connection:
        _find_connection(config, connection)
        conns = {connection: config["connections"][connection]}
    else:
        conns = config["connections"]
    state = load_state()
    if as_json:
        view = {
            "config": {"version": config["version"], "connections": conns},
            "state": {"connections": {k: state["connections"].get(k) for k in conns}},
            "scheduler": {"installed": install.scheduler_installed()},
        }
        click.echo(json.dumps(transport.redact(view), indent=2))
        return
    if not conns:
        click.echo("No connections yet.\nrun: awewarm init\n or: awewarm config add")
        return
    now = _now(config)
    for conn_id in sorted(conns):
        _status_block(conn_id, conns[conn_id], state, now, detailed=bool(connection))
    click.echo(f"\nScheduler: {'enabled' if install.scheduler_installed() else 'not installed — run: awewarm scheduler install'}")


@cli.command("status")
@click.argument("connection", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON (still redacted).")
def status_command(connection, as_json):
    """Show connections and what fires next."""
    _show_status(connection, as_json)


@cli.command("run")
@click.argument("connection", required=False)
@click.option("--force", is_flag=True,
              help="Skip the confirmation prompt. The background scheduler always uses --force.")
@click.option("--reset-due", "reset_due", is_flag=True,
              help="With CONNECTION: reset the interval chain from this run.")
def run_command(connection, force, reset_due):
    """Fire connections manually or run one scheduler tick.

By default, prompts for confirmation before sending any real request.
The background scheduler calls this with --force every minute.

\b
  awewarm run                activate all connections due right now (prompts)
  awewarm run <id>           activate one connection immediately (prompts)
  awewarm run --force        tick, no prompt (the scheduler uses this)
  awewarm run <id> --force   fire one connection, no prompt
  awewarm run <id> --reset-due --force   fire one connection and reset its interval chain
    """
    if not sys.stdin.isatty() and not force:
        die(
            "awewarm run requires --force when stdin is not a terminal.\n"
            "The background scheduler always uses --force."
        )
    summary = _plan_summary(connection)
    if not force:
        if not click.confirm(f"{summary}. Proceed?", default=False):
            click.echo("Cancelled.")
            return
    if connection is not None:
        _activate_now(connection, reset_due=reset_due)
        return
    _tick()


def _refresh_wake_after_edit():
    """Keep the installed wake machinery in sync after schedule edits.

    Rewrites the launchd calendar entries when they drifted, and quietly
    re-registers the pmset fallback (sudo -n only — never prompt here; the
    manual command is printed when passwordless sudo is unavailable).
    """
    if sys.platform != "darwin" or not install.scheduler_installed():
        return
    config = load_config()
    if install.refresh_wake(config):
        click.echo("✓ Calendar wake updated (launchd)")
    spec = install.build_wake_spec(config)
    if spec is None:
        return
    recorded = (load_state().get("wakeSchedule") or {})
    days, time = spec
    if recorded.get("days") == days and recorded.get("time") == time:
        return
    if install.set_wake_schedule(spec, interactive=False):
        click.echo(f"✓ Wake schedule updated: {days} {time}")
    else:
        click.echo("  pmset fallback wake is stale — update manually:")
        click.echo(f"  {install.manual_wake_command(spec)}")


def _wake_flow(wake):
    """macOS pmset wake schedule: per-connection wakeWhenAsleep + wakeLeadMinutes."""
    if sys.platform != "darwin":
        return
    config = load_config()
    spec = install.build_wake_spec(config)
    if spec is None:
        return
    if wake is not None:
        for conn in config.get("connections", {}).values():
            sched = (conn.get("schedule") or {})
            if conn.get("enabled", True) and sched.get("mode") in ("fixed", "hybrid"):
                if "wakeWhenAsleep" not in sched:
                    sched["wakeWhenAsleep"] = wake
        save_config(config)
    else:
        needs_prompt = False
        for conn in config.get("connections", {}).values():
            sched = (conn.get("schedule") or {})
            if conn.get("enabled", True) and sched.get("mode") in ("fixed", "hybrid"):
                if "wakeWhenAsleep" not in sched:
                    needs_prompt = True
                    break
        if needs_prompt:
            enabled = _wake_confirm_and_set(spec)
            if enabled is not None:
                for conn in config.get("connections", {}).values():
                    sched = (conn.get("schedule") or {})
                    if conn.get("enabled", True) and sched.get("mode") in ("fixed", "hybrid"):
                        if "wakeWhenAsleep" not in sched:
                            sched["wakeWhenAsleep"] = enabled
                save_config(config)
    days, time = spec
    if install.set_wake_schedule(spec):
        click.echo(f"✓ Wake schedule set: {days} {time}")
    else:
        click.echo("  sudo pmset failed — set it manually:")
        click.echo(f"  sudo pmset repeat {install.WAKE_TYPE} {days} {time}")


def _wake_confirm_and_set(spec):
    days, time = spec
    click.echo(
        f"\nmacOS wake guard: schedule {install.WAKE_TYPE} at {time} ({days})\n"
        "  pro:  fixed-time warm fires on schedule even with the lid closed\n"
        "  con:  needs sudo; on battery the Mac may fall back asleep shortly after"
    )
    if not click.confirm("Set this wake schedule?", default=True):
        return False
    return True


def _scheduler_install(wake=None):
    target = install.install_scheduler()
    click.echo(f"✓ Scheduler installed: {target}")
    if sys.platform == "darwin":
        entries = install.calendar_entries(load_config())
        if entries:
            times = ", ".join(f"{e['Hour']:02d}:{e['Minute']:02d}" for e in entries)
            click.echo(f"  Calendar wake at {times} — fires with the lid closed, no sudo")
    click.echo(f"  Tick: every {install.TICK_SECONDS}s — log: {log_path()}")
    _wake_flow(wake)


def _scheduler_uninstall():
    if install.uninstall_scheduler():
        click.echo("✓ Scheduler removed")
    else:
        click.echo("Scheduler was not installed")
    if sys.platform == "darwin":
        status, spec = install.cancel_wake_schedule()
        if status == "cancelled":
            click.echo("✓ Wake schedule cancelled")
        elif status == "changed":
            click.echo("Wake schedule left in place (no longer matches what awewarm set)")
        elif status == "failed":
            click.echo(
                "  could not cancel the wake schedule — run manually:\n"
                f"  sudo pmset repeat cancel {install.WAKE_TYPE} {spec['days']} {spec['time']}"
            )


@cli.group()
def scheduler():
    """Install/uninstall the background scheduler.

The installed agent ticks once a minute."""


@scheduler.command("install")
@click.option("--wake/--no-wake", "wake", default=None, help="macOS only: also schedule a pmset wake so fixed times fire while asleep. Choice is remembered.")
def scheduler_install(wake):
    """Install the background scheduler agent."""
    _scheduler_install(wake)


@scheduler.command("uninstall")
def scheduler_uninstall():
    """Remove the background scheduler agent."""
    _scheduler_uninstall()


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
        click.echo("Done. The scheduler picks up the new version on its next tick.")
    else:
        raise SystemExit(result.returncode)


@cli.command("update")
@click.option("--check", "check_only", is_flag=True, help="Show versions without updating.")
def update_command(check_only):
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
        _config_set(connection, None, None, None, None, None, duration, None, None)
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
    _config_set(connection, None, None, mode, True, None, None, None, None)


@cli.command("anchor", hidden=True)
@click.argument("connection")
@click.option("--reset", "reset_hhmm", required=True, help="HH:MM today when the currently-open window closes.")
def legacy_anchor(connection, reset_hhmm):
    """Legacy alias: config set <id> --anchor HH:MM."""
    _moved(f"anchor {connection}", f"config set {connection} --anchor {reset_hhmm}")
    _config_set(connection, None, None, None, None, reset_hhmm, None, None, None)


@cli.command("disable", hidden=True)
@click.argument("connection")
def legacy_disable(connection):
    """Legacy alias: config set <id> --off."""
    _moved(f"disable {connection}", f"config set {connection} --off")
    _config_set(connection, None, None, None, False, None, None, None, None)


@cli.command("times", hidden=True)
@click.argument("connection")
@click.argument("times", nargs=-1)
def legacy_times(connection, times):
    """Legacy alias: config set <id> --times HH:MM...."""
    _moved(f"times {connection}", f"config set {connection} --times HH:MM...")
    _config_set(connection, " ".join(times) if times else None, None, None, None, None, None, None, None)


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


@cli.command("self-update", hidden=True)
@click.option("--check", "check_only", is_flag=True, help="Show versions without updating.")
def legacy_self_update(check_only):
    """Legacy alias for `awewarm update`."""
    _moved("self-update", "update")
    _self_update(check_only)


def main(argv=None):
    """Console entry point; prints an update reminder after interactive commands."""
    get_reminder = check_async(sys.argv[1:] if argv is None else argv)
    try:
        return cli.main(args=argv, prog_name="awewarm")
    finally:
        reminder = get_reminder()
        if reminder:
            click.echo(f"⚠  {reminder}", err=True)
