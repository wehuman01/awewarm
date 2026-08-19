"""Config and state storage: env-overridable paths, load/save, validation.

Everything on disk is JSON written by the tool itself; users interact through
commands, never by hand-editing. Path env overrides (AWEWARM_CONFIG etc.) are
also the primary test seam.
"""
import json
import os
import re
from pathlib import Path

CONFIG_VERSION = 1
STATE_VERSION = 1

KIND_ACCOUNT = "account"
KIND_SUBSCRIPTION = "subscription"
TRANSPORTS = (
    "claude-cli",
    "codex-cli",
    "openai-chat",
    "openai-responses",
    "anthropic-messages",
)
WINDOW_STATUSES = ("verified", "user-confirmed", "unknown", "unsupported")
WINDOW_EVIDENCES = ("builtin-provider", "user-confirmed", "none")
START_RULES = (
    "first-successful-request",
    "fixed-provider-reset",
    "rolling-usage",
    "unknown",
)
SCHEDULE_MODES = ("fixed", "interval", "hybrid")
DAY_RULES = ("weekday", "every-day")
AUTH_STATUSES = ("valid", "missing", "expired", "unknown")

DEFAULT_PROMPT = "Reply with exactly: ok"
DEFAULT_MAX_TOKENS = 4
DEFAULT_GRACE_SECONDS = 75
DEFAULT_JITTER_SECONDS = 30
DEFAULT_CATCHUP_MINUTES = 45
DEFAULT_SKIP_IF_ACTIVATED_MINUTES = 30
DEFAULT_FIXED_AT = "06:35"
DEFAULT_HISTORY_LIMIT = 20

SLOT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def die(message):
    """Exit with an actionable error message on stderr."""
    raise SystemExit(f"awewarm: {message}")


def config_path():
    return Path(
        os.environ.get("AWEWARM_CONFIG", "~/.config/awewarm/config.json")
    ).expanduser()


def state_path():
    return Path(
        os.environ.get("AWEWARM_STATE", "~/.local/state/awewarm/state.json")
    ).expanduser()


def log_path():
    return Path(
        os.environ.get("AWEWARM_LOG", "~/.local/state/awewarm/awewarm.log")
    ).expanduser()


def empty_config():
    return {"version": CONFIG_VERSION, "global": {}, "connections": {}}


def empty_state():
    return {"version": STATE_VERSION, "connections": {}}


def default_conn_state():
    return {
        "lastActivationAt": None,
        "lastAttemptAt": None,
        "lastResult": None,
        "lastError": None,
        "consecutiveFailures": 0,
        "intervalDisabledAt": None,
        "nextDueAt": None,
        "completedSlots": {},
        "skippedSlots": {},
        "history": [],
    }


def conn_state(state, conn_id):
    conns = state.setdefault("connections", {})
    if conn_id not in conns:
        conns[conn_id] = default_conn_state()
    return conns[conn_id]


def _read_json(path, what):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        die(f"cannot read {what}: {path}\n{exc}\nfix: fix the file, or delete it and re-run: awewarm init")


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_config(path=None):
    data = _read_json(path or config_path(), "config")
    if data is None:
        return empty_config()
    if not isinstance(data, dict) or not isinstance(data.get("connections"), dict):
        die("config is malformed (expected a JSON object with 'connections')\nfix: delete the file and re-run: awewarm init")
    return data


def save_config(config, path=None):
    for conn_id, conn in config["connections"].items():
        errors = connection_errors(conn, conn_id)
        if errors:
            die(f"refusing to save invalid connection {conn_id}:\n  " + "\n  ".join(errors))
    _write_json(path or config_path(), config)


def load_state(path=None):
    data = _read_json(path or state_path(), "state")
    if data is None:
        return empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("connections"), dict):
        die("state file is malformed (expected a JSON object with 'connections')\nfix: delete the file; awewarm will rebuild it on the next activation")
    return data


def save_state(state, path=None):
    _write_json(path or state_path(), state)


