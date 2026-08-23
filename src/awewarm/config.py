"""Config and state storage: env-overridable paths, load/save, validation.

Everything on disk is JSON written by the tool itself; users interact through
commands, never by hand-editing. Path env overrides (AWEWARM_CONFIG etc.) are
also the primary test seam.

On-disk format (v3) groups connections under `connections.local` and
`connections.remote`. The location groups and the top-level `settings` block
are the layers: each carries knobs and a `schedule` block (the global layer
always names its schedule's `mode`). A connection's own overrides sit
directly on the connection — `schedule` plus any knob — with no `settings`
wrapper; that wrapped spelling is a legacy read that folds away on load.
The group a connection sits under
is its location — a per-connection `location` field is a legacy spelling
that must agree with its group or refuses to load.
The split is semantic: the schedule block answers when a connection fires
(mode, times, days, and the interval clock — windowMinutes included); the
knobs answer how an activation behaves (request content, failure policy,
machine wake — wakeWhenAsleep). In memory the shape differs: `connections`
is flat (conn_id → connection) and the location settings layers ride under
`connectionDefaults`.
A connection resolves each field own-overrides-first, then its location's
defaults, then the global block; knobs follow that chain everywhere, while a
schedule inherited from the global block never reaches a delegated (`remote`)
connection — the global schedule describes this machine's day, and a server
must only fire times written for remote (or the connection's own). One field
crosses that line: windowMinutes is a plan fact, not a machine-day fact, so
the global block's window duration reaches delegated connections too. An
inherited interval mode never breaks a connection whose window is unverified
(it stays fixed); an explicit own override surfaces the gating error instead.
The earlier spellings of the two swapped fields (a knob-position
windowMinutes, a schedule-position wakeWhenAsleep) fold into the current
positions on load and are never written back. load_config
expands everything into the richer runtime shape the rest of the code reads
(resolved schedule, catch-up, degrade knobs); save_config compacts back,
dropping any override that merely repeats what the connection would inherit
anyway.

There is no upgrade path: a file older than v3 is refused with a pointer to
`awewarm config template` (the same shape as resources/config.template.json),
and the user adjusts the file by hand. Unknown connection fields are refused
the same way, so a hand-edited typo or a leftover v2 field never silently
no-ops.
"""
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .discover import BUILTIN_WINDOWS

CONFIG_VERSION = 3
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
LOCATIONS = ("local", "remote")
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

# Settings every block may carry, split by semantics: the schedule block
# answers "when does a connection fire" (mode, times, days, and the interval
# clock — windowMinutes included); the knobs answer "how an activation
# behaves" (request content, failure policy, machine wake). A new field
# joins the group its semantics belong to, and its disk position and merge
# chain move together. windowMinutes is the one schedule field without a
# code default: absent everywhere means the window is unknown, which keeps
# interval renewal locked for that connection.
KNOB_KEYS = ("catchupMinutes", "catchupAttempts", "degradeAfterNodes", "wakeWhenAsleep", "prompt", "maxTokens")
SCHEDULE_SETTINGS_KEYS = (
    "mode",
    "times",
    "days",
    "skipIfActivatedMinutes",
    "windowMinutes",
    "graceSeconds",
    "jitterSeconds",
)

SLOT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Fields a flat v3 connection may carry on disk; anything else is a hand-edit
# typo or a leftover from an older format and refuses to load. A connection's
# own overrides sit directly on it — `schedule` plus any knob — while
# `settings` (the wrapped spelling), `location`, and a top-level
# `windowMinutes` are legacy reads that fold away on load.
KNOWN_CONN_KEYS = frozenset({
    "label", "url", "protocol", "apiKey", "cli", "model",
    "schedule", "enabled", "hide", *KNOB_KEYS,
    "settings", "location", "windowMinutes",
})

