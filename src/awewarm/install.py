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
from pathlib import Path

from .config import SLOT_RE, die, load_config, load_state, save_state, state_path

LABEL = "com.awewarm.scheduler"
WAKE_TASK_PREFIX = f"{LABEL}.wake-"
TICK_SECONDS = 60

# macOS-only: legacy pmset repeat events registered by awewarm < 0.4 are
# cancelled on scheduler install/uninstall; wake-from-sleep itself is handled
# by the launchd StartCalendarInterval entries below.
WAKE_TYPE = "wakeorpoweron"


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
    bypassing `awewarm update` — the next tick detects the old job, rewrites
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
    one WakeToRun task per entry (see build_wake_ps1). Calendar triggers wake
    the machine from sleep and run the tick at the exact slot time — every
    slot is covered, unlike pmset repeat, which held a single event. Entries
    fire every day regardless of the slot's day rule: the tick itself decides
    whether today is an active day, so a weekend wake for a weekday-only slot
    is a harmless no-op.
    """
    entries = {}
    for conn in (config.get("connections") or {}).values():
        if not conn.get("enabled", True):
            continue
        if conn.get("location") == "remote":
            continue  # an awewarm serve process ticks these — no local wake needed
        schedule = conn.get("schedule") or {}
        if schedule.get("mode") != "fixed":
            continue
        fixed = schedule.get("fixed") or {}
        if not fixed.get("wakeWhenAsleep", schedule.get("wakeWhenAsleep", True)):
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


def _sudo_pmset(args, interactive=True):
    """Run pmset with sudo; -n first so scripted installs fail fast instead of
    hanging, plain sudo second so interactive installs can prompt."""
    prefixes = (["sudo", "-n"], ["sudo"]) if interactive else (["sudo", "-n"],)
    for prefix in prefixes:
        result = subprocess.run(
            [*prefix, "pmset", *args], capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return True
    return False


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
    for key in sorted(wake_task_times() or ()):
        _schtasks(["/Delete", "/F", "/TN", WAKE_TASK_PREFIX + key])
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
    try:
        result = _schtasks(["/Query", "/FO", "/CSV", "/NH"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return set()
    found = set()
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if row and row[0].lstrip("\\").startswith(WAKE_TASK_PREFIX):
            found.add(row[0].lstrip("\\")[len(WAKE_TASK_PREFIX):])
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
