"""System scheduler installation: launchd on macOS, Task Scheduler on Windows.

The agent simply invokes `awewarm run` once a minute; all scheduling state
lives in state.json, so the task definition itself is static. Linux users
can cron the same command until a systemd installer lands.

Windows notes: schtasks tasks run in the user context and inherit user env
vars (what `setx` writes), so unlike the launchd plist nothing needs to bake
PATH or AWEWARM_* into the task; connections already store absolute CLI
paths. The task's stdout is not captured by Task Scheduler — awewarm's own
event log (`awewarm.log`) is the audit trail.
"""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import die, state_path

LABEL = "com.awewarm.scheduler"
TICK_SECONDS = 60


def plist_path():
    return Path(
        os.environ.get("AWEWARM_PLIST", f"~/Library/LaunchAgents/{LABEL}.plist")
    ).expanduser()


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
        "ProgramArguments": [exe, "run"],
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
        "fix: reinstall with: pip install -e . (or pip install awewarm), then retry: awewarm install"
    )


def install_scheduler():
    if sys.platform == "darwin":
        return _install_launchd()
    if sys.platform == "win32":
        return _install_windows()
    die(
        "automatic scheduler install supports macOS (launchd) and Windows (Task Scheduler)\n"
        "on Linux, cron the tick instead:\n  * * * * * awewarm run"
    )


def uninstall_scheduler():
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if sys.platform == "win32":
        return _schtasks(["/Delete", "/F", "/TN", LABEL]).returncode == 0
    die("scheduler uninstall supports macOS (launchd) and Windows (Task Scheduler)")


def scheduler_installed():
    if sys.platform == "darwin":
        return plist_path().exists()
    if sys.platform == "win32":
        try:
            return _schtasks(["/Query", "/TN", LABEL]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return False


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


def _schtasks(argv):
    return subprocess.run(
        ["schtasks", "/NH", *argv], capture_output=True, text=True, timeout=30
    )


def _install_windows():
    exe = resolve_exe()
    # The exe path is quoted inside /TR so paths with spaces survive.
    result = _schtasks(
        ["/Create", "/F", "/SC", "MINUTE", "/MO", str(TICK_SECONDS // 60),
         "/TN", LABEL, "/TR", f'"{exe}" run']
    )
    if result.returncode != 0:
        die(
            f"schtasks failed to create the {LABEL} task\n{(result.stderr or result.stdout or '').strip()}\n"
            "fix: resolve the schtasks error, or create the task manually:\n"
            f'  schtasks /Create /SC MINUTE /TN {LABEL} /TR "{exe} run"'
        )
    return LABEL