# The reference shape of a valid config, printed by `awewarm config template`
# and pointed at by every load refusal. resources/config.template.json holds
# the same text for repo readers; a test keeps the two from drifting.
CONFIG_TEMPLATE = """\
{
  "version": 3,
  "settings": {
    "catchupMinutes": 30,
    "catchupAttempts": 5,
    "degradeAfterNodes": 3,
    "wakeWhenAsleep": false,
    "prompt": "Reply with exactly: ok",
    "maxTokens": 4,
    "schedule": {
      "mode": "fixed",
      "times": ["06:35"],
      "days": "weekday",
      "skipIfActivatedMinutes": 30,
      "windowMinutes": 300,
      "graceSeconds": 75,
      "jitterSeconds": 30
    }
  },
  "connections": {
    "local": {
      "settings": {
        "wakeWhenAsleep": true,
        "schedule": {"times": ["06:35"], "days": "weekday"}
      },
      "claude-code": {
        "label": "Claude Code",
        "cli": "/usr/local/bin/claude",
        "schedule": {"times": ["06:35"], "mode": "fixed"}
      }
    },
    "remote": {
      "settings": {
        "schedule": {"times": ["08:00"], "days": "every-day"}
      },
      "glm": {
        "label": "glm",
        "url": "https://open.bigmodel.cn/api/anthropic",
        "protocol": "anthropic-messages",
        "apiKey": "file:glm",
        "model": "GLM-5-Turbo"
      }
    }
  }
}
"""


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


LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_KEEP_BYTES = 512 * 1024


def append_log(path, message):
    """Append one stamped line to the event log; best-effort, never fatal.

    Shared by the CLI and `awewarm serve`: rotate by truncating to the last
    LOG_KEEP_BYTES once the file passes LOG_ROTATE_BYTES, then append.
    """
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            path.write_bytes(path.read_bytes()[-LOG_KEEP_BYTES:])
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with open(path, "a") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def empty_config():
    return {
        "version": CONFIG_VERSION,
        "global": {},
        "settings": {},
        "connectionDefaults": {},
        "connections": {},
    }


def default_settings():
    # wakeWhenAsleep and prompt/maxTokens materialize into every saved global
    # block (and so into the template), which documents them; windowMinutes
    # stays absent until a layer sets it — a default here would silently
    # confirm every window.
    return {
        "catchupAttempts": DEFAULT_CATCHUP_ATTEMPTS,
        "catchupMinutes": DEFAULT_CATCHUP_MINUTES,
        "degradeAfterNodes": DEFAULT_DEGRADE_AFTER_NODES,
        "wakeWhenAsleep": False,
        "prompt": DEFAULT_PROMPT,
        "maxTokens": DEFAULT_MAX_TOKENS,
    }


def default_schedule():
    """Code defaults for every schedule field; the base of the inheritance chain.

    windowMinutes has no entry here on purpose: absent everywhere means the
    window is unknown, which keeps interval renewal locked — do not "complete"
    the defaults with one."""
    return {
        "mode": "fixed",
        "times": [DEFAULT_FIXED_AT],
        "days": "weekday",
        "skipIfActivatedMinutes": DEFAULT_SKIP_IF_ACTIVATED_MINUTES,
        "graceSeconds": DEFAULT_GRACE_SECONDS,
        "jitterSeconds": DEFAULT_JITTER_SECONDS,
    }


def _resolve_settings(raw):
    """Fill in code defaults for missing knobs so the block is always complete.

    A `schedule` sub-block keeps its own fields (they resolve per-connection
    against the layers above, not here) — except `mode`, which materializes
    its code default so every saved global block names the default mode."""
    resolved = {**default_settings(), **(raw if isinstance(raw, dict) else {})}
    schedule = raw.get("schedule") if isinstance(raw, dict) else None
    resolved["schedule"] = {**{"mode": "fixed"}, **(schedule if isinstance(schedule, dict) else {})}
    return resolved


def _settings_knobs(block):
    """The knob keys of one settings block (everything but `schedule`)."""
    return {key: value for key, value in (block or {}).items() if key != "schedule"}


def _schedule_of(block):
    schedule = (block or {}).get("schedule")
    return schedule if isinstance(schedule, dict) else {}


