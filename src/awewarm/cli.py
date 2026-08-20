#!/usr/bin/env python3
"""awewarm CLI: eight visible commands — init, discover, config, status, run,
tick (hidden), scheduler, update. Older command names still work as hidden
aliases (removed in v1.0); the scheduler's `awewarm tick` invocation is fixed
because installed scheduler agents run it verbatim and self-heal if outdated."""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from . import __version__, discover, install, keystore, remote, schedule, transport
from .update_check import check_async, get_pypi_latest, version_gte
from .config import (
    DEFAULT_CATCHUP_ATTEMPTS,
    DEFAULT_CATCHUP_MINUTES,
    DEFAULT_DEGRADE_AFTER_NODES,
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


def _next_occurrence(hhmm, now):
    """Next wall-clock occurrence of HH:MM: today when still ahead, else tomorrow."""
    moment = schedule.slot_datetime(now.date(), hhmm, now.tzinfo)
    if moment is not None and moment > now:
        return moment
    return schedule.slot_datetime(now.date() + timedelta(days=1), hhmm, now.tzinfo)


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
            schedule.record_failure(cs, conn, now, kind, "API key unavailable (secrets file or env)", node=node)
            log_event(f"{conn_id} activation ({kind}) failed: API key unavailable")
            return {"ok": False, "detail": "API key unavailable (secrets file or env)"}
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


def _prompt_fixed_settings(window_minutes=None):
    fixed_at = click.prompt(
        "Fixed activation times (one or more, comma-separated)",
        default=DEFAULT_FIXED_AT, value_proc=_slots_proc,
    )
    used_grid = False
    if window_minutes and len(fixed_at) == 1:
        fixed_at, used_grid = _maybe_expand_day_grid(fixed_at[0], window_minutes)
    days_choice = _choice_prompt(
        "Select days\n  1. weekday (Mon-Fri)\n  2. every day", ["1", "2"],
        "2" if used_grid else "1",
    )
    return fixed_at, "weekday" if days_choice == "1" else "every-day"


def _prompt_wake_when_asleep():
    """Offer wake-from-sleep for fixed slots where the OS supports it (None elsewhere)."""
    if sys.platform == "darwin":
        return click.confirm("Wake the Mac at these times even when it's asleep?", default=True)
    if sys.platform == "win32":
        return click.confirm("Wake the PC at these times even when it's asleep?", default=True)
    return None  # Linux: nothing can wake a suspended machine


def _maybe_expand_day_grid(entered_time, window_minutes):
    """Offer the full-day slot grid; a single entered time rarely covers a day.

    Asks for the plan's daily quota reset first — anchoring the grid there
    minimizes drift; the entered time is the fallback anchor.
    """
    reset_hhmm = click.prompt(
        "Daily quota reset time (optional HH:MM — anchors the grid to it)",
        default="", show_default=False, value_proc=_optional_slot_proc,
    )
    anchor = reset_hhmm or entered_time
    grid = schedule.grid_times(anchor, window_minutes)
    if len(grid) < 2:
        return [entered_time], False
    click.echo(f"  Full-day coverage for a {window_minutes}-min window: {', '.join(grid)}")
    if click.confirm("  Use these times?", default=True):
        return grid, True
    return [entered_time], False


def _optional_slot_proc(value):
    value = value.strip()
    if not value:
        return None
    if not SLOT_RE.match(value):
        raise click.BadParameter("use HH:MM, e.g. 01:14")
    return value


def _optional_positive_int_proc(value):
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        raise click.BadParameter("enter minutes as a whole number, or leave empty")
    if number <= 0:
        raise click.BadParameter("enter a number greater than 0, or leave empty")
    return number


def _fixed_block(fixed_at, days):
    return {
        "at": list(fixed_at),
        "days": days,
        "skipIfActivatedWithinMinutes": DEFAULT_SKIP_IF_ACTIVATED_MINUTES,
    }


def _catchup_block():
    return {"attempts": DEFAULT_CATCHUP_ATTEMPTS, "withinMinutes": DEFAULT_CATCHUP_MINUTES}


def _interval_block():
    return {"graceSeconds": DEFAULT_GRACE_SECONDS, "jitterSeconds": DEFAULT_JITTER_SECONDS}


def _account_connection(conn_id, finding, mode, fixed_at, days, wake_when_asleep):
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
        "catchup": _catchup_block(),
        "degradeAfterNodes": DEFAULT_DEGRADE_AFTER_NODES,
        "schedule": {
            "mode": mode,
            "fixed": _fixed_block(fixed_at, days),
            "interval": _interval_block(),
            "wakeWhenAsleep": wake_when_asleep,
        },
    }


