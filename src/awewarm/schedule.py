"""Pure scheduling logic: decide which actions are due, and how state moves.

No network, no disk, no clock reads — `now` is always injected. Actions are
plain dicts so `run` can execute or dry-run them.

Action shapes:
  {"type": "activate", "reason": "fixed"|"interval"|"first-anchor", "slot": "HH:MM"?, "slotAt": datetime?, "dueAt": datetime?}
  {"type": "skip-slot", "slot": "HH:MM", "why": "past-catchup"|"recently-activated", "lost": True?}
  {"type": "node-lost"}

Health ladder (per connection, both modes share it):
  connected → failing (a node failed, catch-up retries allowed)
            → degraded (N consecutive lost nodes; single shot per node)
            → auto-disabled (N more lost nodes; fully silent until --on)
Any scheduled or manual success resets the whole ladder.
"""
import random
from datetime import datetime, timedelta

from .config import (
    DEFAULT_CATCHUP_ATTEMPTS,
    DEFAULT_CATCHUP_MINUTES,
    DEFAULT_DEGRADE_AFTER_NODES,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_JITTER_SECONDS,
    DEFAULT_SKIP_IF_ACTIVATED_MINUTES,
    SLOT_RE,
    connection_errors,
)


def status_word(conn_id, conn, cs):
    """The one-word health rung: disabled / invalid / auto-disabled /
    degraded / failing / connected. Shared by status rendering and the hub's
    tenant table so the ladder can never read differently in the two places."""
    if not conn.get("enabled", True):
        return "disabled"
    if connection_errors(conn, conn_id):
        return "invalid"
    if cs.get("autoDisabledAt"):
        return "auto-disabled"
    if cs.get("degradedAt"):
        return "degraded"
    if cs.get("nodeKey") or cs.get("failedNodes", 0) > 0:
        return "failing"
    return "connected"

RETRY_THROTTLE = timedelta(minutes=5)
SLOT_KEEP_DAYS = 7


def _catchup(connection):
    block = connection.get("catchup") or {}
    attempts = block.get("attempts", DEFAULT_CATCHUP_ATTEMPTS)
    within = timedelta(minutes=block.get("withinMinutes", DEFAULT_CATCHUP_MINUTES))
    return max(1, attempts), within


def _degrade_after(connection):
    return max(1, connection.get("degradeAfterNodes", DEFAULT_DEGRADE_AFTER_NODES))


def _window(connection):
    return timedelta(minutes=(connection["window"].get("durationMinutes") or 60))


def window_override_notice(old_window, new_minutes, grace_seconds=None):
    """Warning when a user-set duration overrides a verified window, or None.

    Advisory only — the caller prints it and the user decides. A renewal
    cadence shorter than the real window fires inside the still-open window
    and starts nothing, leaving a cold gap of the difference each cycle.
    """
    if not old_window or old_window.get("status") != "verified":
        return None
    real = old_window.get("durationMinutes")
    if not isinstance(real, int) or real <= 0 or real == new_minutes:
        return None
    if grace_seconds is None:
        grace_seconds = DEFAULT_GRACE_SECONDS
    if new_minutes * 60 + grace_seconds < real * 60:
        return (
            f"⚠ The verified window is {real} minutes, but you recorded {new_minutes}."
            f" Renewal fires {new_minutes} min + grace after each success — inside the"
            " still-open window, so it starts nothing and leaves a"
            f" ~{real - new_minutes} min cold gap each cycle."
            f" Keep {real} unless you verified a different window yourself."
        )
    return (
        f"⚠ The verified window is {real} minutes, but you recorded {new_minutes}."
        f" Renewal now fires every ~{new_minutes} min instead."
    )