def _fold_legacy_settings(block):
    """Move the pre-reshuffle spellings of the two swapped fields into their
    current positions, so a file written by an older build loads unchanged.

    `windowMinutes` used to sit among the knobs, `wakeWhenAsleep` inside the
    schedule block; on load they fold the other way and only the current
    spellings are ever written back. Where both spellings exist (a hand
    edit), the current position wins."""
    if not isinstance(block, dict):
        return block
    schedule = block.get("schedule")
    if not isinstance(schedule, dict) and isinstance(block.get("windowMinutes"), int):
        schedule = {}
        block["schedule"] = schedule
    if isinstance(schedule, dict):
        if "windowMinutes" not in schedule and isinstance(block.get("windowMinutes"), int):
            schedule["windowMinutes"] = block["windowMinutes"]
        if "wakeWhenAsleep" in schedule:
            # a knob-position value (the current spelling) outranks it
            block.setdefault("wakeWhenAsleep", schedule.pop("wakeWhenAsleep"))
    block.pop("windowMinutes", None)
    return block


def _inherited_knobs(location, global_settings, connection_defaults):
    """Knob values a connection inherits before its own overrides."""
    chain = dict(_settings_knobs(global_settings))
    chain.update(_settings_knobs((connection_defaults or {}).get(location)))
    return chain


def _inherited_schedule(location, global_settings, connection_defaults):
    """Schedule fields a connection inherits before its own overrides.

    Delegated (`remote`) connections never see the global schedule: it
    describes the delegating machine's workday. They follow only defaults
    written explicitly for remote — everything else falls to the code
    defaults. One field is exempt from that exemption: `windowMinutes` is a
    fact about the plan (how long its quota window lasts), not about any
    machine's day, so the global block's window duration reaches delegated
    connections too."""
    global_schedule = _schedule_of(global_settings)
    chain = {}
    if location != "remote":
        chain.update(global_schedule)
    elif "windowMinutes" in global_schedule:
        chain["windowMinutes"] = global_schedule["windowMinutes"]
    chain.update(_schedule_of((connection_defaults or {}).get(location)))
    return chain


def _window_allows_interval(window):
    return (
        isinstance(window, dict)
        and window.get("status") in ("verified", "user-confirmed")
        and isinstance(window.get("durationMinutes"), int)
        and not isinstance(window.get("durationMinutes"), bool)
        and window["durationMinutes"] > 0
    )


def _resolved_schedule(own, window, inherited_schedule):
    """Merge one connection's own schedule overrides over its inherited chain."""
    schedule = {**default_schedule(), **inherited_schedule, **(own or {})}
    # An inherited interval mode must not invalidate a connection whose window
    # is unverified (the tick would skip it entirely) — such connections stay
    # on fixed. An explicit own override is honored as-is so the usual gating
    # error tells the user to verify the window first.
    if (
        schedule["mode"] == "interval"
        and not (own or {}).get("mode")
        and not _window_allows_interval(window)
    ):
        schedule["mode"] = "fixed"
    return schedule


def _resolve_window(conn, own, inherited_schedule):
    """The connection's window: own recorded duration first, then a window the
    conn already carries (connections built in code — the add wizard — set it
    directly; compaction persists it as an own override), then (for CLI
    accounts) the builtin provider fact, then the layers' windowMinutes —
    a schedule field, inherited through the same chain as the rest of the
    schedule. Absent everywhere means unknown, which keeps interval renewal
    locked — a layer's windowMinutes is the user vouching for every
    connection under it, so it unlocks exactly like a per-connection record.
    The value only takes effect while the resolved schedule mode is interval;
    fixed-mode connections merely record it."""
    duration = _schedule_of(own).get("windowMinutes")
    carried = conn.get("window") or {}
    if duration is None and carried.get("status") in ("verified", "user-confirmed"):
        duration = carried.get("durationMinutes")
    if conn.get("kind") == KIND_ACCOUNT:
        provider = "claude-code" if (conn.get("transport") or {}).get("kind") == "claude-cli" else "codex"
        builtin = BUILTIN_WINDOWS[provider]
        # The builtin provider fact (Claude Code's verified 5 h) is not
        # "upgraded" past a different duration the user recorded by hand.
        if builtin["status"] == "verified" and duration in (None, builtin["durationMinutes"]):
            return dict(builtin)
    if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
        return {
            "status": "user-confirmed",
            "startRule": carried.get("startRule", "unknown"),
            "durationMinutes": duration,
            "evidence": "user-confirmed",
        }
    inherited = inherited_schedule.get("windowMinutes")
    if isinstance(inherited, int) and not isinstance(inherited, bool) and inherited > 0:
        return {
            "status": "user-confirmed",
            "startRule": "unknown",
            "durationMinutes": inherited,
            "evidence": "user-confirmed",
        }
    return {"status": "unknown", "startRule": "unknown", "durationMinutes": None, "evidence": "none"}


