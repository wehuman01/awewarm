"""Config and state storage: env-overridable paths, load/save, validation.

Everything on disk is JSON written by the tool itself; users interact through
commands, never by hand-editing. Path env overrides (AWEWARM_CONFIG etc.) are
also the primary test seam.

On-disk format (v2) is flat — one level of connection fields. Tuning knobs
(catch-up, degrade) live in one top-level `settings` object, always written
with its effective values; a connection can override individual knobs in its
own `settings`, and anything it leaves out falls back to the top level — the
same layering the schedule fields use. load_config expands everything into
the richer runtime shape the rest of the code reads (each connection gets its
resolved knobs); save_config compacts back. v1 files (nested) upgrade in place
on first load, and knob keys written flat by earlier v2 builds migrate into
the connection's `settings` the same way.
"""
import json
import os
import re
from pathlib import Path

CONFIG_VERSION = 2
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
SCHEDULE_MODES = ("fixed", "interval")
DAY_RULES = ("weekday", "every-day")
AUTH_STATUSES = ("valid", "missing", "expired", "unknown")

DEFAULT_PROMPT = "Reply with exactly: ok"
DEFAULT_MAX_TOKENS = 4
DEFAULT_GRACE_SECONDS = 75
DEFAULT_JITTER_SECONDS = 30
DEFAULT_CATCHUP_MINUTES = 30
DEFAULT_CATCHUP_ATTEMPTS = 5
DEFAULT_DEGRADE_AFTER_NODES = 3
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
    return {"version": CONFIG_VERSION, "global": {}, "settings": {}, "connections": {}}


def default_settings():
    return {
        "catchupAttempts": DEFAULT_CATCHUP_ATTEMPTS,
        "catchupMinutes": DEFAULT_CATCHUP_MINUTES,
        "degradeAfterNodes": DEFAULT_DEGRADE_AFTER_NODES,
    }


def _resolve_settings(raw):
    """Fill in code defaults for missing knobs so the block is always complete."""
    return {**default_settings(), **(raw if isinstance(raw, dict) else {})}


def empty_state():
    return {"version": STATE_VERSION, "connections": {}}


