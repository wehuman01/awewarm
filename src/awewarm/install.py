"""System scheduler installation: launchd on macOS, Task Scheduler on Windows,
systemd user timer on Linux.

The agent simply invokes `awewarm run --force` once a minute; all scheduling
state lives in state.json, so the task definition itself is static. Where
systemd is unavailable (e.g. some servers or containers), cron the same
command. The tick self-heals: if the installed job's command line is stale
(older awewarm, or a pre-`--force` upgrade done via `pip install --upgrade`
directly), the next tick rewrites it.

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
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import SLOT_RE, die, load_state, save_state, state_path

LABEL = "com.awewarm.scheduler"
TICK_SECONDS = 60

# macOS-only: pmset wake scheduling so fixed slots fire with the lid closed.
WAKE_TYPE = "wakeorpoweron"
WAKE_LEAD_MINUTES = 5
DAY_LETTERS = {"weekday": "MTWRF", "every-day": "MTWRFSU"}


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


def build_plist(exe):
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
    return {
        "Label": LABEL,
        "ProgramArguments": [exe, "run", "--force"],
        "StartInterval": TICK_SECONDS,
        "RunAtLoad": True,
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(out_log),
        "EnvironmentVariables": environment,
    }


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
        "elsewhere, cron the tick instead:\n  * * * * * awewarm run --force"
    )


def uninstall_scheduler():
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if sys.platform == "win32":
        return _schtasks(["/Delete", "/F", "/TN", LABEL]).returncode == 0
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


def _maybe_self_heal_job():
    """Rewrite the installed scheduler job if its command line is outdated.

    Called at the top of every scheduler tick. Cheap (one file read or one
    `schtasks /Query`). No-op in the common case where the job already
    includes `--force`.

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
            if "--force" in (data.get("ProgramArguments") or []):
                return
            install_scheduler()
            return
        if sys.platform == "win32":
            try:
                result = _schtasks(["/Query", "/TN", LABEL, "/FO", "LIST", "/V"])
            except (OSError, subprocess.SubprocessError):
                return
            if result.returncode != 0:
                return
            if "--force" in (result.stdout or ""):
                return
            install_scheduler()
            return
        if sys.platform.startswith("linux"):
            svc = service_path()
            if not svc.exists():
                return
            if "--force" in svc.read_text():
                return
            install_scheduler()
    except (OSError, subprocess.SubprocessError, ValueError):
        # A self-heal failure must not break the tick — the old job, even
        # with a stale command line, will just hit the non-tty error path
        # which produces a clear log entry. The next tick retries.
        return


def _install_launchd():
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    with open(plist, "wb") as handle:
        plistlib.dump(build_plist(resolve_exe()), handle)
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


def build_wake_spec(config):
    """Earliest wake time across enabled fixed/hybrid connections → (pmset days, HH:MM:SS).

    pmset repeat holds a single repeating event, so the earliest computed wake
    time wins; later slots still fire on time when the Mac is awake and rely
    on catch-up otherwise. Returns None when no enabled connection opts in.
    """
    earliest_minutes, every_day = None, False
    for conn in (config.get("connections") or {}).values():
        if not conn.get("enabled", True):
            continue
        schedule = conn.get("schedule") or {}
        if schedule.get("mode") not in ("fixed", "hybrid"):
            continue
        fixed = schedule.get("fixed") or {}
        if not fixed.get("wakeWhenAsleep", schedule.get("wakeWhenAsleep", True)):
            continue
        if fixed.get("days") == "every-day":
            every_day = True
        lead = schedule.get("wakeLeadMinutes", fixed.get("wakeLeadMinutes", WAKE_LEAD_MINUTES))
        for slot in fixed.get("at") or []:
            if not SLOT_RE.match(slot):
                continue
            hh, mm = (int(part) for part in slot.split(":"))
            slot_minutes = hh * 60 + mm
            wake_minutes = (slot_minutes - lead) % (24 * 60)
            if earliest_minutes is None or wake_minutes < earliest_minutes:
                earliest_minutes = wake_minutes
    if earliest_minutes is None:
        return None
    days = DAY_LETTERS["every-day" if every_day else "weekday"]
    return days, f"{earliest_minutes // 60:02d}:{earliest_minutes % 60:02d}:00"


def manual_wake_command(spec):
    days, time = spec
    return f"sudo pmset repeat {WAKE_TYPE} {days} {time}"


def _sudo_pmset(args):
    """Run pmset with sudo; -n first so scripted installs fail fast instead of
    hanging, plain sudo second so interactive installs can prompt."""
    for prefix in (["sudo", "-n"], ["sudo"]):
        result = subprocess.run(
            [*prefix, "pmset", *args], capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return True
    return False


def set_wake_schedule(spec):
    """Register the pmset repeat wake and remember it in state.json."""
    days, time = spec
    if not _sudo_pmset(["repeat", WAKE_TYPE, days, time]):
        return False
    state = load_state()
    state["wakeSchedule"] = {"type": WAKE_TYPE, "days": days, "time": time}
    save_state(state)
    return True


def _current_repeat_line():
    result = subprocess.run(
        ["pmset", "-g", "sched"], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("Repeating power event"):
            return line
    return ""


def cancel_wake_schedule():
    """Undo the wake we set — but only if the live schedule is still ours.

    Returns (status, spec): status is "none" (we never set one), "changed"
    (the user replaced it — leave it alone), "cancelled", or "failed".
    """
    state = load_state()
    spec = state.pop("wakeSchedule", None)
    save_state(state)
    if not spec:
        return "none", None
    if spec["time"] not in _current_repeat_line():
        return "changed", spec
    args = ["repeat", "cancel", spec["type"], spec["days"], spec["time"]]
    return ("cancelled" if _sudo_pmset(args) else "failed"), spec


def _schtasks(argv):
    return subprocess.run(
        ["schtasks", "/NH", *argv], capture_output=True, text=True, timeout=30
    )


def _install_windows():
    exe = resolve_exe()
    # The exe path is quoted inside /TR so paths with spaces survive.
    result = _schtasks(
        ["/Create", "/F", "/SC", "MINUTE", "/MO", str(TICK_SECONDS // 60),
         "/TN", LABEL, "/TR", f'"{exe}" run --force']
    )
    if result.returncode != 0:
        die(
            f"schtasks failed to create the {LABEL} task\n{(result.stderr or result.stdout or '').strip()}\n"
            "fix: resolve the schtasks error, or create the task manually:\n"
            f'  schtasks /Create /SC MINUTE /TN {LABEL} /TR "{exe} run --force"'
        )
    return LABEL


def build_service(exe):
    lines = [
        "[Unit]",
        "Description=awewarm scheduler tick",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={exe} run --force",
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
    # ticks keep a ~60s cadence. Missed ticks are recovered from state.json
    # (catch-up windows), so no Persistent=true is needed.
    return (
        "[Unit]\n"
        "Description=Run the awewarm scheduler tick every minute\n"
        "[Timer]\n"
        "OnStartupSec=1min\n"
        f"OnUnitActiveSec={TICK_SECONDS}s\n"
        "AccuracySec=5s\n"
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
            "cron the tick instead:\n  * * * * * awewarm run --force"
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
            "or cron the tick instead:\n  * * * * * awewarm run --force"
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