def _apply_resolved(conn, global_settings, connection_defaults):
    """(Re)compute a runtime connection's resolved window, schedule, and
    catchup/degrade/activation fields from its own `settings` overrides plus
    the config's layers."""
    own = conn.get("settings") if isinstance(conn.get("settings"), dict) else {}
    location = conn.get("location") or "local"
    knobs = {**_inherited_knobs(location, global_settings, connection_defaults), **_settings_knobs(own)}
    inherited_schedule = _inherited_schedule(location, global_settings, connection_defaults)
    conn["window"] = _resolve_window(conn, own, inherited_schedule)
    sched = _resolved_schedule(own.get("schedule"), conn["window"], inherited_schedule)
    conn["schedule"] = {
        "mode": sched["mode"],
        "fixed": {
            "at": list(sched["times"]),
            "days": sched["days"],
            "skipIfActivatedWithinMinutes": sched["skipIfActivatedMinutes"],
        },
        "interval": {
            "graceSeconds": sched["graceSeconds"],
            "jitterSeconds": sched["jitterSeconds"],
        },
        # a knob in the layers (a machine behavior switch), but the resolved
        # schedule carries it so the wake layer and status read one dict
        "wakeWhenAsleep": knobs.get("wakeWhenAsleep", False),
    }
    conn["catchup"] = {
        "attempts": knobs.get("catchupAttempts", DEFAULT_CATCHUP_ATTEMPTS),
        "withinMinutes": knobs.get("catchupMinutes", DEFAULT_CATCHUP_MINUTES),
    }
    conn["degradeAfterNodes"] = knobs.get("degradeAfterNodes", DEFAULT_DEGRADE_AFTER_NODES)
    conn["activation"] = {
        "model": (conn.get("activation") or {}).get("model"),
        "prompt": knobs.get("prompt", DEFAULT_PROMPT),
        "maxTokens": knobs.get("maxTokens", DEFAULT_MAX_TOKENS),
    }
    return conn


def resolve_connection(conn, config):
    """Re-resolve one runtime connection against a loaded config's layers.

    Called after edits (CLI `config set`, delegation flows) so the resolved
    fields never drift from the overrides and defaults layers."""
    return _apply_resolved(
        conn, config.get("settings") or {}, config.get("connectionDefaults") or {}
    )


def flatten_schedule(schedule):
    """Runtime schedule block → flat own-settings fields (all seven).

    Used by the v1 upgrade (the resolved schedule a connection ran on becomes
    its own overrides) and by delegation (freezing the effective schedule of
    a connection the moment it moves to the server). The six timing fields
    land in a settings block's `schedule`; `wakeWhenAsleep` is a knob now —
    callers must file it outside the schedule dict."""
    fixed = (schedule or {}).get("fixed") or {}
    interval = (schedule or {}).get("interval") or {}
    return {
        "mode": (schedule or {}).get("mode") or "fixed",
        "times": list(fixed.get("at") or [DEFAULT_FIXED_AT]),
        "days": fixed.get("days") or "weekday",
        "skipIfActivatedMinutes": fixed.get("skipIfActivatedWithinMinutes", DEFAULT_SKIP_IF_ACTIVATED_MINUTES),
        "graceSeconds": interval.get("graceSeconds", DEFAULT_GRACE_SECONDS),
        "jitterSeconds": interval.get("jitterSeconds", DEFAULT_JITTER_SECONDS),
        "wakeWhenAsleep": (schedule or {}).get("wakeWhenAsleep", False),
    }


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
    # Same-dir temp + rename: readers (and a concurrent writer in another
    # process — the scheduler tick racing an edit command) never see a torn
    # file, where truncate-then-write once left two JSON docs concatenated.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _template_fix(where):
    """The actionable tail of every config-load refusal."""
    return f"fix: see the current format: awewarm config template\n     adjust {where} accordingly, or start fresh: awewarm init"


