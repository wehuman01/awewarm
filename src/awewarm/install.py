"""System scheduler installation: launchd on macOS, Task Scheduler on Windows,
systemd user timer on Linux.

The agent simply invokes `awewarm tick` once a minute; all scheduling state
lives in state.json, so the task definition itself is static. Where systemd
is unavailable (e.g. some servers or containers), cron the same command. The
tick self-heals: if the installed job's command line is stale (older
awewarm, or an upgrade done via `pip install --upgrade` directly), the
next tick rewrites it.

Windows notes: schtasks tasks run in the user context and inherit user env
vars (what `setx` writes), so unlike the launchd plist nothing needs to bake
PATH or AWEWARM_* into the task; connections already store absolute CLI
paths. The task's stdout is not captured by Task Scheduler — awewarm's own
event log (`awewarm.log`) is the audit trail.

Linux notes: the user manager's environment is sparser than a login shell
(same problem as launchd's PATH), so AWEWARM_* and PATH are written into the
service unit as Environment= lines. SSH-only accounts may need
`loginctl enable-linger $USER` before the user manager runs without a
session; `awewarm scheduler install` says so when systemctl cannot reach the bus.
"""
import csv
import io
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import schedule
from .config import (
    SLOT_RE,
    append_log,
    conn_state,
    connection_errors,
    die,
    load_config,
    load_state,
    log_path,
    save_state,
    state_path,
)

LABEL = "com.awewarm.scheduler"
WAKE_TASK_PREFIX = f"{LABEL}.wake-"
IWAKE_TASK_PREFIX = f"{LABEL}.iwake-"
TICK_SECONDS = 60

# macOS-only: wake-from-sleep is layered. StartCalendarInterval fires the tick
# at the exact slot time while the machine is awake; the RTC wake layer
# (pmset one-shot events, armed by the tick through a scoped sudoers grant)
# wakes the lid-closed machine first. Legacy pmset repeat events registered
# by awewarm < 0.4 are cancelled on scheduler install/uninstall.
WAKE_TYPE = "wakeorpoweron"
SUDOERS_PATH = Path("/etc/sudoers.d/awewarm")
PMSET_BIN = "/usr/bin/pmset"
WAKE_EVENT_LIMIT = 16  # one-shot RTC events kept armed at once
WAKE_HORIZON = timedelta(days=2)  # far enough to survive an overnight sleep


def plist_path():
    return Path(
        os.environ.get("AWEWARM_PLIST", f"~/Library/LaunchAgents/{LABEL}.plist")
    ).expanduser()


def unit_dir():
    return Path(
        os.environ.get("AWEWARM_SYSTEMD_DIR", "~/.config/systemd/user")
    ).expanduser()


def service_path():
    return unit_dir() / "awewarm.service"


def timer_path():
    return unit_dir() / "awewarm.timer"


def build_plist(exe, calendar=None):
    out_log = state_path().parent / "launchd.log"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("AWEWARM_") and value
    }
    # launchd's default PATH (/usr/bin:/bin:...) lacks user-local install
    # dirs, so bare CLI names like "claude" would not resolve inside ticks.
    if os.environ.get("PATH"):
        environment["PATH"] = os.environ["PATH"]
    plist = {
        "Label": LABEL,
        "ProgramArguments": [exe, "tick"],
        "StartInterval": TICK_SECONDS,
        "RunAtLoad": True,
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(out_log),
        "EnvironmentVariables": environment,
    }
    if calendar:
        plist["StartCalendarInterval"] = calendar
    return plist


def resolve_exe():
    exe = shutil.which("awewarm")
    if exe:
        return exe
    die(
        "awewarm entry point not found on PATH\n"
        "fix: reinstall with: pip install -e . (or pip install awewarm), then retry: awewarm scheduler install"
    )


def install_scheduler():
    if sys.platform == "darwin":
        return _install_launchd()
    if sys.platform == "win32":
        return _install_windows()
    if sys.platform.startswith("linux"):
        return _install_linux()
    die(
        "automatic scheduler install supports macOS (launchd), Windows (Task Scheduler),\n"
        "and Linux (systemd user timer)\n"
        "elsewhere, cron the tick instead:\n  * * * * * awewarm tick"
    )


def uninstall_scheduler():
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if sys.platform == "win32":
        return _uninstall_windows()
    if sys.platform.startswith("linux"):
        return _uninstall_linux()
    die("scheduler uninstall supports macOS, Windows, and Linux (systemd)")