def _plan_connection(conn_id, label, base_url, api_key_ref, plan_url, transport_kind, model, mode, window, fixed_at, days, wake_when_asleep):
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
        "catchup": _catchup_block(),
        "degradeAfterNodes": DEFAULT_DEGRADE_AFTER_NODES,
        "schedule": {
            "mode": mode,
            "fixed": _fixed_block(fixed_at, days),
            "interval": _interval_block(),
            "wakeWhenAsleep": wake_when_asleep,
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
    mode_label = (
        f"Select {finding['label']} warm-up mode\n"
        "  1. fixed — scheduled times (recommended)\n"
        "  2. interval — renew continuously from last success"
    )
    mode_choice = _choice_prompt(mode_label, ["1", "2"] if verified else ["1"], "1")
    mode = "interval" if mode_choice == "2" else "fixed"
    wake = None
    if mode == "fixed":
        fixed_at, days = _prompt_fixed_settings(finding["builtinWindow"].get("durationMinutes"))
        wake = _prompt_wake_when_asleep()
    else:
        fixed_at, days = [DEFAULT_FIXED_AT], "weekday"
    conn_id = unique_connection_id(config, finding["label"])
    conn = _account_connection(conn_id, finding, mode, fixed_at, days, bool(wake))
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
    if mode == "interval" and verified and click.confirm(
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
    api_key = click.prompt("API key", hide_input=True).strip()
    if not api_key:
        die("API key must not be empty")
    model = click.prompt("Model for warm-up requests", value_proc=_nonempty_proc, show_default=False)

    draft = _plan_connection(
        "draft", label, base_url, None, base_url, transport_kind, model,
        "fixed", _unknown_window(), DEFAULT_FIXED_AT, "weekday", True,
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
    wake = None
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
        mode = "interval"
        fixed_at, days = [DEFAULT_FIXED_AT], "weekday"
        if click.confirm("Is this plan's window already open right now?", default=False):
            reset_at = _prompt_window_reset(config)
    else:
        window_minutes = click.prompt(
            "Window duration in minutes (drives the full-day slot grid)",
            default=300, value_proc=_optional_positive_int_proc,
        )
        window = {
            "status": "user-confirmed",
            "startRule": "unknown",
            "durationMinutes": window_minutes,
            "evidence": "user-confirmed",
        }
        click.echo(f"✓ Window recorded as {window_minutes} minutes — interval renewal unlocked")
        fixed_at, days = _prompt_fixed_settings(window_minutes)
        wake = _prompt_wake_when_asleep()

    api_key_ref = keystore.store_api_key(conn_id, api_key)
    click.echo(f"✓ API key stored in {keystore.secrets_path()} (chmod 600)")
    config["connections"][conn_id] = _plan_connection(
        conn_id, label, base_url, api_key_ref, base_url, transport_kind, model,
        mode, window, fixed_at, days, bool(wake),
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
    """One scheduler pass over every enabled connection: fire what's due only.

    Calls install._maybe_self_heal_job() at the top so an old scheduler job
    (e.g. left over from a pre-`tick` version after a manual `pip install
    --upgrade`) is rewritten on the first tick and the second tick onward
    uses the current command line; on macOS it also heals stale calendar
    wake entries after schedule edits.

    This is the body of `awewarm tick`, called by the scheduler every minute.
    Distinct from `_fire_all`, which fires unconditionally regardless of due.
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
            continue  # an awewarm serve process owns its schedule now
        errors = connection_errors(conn, conn_id)
        if errors:
            log_event(f"skipping {conn_id}: {errors[0]}")
            click.echo(f"skipping {conn_id}: {errors[0]}")
            continue
        cs = conn_state(state, conn_id)
        schedule.migrate_state(cs)
        for action in schedule.plan_actions(conn, cs, now):
            if action["type"] == "skip-slot":
                schedule.record_skip(cs, now, action["slot"], action["why"])
                skipped += 1
                if action.get("lost"):
                    schedule.close_lost_node(cs, conn, now, "catch-up window expired")
                continue
            if action["type"] == "node-lost":
                schedule.close_lost_node(cs, conn, now, "catch-up window expired")
                continue
            reason = action["reason"]
            slot_note = f", slot {action['slot']}" if action.get("slot") else ""
            node = schedule.node_for(action, now)
            result = _execute_activation(conn, conn_id, cs, now, reason, action.get("slot"), node=node)
            mark = "✓" if result["ok"] else "✗"
            suffix = f" — {result['detail']}" if result["detail"] else ""
            click.echo(f"{mark} activated {conn_id} ({reason}{slot_note}){suffix}")
            activated.append(result["ok"])
        schedule.prune_state(cs, now)
    _maybe_sync_remote(config, state)
    save_state(state)
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
                remote.remote_url(config), remote.load_token(), conn_id, reset_due=reset_due
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
                result = remote.run_connection(remote.remote_url(config), remote.load_token(), conn_id)
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


def _ensure_fixed(conn):
    fixed = conn["schedule"].setdefault("fixed", {})
    fixed.setdefault("days", "weekday")
    fixed.setdefault("skipIfActivatedWithinMinutes", DEFAULT_SKIP_IF_ACTIVATED_MINUTES)
    return fixed


def _ensure_catchup(conn):
    return conn.setdefault("catchup", {})


# --- remote delegation: the local machine stays the owner of every secret ---


REMOTE_SYNC_EVERY = timedelta(minutes=30)


def _push_timezone(config):
    """IANA name of this machine's zone — fixed slots are wall-clock times."""
    name = timezone_name(config)
    if name:
        return name
    tz = datetime.now().astimezone().tzinfo
    return getattr(tz, "key", "UTC")


def _require_api_key(conn, conn_id):
    api_key = _resolve_api_key(conn)
    if api_key is None:
        raise remote.RemoteError(
            f"{conn_id}: no API key stored locally\n"
            f"  fix: awewarm config set {conn_id} --api-key <key>"
        )
    return api_key


def _sync_remote(config, state, force_ids=()):
    """Bring the server's copy back in line with local truth.

    Re-pushes edited or missing connections (their schedule changed, so the
    server state resets), and re-sends keys the server lost to a restart
    (its state on disk stays — only the RAM keyring was wiped). Returns
    (pushed, rekeyed) connection ids.
    """
    url = remote.remote_url(config)
    token = remote.load_token()
    view = remote.ensure_session(config)
    have = view.get("connections") or {}
    pending = state.get("pendingPush") or {}
    tz = _push_timezone(config)
    pushed, rekeyed, keys = [], [], {}
    for conn_id, conn in sorted(config["connections"].items()):
        if conn.get("location") != "remote":
            continue
        server = have.get(conn_id)
        if conn_id in force_ids or conn_id in pending or server is None:
            remote.push_connection(url, token, conn_id, conn, _require_api_key(conn, conn_id), tz)
            pending.pop(conn_id, None)
            pushed.append(conn_id)
        elif server.get("keyMissing"):
            keys[conn_id] = _require_api_key(conn, conn_id)
            rekeyed.append(conn_id)
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


def _delegate_remote(config, conn_id, conn):
    """Hand one connection to the remote server (`config set <id> --remote`).

    The flag only lands after the server accepted the push — otherwise neither
    side would tick the connection.
    """
    if conn.get("location") == "remote":
        return
    if conn.get("kind") != "subscription":
        die(
            f"{conn_id} is a local CLI account — its login lives on this machine\n"
            "  and cannot run on a server; only subscription connections can be remote"
        )
    if not remote.remote_url(config):
        die("no remote server connected\nfix: awewarm remote connect <url>")
    api_key = _resolve_api_key(conn)
    if api_key is None:
        die(f"{conn_id} has no stored API key\nfix: awewarm config set {conn_id} --api-key <key>")
    try:
        remote.ensure_session(config)
        remote.push_connection(
            remote.remote_url(config), remote.load_token(), conn_id, conn, api_key,
            _push_timezone(config),
        )
    except remote.RemoteError as exc:
        die(f"could not hand {conn_id} to the remote server — it stays local:\n{exc}")
    conn["location"] = "remote"
    click.echo(f"✓ {conn_id} delegated — the server ticks it; the local scheduler skips it from now on")


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
    (state.get("pendingPush") or {}).pop(conn_id, None)
    click.echo(f"✓ {conn_id} back on local scheduling (server state pulled)")


def _show_settings(config, conn_id, conn):
    fixed = conn["schedule"].get("fixed") or {}
    window = conn["window"]
    duration = f"{window['durationMinutes']} minutes, {window['status']}" if window.get("durationMinutes") else "unknown"
    wake = conn["schedule"].get("wakeWhenAsleep", True)
    location = conn.get("location", "local")
    where = f" ({remote.remote_url(config)})" if location == "remote" else ""
    click.echo(f"Settings for {conn_id}:")
    click.echo(f"  enabled: {'true' if conn.get('enabled', True) else 'false'}")
    click.echo(f"  location: {location}{where}")
    click.echo(f"  mode: {conn['schedule']['mode']}")
    click.echo(f"  fixed times: {', '.join(fixed.get('at') or []) or 'none'} ({fixed.get('days', 'weekday')})")
    click.echo(f"  window: {duration}")
    catchup = conn.get("catchup") or {}
    click.echo(
        f"  catch-up: {catchup.get('attempts', DEFAULT_CATCHUP_ATTEMPTS)} attempts within "
        f"{catchup.get('withinMinutes', DEFAULT_CATCHUP_MINUTES)} minutes"
    )
    click.echo(f"  degrade after nodes: {conn.get('degradeAfterNodes', DEFAULT_DEGRADE_AFTER_NODES)}")
    click.echo(f"  wake when asleep: {'true' if wake else 'false'} (macOS/Windows only; Linux cannot wake)")
    click.echo(f"change with: awewarm config set {conn_id} --times 06:35 11:40 --mode fixed --no-wake")


def _status_block(conn_id, conn, state, now, detailed, where=None):
    enabled = conn.get("enabled", True)
    errors = connection_errors(conn, conn_id)
    cs = conn_state(state, conn_id)
    schedule.migrate_state(cs)
    if not enabled:
        word = "disabled"
    elif errors:
        word = "invalid"
    elif cs.get("autoDisabledAt"):
        word = "auto-disabled"
    elif cs.get("degradedAt"):
        word = "degraded"
    elif cs.get("nodeKey") or cs.get("failedNodes", 0) > 0:
        word = "failing"
    else:
        word = "connected"
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
    click.echo(f"  Last activation: {_fmt_moment(last, now)}")
    if cs.get("lastResult") == "failure":
        attempted = schedule.parse_ts(cs.get("lastAttemptAt"))
        detail = cs.get("lastError") or "unknown error"
        click.echo(f"  Last result: failure ({_fmt_moment(attempted, now)}) — {detail}")
    if not enabled:
        click.echo("  Next due: none (disabled)")
        return
    if cs.get("autoDisabledAt"):
        click.echo("  Next due: none (auto-disabled)")
        return
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


def _config_set(connection, times, days, mode, enabled, anchor_hhmm, start_hhmm, window_minutes, api_key, wake,
                catchup_minutes=None, catchup_attempts=None, degrade_after_nodes=None, location=None):
    config = load_config()
    conn_id, conn = _find_connection(config, connection)
    slots = []
    if times:
        try:
            slots = _slots_proc(times)
        except ValueError as exc:
            die(str(exc))
    if all(value is None for value in (
        times, days, mode, enabled, anchor_hhmm, start_hhmm, window_minutes, api_key, wake,
        catchup_minutes, catchup_attempts, degrade_after_nodes, location,
    )):
        _show_settings(config, conn_id, conn)
        return
    if conn.get("location") == "remote" and anchor_hhmm is not None:
        die(f"{conn_id} is delegated — its state lives on the server\n"
            f"  fix: take it back first: awewarm config set {conn_id} --local")
    if conn.get("location") == "remote" and start_hhmm is not None:
        die(f"{conn_id} is delegated — its state lives on the server\n"
            f"  fix: take it back first: awewarm config set {conn_id} --local")
    if api_key is not None:
        if not api_key.strip() or "\n" in api_key:
            die("--api-key must be a single non-empty line")
        conn.setdefault("auth", {})["apiKeyRef"] = keystore.store_api_key(conn_id, api_key.strip())
    state = load_state()
    state_changed = False
    anchor_now = None
    window_notice = None

    if slots:
        if conn["schedule"]["mode"] != "fixed":
            click.echo(
                f"note: {conn_id} is in {conn['schedule']['mode']} mode — "
                f"these times apply after: awewarm config set {conn_id} --mode fixed"
            )
        _ensure_fixed(conn)["at"] = slots
    if days:
        _ensure_fixed(conn)["days"] = days
    if mode:
        conn["schedule"]["mode"] = mode
    if enabled is not None:
        conn["enabled"] = enabled
        if enabled:
            # Resuming is a conscious fresh start: drop the failure ladder,
            # keep schedule memory (anchor, chain, completed slots).
            schedule.migrate_state(conn_state(state, conn_id))
            schedule.reset_ladder(conn_state(state, conn_id))
            state_changed = True
    if wake is not None:
        conn["schedule"]["wakeWhenAsleep"] = wake
    if catchup_minutes is not None or catchup_attempts is not None:
        block = _ensure_catchup(conn)
        overrides = conn.setdefault("settings", {})
        if catchup_minutes is not None:
            if not 5 <= catchup_minutes <= 240:
                die("--catchup-minutes must be between 5 and 240")
            block["withinMinutes"] = catchup_minutes
            overrides["catchupMinutes"] = catchup_minutes
        if catchup_attempts is not None:
            if not 1 <= catchup_attempts <= 10:
                die("--catchup-attempts must be between 1 and 10")
            block["attempts"] = catchup_attempts
            overrides["catchupAttempts"] = catchup_attempts
    if degrade_after_nodes is not None:
        if not 1 <= degrade_after_nodes <= 10:
            die("--degrade-after-nodes must be between 1 and 10")
        conn["degradeAfterNodes"] = degrade_after_nodes
        conn.setdefault("settings", {})["degradeAfterNodes"] = degrade_after_nodes
    if anchor_hhmm is not None:
        window = conn["window"]
        if window.get("status") not in ("verified", "user-confirmed") or not window.get("durationMinutes"):
            die(f"{conn_id}: anchoring needs a known window duration\n"
                f"  fix: run: awewarm config set {conn_id} --window <minutes>")
        if conn["schedule"]["mode"] != "interval":
            die(f"{conn_id}: anchoring only affects interval renewal\n"
                f"  fix: run: awewarm config set {conn_id} --mode interval")
        if not SLOT_RE.match(anchor_hhmm):
            die("use HH:MM, e.g. 13:27")
        anchor_now = _now(config)
        reset_at = schedule.slot_datetime(anchor_now.date(), anchor_hhmm, anchor_now.tzinfo)
        if reset_at is None or reset_at <= anchor_now:
            die("that time already passed today — enter a later time today")
        schedule.apply_user_anchor(conn_state(state, conn_id), conn, reset_at)
        state_changed = True
    if start_hhmm is not None:
        if not SLOT_RE.match(start_hhmm):
            die("use HH:MM, e.g. 08:00")
        if conn["schedule"]["mode"] != "interval":
            die(f"{conn_id}: --start only defers interval activation\n"
                f"  fix: run: awewarm config set {conn_id} --mode interval --start {start_hhmm}")
        start_now = _now(config)
        conn_state(state, conn_id)["deferUntil"] = schedule.iso(
            _next_occurrence(start_hhmm, start_now)
        )
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

    if location is not None:
        if location:
            _delegate_remote(config, conn_id, conn)
        else:
            _takeback_remote(config, state, conn_id, conn)
            state_changed = True

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
        click.echo(f"✓ {conn_id} enabled (mode: {conn['schedule']['mode']}, failure counters reset)")
    if enabled is False:
        click.echo(f"✓ {conn_id} disabled — resume with: awewarm config set {conn_id} --on")
    if catchup_minutes is not None or catchup_attempts is not None:
        block = conn.get("catchup") or {}
        click.echo(
            f"✓ Catch-up for {conn_id}: {block.get('attempts', DEFAULT_CATCHUP_ATTEMPTS)} attempts within "
            f"{block.get('withinMinutes', DEFAULT_CATCHUP_MINUTES)} minutes"
        )
    if degrade_after_nodes is not None:
        click.echo(f"✓ Degrade after {conn['degradeAfterNodes']} consecutive lost nodes (both rungs)")
    if anchor_hhmm is not None:
        next_due = schedule.parse_ts(conn_state(state, conn_id)["nextDueAt"])
        click.echo(f"✓ {conn_id} anchored — next request at {_fmt_moment(next_due, anchor_now)} (interval)")
    if start_hhmm is not None:
        defer = schedule.parse_ts(conn_state(state, conn_id)["deferUntil"])
        click.echo(f"✓ {conn_id} interval deferred until {_fmt_moment(defer, start_now)} — no request fires before then")
    if window_minutes is not None:
        click.echo(f"✓ Window recorded as {window_minutes} minutes, user-confirmed.")
        if window_notice:
            click.echo(window_notice)
        click.echo(f"Interval renewal is unlocked — switch modes with: awewarm config set {conn_id} --mode interval")
    if api_key is not None:
        click.echo(f"✓ API key for {conn_id} stored in {keystore.secrets_path()}")
    if wake is not None:
        if wake:
            click.echo(f"✓ {conn_id} may wake a sleeping machine at its fixed slots")
            if sys.platform not in ("darwin", "win32"):
                click.echo("  note: this platform cannot wake a suspended machine — the flag has no effect here")
        else:
            click.echo(f"✓ {conn_id} will not wake a sleeping machine (missed slots catch up on next wake)")
    if conn.get("location") == "remote" and location is not True and any(value is not None for value in (
        times, days, mode, enabled, window_minutes, api_key,
        catchup_minutes, catchup_attempts, degrade_after_nodes,
    )):
        _push_edits_to_remote(config, state, conn_id)
    if any(value is not None for value in (times, days, mode, enabled, wake)):
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
@click.option("--anchor", "anchor_hhmm", default=None, metavar="HH:MM", help="Anchor renewal to a window open now (its close time today).")
@click.option("--start", "start_hhmm", default=None, metavar="HH:MM", help="Defer interval activation until this time (today, or tomorrow if passed).")
@click.option("--window", "window_minutes", type=int, default=None, metavar="MINUTES", help="Record the window duration you verified (unlocks interval).")
@click.option("--api-key", "api_key", default=None, help="Store a new API key in awewarm's secrets file.")
@click.option("--wake/--no-wake", "wake", default=None, help="Let fixed slots wake a sleeping machine (macOS/Windows).")
@click.option("--catchup-minutes", "catchup_minutes", type=int, default=None, metavar="MINUTES", help="Catch-up window after a failed node — overrides the global default (30).")
@click.option("--catchup-attempts", "catchup_attempts", type=int, default=None, metavar="N", help="Max attempts per failed node — overrides the global default (5).")
@click.option("--degrade-after-nodes", "degrade_after_nodes", type=int, default=None, metavar="N", help="Lost nodes before degraded, and again before auto-disabled — overrides the global default (3).")
@click.option("--remote/--local", "location", default=None, help="Delegate this connection to the remote server (--remote) or resume local scheduling (--local).")
def config_set(connection, times, days, mode, enabled, anchor_hhmm, start_hhmm, window_minutes, api_key, wake,
               catchup_minutes, catchup_attempts, degrade_after_nodes, location):
    """Show or change one connection's settings.

    With no flags, prints the current settings."""
    _config_set(connection, times, days, mode, enabled, anchor_hhmm, start_hhmm, window_minutes, api_key, wake,
                catchup_minutes, catchup_attempts, degrade_after_nodes, location)


def _config_settings(catchup_minutes, catchup_attempts, degrade_after_nodes):
    config = load_config()
    settings = config.setdefault("settings", {})
    if all(value is None for value in (catchup_minutes, catchup_attempts, degrade_after_nodes)):
        click.echo(
            f"catch-up: {settings.get('catchupAttempts', DEFAULT_CATCHUP_ATTEMPTS)} attempts within "
            f"{settings.get('catchupMinutes', DEFAULT_CATCHUP_MINUTES)} minutes"
        )
        click.echo(
            f"degrade after nodes: {settings.get('degradeAfterNodes', DEFAULT_DEGRADE_AFTER_NODES)} "
            "(lost nodes before degraded, and again before auto-disabled)"
        )
        click.echo("defaults for every connection — override one with: awewarm config set <id> --catchup-minutes 45")
        return
    if catchup_minutes is not None:
        if not 5 <= catchup_minutes <= 240:
            die("--catchup-minutes must be between 5 and 240")
        settings["catchupMinutes"] = catchup_minutes
    if catchup_attempts is not None:
        if not 1 <= catchup_attempts <= 10:
            die("--catchup-attempts must be between 1 and 10")
        settings["catchupAttempts"] = catchup_attempts
    if degrade_after_nodes is not None:
        if not 1 <= degrade_after_nodes <= 10:
            die("--degrade-after-nodes must be between 1 and 10")
        settings["degradeAfterNodes"] = degrade_after_nodes
    save_config(config)
    if catchup_minutes is not None or catchup_attempts is not None:
        click.echo(
            f"✓ Catch-up defaults: {settings['catchupAttempts']} attempts within "
            f"{settings['catchupMinutes']} minutes (connections without their own override)"
        )
    if degrade_after_nodes is not None:
        click.echo(f"✓ Degrade after {settings['degradeAfterNodes']} consecutive lost nodes by default (both rungs)")


@config.command("settings")
@click.option("--catchup-minutes", "catchup_minutes", type=int, default=None, metavar="MINUTES", help="Catch-up window after a failed node (default 30).")
@click.option("--catchup-attempts", "catchup_attempts", type=int, default=None, metavar="N", help="Max attempts per failed node (default 5).")
@click.option("--degrade-after-nodes", "degrade_after_nodes", type=int, default=None, metavar="N", help="Lost nodes before degraded, and again before auto-disabled (default 3).")
def config_settings(catchup_minutes, catchup_attempts, degrade_after_nodes):
    """Show or change the tuning knobs every connection inherits.

    With no flags, prints the current defaults."""
    _config_settings(catchup_minutes, catchup_attempts, degrade_after_nodes)


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


def _fetch_remote_view(config, state):
    """Server truth for delegated connections, cached for offline display.

    Returns (view or None, note or None); the note explains stale or missing
    data. Never fatal — status works offline off the last successful sync.
    """
    try:
        view = remote.ensure_session(config)
        state["remoteCache"] = {"fetchedAt": schedule.iso(datetime.now().astimezone()), "server": view}
        return view, None
    except remote.RemoteError as exc:
        cache = state.get("remoteCache") or {}
        if cache.get("server"):
            fetched = schedule.parse_ts(cache.get("fetchedAt"))
            when = _fmt_moment(fetched, datetime.now().astimezone()) if fetched else "an unknown time"
            return cache["server"], f"server unreachable, showing the last sync from {when}"
        return None, f"server unreachable ({exc})"


def _show_status(connection, as_json):
    config = load_config()
    if connection:
        _find_connection(config, connection)
        conns = {connection: config["connections"][connection]}
    else:
        conns = config["connections"]
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
            "scheduler": {"installed": install.scheduler_installed()},
            "remote": {"url": remote.remote_url(config), "server": remote_view, "note": remote_note},
        }
        click.echo(json.dumps(transport.redact(view), indent=2))
        return
    if not conns:
        click.echo("No connections yet.\nrun: awewarm init\n or: awewarm config add")
        return
    now = _now(config)
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
                if entry.get("keyMissing"):
                    click.echo("  ⚠ the server lost its key (restarted?) — rerun: awewarm remote push")
                continue
        _status_block(conn_id, conn, state, now, detailed=bool(connection))
    footer = f"\nScheduler: {'enabled' if install.scheduler_installed() else 'not installed — run: awewarm scheduler install'}"
    if remote.remote_url(config):
        delegated = sum(1 for c in config["connections"].values() if c.get("location") == "remote")
        cached = state.get("remoteCache") or {}
        synced = _fmt_moment(schedule.parse_ts(cached.get("fetchedAt")), now) if cached.get("fetchedAt") else None
        footer += f"\nRemote: {remote.remote_url(config)} ({delegated} delegated"
        footer += f", last sync {synced}" if synced else ""
        footer += ")"
        if remote_note:
            footer += f" — {remote_note}"
    click.echo(footer)


@cli.command("status")
@click.argument("connection", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON (still redacted).")
def status_command(connection, as_json):
    """Show connections and what fires next."""
    _show_status(connection, as_json)


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


def _refresh_wake_after_edit():
    """Keep the installed wake schedule in sync after schedule edits.

    Rewrites the launchd calendar entries / Windows wake tasks when they
    drifted.
    """
    if sys.platform not in ("darwin", "win32") or not install.scheduler_installed():
        return
    if install.refresh_wake(load_config()):
        where = "launchd" if sys.platform == "darwin" else "Task Scheduler"
        click.echo(f"✓ Wake schedule updated ({where})")


def _legacy_pmset_cleanup():
    """Cancel a pmset repeat wake left behind by awewarm < 0.4, if any.

    The calendar entries replaced it; this runs after scheduler
    install/uninstall and after `awewarm update`, and is a no-op once the
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


def _scheduler_install():
    target = install.install_scheduler()
    click.echo(f"✓ Scheduler installed: {target}")
    entries = install.calendar_entries(load_config())
    if sys.platform == "darwin":
        if entries:
            times = ", ".join(f"{e['Hour']:02d}:{e['Minute']:02d}" for e in entries)
            click.echo(f"  Calendar wake at {times} — fires with the lid closed, no sudo")
    elif sys.platform == "win32":
        if entries:
            times = ", ".join(f"{e['Hour']:02d}:{e['Minute']:02d}" for e in entries)
            click.echo(f"  Wake tasks at {times} — fire with the lid closed (Task Scheduler)")
    elif sys.platform.startswith("linux"):
        click.echo("  note: Linux cannot wake a suspended machine — missed slots catch up on the next wake")
    click.echo(f"  Tick: every {install.TICK_SECONDS}s — log: {log_path()}")
    _legacy_pmset_cleanup()


def _scheduler_uninstall():
    if install.uninstall_scheduler():
        click.echo("✓ Scheduler removed")
    else:
        click.echo("Scheduler was not installed")
    _legacy_pmset_cleanup()


@cli.group()
def scheduler():
    """Install/uninstall the background scheduler.

The installed agent ticks once a minute."""


@scheduler.command("install")
def scheduler_install():
    """Install the background scheduler agent."""
    _scheduler_install()


@scheduler.command("uninstall")
def scheduler_uninstall():
    """Remove the background scheduler agent."""
    _scheduler_uninstall()


@cli.group("remote")
def remote_group():
    """Manage the always-on server that ticks delegated connections.

    The server runs `awewarm serve` on any 24/7 machine; this machine owns
    every secret and pushes keys over the wire (the server keeps them in RAM
    only). Delegate per connection with: awewarm config set <id> --remote."""


def _remote_connect(url, token_opt):
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        die("server URL must start with http:// or https://")
    try:
        health = remote.healthz(url)
    except remote.RemoteError as exc:
        die(str(exc))
    if not health.get("ok"):
        die(f"{url} answered, but is not an awewarm server")
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


@remote_group.command("connect")
@click.argument("url")
@click.option("--token", "token_opt", default=None, help="Use this token when the server runs `serve --token`.")
def remote_connect_command(url, token_opt):
    """Pair with an `awewarm serve` process (URL + token stored locally)."""
    _remote_connect(url, token_opt)


def _remote_status():
    config = load_config()
    if not remote.remote_url(config):
        die("no remote server connected\nfix: awewarm remote connect <url>")
    try:
        view = remote.ensure_session(config)
    except remote.RemoteError as exc:
        die(str(exc))
    now = datetime.now().astimezone()
    started = schedule.parse_ts(view.get("startedAt"))
    ticked = schedule.parse_ts(view.get("lastTickAt"))
    click.echo(f"awewarm server {view.get('version')} — up since {_fmt_moment(started, now)}")
    click.echo(f"  last tick: {_fmt_moment(ticked, now)}")
    conns = view.get("connections") or {}
    if not conns:
        click.echo("  no delegated connections — delegate one with: awewarm config set <id> --remote")
        return
    for conn_id, entry in conns.items():
        conn = entry.get("config") or {}
        cs = entry.get("state") or {}
        mode = (conn.get("schedule") or {}).get("mode", "fixed")
        tz_name = conn.get("timezone")
        try:
            conn_now = datetime.now(ZoneInfo(tz_name)) if tz_name else now
        except Exception:
            conn_now = now
        due_at, _ = schedule.next_due(conn, cs, conn_now)
        note = " — key missing, rerun: awewarm remote push" if entry.get("keyMissing") else ""
        click.echo(f"  {conn_id}: {mode}, next due {_fmt_moment(due_at, conn_now)}{note}")


@remote_group.command("status")
def remote_status_command():
    """Show the server's view: uptime, last tick, delegated connections."""
    _remote_status()


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
    config.pop("remote", None)
    save_config(config)
    remote.delete_token()
    click.echo("✓ Remote server forgotten (it keeps nothing secret — its keyring was RAM-only)")


@remote_group.command("disconnect")
def remote_disconnect_command():
    """Forget the server (refuses while connections are delegated)."""
    _remote_disconnect()


@cli.command("serve")
@click.option("--data-dir", default="~/.awewarm-server", show_default=True, help="Directory for server config/state/log (never secrets).")
@click.option("--bind", default="127.0.0.1", show_default=True, help="Address to listen on.")
@click.option("--port", default=8790, show_default=True, type=int, help="Port to listen on (0 picks a free one).")
@click.option("--token", "fixed_token", default=None, help="Require exactly this token instead of the first-connect claim.")
@click.option("--tick-seconds", default=60, show_default=True, type=int, help="Seconds between scheduling passes.")
def serve_command(data_dir, bind, port, fixed_token, tick_seconds):
    """Run the always-on server that ticks delegated connections.

\b
  awewarm serve                    # token claimed by the first remote connect
  awewarm serve --token awt_...    # fixed token (RAM only)
  awewarm serve --data-dir /data   # keep config/state/log in one place

Expose it safely with a cloudflared tunnel (README → Remote server).
Nothing secret is ever written to disk: API keys live in server RAM and are
re-pushed by the local machine after a restart.
    """
    from . import server
    server.run(data_dir, bind=bind, port=port, fixed_token=fixed_token, tick_seconds=tick_seconds)


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
        _legacy_pmset_cleanup()
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
        _config_set(connection, None, None, None, None, None, None, duration, None, None)
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
    _config_set(connection, None, None, mode, True, None, None, None, None, None)


@cli.command("anchor", hidden=True)
@click.argument("connection")
@click.option("--reset", "reset_hhmm", required=True, help="HH:MM today when the currently-open window closes.")
def legacy_anchor(connection, reset_hhmm):
    """Legacy alias: config set <id> --anchor HH:MM."""
    _moved(f"anchor {connection}", f"config set {connection} --anchor {reset_hhmm}")
    _config_set(connection, None, None, None, None, reset_hhmm, None, None, None, None)


@cli.command("disable", hidden=True)
@click.argument("connection")
def legacy_disable(connection):
    """Legacy alias: config set <id> --off."""
    _moved(f"disable {connection}", f"config set {connection} --off")
    _config_set(connection, None, None, None, False, None, None, None, None, None)


@cli.command("times", hidden=True)
@click.argument("connection")
@click.argument("times", nargs=-1)
def legacy_times(connection, times):
    """Legacy alias: config set <id> --times HH:MM...."""
    _moved(f"times {connection}", f"config set {connection} --times HH:MM...")
    _config_set(connection, " ".join(times) if times else None, None, None, None, None, None, None, None, None)


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