def load_config(path=None):
    data = _read_json(path or config_path(), "config")
    if data is None:
        return empty_config()
    if not isinstance(data, dict) or not isinstance(data.get("connections"), dict):
        die("config is malformed (expected a JSON object with 'connections')\n"
            + _template_fix(path or config_path()))
    version = data.get("version", 1)
    if version > CONFIG_VERSION:
        die(f"config version {version} is newer than this awewarm understands\nfix: update awewarm: awewarm self-update")
    if version < CONFIG_VERSION:
        die(
            f"config version {version} predates the current format (version {CONFIG_VERSION}); "
            "older files are not upgraded automatically\n"
            + _template_fix(path or config_path())
        )

    raw_location_blocks = data.get("connections") or {}
    if not isinstance(raw_location_blocks, dict):
        die("config is malformed (connections must be an object)\n"
            + _template_fix(path or config_path()))

    connection_defaults = {}
    flat_connections = {}

    for location_block_id, location_block in raw_location_blocks.items():
        if location_block_id not in LOCATIONS:
            die(
                f"connections must group connections under 'local' and 'remote' — "
                f"found unexpected key '{location_block_id}'\n"
                + _template_fix(path or config_path())
            )
        if not isinstance(location_block, dict):
            die(f"connections.{location_block_id} must be a JSON object\n" + _template_fix(path or config_path()))

        settings_block = _fold_legacy_settings(location_block.get("settings"))
        if settings_block is not None:
            errors = settings_block_errors(settings_block, f"connections.{location_block_id}.settings")
            if errors:
                die(
                    f"config has invalid connections.{location_block_id}.settings:\n  " + "\n  ".join(errors)
                    + "\nfix: fix the file, or reset the block: awewarm config settings --reset"
                )
            # kept raw (unlike the resolved global block): missing knobs fall to
            # code defaults per field, and save writes back only what was set
            connection_defaults[location_block_id] = settings_block

        for conn_id, flat in location_block.items():
            if conn_id == "settings":
                continue
            if conn_id in LOCATIONS:
                continue
            if not isinstance(flat, dict):
                die(f"connection '{conn_id}' must be a JSON object\n" + _template_fix(path or config_path()))
            unknown = sorted(set(flat) - KNOWN_CONN_KEYS)
            if unknown:
                die(
                    f"connection '{conn_id}' has unknown field(s): {', '.join(unknown)}\n"
                    "  (schedule fields live under settings.schedule since version 3)\n"
                    + _template_fix(path or config_path())
                )
            flat_connections[conn_id] = (location_block_id, flat)

    global_settings_raw = _fold_legacy_settings(data.get("settings"))
    if global_settings_raw is not None:
        errors = settings_block_errors(global_settings_raw, "settings")
        if errors:
            die(
                "config has invalid settings:\n  " + "\n  ".join(errors)
                + "\nfix: fix the file, or reset the block: awewarm config settings --reset"
            )
    global_settings = _resolve_settings(global_settings_raw)

    return {
        "version": CONFIG_VERSION,
        "global": data.get("global") or {},
        "settings": global_settings,
        "connectionDefaults": connection_defaults,
        "remote": data.get("remote") or {},
        "connections": {
            conn_id: _expand_conn(conn_id, flat, group, global_settings, connection_defaults)
            for conn_id, (group, flat) in flat_connections.items()
        },
    }


