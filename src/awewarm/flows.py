"""Interactive setup flows: the init/config add wizards and their machinery.

Prompts, value parsers, and connection constructors live here; command
wiring stays in cli.py. Shared helpers (_now, _fmt_moment) are reached
through a call-time `from . import cli` (never a top-level import) so the
modules stay import-order-safe and tests can keep patching `awewarm.cli._now`
around these flows — the same seam the command bodies use.
"""
import sys

import click

from . import discover, install, keystore, schedule, transport
from .config import (
    DEFAULT_FIXED_AT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT,
    SLOT_RE,
    conn_state,
    die,
    load_config,
    load_state,
    resolve_connection,
    save_config,
    save_state,
    unique_connection_id,
)

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
    from . import cli
    now = cli._now(config)

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
    """Offer wake-from-sleep for fixed slots where the OS supports it (None elsewhere).

    Off by default: waking a sleeping machine is opt-in, usually via the
    local settings layer (`awewarm config settings local --wake`)."""
    if sys.platform == "darwin":
        return click.confirm("Wake the Mac at these times even when it's asleep?", default=False)
    if sys.platform == "win32":
        return click.confirm("Wake the PC at these times even when it's asleep?", default=False)
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


def _own_schedule_settings(mode, fixed_at, days, wake_when_asleep):
    """The connection's own schedule overrides: what the prompts collected.

    wakeWhenAsleep rides along only where the platform can wake (None on
    Linux) — elsewhere it follows the settings layers."""
    schedule_settings = {"mode": mode, "times": list(fixed_at), "days": days}
    if wake_when_asleep is not None:
        schedule_settings["wakeWhenAsleep"] = bool(wake_when_asleep)
    return {"schedule": schedule_settings}


def _account_connection(conn_id, finding, mode, fixed_at, days, wake_when_asleep):
    provider = finding["provider"]
    window = dict(finding["builtinWindow"])
    auth_status = "valid" if finding["authFound"] else "unknown"
    conn = {
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
        "settings": _own_schedule_settings(mode, fixed_at, days, wake_when_asleep),
    }
    return resolve_connection(conn, load_config())


def _plan_connection(conn_id, label, base_url, api_key_ref, plan_url, transport_kind, model, mode, window, fixed_at, days, wake_when_asleep):
    conn = {
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
        "settings": _own_schedule_settings(mode, fixed_at, days, wake_when_asleep),
    }
    return resolve_connection(conn, load_config())


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
    conn = _account_connection(conn_id, finding, mode, fixed_at, days, wake)
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
    from . import cli
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
    now = cli._now(config)

    if mode_choice == "2":
        if result["ok"]:
            # The endpoint test already sent one real request; reuse it as
            # the verification anchor instead of sending a second one.
            cs = conn_state(state, conn_id)
            schedule.record_attempt(cs, now)
            schedule.record_success(cs, draft, now, "verify")
            save_state(state)
            click.echo(f"✓ Verification request recorded at {cli._fmt_moment(now, now)}")
        elif click.confirm("Send the verification request now?", default=True):
            verify_result = transport.send_activation(draft, api_key)
            if verify_result["ok"]:
                cs = conn_state(state, conn_id)
                schedule.record_attempt(cs, now)
                schedule.record_success(cs, draft, now, "verify")
                save_state(state)
                click.echo(f"✓ Verification request recorded at {cli._fmt_moment(now, now)}")
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
        mode, window, fixed_at, days, wake,
    )
    save_config(config)
    if reset_at is not None:
        conn = config["connections"][conn_id]
        schedule.apply_user_anchor(conn_state(state, conn_id), conn, reset_at)
        save_state(state)
        click.echo(f"✓ Renewal anchored: next request after {reset_at.strftime('%H:%M')}")
    click.echo(f"\n✓ {label} added ({conn_id}) in {mode} mode.")
    cli._refresh_wake_after_edit()
    _scheduler_hint()


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