def parse_ts(value):
    """Parse an ISO timestamp written by this tool; None for missing/naive."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def iso(moment):
    return moment.isoformat()


def node_for(action, now):
    """Scheduled node an activate action belongs to; manual fires pass None.

    Shared by the local tick and the serve engines (`awewarm serve`,
    `awewarm-hub serve`) so all count catch-up and ladder nodes identically.
    """
    reason = action.get("reason")
    if reason == "fixed":
        return {
            "key": f"{action['slotAt'].strftime('%Y-%m-%d')} {action['slot']}",
            "dueAt": action["slotAt"],
            "slot": action["slot"],
        }
    if reason == "interval":
        due = action.get("dueAt") or now
        return {"key": f"interval {iso(due)}", "dueAt": due}
    if reason == "first-anchor":
        return {"key": "first-anchor", "dueAt": None}
    return None


def slot_datetime(day, hhmm, tz):
    """Local datetime for a HH:MM slot on a date. Never raises on DST edges."""
    match = SLOT_RE.match(hhmm)
    if not match:
        return None
    return datetime(day.year, day.month, day.day, int(match.group(1)), int(match.group(2)), tzinfo=tz)


def is_active_day(day, days_rule):
    if days_rule == "every-day":
        return True
    return day.weekday() < 5  # weekday rule: Monday..Friday


def grid_times(anchor_hhmm, window_minutes):
    """Full-day fixed slots spaced one window (+5 min) apart from the anchor.

    Each slot opens a window that closes ~5 min before the next slot renews
    it, so the grid keeps a window open around the clock. Slots stop before
    wrapping past the anchor again. [] when a grid can't help (unknown anchor,
    or windows under 2 h — too many slots; interval mode fits those better).
    """
    match = SLOT_RE.match(anchor_hhmm or "")
    if not match or not isinstance(window_minutes, int) or window_minutes < 120:
        return []
    start = int(match.group(1)) * 60 + int(match.group(2))
    step = window_minutes + 5
    return [f"{minutes // 60:02d}:{minutes % 60:02d}" for minutes in range(start, 24 * 60, step)]


def compute_next_due(connection, success_at, jitter_seconds=None):
    """When interval renewal comes due: window + grace (+ jitter).

    Grace runs AFTER the window so the old window has certainly closed —
    firing early would land inside the old window and start nothing.
    """
    window_minutes = connection["window"]["durationMinutes"]
    interval = connection["schedule"].get("interval") or {}
    due = success_at + timedelta(minutes=window_minutes)
    due = due + timedelta(seconds=interval.get("graceSeconds", DEFAULT_GRACE_SECONDS))
    max_jitter = interval.get("jitterSeconds", DEFAULT_JITTER_SECONDS)
    if jitter_seconds is None:
        jitter_seconds = random.uniform(0, max_jitter) if max_jitter else 0
    return due + timedelta(seconds=jitter_seconds)


def _last_success(conn_state):
    return parse_ts(conn_state.get("lastActivationAt"))


def _throttled(conn_state, now):
    """True when a recent failed attempt should hold off another one."""
    last_attempt = parse_ts(conn_state.get("lastAttemptAt"))
    failed = conn_state.get("lastResult") == "failure"
    return failed and last_attempt is not None and now - last_attempt < RETRY_THROTTLE


def _fixed_node_key(day_key, hhmm):
    return f"{day_key} {hhmm}"


def _due_fixed(connection, conn_state, now):
    """At most one activate for the earliest due slot, plus skip bookkeeping.

    In degraded mode a slot still fires (single shot); the node closes and the
    slot is marked skipped on that one failure, so it never refires.
    """
    fixed = connection["schedule"].get("fixed") or {}
    if not is_active_day(now.date(), fixed.get("days", "weekday")):
        return [], None
    defer = parse_ts(conn_state.get("deferUntil"))
    if defer is not None and now < defer:
        return [], None  # --start gate: slots fire late within catch-up once it lifts
    at_times = fixed.get("at") or []
    catchup = _catchup(connection)[1]
    skip_window = timedelta(
        minutes=fixed.get("skipIfActivatedWithinMinutes", DEFAULT_SKIP_IF_ACTIVATED_MINUTES)
    )
    day_key = now.strftime("%Y-%m-%d")
    done = set(conn_state["completedSlots"].get(day_key, []))
    skipped = set(conn_state["skippedSlots"].get(day_key, []))
    last_ok = _last_success(conn_state)
    pending_skip = []
    activate = None
    for hhmm in sorted(at_times):
        if hhmm in done or hhmm in skipped:
            continue
        slot_at = slot_datetime(now.date(), hhmm, now.tzinfo)
        if slot_at is None or now < slot_at:
            continue
        if now > slot_at + catchup:
            expired = {
                "type": "skip-slot",
                "slot": hhmm,
                "why": "past-catchup",
            }
            node_key = conn_state.get("nodeKey")
            if node_key == _fixed_node_key(day_key, hhmm) and conn_state.get("nodeAttempts"):
                expired["lost"] = True
            pending_skip.append(expired)
            continue
        if last_ok is not None and slot_at - last_ok < skip_window:
            pending_skip.append({"type": "skip-slot", "slot": hhmm, "why": "recently-activated"})
            continue
        if _throttled(conn_state, now):
            continue
        activate = {"type": "activate", "reason": "fixed", "slot": hhmm, "slotAt": slot_at}
        break
    return pending_skip, activate


def _probe_at(conn_state, connection):
    """Earliest single-shot probe moment while degraded, or None when running.

    A failed probe re-stamps nextProbeAt for another full window, so degraded
    connections back off without going silent forever.
    """
    explicit = parse_ts(conn_state.get("nextProbeAt"))
    if explicit is not None:
        return explicit
    entered = parse_ts(conn_state.get("degradedAt"))
    if entered is None:
        return None
    return entered + _window(connection)


def _due_interval(connection, conn_state, now):
    if conn_state.get("autoDisabledAt"):
        return None
    defer = parse_ts(conn_state.get("deferUntil"))
    if defer is not None and now < defer:
        return None
    catchup_attempts, catchup_within = _catchup(connection)
    if not conn_state.get("degradedAt"):
        node_due = parse_ts(conn_state.get("nodeDueAt"))
        if conn_state.get("nodeKey") and node_due is not None and now > node_due + catchup_within:
            return {"type": "node-lost"}
        if _last_success(conn_state) is None:
            # No anchor yet: fire once to open the first window. nextDueAt,
            # when set, is the backoff from a lost first-anchor node.
            floor = parse_ts(conn_state.get("nextDueAt"))
            if floor is not None and now < floor:
                return None
            if _throttled(conn_state, now):
                return None
            return {"type": "activate", "reason": "first-anchor"}
        due = parse_ts(conn_state.get("nextDueAt"))
        if due is None:
            # State written before nextDueAt existed; recompute deterministically.
            due = compute_next_due(connection, _last_success(conn_state), jitter_seconds=0)
        if now < due:
            return None
        if _throttled(conn_state, now):
            return None
        return {"type": "activate", "reason": "interval", "dueAt": due}
    probe = _probe_at(conn_state, connection)
    if probe is not None and now < probe:
        return None
    if _throttled(conn_state, now):
        return None
    return {"type": "activate", "reason": "interval", "dueAt": probe}


def plan_actions(connection, conn_state, now):
    """All bookkeeping actions plus at most one activation for this tick."""
    migrate_state(conn_state)
    if conn_state.get("autoDisabledAt"):
        return []
    mode = connection["schedule"]["mode"]
    actions = []
    activate = None
    if mode == "fixed":
        pending_skip, activate = _due_fixed(connection, conn_state, now)
        actions.extend(pending_skip)
    else:
        activate = _due_interval(connection, conn_state, now)
        if activate is not None and activate.get("type") == "node-lost":
            actions.append(activate)
            activate = None
    if activate is not None:
        actions.append(activate)
    return actions


def dispatch_actions(connection, conn_state, now, activate):
    """Run one connection's planned actions through the shared bookkeeping.

    Every tick engine — the local `awewarm tick`, `awewarm serve`, and
    `awewarm-hub serve` — routes through this so skip bookkeeping, node
    closure, and pruning stay identical;
    only the I/O differs. activate(action, node) sends the real request and
    returns its {"ok", "detail"} dict, or None when the attempt was held
    (nothing sent, no state recorded — the server's key-missing case).
    Returns (results, skipped_slots): results in dispatch order.
    """
    results, skipped = [], 0
    for action in plan_actions(connection, conn_state, now):
        if action["type"] == "skip-slot":
            record_skip(conn_state, now, action["slot"], action["why"])
            skipped += 1
            if action.get("lost"):
                close_lost_node(conn_state, connection, now, "catch-up window expired")
            continue
        if action["type"] == "node-lost":
            close_lost_node(conn_state, connection, now, "catch-up window expired")
            continue
        result = activate(action, node_for(action, now))
        if result is not None:
            results.append(result)
    prune_state(conn_state, now)
    return results, skipped


def reset_ladder(conn_state):
    """Clear the whole health ladder; schedule memory (anchor, slots) stays."""
    for key in ("nodeKey", "nodeDueAt", "nodeSlot", "degradedAt", "nextProbeAt", "autoDisabledAt"):
        conn_state[key] = None
    conn_state["nodeAttempts"] = 0
    conn_state["failedNodes"] = 0
    conn_state["degradedFailedNodes"] = 0


def migrate_state(conn_state):
    """Fold pre-ladder state fields into the new ones, in place."""
    if conn_state.get("intervalDisabledAt"):
        stamp = parse_ts(conn_state["intervalDisabledAt"])
        conn_state["degradedAt"] = conn_state["intervalDisabledAt"]
        conn_state["nextProbeAt"] = None  # _probe_at derives stamp + window from degradedAt
        conn_state["degradedFailedNodes"] = 0
        if stamp is None:
            conn_state["degradedAt"] = None
    conn_state.pop("intervalDisabledAt", None)
    conn_state.pop("consecutiveFailures", None)


def _open_node(conn_state, node, now):
    if conn_state.get("nodeKey") != node["key"]:
        conn_state["nodeKey"] = node["key"]
        conn_state["nodeDueAt"] = iso(node.get("dueAt") or now)
        conn_state["nodeSlot"] = node.get("slot")
        conn_state["nodeAttempts"] = 0


def close_lost_node(conn_state, connection, now, why):
    """Resolve the open node as lost and move the ladder one rung if due.

    In fixed mode the lost slot is marked skipped so it never refires today.
    """
    slot = conn_state.get("nodeSlot")
    node_due = parse_ts(conn_state.get("nodeDueAt"))
    conn_state["nodeKey"] = None
    conn_state["nodeDueAt"] = None
    conn_state["nodeSlot"] = None
    conn_state["nodeAttempts"] = 0
    if slot and node_due is not None and connection["schedule"]["mode"] == "fixed":
        day_key = node_due.strftime("%Y-%m-%d")
        slots = conn_state.setdefault("skippedSlots", {}).setdefault(day_key, [])
        if slot not in slots:
            slots.append(slot)
    threshold = _degrade_after(connection)
    if conn_state.get("degradedAt"):
        lost = conn_state.get("degradedFailedNodes", 0) + 1
        conn_state["degradedFailedNodes"] = lost
        if lost >= threshold:
            conn_state["autoDisabledAt"] = iso(now)
            _push_history(conn_state, now, "ladder", "auto-disabled", why)
            return
        _push_history(conn_state, now, "ladder", "node-lost", why)
    else:
        lost = conn_state.get("failedNodes", 0) + 1
        conn_state["failedNodes"] = lost
        if lost >= threshold:
            conn_state["degradedAt"] = iso(now)
            conn_state["degradedFailedNodes"] = 0
            _push_history(conn_state, now, "ladder", "degraded", why)
        else:
            _push_history(conn_state, now, "ladder", "node-lost", why)
    if connection["schedule"]["mode"] == "interval":
        # Next node one full window out — pure backoff, the old window is closed.
        if conn_state.get("degradedAt"):
            conn_state["nextProbeAt"] = iso(now + _window(connection))
        else:
            conn_state["nextDueAt"] = iso(now + _window(connection))


def apply_user_anchor(conn_state, connection, reset_at):
    """Seed renewal from a user-reported reset time (window already open).

    The window is treated as having opened at reset - duration, so renewal
    fires right after the reported close instead of a wasteful immediate
    first anchor that would land inside the still-open window.
    """
    duration_minutes = connection["window"]["durationMinutes"]
    opened_at = reset_at - timedelta(minutes=duration_minutes)
    conn_state["lastActivationAt"] = iso(opened_at)
    conn_state["lastAttemptAt"] = iso(opened_at)
    conn_state["lastResult"] = "success"
    conn_state["lastError"] = None
    conn_state["deferUntil"] = None
    reset_ladder(conn_state)
    conn_state["nextDueAt"] = iso(compute_next_due(connection, opened_at, jitter_seconds=0))
    _push_history(conn_state, opened_at, "user-anchor", "success", None)


def record_attempt(conn_state, now):
    conn_state["lastAttemptAt"] = iso(now)


def record_success(conn_state, connection, now, kind, slot=None, reset_due=True):
    """Apply a successful activation: anchor, renewal chain, slot completion.

    reset_due=False keeps the interval chain's nextDueAt untouched, for manual
    fires that must not push the renewal cadence out by a full window.
    """
    conn_state["lastActivationAt"] = iso(now)
    conn_state["lastResult"] = "success"
    conn_state["lastError"] = None
    conn_state["deferUntil"] = None
    reset_ladder(conn_state)
    if kind == "fixed" and slot:
        day_key = now.strftime("%Y-%m-%d")
        slots = conn_state["completedSlots"].setdefault(day_key, [])
        if slot not in slots:
            slots.append(slot)
    if reset_due and connection["schedule"]["mode"] == "interval":
        conn_state["nextDueAt"] = iso(compute_next_due(connection, now))
    _push_history(conn_state, now, kind, "success", None)


def record_failure(conn_state, connection, now, kind, error, node=None):
    """Apply a failed activation and advance the catch-up/ladder bookkeeping.

    node identifies the scheduled node the attempt belongs to; manual and
    verify fires pass None and never count as nodes.
    """
    conn_state["lastResult"] = "failure"
    conn_state["lastError"] = str(error)[:300]
    _push_history(conn_state, now, kind, "failure", conn_state["lastError"])
    if node is None:
        return
    _open_node(conn_state, node, now)
    conn_state["nodeAttempts"] += 1
    if conn_state.get("degradedAt"):
        # Single-shot rung: the failed attempt was the whole node.
        close_lost_node(conn_state, connection, now, "single-shot failed")
        return
    if conn_state["nodeAttempts"] >= _catchup(connection)[0]:
        close_lost_node(conn_state, connection, now, "catch-up exhausted")


def record_skip(conn_state, now, slot, why):
    day_key = now.strftime("%Y-%m-%d")
    slots = conn_state["skippedSlots"].setdefault(day_key, [])
    if slot not in slots:
        slots.append(slot)
    _push_history(conn_state, now, f"fixed:{slot}", "skipped", why)


def _push_history(conn_state, now, kind, result, error):
    entry = {"at": iso(now), "kind": kind, "result": result, "error": error}
    history = conn_state.setdefault("history", [])
    history.append(entry)
    del history[:-DEFAULT_HISTORY_LIMIT]


def prune_state(conn_state, now):
    """Drop per-day slot records older than SLOT_KEEP_DAYS days."""
    cutoff = (now - timedelta(days=SLOT_KEEP_DAYS)).strftime("%Y-%m-%d")
    for key in ("completedSlots", "skippedSlots"):
        days = conn_state.get(key) or {}
        for day_key in [d for d in days if d < cutoff]:
            del days[day_key]


def next_due(connection, conn_state, now):
    """Earliest future activation moment, for status display. None if none."""
    migrate_state(conn_state)
    if conn_state.get("autoDisabledAt"):
        return None, None
    mode = connection["schedule"]["mode"]
    candidates = []
    defer = parse_ts(conn_state.get("deferUntil"))
    if mode == "fixed":
        fixed = connection["schedule"].get("fixed") or {}
        catchup = _catchup(connection)[1]
        day = now.date()
        for _ in range(8):  # scan up to a week ahead for the next active day
            if is_active_day(day, fixed.get("days", "weekday")):
                day_key = day.strftime("%Y-%m-%d")
                blocked = set((conn_state.get("completedSlots") or {}).get(day_key, []))
                blocked |= set((conn_state.get("skippedSlots") or {}).get(day_key, []))
                for hhmm in sorted(fixed.get("at") or []):
                    if hhmm in blocked:
                        continue
                    slot_at = slot_datetime(day, hhmm, now.tzinfo)
                    if slot_at is None:
                        continue
                    if slot_at + catchup <= now and day == now.date():
                        continue  # today's slot is already past its catch-up
                    moment, kind = slot_at, "fixed"
                    if defer is not None and defer > moment:
                        moment, kind = defer, "fixed (deferred)"
                    candidates.append((moment, "fixed (single-shot)" if conn_state.get("degradedAt") else kind))
                    break
                if candidates:
                    break
            day += timedelta(days=1)
    if mode == "interval":
        if conn_state.get("degradedAt"):
            probe_at = _probe_at(conn_state, connection)
            if probe_at is not None and now < probe_at:
                if defer is not None and defer > probe_at:
                    probe_at = defer
                candidates.append((probe_at, "interval (probing after failures)"))
            else:
                candidates.append((now, "interval (probing after failures)"))
        elif _last_success(conn_state) is None:
            moment = now
            floor = parse_ts(conn_state.get("nextDueAt"))
            if floor is not None and floor > moment:
                moment = floor  # backoff from a lost first-anchor node
            if defer is not None and defer > moment:
                moment = defer
            candidates.append((moment, "interval (first anchor)"))
        else:
            due = parse_ts(conn_state.get("nextDueAt"))
            if due is None:
                due = compute_next_due(connection, _last_success(conn_state), jitter_seconds=0)
            if defer is not None and defer > due:
                due = defer
            candidates.append((due, "interval"))
    if not candidates:
        return None, None
    return min(candidates, key=lambda pair: pair[0])