def _expand_conn(conn_id, flat, group, global_settings, connection_defaults):
    """Flat v3 fields → the nested runtime shape the codebase reads.

    The location group the connection sits under decides where it ticks; a
    `location` field is the pre-reshuffle spelling (still read so old files
    load, then dropped — a contradicting one is a hand edit and refuses).
    Schedule and knobs resolve own-overrides first, then the location's
    defaults, then the global block (schedule: local connections only)."""
    explicit = flat.get("location")
    if explicit is not None and explicit != group:
        die(
            f"connection '{conn_id}' sits under connections.{group} but its location field says '{explicit}'\n"
            "  (the group decides where a connection ticks — remove the stray field)\n"
            + _template_fix(config_path())
        )
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
    own = {}
    # The wrapped `settings` block is the pre-reshuffle spelling of a
    # connection's own overrides (still read so old files load); flat fields
    # win key-by-key where both spellings exist (a hand edit).
    legacy = _fold_legacy_settings(
        dict(flat.get("settings") or {}) if isinstance(flat.get("settings"), dict) else {}
    )
    for key in KNOB_KEYS:
        if key in flat:
            own[key] = flat[key]
        elif key in legacy:
            own[key] = legacy[key]
    schedule = dict(flat["schedule"]) if isinstance(flat.get("schedule"), dict) else {}
    for key, value in (legacy.get("schedule") or {}).items():
        schedule.setdefault(key, value)
    # A top-level windowMinutes is the pre-knob spelling of an own override;
    # it folds into the schedule, where compaction writes it from now on.
    if "windowMinutes" not in schedule and isinstance(flat.get("windowMinutes"), int):
        schedule["windowMinutes"] = flat["windowMinutes"]
    # An explicit (possibly empty) schedule marker records "this connection
    # has no own overrides" — compaction then never pins inherited values.
    own["schedule"] = schedule
    conn = {
        "label": flat.get("label") or conn_id,
        "kind": kind,
        "enabled": flat.get("enabled", True),
        # display-only: hidden connections keep warming, status just omits them
        "hide": bool(flat.get("hide", False)),
        # where this connection ticks: the group it was loaded from
        # (connections.local / connections.remote)
        "location": group,
        "auth": auth,
        "transport": transport,
        "activation": {"model": flat.get("model")},
        # the connection's own overrides (knobs + schedule; empty schedule =
        # pure inheritance) — window/schedule/catchup/degrade/activation below
        # are the resolved values the rest of the code reads
        "settings": own,
    }
    return _apply_resolved(conn, global_settings, connection_defaults)