def default_conn_state():
    return {
        "lastActivationAt": None,
        "lastAttemptAt": None,
        "lastResult": None,
        "lastError": None,
        "nextDueAt": None,
        "deferUntil": None,
        # Health ladder: failing → degraded → auto-disabled (see schedule.py)
        "failedNodes": 0,
        "nodeKey": None,
        "nodeDueAt": None,
        "nodeSlot": None,
        "nodeAttempts": 0,
        "degradedAt": None,
        "nextProbeAt": None,
        "degradedFailedNodes": 0,
        "autoDisabledAt": None,
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
    version = data.get("version", 1)
    if version == 1:
        # Legacy nested files are already the runtime shape; rewrite as v2.
        runtime = data
        runtime["version"] = CONFIG_VERSION
        _migrate_hybrid_nested(runtime)
        _write_json(path or config_path(), _compact_config(runtime))
        return runtime
    if version != CONFIG_VERSION:
        die(f"config version {version} is newer than this awewarm understands\nfix: update awewarm: awewarm update")

    if _migrate_hybrid_flat(data) or _migrate_settings_flat(data):
        _write_json(path or config_path(), data)

    global_cfg = data.get("global") or {}
    if "wakeWhenAsleep" in global_cfg:
        for conn in data.get("connections", {}).values():
            sched = conn.setdefault("schedule", {})
            if "wakeWhenAsleep" not in sched:
                sched["wakeWhenAsleep"] = bool(global_cfg["wakeWhenAsleep"])
        global_cfg.pop("wakeWhenAsleep")
        data["global"] = global_cfg
        _write_json(path or config_path(), data)

    for conn in data.get("connections", {}).values():
        sched = conn.get("schedule") if isinstance(conn.get("schedule"), dict) else None
        if sched and "wakeWhenAsleep" in sched:
            continue
        if "wakeWhenAsleep" in conn:
            sched = conn.setdefault("schedule", {})
            sched["wakeWhenAsleep"] = bool(conn.pop("wakeWhenAsleep"))
            _write_json(path or config_path(), data)

    settings = _resolve_settings(data.get("settings"))
    return {
        "version": CONFIG_VERSION,
        "global": data.get("global") or {},
        "settings": settings,
        "connections": {
            conn_id: _expand_conn(conn_id, conn, settings)
            for conn_id, conn in data["connections"].items()
        },
    }


def _migrate_hybrid_flat(data):
    """v2 flat config: hybrid was removed; map it to fixed (times already set)."""
    changed = False
    for conn in data.get("connections", {}).values():
        if isinstance(conn, dict) and conn.get("mode") == "hybrid":
            conn["mode"] = "fixed"
            changed = True
    return changed


def _migrate_settings_flat(data):
    """Knobs live in a top-level `settings` block with optional per-connection
    overrides. Legacy flat per-connection keys lift into the connection's own
    `settings` (they were per-connection values), and the top-level block is
    materialized so it is always visible on disk."""
    changed = False
    defaults = default_settings()
    for conn in data.get("connections", {}).values():
        if not isinstance(conn, dict):
            continue
        legacy = {key: conn.pop(key) for key in ("catchupMinutes", "catchupAttempts", "degradeAfterNodes") if key in conn}
        if not legacy:
            continue
        settings = conn.get("settings") if isinstance(conn.get("settings"), dict) else {}
        for key, value in legacy.items():
            if key not in settings and value != defaults.get(key):
                settings[key] = value
        if settings:
            conn["settings"] = settings
        changed = True
    resolved = _resolve_settings(data.get("settings"))
    if data.get("settings") != resolved:
        data["settings"] = resolved
        changed = True
    return changed


def _migrate_hybrid_nested(runtime):
    """v1 nested config: same migration on the runtime shape."""
    for conn in runtime.get("connections", {}).values():
        sched = conn.get("schedule") if isinstance(conn, dict) else None
        if isinstance(sched, dict) and sched.get("mode") == "hybrid":
            sched["mode"] = "fixed"


def _expand_conn(conn_id, flat, global_settings):
    """Flat v2 fields → the nested runtime shape the codebase reads.

    Knobs resolve per-connection first, then the top-level `settings`."""
    subscription = bool(flat.get("url"))
    if subscription:
        kind, auth, transport = KIND_SUBSCRIPTION, (
            {"type": "api-key", "status": "valid", "apiKeyRef": flat.get("apiKey")}
        ), {"kind": flat.get("protocol") or "openai-chat", "baseUrl": flat["url"], "cliCommand": None}
    else:
        cli = flat.get("cli") or ""
        # Provider rides on the CLI basename; discover only ever finds `claude` / `codex`.
        transport_kind = "codex-cli" if "codex" in Path(cli).name.lower() else "claude-cli"
        kind, auth, transport = KIND_ACCOUNT, (
            {"type": "local-cli", "status": "valid", "apiKeyRef": None}
        ), {"kind": transport_kind, "baseUrl": None, "cliCommand": cli}
    duration = flat.get("windowMinutes")
    window = (
        {"status": "user-confirmed", "startRule": "unknown", "durationMinutes": duration, "evidence": "user-confirmed"}
        if isinstance(duration, int) and duration > 0
        else {"status": "unknown", "startRule": "unknown", "durationMinutes": None, "evidence": "none"}
    )
    settings = {**global_settings, **(flat.get("settings") or {})}
    schedule = {
        "mode": flat.get("mode") or "fixed",
        "fixed": {
            "at": list(flat.get("times") or [DEFAULT_FIXED_AT]),
            "days": flat.get("days") or "weekday",
            "skipIfActivatedWithinMinutes": flat.get("skipIfActivatedMinutes", DEFAULT_SKIP_IF_ACTIVATED_MINUTES),
        },
        "interval": {
            "graceSeconds": flat.get("graceSeconds", DEFAULT_GRACE_SECONDS),
            "jitterSeconds": flat.get("jitterSeconds", DEFAULT_JITTER_SECONDS),
        },
        "wakeWhenAsleep": (flat.get("schedule") or {}).get("wakeWhenAsleep", flat.get("wakeWhenAsleep", True)),
    }
    return {
        "label": flat.get("label") or conn_id,
        "kind": kind,
        "enabled": flat.get("enabled", True),
        "auth": auth,
        "transport": transport,
        "window": window,
        "activation": {
            "model": flat.get("model"),
            "prompt": DEFAULT_PROMPT,
            "maxTokens": DEFAULT_MAX_TOKENS,
        },
        # the connection's own knob overrides (empty = pure inheritance);
        # catchup/degradeAfterNodes below are the resolved values code reads
        "settings": dict(flat.get("settings") or {}),
        "catchup": {
            "attempts": settings.get("catchupAttempts", DEFAULT_CATCHUP_ATTEMPTS),
            "withinMinutes": settings.get("catchupMinutes", DEFAULT_CATCHUP_MINUTES),
        },
        "degradeAfterNodes": settings.get("degradeAfterNodes", DEFAULT_DEGRADE_AFTER_NODES),
        "schedule": schedule,
    }


def _compact_conn(conn, global_settings):
    """Runtime shape → flat v2 fields. The connection's `settings` overrides
    persist as-is, minus any that merely repeat the top-level block."""
    flat = {}
    if conn.get("label"):
        flat["label"] = conn["label"]
    transport = conn.get("transport") or {}
    if conn.get("kind") == KIND_SUBSCRIPTION:
        flat["url"] = transport.get("baseUrl")
        flat["protocol"] = transport.get("kind")
        api_key_ref = (conn.get("auth") or {}).get("apiKeyRef")
        if api_key_ref:
            flat["apiKey"] = api_key_ref
    elif transport.get("cliCommand"):
        flat["cli"] = transport["cliCommand"]
    activation = conn.get("activation") or {}
    if activation.get("model"):
        flat["model"] = activation["model"]
    window = conn.get("window") or {}
    duration = window.get("durationMinutes")
    if window.get("status") in ("verified", "user-confirmed") and isinstance(duration, int) and duration > 0:
        flat["windowMinutes"] = duration
    schedule = conn.get("schedule") or {}
    flat["mode"] = schedule.get("mode") or "fixed"
    fixed = schedule.get("fixed") or {}
    if schedule.get("mode") == "fixed" or fixed.get("at"):
        flat["times"] = list(fixed.get("at") or [DEFAULT_FIXED_AT])
        if fixed.get("days"):
            flat["days"] = fixed["days"]
        value = fixed.get("skipIfActivatedWithinMinutes")
        if value is not None and value != DEFAULT_SKIP_IF_ACTIVATED_MINUTES:
            flat["skipIfActivatedMinutes"] = value
    overrides = {
        key: value for key, value in (conn.get("settings") or {}).items()
        if key in ("catchupMinutes", "catchupAttempts", "degradeAfterNodes") and value != global_settings.get(key)
    }
    if overrides:
        flat["settings"] = overrides
    flat.setdefault("schedule", {})["wakeWhenAsleep"] = bool(schedule.get("wakeWhenAsleep", True))
    interval = schedule.get("interval") or {}
    for run_key, default in (
        ("graceSeconds", DEFAULT_GRACE_SECONDS),
        ("jitterSeconds", DEFAULT_JITTER_SECONDS),
    ):
        value = interval.get(run_key)
        if value is not None and value != default:
            flat[run_key] = value
    if conn.get("enabled") is False:
        flat["enabled"] = False
    return flat


def _compact_config(config):
    settings = _resolve_settings(config.get("settings"))
    return {
        "version": CONFIG_VERSION,
        **({"global": config["global"]} if config.get("global") else {}),
        "settings": settings,
        "connections": {
            conn_id: _compact_conn(conn, settings)
            for conn_id, conn in config["connections"].items()
        },
    }


def save_config(config, path=None):
    for conn_id, conn in config["connections"].items():
        errors = connection_errors(conn, conn_id)
        if errors:
            die(f"refusing to save invalid connection {conn_id}:\n  " + "\n  ".join(errors))
    _write_json(path or config_path(), _compact_config(config))


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
    if schedule["mode"] == "interval" and interval_locked:
        errors.append(
            f"{conn_id}: schedule.mode '{schedule['mode']}' needs a verified or user-confirmed window"
            " with durationMinutes > 0\n"
            f"  fix: run: awewarm config set {conn_id} --window <minutes>"
        )

    fixed = schedule.get("fixed")
    if schedule["mode"] == "fixed":
        if not isinstance(fixed, dict):
            errors.append(f"{conn_id}: schedule.fixed is required for fixed mode")
        else:
            at = fixed.get("at")
            if not isinstance(at, list) or not at or not all(SLOT_RE.match(s) for s in at):
                errors.append(f"{conn_id}: schedule.fixed.at must be a non-empty list of HH:MM times")
            if fixed.get("days") not in DAY_RULES:
                errors.append(f"{conn_id}: schedule.fixed.days must be 'weekday' or 'every-day'")
            value = fixed.get("skipIfActivatedWithinMinutes")
            if not isinstance(value, int) or value < 0:
                errors.append(f"{conn_id}: schedule.fixed.skipIfActivatedWithinMinutes must be an integer >= 0")
            if not isinstance(schedule.get("wakeWhenAsleep", True), bool):
                errors.append(f"{conn_id}: schedule.wakeWhenAsleep must be a boolean")
    catchup = conn.get("catchup")
    if catchup is not None:
        if not isinstance(catchup, dict):
            errors.append(f"{conn_id}: catchup must be an object with attempts/withinMinutes")
        else:
            for key in ("attempts", "withinMinutes"):
                value = catchup.get(key)
                if not isinstance(value, int) or value <= 0:
                    errors.append(f"{conn_id}: catchup.{key} must be an integer > 0")
    nodes = conn.get("degradeAfterNodes")
    if nodes is not None and (not isinstance(nodes, int) or nodes <= 0):
        errors.append(f"{conn_id}: degradeAfterNodes must be an integer > 0")
    interval = schedule.get("interval")
    if schedule["mode"] == "interval":
        if not isinstance(interval, dict):
            errors.append(f"{conn_id}: schedule.interval is required for interval mode")
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
