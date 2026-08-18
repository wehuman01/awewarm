"""Detect locally installed coding CLIs and their login state.

Read-only by contract: no network requests, no activation, and credential
checks only test for existence — secret values are never read or printed.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Window knowledge built into awewarm. This is documented provider behavior,
# not something verified per-account; `verify` exists for everything else.
BUILTIN_WINDOWS = {
    "claude-code": {
        "status": "verified",
        "startRule": "first-successful-request",
        "durationMinutes": 300,
        "evidence": "builtin-provider",
    },
    "codex": {
        "status": "unknown",
        "startRule": "unknown",
        "durationMinutes": None,
        "evidence": "none",
    },
}

PROVIDER_CLIS = {"claude-code": "claude", "codex": "codex"}
PROVIDER_LABELS = {"claude-code": "Claude Code", "codex": "Codex"}
PROVIDER_TRANSPORTS = {"claude-code": "claude-cli", "codex": "codex-cli"}
PROVIDER_MODELS = {"claude-code": "haiku", "codex": None}


def _cli_version(command):
    try:
        proc = subprocess.run(
            [command, "--version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    first_line = (proc.stdout or proc.stderr or "").strip().splitlines()
    return first_line[0][:80] if first_line else None


def _claude_auth_found():
    """Claude Code keeps credentials in the macOS Keychain or a local file."""
    if sys.platform == "darwin" and shutil.which("security"):
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials"],
                capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            return True, "keychain: Claude Code-credentials"
    credentials = Path("~/.claude/.credentials.json").expanduser()
    if credentials.exists():
        return True, str(credentials)
    return False, None


def _codex_auth_found():
    auth = Path("~/.codex/auth.json").expanduser()
    if auth.exists():
        return True, str(auth)
    return False, None


def discover_accounts():
    """Scan local CLIs. Pure reads; no request is ever sent here."""
    findings = []
    for provider, command in PROVIDER_CLIS.items():
        cli_path = shutil.which(command)
        finding = {
            "provider": provider,
            "label": PROVIDER_LABELS[provider],
            "cliCommand": command,
            # launchd's PATH lacks user-local install dirs, so connections
            # must store the absolute path or ticks can't find the CLI.
            "cliPath": cli_path,
            "installed": cli_path is not None,
            "version": None,
            "authFound": False,
            "authDetail": None,
            "builtinWindow": BUILTIN_WINDOWS[provider],
        }
        if finding["installed"]:
            finding["version"] = _cli_version(command)
            if provider == "claude-code":
                finding["authFound"], finding["authDetail"] = _claude_auth_found()
            else:
                finding["authFound"], finding["authDetail"] = _codex_auth_found()
        findings.append(finding)
    return findings


def describe_finding(finding):
    """Human-readable ✓/? lines for one finding, without secrets."""
    lines = []
    name = finding["label"]
    if not finding["installed"]:
        lines.append(f"✗ {name} CLI not found ({finding['cliCommand']} not in PATH)")
        return lines
    lines.append(f"✓ {name} CLI found: {finding['version'] or finding['cliCommand']}")
    if finding["authFound"]:
        lines.append(f"✓ {name} authentication found ({finding['authDetail']})")
    else:
        lines.append(f"? {name} authentication not found — log in first")
    window = finding["builtinWindow"]
    if window["status"] == "verified":
        lines.append(f"✓ Subscription session window detected: {window['durationMinutes'] // 60} hours")
    else:
        lines.append("? Window semantics not verified — interval stays locked until confirmed")
    return lines