def scheduler_installed():
    if sys.platform == "darwin":
        return plist_path().exists()
    if sys.platform == "win32":
        try:
            return _schtasks(["/Query", "/TN", LABEL]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    if sys.platform.startswith("linux"):
        return timer_path().exists()
    return False


def _maybe_self_heal_job(config=None):
    """Rewrite the installed scheduler job if its command line is outdated or,
    on macOS/Windows, its wake entries no longer match the config.

    Called at the top of every scheduler tick. Cheap (one file read or one
    `schtasks /Query`). No-op in the common case where the job already
    invokes `awewarm tick` and the wake entries are current.

    Covers users who upgraded via `pip install --upgrade awewarm` directly,
    bypassing `awewarm self-update` — the next tick detects the old job, rewrites
    it, and from the tick after that the new command line is in use.
    """
    try:
        if sys.platform == "darwin":
            plist = plist_path()
            if not plist.exists():
                return
            with open(plist, "rb") as handle:
                data = plistlib.load(handle)
            if "tick" not in (data.get("ProgramArguments") or []):
                install_scheduler()
                return
            if (data.get("StartCalendarInterval") or []) != calendar_entries(
                config or load_config()
            ):
                install_scheduler()
            return
        if sys.platform == "win32":
            try:
                result = _schtasks(["/Query", "/TN", LABEL, "/FO", "LIST", "/V"])
            except (OSError, subprocess.SubprocessError):
                return
            if result.returncode != 0:
                return
            if "tick" not in (result.stdout or ""):
                install_scheduler()
                return
            installed = wake_task_times()
            if installed is not None and installed != _wake_time_keys(
                calendar_entries(config or load_config())
            ):
                sync_windows_wake(config or load_config())
            return
        if sys.platform.startswith("linux"):
            svc = service_path()
            if not svc.exists():
                return
            if "tick" in svc.read_text():
                return
            install_scheduler()
    except (OSError, subprocess.SubprocessError, ValueError, SystemExit):
        # A self-heal failure must not break the tick — including die()
        # (SystemExit) from resolve_exe or a failed launchctl bootstrap.
        # The old job, even with a stale command line, will just hit the
        # non-tty error path which produces a clear log entry. The next
        # tick retries.
        return


def _install_launchd():
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    with open(plist, "wb") as handle:
        plistlib.dump(build_plist(resolve_exe(), calendar_entries(load_config())), handle)
    uid = os.getuid()
    # A stale registration for the same label breaks bootstrap; ignore errors.
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True, timeout=30
    )
    boot = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True, text=True, timeout=30,
    )
    if boot.returncode != 0:
        legacy = subprocess.run(
            ["launchctl", "load", str(plist)], capture_output=True, text=True, timeout=30
        )
        if legacy.returncode != 0:
            die(
                f"launchctl failed to load {plist}\n{(boot.stderr or legacy.stderr or '').strip()}\n"
                "fix: resolve the launchctl error, or load manually: launchctl load " + str(plist)
            )
    return plist


def _uninstall_launchd():
    plist = plist_path()
    uid = os.getuid()
    was_present = plist.exists()
    if was_present:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True, timeout=30
        )
        plist.unlink()
    else:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True, timeout=30
        )
    return was_present


def calendar_entries(config):
    """Fixed-slot wake schedule, shared by platform wake mechanisms.

    launchd consumes them as StartCalendarInterval entries; Windows registers
    one WakeToRun task per entry (see build_wake_ps1). These fire the tick at
    the exact slot time whenever the machine is awake — every slot is covered,
    unlike pmset repeat, which held a single event. Entries fire every day
    regardless of the slot's day rule: the tick itself decides whether today
    is an active day, so a weekend wake for a weekday-only slot is a harmless
    no-op. Waking a lid-closed sleeping machine first is the RTC wake layer's
    job (see sync_wake_events).
    """
    entries = {}
    for conn in (config.get("connections") or {}).values():
        if not conn.get("enabled", True):
            continue
        if conn.get("location") == "remote":
            continue  # the remote server ticks these — no local wake needed
        schedule = conn.get("schedule") or {}
        if schedule.get("mode") != "fixed":
            continue
        fixed = schedule.get("fixed") or {}
        if not schedule.get("wakeWhenAsleep", False):
            continue
        for slot in fixed.get("at") or []:
            if not SLOT_RE.match(slot):
                continue
            hh, mm = (int(part) for part in slot.split(":"))
            entries[(hh, mm)] = {"Hour": hh, "Minute": mm}
    return [entries[key] for key in sorted(entries)]


