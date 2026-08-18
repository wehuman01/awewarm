"""launchd scheduler installation (macOS-only in v0.1).

The agent simply invokes `awewarm run` once a minute; all scheduling state
lives in state.json, so the plist itself is static. Linux users can cron the
same command until the systemd installer lands.
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
    if sys.platform != "darwin":
        die(
            "automatic scheduler install is macOS-only (launchd) for now\n"
            "on Linux, cron the tick instead:\n  * * * * * awewarm run"
        )
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


def uninstall_scheduler():
    if sys.platform != "darwin":
        die("scheduler uninstall is macOS-only (launchd) for now")
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


def scheduler_installed():
    return plist_path().exists()
