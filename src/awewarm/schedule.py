"""Pure scheduling logic: decide which actions are due, and how state moves.

No network, no disk, no clock reads — `now` is always injected. Actions are
plain dicts so `run` can execute or dry-run them.

Action shapes:
  {"type": "activate", "reason": "fixed"|"interval"|"first-anchor", "slot": "HH:MM"?, "slotAt": datetime?}
  {"type": "skip-slot", "slot": "HH:MM", "why": "past-catchup"|"recently-activated"}
"""
import random
from datetime import datetime, timedelta

from .config import (
    DEFAULT_CATCHUP_MINUTES,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_JITTER_SECONDS,
    DEFAULT_SKIP_IF_ACTIVATED_MINUTES,
    SLOT_RE,
)

RETRY_THROTTLE = timedelta(minutes=5)
DEGRADE_AFTER_FAILURES = 3
SLOT_KEEP_DAYS = 7


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
    slots = []
    for k in range(8):  # guard only: the day bound below already caps the list
        minutes = start + k * step
        if minutes >= 24 * 60:  # wrapped past the anchor — day is covered
            break
        slots.append(f"{minutes // 60:02d}:{minutes % 60:02d}")
    return slots


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


def _due_fixed(connection, conn_state, now):
    """At most one activate for the earliest due slot, plus skip bookkeeping."""
    fixed = connection["schedule"].get("fixed") or {}
    if not is_active_day(now.date(), fixed.get("days", "weekday")):
        return [], None
    at_times = fixed.get("at") or []
    catchup = timedelta(minutes=fixed.get("catchUpWindowMinutes", DEFAULT_CATCHUP_MINUTES))
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
            pending_skip.append({"type": "skip-slot", "slot": hhmm, "why": "past-catchup"})
            continue
        if last_ok is not None and slot_at - last_ok < skip_window:
            pending_skip.append({"type": "skip-slot", "slot": hhmm, "why": "recently-activated"})
            continue
        if _throttled(conn_state, now):
            continue
        activate = {"type": "activate", "reason": "fixed", "slot": hhmm, "slotAt": slot_at}
        break
    return pending_skip, activate


def _due_interval(connection, conn_state, now):
    if conn_state.get("intervalDisabledAt"):
        return None
    if _last_success(conn_state) is None:
        # No anchor yet: fire once to open the first window.
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


def plan_actions(connection, conn_state, now):
    """All bookkeeping actions plus at most one activation for this tick."""
    mode = connection["schedule"]["mode"]
    actions = []
    activate = None
    if mode == "fixed":
        pending_skip, activate = _due_fixed(connection, conn_state, now)
        actions.extend(pending_skip)
    if activate is None and mode == "interval":
        activate = _due_interval(connection, conn_state, now)
    if activate is not None:
        actions.append(activate)
    return actions


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
    conn_state["consecutiveFailures"] = 0
    conn_state["intervalDisabledAt"] = None
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
    conn_state["consecutiveFailures"] = 0
    conn_state["intervalDisabledAt"] = None
    if kind == "fixed" and slot:
        day_key = now.strftime("%Y-%m-%d")
        slots = conn_state["completedSlots"].setdefault(day_key, [])
        if slot not in slots:
            slots.append(slot)
    if reset_due and connection["schedule"]["mode"] == "interval":
        conn_state["nextDueAt"] = iso(compute_next_due(connection, now))
    _push_history(conn_state, now, kind, "success", None)


def record_failure(conn_state, now, kind, error):
    """Apply a failed activation; auto-pause interval after repeated failures."""
    conn_state["lastResult"] = "failure"
    conn_state["lastError"] = str(error)[:300]
    conn_state["consecutiveFailures"] = conn_state.get("consecutiveFailures", 0) + 1
    mode_degrades = conn_state.get("consecutiveFailures", 0) >= DEGRADE_AFTER_FAILURES
    if mode_degrades and not conn_state.get("intervalDisabledAt"):
        conn_state["intervalDisabledAt"] = iso(now)
    _push_history(conn_state, now, kind, "failure", conn_state["lastError"])


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
    mode = connection["schedule"]["mode"]
    candidates = []
    if mode == "fixed":
        fixed = connection["schedule"].get("fixed") or {}
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
                    if slot_at + timedelta(
                        minutes=fixed.get("catchUpWindowMinutes", DEFAULT_CATCHUP_MINUTES)
                    ) <= now and day == now.date():
                        continue  # today's slot is already past its catch-up
                    candidates.append((slot_at, "fixed"))
                    break
                if candidates:
                    break
            day += timedelta(days=1)
    if mode == "interval" and not conn_state.get("intervalDisabledAt"):
        if _last_success(conn_state) is None:
            candidates.append((now, "interval (first anchor)"))
        else:
            due = parse_ts(conn_state.get("nextDueAt"))
            if due is None:
                due = compute_next_due(connection, _last_success(conn_state), jitter_seconds=0)
            candidates.append((due, "interval"))
    if not candidates:
        return None, None
    return min(candidates, key=lambda pair: pair[0])