def connection_errors(conn, conn_id="<connection>"):
    """Return a list of problems with one connection; empty means valid.

    Structural checks keep run/status from crashing; the window gating keeps
    interval mode locked until the window semantics are known.
    """
    errors = []
    if not isinstance(conn, dict):
        return [f"{conn_id}: connection must be an object"]

    kind = conn.get("kind")
    if kind not in (KIND_ACCOUNT, KIND_SUBSCRIPTION):
        errors.append(f"{conn_id}: kind must be '{KIND_ACCOUNT}' or '{KIND_SUBSCRIPTION}'")

    transport = conn.get("transport")
    if not isinstance(transport, dict) or transport.get("kind") not in TRANSPORTS:
        errors.append(f"{conn_id}: transport.kind must be one of: {', '.join(TRANSPORTS)}")
    elif kind == KIND_SUBSCRIPTION:
        base = transport.get("baseUrl")
        if not isinstance(base, str) or not base.startswith(("http://", "https://")):
            errors.append(f"{conn_id}: subscription connections need an http(s) transport.baseUrl")
    elif kind == KIND_ACCOUNT:
        if not isinstance(transport.get("cliCommand"), str) or not transport["cliCommand"]:
            errors.append(f"{conn_id}: account connections need transport.cliCommand")

    window = conn.get("window")
    if not isinstance(window, dict) or window.get("status") not in WINDOW_STATUSES:
        errors.append(f"{conn_id}: window.status must be one of: {', '.join(WINDOW_STATUSES)}")
    elif window.get("evidence") not in WINDOW_EVIDENCES:
        errors.append(f"{conn_id}: window.evidence must be one of: {', '.join(WINDOW_EVIDENCES)}")

    schedule = conn.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("mode") not in SCHEDULE_MODES:
        errors.append(f"{conn_id}: schedule.mode must be one of: {', '.join(SCHEDULE_MODES)}")
        return errors

    interval_locked = isinstance(window, dict) and (
        window.get("status") not in ("verified", "user-confirmed")
        or not isinstance(window.get("durationMinutes"), int)
        or window["durationMinutes"] <= 0
    )
    if schedule["mode"] in ("interval", "hybrid") and interval_locked:
        errors.append(
            f"{conn_id}: schedule.mode '{schedule['mode']}' needs a verified or user-confirmed window"
            " with durationMinutes > 0\n"
            f"  fix: run: awewarm config set {conn_id} --window <minutes>"
        )

    fixed = schedule.get("fixed")
    if schedule["mode"] in ("fixed", "hybrid"):
        if not isinstance(fixed, dict):
            errors.append(f"{conn_id}: schedule.fixed is required for fixed/hybrid mode")
        else:
            at = fixed.get("at")
            if not isinstance(at, list) or not at or not all(SLOT_RE.match(s) for s in at):
                errors.append(f"{conn_id}: schedule.fixed.at must be a non-empty list of HH:MM times")
            if fixed.get("days") not in DAY_RULES:
                errors.append(f"{conn_id}: schedule.fixed.days must be 'weekday' or 'every-day'")
            for key in ("catchUpWindowMinutes", "skipIfActivatedWithinMinutes"):
                value = fixed.get(key)
                if not isinstance(value, int) or value < 0:
                    errors.append(f"{conn_id}: schedule.fixed.{key} must be an integer >= 0")
    interval = schedule.get("interval")
    if schedule["mode"] in ("interval", "hybrid"):
        if not isinstance(interval, dict):
            errors.append(f"{conn_id}: schedule.interval is required for interval/hybrid mode")
        else:
            for key in ("graceSeconds", "jitterSeconds"):
                value = interval.get(key)
                if not isinstance(value, int) or value < 0:
                    errors.append(f"{conn_id}: schedule.interval.{key} must be an integer >= 0")

    activation = conn.get("activation")
    if not isinstance(activation, dict) or not isinstance(activation.get("prompt"), str) or not activation["prompt"]:
        errors.append(f"{conn_id}: activation.prompt must be a non-empty string")
    return errors


def timezone_name(config):
    return (config.get("global") or {}).get("timezone")


def slugify(label):
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return slug or "plan"


def unique_connection_id(config, label):
    base = slugify(label)
    conn_id = base
    counter = 2
    while conn_id in config["connections"]:
        conn_id = f"{base}-{counter}"
        counter += 1
    return conn_id