def _compact_conn(conn, global_settings, connection_defaults):
    """Runtime shape → flat v3 fields.

    Own overrides persist as-is, minus any that merely repeat what the
    connection would inherit anyway. A runtime connection without the
    schedule marker (built in code, never loaded) pins the schedule it
    carries, the way every connection did before settings layers existed."""
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
    own = conn.get("settings") if isinstance(conn.get("settings"), dict) else {}
    location = conn.get("location") or "local"
    inherited_knobs = _inherited_knobs(location, global_settings, connection_defaults)
    inherited_schedule = {
        **default_schedule(),
        **_inherited_schedule(location, global_settings, connection_defaults),
    }
    own_knobs = _settings_knobs(own)
    if isinstance(own.get("schedule"), dict):
        own_schedule = dict(own["schedule"])
    else:
        # A runtime connection without the schedule marker (built in code,
        # never loaded) pins the schedule it carries, the way every connection
        # did before settings layers existed.
        own_schedule = flatten_schedule(conn.get("schedule"))
        own_knobs = {**own_knobs, "wakeWhenAsleep": own_schedule.pop("wakeWhenAsleep")}
    overrides = {
        key: value for key, value in own_knobs.items()
        if key in KNOB_KEYS and value != inherited_knobs.get(key)
    }
    if "windowMinutes" not in own_schedule:
        # A window the conn carries but never recorded as an own schedule
        # field (built in code, or loaded from an older spelling and since
        # resolved) persists as an own override — unless the layers already
        # inherit the same duration, in which case pinning it would freeze
        # the layer.
        carried = conn.get("window") or {}
        carried_duration = carried.get("durationMinutes")
        if (
            carried.get("status") in ("verified", "user-confirmed")
            and carried.get("evidence") != "builtin-provider"
            and isinstance(carried_duration, int) and not isinstance(carried_duration, bool)
            and carried_duration > 0
            and carried_duration != inherited_schedule.get("windowMinutes")
        ):
            own_schedule["windowMinutes"] = carried_duration
    schedule_overrides = {
        key: value for key, value in own_schedule.items()
        if value != inherited_schedule.get(key)
    }
    # mode is the one schedule field always written, even when it matches the
    # chain: it is each connection's headline fact, and the file must show it
    # without running status. The costs are deliberate — a layer's mode change
    # never re-modes an existing connection (switch each one explicitly), and
    # an inherited interval whose window is unverified pins the fixed it
    # actually runs on (the load-time fallback, frozen at its resolved value).
    written_mode = own_schedule.get("mode") or inherited_schedule.get("mode", "fixed")
    if (
        written_mode == "interval"
        and "mode" not in own_schedule
        and not _window_allows_interval(_resolve_window(conn, own, inherited_schedule))
    ):
        written_mode = "fixed"
    schedule_overrides.setdefault("mode", written_mode)
    if schedule_overrides:
        overrides["schedule"] = schedule_overrides
    # own overrides sit directly on the connection — `schedule` plus knobs —
    # no `settings` wrapper (that spelling folds away on load)
    flat.update(overrides)
    if conn.get("enabled") is False:
        flat["enabled"] = False
    if conn.get("hide"):
        flat["hide"] = True
    # location rides on the group the connection is nested under, never a field
    return flat


def _compact_config(config):
    """Runtime shape → the nested on-disk one.

    The runtime keeps `connections` flat (conn_id → conn) and carries the
    location settings layers under `connectionDefaults`; this is the one
    place the two shapes meet."""
    settings = _resolve_settings(config.get("settings"))
    defaults = config.get("connectionDefaults") or {}

    by_location = {}
    for conn_id, conn in config["connections"].items():
        if conn_id in LOCATIONS:
            continue
        location = conn.get("location") or "local"
        by_location.setdefault(location, {})[conn_id] = conn

    nested = {}
    for loc in LOCATIONS:
        location_conns = by_location.get(loc, {})
        loc_settings = defaults.get(loc)

        if not location_conns and loc_settings is None:
            continue

        block = {}
        if loc_settings is not None:
            block["settings"] = loc_settings

        for conn_id, conn in location_conns.items():
            flat = _compact_conn(conn, settings, defaults)
            if flat:
                block[conn_id] = flat

        if block:
            nested[loc] = block

    return {
        "version": CONFIG_VERSION,
        **({"global": config["global"]} if config.get("global") else {}),
        **({"remote": config["remote"]} if config.get("remote") else {}),
        "settings": settings,
        "connections": nested,
    }


def settings_block_errors(block, what="settings"):
    """Problems with one settings block (the global one or a location's
    defaults); empty means valid. Knobs must be positive integers, schedule
    fields must match their connection-level shapes, unknown keys are
    rejected so typos never silently no-op."""
    if not block:
        return []
    if not isinstance(block, dict):
        return [f"{what}: must be an object"]
    errors = []
    for key, value in block.items():
        if key == "schedule":
            continue
        if key not in KNOB_KEYS:
            errors.append(
                f"{what}: unknown key '{key}' (knobs: {', '.join(KNOB_KEYS)}; or a 'schedule' block)"
            )
        elif key == "prompt":
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{what}: prompt must be a non-empty string")
        elif key == "wakeWhenAsleep":
            if not isinstance(value, bool):
                errors.append(f"{what}: wakeWhenAsleep must be a boolean")
        elif not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"{what}: {key} must be an integer > 0")
    schedule = block.get("schedule")
    if schedule is None:
        return errors
    if not isinstance(schedule, dict):
        return errors + [f"{what}.schedule: must be an object"]
    for key, value in schedule.items():
        if key not in SCHEDULE_SETTINGS_KEYS:
            errors.append(
                f"{what}.schedule: unknown key '{key}' (known: {', '.join(SCHEDULE_SETTINGS_KEYS)})"
            )
        elif key == "mode" and value not in SCHEDULE_MODES:
            errors.append(f"{what}.schedule: mode must be one of: {', '.join(SCHEDULE_MODES)}")
        elif key == "times" and (
            not isinstance(value, list)
            or not value
            or not all(isinstance(slot, str) and SLOT_RE.match(slot) for slot in value)
        ):
            errors.append(f"{what}.schedule: times must be a non-empty list of HH:MM")
        elif key == "days" and value not in DAY_RULES:
            errors.append(f"{what}.schedule: days must be 'weekday' or 'every-day'")
        elif key == "windowMinutes" and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            errors.append(f"{what}.schedule: windowMinutes must be an integer > 0")
        elif key in ("skipIfActivatedMinutes", "graceSeconds", "jitterSeconds") and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            errors.append(f"{what}.schedule: {key} must be an integer >= 0")
    return errors