def refresh_wake(config):
    """Rewrite the installed wake schedule when it no longer matches the
    config (fixed times/days/wake opt-in changed).

    Called from config-editing commands; the tick's self-heal covers the same
    drift for edits that bypass the CLI. Returns True when something was
    rewritten. No-op on platforms without wake support or when nothing is
    installed.
    """
    if sys.platform == "darwin":
        plist = plist_path()
        if not plist.exists():
            return False
        try:
            with open(plist, "rb") as handle:
                data = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return False
        if (data.get("StartCalendarInterval") or []) == calendar_entries(config):
            return False
        _install_launchd()
        return True
    if sys.platform == "win32":
        try:
            if _schtasks(["/Query", "/TN", LABEL]).returncode != 0:
                return False
        except (OSError, subprocess.SubprocessError):
            return False
        return sync_windows_wake(config)
    return False


def _sudo_cmd(argv, interactive=True):
    """Run argv with sudo; -n first so scripted calls fail fast instead of
    hanging, plain sudo second so interactive calls can prompt."""
    prefixes = (["sudo", "-n"], ["sudo"]) if interactive else (["sudo", "-n"],)
    for prefix in prefixes:
        result = subprocess.run(
            [*prefix, *argv], capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return True
    return False


def _sudo_pmset(args, interactive=True):
    return _sudo_cmd([PMSET_BIN, *args], interactive=interactive)


def _current_repeat_line():
    """Repeating-events block from `pmset -g sched` — header plus event lines,
    '' when none. macOS prints this block differently across versions (older:
    one line holding 'wake at 05:55:00'; newer: a 'Repeating power events:'
    header with 'wakepoweron at 6:30AM every day' beneath), so callers match
    normalized wall-clock times instead of raw strings.
    """
    result = subprocess.run(
        ["pmset", "-g", "sched"], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return ""
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Repeating power event"):
            block = [line]
            for follow in lines[index + 1:]:
                if not follow.strip() or follow.lstrip().startswith("Scheduled power"):
                    break
                block.append(follow)
            return "\n".join(block)
    return ""


def _normalize_wallclock(text):
    """First HH:MM[:SS][AM/PM] in text → 'H:MM' on 24h, or None."""
    match = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?", str(text), re.IGNORECASE)
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), match.group(2), (match.group(3) or "").upper()
    if meridiem == "PM" and hour != 12:
        hour += 12
    if meridiem == "AM" and hour == 12:
        hour = 0
    return f"{hour}:{minute}"


def _repeat_block_has_time(block, spec_time):
    """True when the pmset repeating block contains the spec's wall-clock time."""
    want = _normalize_wallclock(spec_time)
    if not want:
        return False
    return any(
        normalized == want
        for normalized in map(
            _normalize_wallclock,
            re.findall(r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?", block, re.IGNORECASE),
        )
    )


def cancel_wake_schedule(interactive=True):
    """One-time cleanup of the pmset repeat wake registered by awewarm < 0.4 —
    cancelled only if the live repeating event is still ours. The state key
    survives a failed cancel so a later install/uninstall/update retries.

    Returns (status, spec): status is "none" (we never set one), "changed"
    (the user replaced it — leave it alone), "cancelled", or "failed".
    """
    state = load_state()
    spec = state.get("wakeSchedule")
    if not spec:
        return "none", None
    if not _repeat_block_has_time(_current_repeat_line(), spec["time"]):
        state.pop("wakeSchedule", None)
        save_state(state)
        return "changed", spec
    args = ["repeat", "cancel", spec["type"], spec["days"], spec["time"]]
    if not _sudo_pmset(args, interactive):
        return "failed", spec
    state.pop("wakeSchedule", None)
    save_state(state)
    return "cancelled", spec


# --- RTC wake layer -------------------------------------------------------
#
# StartCalendarInterval runs jobs only while the machine is awake (a job whose
# moment passes during sleep fires, coalesced, at whatever wake happens next).
# The only user-controllable way to wake a lid-closed sleeping Mac on schedule
# is an RTC power event: `pmset schedule wakeorpoweron`. Those one-shot events
# stack, but arming them needs root, so the layer has two halves:
#
#   * a scoped sudoers grant (install_wake_grant) letting this user arm and
#     cancel wake events without a password — no other root capability;
#   * sync_wake_events, run at the tail of every tick: it recomputes the
#     moments the schedules next need the machine awake for and converges the
#     armed events to them. Interval chains drift by design (each renewal
#     re-anchors on its success), so re-deriving every tick follows the drift
#     with no pre-computed table to go stale. pmset events carry no owner
#     tag, so a race between two converging syncs (a tick tail and a config
#     edit) can leave untracked debris; every pass reclaims such orphans
#     too. Windows needs no grant: the
#     same sync registers one-shot WakeToRun tasks for interval moments.


def wake_specs(config, state, now):
    """(moment, kind) pairs the machine must be awake for, soonest first.

    Fixed slots expand to today and tomorrow, day-rule-agnostic — the tick
    decides whether a day is active, so a weekend wake for a weekday-only
    slot is a harmless no-op dark wake (same trade-off as the calendar
    entries). Interval contributes only its governing next_due moment: the
    chain cannot be pre-computed past one node anyway. Remote, disabled,
    opted-out and invalid connections contribute nothing.
    """
    moments = set()
    connections = config.get("connections") or {}
    for conn_id in sorted(connections):
        conn = connections[conn_id]
        if not conn.get("enabled", True) or conn.get("location") == "remote":
            continue
        sched = conn.get("schedule") or {}
        if not sched.get("wakeWhenAsleep", False):
            continue
        if connection_errors(conn, conn_id):
            continue
        if sched.get("mode") == "fixed":
            at_times = (sched.get("fixed") or {}).get("at") or []
            for offset in range(2):
                day = now.date() + timedelta(days=offset)
                for hhmm in at_times:
                    if not SLOT_RE.match(hhmm):
                        continue
                    moment = schedule.slot_datetime(day, hhmm, now.tzinfo)
                    if moment is not None:
                        moments.add((moment, "fixed"))
        elif sched.get("mode") == "interval":
            moment, _kind = schedule.next_due(
                conn, conn_state(state, conn_id), now
            )
            if moment is not None:
                moments.add((moment, "interval"))
    horizon = now + WAKE_HORIZON
    return sorted(
        (moment, kind) for moment, kind in moments if now < moment <= horizon
    )[:WAKE_EVENT_LIMIT]


def _wake_wire_spec(moment):
    """pmset wire format: 'MM/DD/YY HH:MM:SS' local wall time (man pmset)."""
    return moment.strftime("%m/%d/%y %H:%M:%S")


def _canonical_spec(text):
    """Normalize a pmset date-time (2- or 4-digit year) for set comparison."""
    match = re.search(
        r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", str(text)
    )
    if not match:
        return None
    year = match.group(3)
    if len(year) == 2:
        year = f"20{year}"
    return (
        f"{int(match.group(1)):02d}/{int(match.group(2)):02d}/{year} "
        f"{int(match.group(4)):02d}:{match.group(5)}:{match.group(6) or '00'}"
    )


def _wire_from_canonical(canon):
    """Canonical spec back into the 2-digit-year wire form pmset expects —
    the same format arming writes, which `schedule cancel` matches on."""
    day, time_part = canon.split(" ")
    return f"{day[:-4]}{day[-2:]} {time_part}"


def _live_wake_entries():
    """(canonical, type, creator) per one-shot power event in `pmset -g
    sched`; None on error. The creator is the `by '…'` attribution: macOS
    arms its own alarms as plain `wake` with a com.apple creator, while the
    pmset command line (awewarm's sync or a manual sudo) arms
    `wakeorpoweron` events whose creator is 'pmset' or absent."""
    try:
        result = subprocess.run(
            ["pmset", "-g", "sched"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    found = set()
    for match in re.finditer(
        r"(wake|wakeorpoweron|poweron) at "
        r"(\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?)"
        r"(?:\s+by\s+'([^']*)')?",
        result.stdout or "",
    ):
        canon = _canonical_spec(match.group(2))
        if canon:
            found.add((canon, match.group(1), match.group(3)))
    return found


def sync_wake_events(config, state, now):
    """Converge armed RTC wake events with what the schedules need now.

    macOS mirrors the desired moments into pmset one-shot events through the
    sudoers grant; the ledger in state['wakeEvents'] is reconciled against
    `pmset -g sched` so fired events drop out and lost-ledger events are
    re-tracked instead of double-armed. Events the pmset command line armed
    that no ledger entry tracks and no schedule wants are reclaimed as
    orphans (see _reclaim_orphan_wakes). Returns True when pmset was touched.
    Saves state only when the ledger or the blocked flag changed.
    """
    if sys.platform == "win32":
        return _sync_windows_interval_wake(config, state, now)
    if sys.platform != "darwin":
        return False
    if not wake_grant_installed() or not scheduler_installed():
        return False
    desired = {
        _canonical_spec(_wake_wire_spec(moment)): _wake_wire_spec(moment)
        for moment, _kind in wake_specs(config, state, now)
    }
    ledger = list(state.get("wakeEvents") or [])
    entries = _live_wake_entries()
    live = None if entries is None else {canon for canon, _etype, _creator in entries}
    kept, blocked, touched = [], False, False
    cancelled_now = set()
    for spec in ledger:
        canon = _canonical_spec(spec) or spec
        if live is not None and canon not in live:
            continue  # already fired or cancelled — stop tracking it
        if canon not in desired:
            if _sudo_pmset(["schedule", "cancel", WAKE_TYPE, spec], interactive=False):
                touched = True
                cancelled_now.add(canon)
                continue
            blocked = True  # cancel refused (grant broken?) — keep tracking
        kept.append(spec)
    for canon, wire in sorted(desired.items()):
        if any((_canonical_spec(spec) or spec) == canon for spec in kept):
            continue
        if live is not None and canon in live:
            kept.append(wire)  # already armed, just lost from the ledger
            continue
        if _sudo_pmset(["schedule", WAKE_TYPE, wire], interactive=False):
            kept.append(wire)
            touched = True
        else:
            blocked = True
    if entries is not None:
        # a just-cancelled ledger entry is still in this pass's snapshot —
        # tracked, or the sweep below would cancel it a second time
        tracked = (
            {(_canonical_spec(spec) or spec) for spec in kept} | set(desired) | cancelled_now
        )
        cancelled, failed = _reclaim_orphan_wakes(entries, tracked)
        touched = touched or bool(cancelled)
        blocked = blocked or bool(failed)
        # a failed reclaim adopts the orphan into the ledger so the normal
        # retry path owns it and `status` shows it
        kept.extend(failed)
    _record_sync(state, kept, blocked)
    return touched


def _reclaim_orphan_wakes(entries, tracked, interactive=False):
    """Cancel pmset-armed events no tracked canonical covers.

    pmset events carry no owner tag, so the ledger is the only record of
    what awewarm armed — a race between two converging syncs (a tick tail
    and a config edit) leaves live debris the ledger loop never sees again.
    Only wakeorpoweron events with creator 'pmset' (or none) qualify; macOS
    arms its own alarms as plain `wake` with a com.apple creator and those
    are never touched. Each cancel is logged with the re-arm command so a
    manual event reclaimed by mistake can be restored verbatim; one cancel
    removes one entry, so a duplicated orphan converges over following
    ticks. Returns (cancelled, failed) as wire specs.
    """
    cancelled, failed = [], []
    for canon, etype, creator in sorted(entries, key=lambda e: (e[0], e[1], e[2] or "")):
        if etype != WAKE_TYPE or creator not in (None, "pmset") or canon in tracked:
            continue
        wire = _wire_from_canonical(canon)
        if _sudo_pmset(["schedule", "cancel", WAKE_TYPE, wire], interactive=interactive):
            cancelled.append(wire)
            append_log(
                log_path(),
                f'wake sync: reclaimed orphan event "{wire}" — no schedule wants it; '
                f're-arm: sudo pmset schedule {WAKE_TYPE} "{wire}"',
            )
        else:
            failed.append(wire)
    return cancelled, failed


def _record_sync(state, kept, blocked):
    """Persist the ledger, and the once-per-episode blocked warning."""
    before_events = list(state.get("wakeEvents") or [])
    before_blocked = bool(state.get("wakeSyncBlocked"))
    changed = sorted(kept) != sorted(before_events) or blocked != before_blocked
    if sorted(kept) != sorted(before_events):
        state["wakeEvents"] = sorted(kept)
    if blocked != before_blocked:
        if blocked:
            state["wakeSyncBlocked"] = True
            append_log(
                log_path(),
                "wake sync: sudo pmset rejected — check /etc/sudoers.d/awewarm "
                "(retry: awewarm scheduler install --wake)",
            )
        else:
            state.pop("wakeSyncBlocked", None)
    if changed:
        save_state(state)


def sudoers_rule(user):
    """The one-line grant: arm and cancel RTC wake events, nothing else."""
    return (
        f"{user} ALL=(root) NOPASSWD: {PMSET_BIN} schedule {WAKE_TYPE} *, "
        f"{PMSET_BIN} schedule cancel {WAKE_TYPE} *\n"
    )


def wake_grant_installed():
    try:
        return SUDOERS_PATH.is_file()
    except OSError:
        return False


def install_wake_grant():
    """Write /etc/sudoers.d/awewarm so the tick can arm wakes unattended.

    The rule is validated with visudo before the file is moved into place
    (root-owned, 0440); a failure leaves the system untouched. Needs one
    interactive sudo.
    """
    if sys.platform != "darwin":
        die(
            "the wake grant supports macOS only\n"
            "Windows arms wake tasks without a grant; Linux cannot wake a suspended machine"
        )
    # Unix-only module; imported here, not at the top, so a Windows
    # interpreter can import this module (there is no pwd on Windows).
    import pwd
    user = pwd.getpwuid(os.getuid()).pw_name
    handle = tempfile.NamedTemporaryFile("w", prefix="awewarm-sudoers-", delete=False)
    staged = Path(handle.name)
    try:
        with handle:
            handle.write(sudoers_rule(user))
        os.chmod(staged, 0o600)
        if not _sudo_cmd(["visudo", "-cf", str(staged)]):
            die("visudo rejected the wake rule — this should not happen; please report it")
        if not _sudo_cmd(
            [
                "install", "-m", "0440", "-o", "root", "-g", "wheel",
                str(staged), str(SUDOERS_PATH),
            ]
        ):
            die(
                "could not write /etc/sudoers.d/awewarm\n"
                "fix: resolve the sudo error; wakes can also be armed per slot with:\n"
                f'  sudo pmset schedule {WAKE_TYPE} "MM/DD/YY HH:MM:SS"'
            )
    finally:
        staged.unlink(missing_ok=True)
    return True


def uninstall_wake_grant(interactive=True):
    if not wake_grant_installed():
        return False
    return _sudo_cmd(["rm", "-f", str(SUDOERS_PATH)], interactive=interactive)


def teardown_wake_layer(interactive=True):
    """Scheduler-uninstall path: cancel armed RTC wakes, drop the grant.

    Returns (cancelled, total, grant_removed); failures keep their ledger
    entries so a later uninstall retries and converges. Orphaned pmset
    events beyond the ledger are swept by the same filter the tick's sync
    uses — with the scheduler gone, nothing else would ever cancel them.
    """
    state = load_state()
    armed = state.get("wakeEvents") or []
    ledger_cancelled = 0
    for spec in armed:
        if _sudo_pmset(["schedule", "cancel", WAKE_TYPE, spec], interactive=interactive):
            ledger_cancelled += 1
    cancelled, total = ledger_cancelled, len(armed)
    entries = _live_wake_entries()
    if entries is not None:
        # every ledger canonical is excluded, cancelled or not: a failed
        # ledger cancel stays a ledger problem, not an orphan
        tracked = {(_canonical_spec(spec) or spec) for spec in armed}
        reclaimed, failed = _reclaim_orphan_wakes(entries, tracked, interactive=interactive)
        cancelled += len(reclaimed)
        total += len(reclaimed) + len(failed)
    if ledger_cancelled == len(armed):
        state.pop("wakeEvents", None)
        state.pop("wakeSyncBlocked", None)
        save_state(state)
    return cancelled, total, uninstall_wake_grant(interactive=interactive)


def armed_wake_moments(state):
    """Ledger specs as naive datetimes, sorted — for status display."""
    moments = []
    for spec in state.get("wakeEvents") or []:
        canon = _canonical_spec(spec)
        if not canon:
            continue
        try:
            moments.append(datetime.strptime(canon, "%m/%d/%Y %H:%M:%S"))
        except ValueError:
            continue
    return sorted(moments)


def _schtasks(argv):
    return subprocess.run(
        ["schtasks", "/NH", *argv], capture_output=True, text=True, timeout=30
    )


def _install_windows():
    exe = resolve_exe()
    # The exe path is quoted inside /TR so paths with spaces survive.
    result = _schtasks(
        ["/Create", "/F", "/SC", "MINUTE", "/MO", str(TICK_SECONDS // 60),
         "/TN", LABEL, "/TR", f'"{exe}" tick']
    )
    if result.returncode != 0:
        die(
            f"schtasks failed to create the {LABEL} task\n{(result.stderr or result.stdout or '').strip()}\n"
            "fix: resolve the schtasks error, or create the task manually:\n"
            f'  schtasks /Create /SC MINUTE /TN {LABEL} /TR "{exe} tick"'
        )
    sync_windows_wake(load_config())
    return LABEL


def _uninstall_windows():
    ok = _schtasks(["/Delete", "/F", "/TN", LABEL]).returncode == 0
    for key in sorted(_task_keys(WAKE_TASK_PREFIX) or ()):
        _schtasks(["/Delete", "/F", "/TN", WAKE_TASK_PREFIX + key])
    for key in sorted(_task_keys(IWAKE_TASK_PREFIX) or ()):
        _schtasks(["/Delete", "/F", "/TN", IWAKE_TASK_PREFIX + key])
    return ok


def _powershell():
    return (
        shutil.which("powershell")
        or shutil.which("powershell.exe")
        or "powershell.exe"
    )


def _run_powershell(script):
    return subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )


def _wake_time_keys(entries):
    return {f"{entry['Hour']:02d}{entry['Minute']:02d}" for entry in entries}


def build_wake_ps1(exe, entries):
    """PowerShell that registers one daily WakeToRun task per fixed slot.

    The Windows twin of the launchd StartCalendarInterval entries: wake only
    at slot times, never on the per-minute tick (a waking tick would keep
    the machine from ever staying asleep). schtasks.exe cannot set
    WakeToRun, hence PowerShell's Register-ScheduledTask.
    """
    times = ", ".join(f"'{entry['Hour']:02d}:{entry['Minute']:02d}'" for entry in entries)
    lines = [
        f"$action = New-ScheduledTaskAction -Execute '{exe.replace(chr(39), chr(39) * 2)}' -Argument 'tick'",
        "$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable"
        " -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
        f"foreach ($t in @({times})) {{",
        "  $trigger = New-ScheduledTaskTrigger -Daily -At $t",
        f"  Register-ScheduledTask -TaskName ('{WAKE_TASK_PREFIX}' + $t.Replace(':', ''))"
        " -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null",
        "}",
    ]
    return "\n".join(lines) + "\n"


def wake_task_times():
    """Time keys of the installed Windows wake tasks; None when the query fails."""
    return _task_keys(WAKE_TASK_PREFIX)


def _task_keys(prefix):
    """Keys of installed Windows tasks under prefix; None when the query fails."""
    try:
        result = _schtasks(["/Query", "/FO", "/CSV", "/NH"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return set()
    found = set()
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if row and row[0].lstrip("\\").startswith(prefix):
            found.add(row[0].lstrip("\\")[len(prefix):])
    return found


def sync_windows_wake(config):
    """Align installed wake tasks with the config's fixed slots; True on change."""
    entries = calendar_entries(config)
    desired = _wake_time_keys(entries)
    installed = wake_task_times()
    if installed is None or installed == desired:
        return False
    if desired:
        result = _run_powershell(build_wake_ps1(resolve_exe(), entries))
        if result.returncode != 0:
            die(
                "PowerShell failed to register the wake tasks\n"
                f"{(result.stderr or result.stdout or '').strip()}\n"
                "fix: resolve the PowerShell error, then re-run: awewarm scheduler install"
            )
    for extra in sorted(installed - desired):
        _schtasks(["/Delete", "/F", "/TN", WAKE_TASK_PREFIX + extra])
    return True


def build_iwake_ps1(exe, moments):
    """PowerShell registering one -Once WakeToRun task per interval moment.

    The dynamic twin of the static daily wake tasks: fixed slots repeat daily
    on their own, interval renewals drift, so each gets a one-shot task named
    after its moment (keys match _task_keys). ParseExact keeps the timestamp
    culture-invariant; schtasks.exe cannot set WakeToRun, hence PowerShell.
    """
    stamps = ", ".join(
        "'{}'".format(moment.strftime("%Y-%m-%dT%H:%M:%S")) for moment in moments
    )
    lines = [
        f"$action = New-ScheduledTaskAction -Execute '{exe.replace(chr(39), chr(39) * 2)}' -Argument 'tick'",
        "$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable"
        " -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
        f"foreach ($t in @({stamps})) {{",
        "  $at = [datetime]::ParseExact($t, 'yyyy-MM-ddTHH:mm:ss', $null)",
        "  $name = '" + IWAKE_TASK_PREFIX + "' + $t.Replace('-', '').Replace(':', '').Replace('T', '')",
        "  Register-ScheduledTask -TaskName $name -Action $action"
        " -Trigger (New-ScheduledTaskTrigger -Once -At $at) -Settings $settings -Force | Out-Null",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _sync_windows_interval_wake(config, state, now):
    """Arm one-shot WakeToRun tasks for interval moments (fixed slots keep
    their static daily tasks). Task names embed the moment, so the schtasks
    query is the ledger — nothing lives in state."""
    moments = sorted(
        moment for moment, kind in wake_specs(config, state, now) if kind == "interval"
    )
    desired = {moment.strftime("%Y%m%d%H%M%S") for moment in moments}
    installed = _task_keys(IWAKE_TASK_PREFIX)
    if installed is None or installed == desired:
        return False
    if desired:
        result = _run_powershell(build_iwake_ps1(resolve_exe(), moments))
        if result.returncode != 0:
            append_log(
                log_path(),
                "wake sync: interval wake task registration failed: "
                f"{(result.stderr or result.stdout or '').strip()[:200]}",
            )
            return False
    for extra in sorted(installed - desired):
        _schtasks(["/Delete", "/F", "/TN", IWAKE_TASK_PREFIX + extra])
    return True


def build_service(exe):
    lines = [
        "[Unit]",
        "Description=awewarm scheduler tick",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={exe} tick",
    ]
    # Same reasoning as the launchd plist: the user manager's environment is
    # sparser than a login shell, so propagate AWEWARM_* and PATH explicitly.
    for key, value in sorted(os.environ.items()):
        if key.startswith("AWEWARM_") and value:
            lines.append(f'Environment="{key}={value}"')
    if os.environ.get("PATH"):
        lines.append(f'Environment="PATH={os.environ["PATH"]}"')
    return "\n".join(lines) + "\n"


def build_timer():
    # AccuracySec tightens systemd's default 1-minute coalescing window so
    # ticks keep a ~60s cadence. Persistent=true fires a missed tick at boot,
    # which matters on an always-on server that was rebooted mid-window;
    # ordinary missed ticks are still recovered from state.json catch-up.
    return (
        "[Unit]\n"
        "Description=Run the awewarm scheduler tick every minute\n"
        "[Timer]\n"
        "OnStartupSec=1min\n"
        f"OnUnitActiveSec={TICK_SECONDS}s\n"
        "AccuracySec=5s\n"
        "Persistent=true\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _systemctl(argv):
    return subprocess.run(
        ["systemctl", "--user", *argv], capture_output=True, text=True, timeout=30
    )


def _install_linux():
    if shutil.which("systemctl") is None:
        die(
            "systemctl not found — this system has no systemd\n"
            "cron the tick instead:\n  * * * * * awewarm tick"
        )
    exe = resolve_exe()
    unit_dir().mkdir(parents=True, exist_ok=True)
    service_path().write_text(build_service(exe))
    timer_path().write_text(build_timer())
    reload = _systemctl(["daemon-reload"])
    enable = _systemctl(["enable", "--now", "awewarm.timer"])
    if reload.returncode != 0 or enable.returncode != 0:
        stderr = (reload.stderr or "") + (enable.stderr or "")
        hint = ""
        if "Failed to connect to bus" in stderr:
            hint = (
                "\nfix: the systemd user manager is not running for this account;\n"
                "  enable lingering (survives logout): loginctl enable-linger $USER\n"
                "  then re-run: awewarm scheduler install"
            )
        die(
            f"systemctl failed to enable the awewarm timer\n{stderr.strip()}{hint}\n"
            "or cron the tick instead:\n  * * * * * awewarm tick"
        )
    return timer_path()


def _uninstall_linux():
    _systemctl(["disable", "--now", "awewarm.timer"])
    was_present = timer_path().exists() or service_path().exists()
    for path in (timer_path(), service_path()):
        if path.exists():
            path.unlink()
    _systemctl(["daemon-reload"])
    return was_present