def remote_errors(remote):
    """Problems with the top-level remote-server block; empty means valid."""
    if not remote:
        return []
    errors = []
    url = remote.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        errors.append("remote: url must be the http(s) address of an `awewarm serve` process")
    token_ref = remote.get("tokenRef")
    if not isinstance(token_ref, str) or not token_ref.startswith("file:"):
        errors.append("remote: tokenRef must be a file: secret reference")
    return errors


def save_config(config, path=None):
    defaults = config.get("connectionDefaults") or {}
    blocks = [("settings", config.get("settings"))]
    blocks.extend((f"connectionDefaults.{loc}", defaults[loc]) for loc in LOCATIONS if loc in defaults)
    for name, block in blocks:
        errors = settings_block_errors(block, name)
        if errors:
            die(f"refusing to save invalid {name}:\n  " + "\n  ".join(errors))
    for conn_id, conn in config["connections"].items():
        if conn_id in LOCATIONS:
            continue
        errors = connection_errors(conn, conn_id)
        if errors:
            die(f"refusing to save invalid connection {conn_id}:\n  " + "\n  ".join(errors))
    errors = remote_errors(config.get("remote"))
    if errors:
        die("refusing to save invalid remote block:\n  " + "\n  ".join(errors))
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

    if not isinstance(conn.get("hide", False), bool):
        errors.append(f"{conn_id}: hide must be a boolean")

    location = conn.get("location", "local")
    if location not in LOCATIONS:
        errors.append(f"{conn_id}: location must be 'local' or 'remote'")
    elif location == "remote" and kind == KIND_ACCOUNT:
        errors.append(
            f"{conn_id}: account connections cannot be remote — their CLI login lives on this machine"
        )

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

    interval_locked = not _window_allows_interval(window)
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
            if not isinstance(schedule.get("wakeWhenAsleep", False), bool):
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


OFFSET_TZ_RE = re.compile(r"^UTC([+-])([01]\d|2[0-3]):([0-5]\d)$")


def timezone_for(name):
    """tzinfo for an IANA zone name or a fixed offset ("UTC+08:00"); ValueError otherwise.

    Fixed-offset names carry delegation from machines whose local zone has no
    IANA name (Windows): their fixed slots still fire at the right wall-clock
    times, they just never follow DST rules a named zone would.
    """
    if isinstance(name, str):
        try:
            return ZoneInfo(name)
        except Exception:
            match = OFFSET_TZ_RE.match(name)
            if match:
                sign = 1 if match.group(1) == "+" else -1
                return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    raise ValueError(f"not a timezone: {name!r}")


def slugify(label):
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return slug or "plan"


def unique_connection_id(config, label):
    base = slugify(label)
    conn_id = base
    counter = 2
    # "local"/"remote" are the location group names in the on-disk format —
    # a connection named after one would be swallowed by the grouping.
    while conn_id in config["connections"] or conn_id in LOCATIONS:
        conn_id = f"{base}-{counter}"
        counter += 1
    return conn_id
