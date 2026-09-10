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

# aweswitch official accounts: each one is a private CLI config dir under
# ~/.config/aweswitch/accounts/<provider>/<name>/ holding its own login file.
# Mapping from awewarm provider ids to aweswitch's dir names and the login
# file aweswitch keeps inside (see aweswitch's ACCOUNT_CRED_FILENAME).
AWESWITCH_PROVIDER_DIRS = {"claude-code": "claude", "codex": "codex"}
AWESWITCH_CRED_FILES = {"claude-code": ".credentials.json", "codex": "auth.json"}


def aweswitch_accounts_root():
    return Path(
        os.environ.get("AWESWITCH_CONFIG", "~/.config/aweswitch/config.json")
    ).expanduser().parent / "accounts"


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


def _base_finding(provider, cli_path, version):
    return {
        "provider": provider,
        "label": PROVIDER_LABELS[provider],
        "cliCommand": PROVIDER_CLIS[provider],
        "cliPath": cli_path,
        "installed": cli_path is not None,
        "version": version,
        "authFound": False,
        "authDetail": None,
        "authHome": None,
        "builtinWindow": BUILTIN_WINDOWS[provider],
    }


def _aweswitch_findings(provider, cli_path, version):
    """One extra finding per aweswitch official account that has a login on
    disk. Existence-only: the login file is never read here. A dir without
    its login file is not warmable and is skipped."""
    root = aweswitch_accounts_root() / AWESWITCH_PROVIDER_DIRS[provider]
    try:
        dirs = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return []
    findings = []
    cred_name = AWESWITCH_CRED_FILES[provider]
    for account_dir in dirs:
        cred = account_dir / cred_name
        if not cred.exists():
            continue
        finding = _base_finding(provider, cli_path, version)
        finding["label"] = f"{PROVIDER_LABELS[provider]} ({account_dir.name})"
        finding["authFound"] = True
        finding["authDetail"] = str(cred)
        finding["authHome"] = str(account_dir)
        findings.append(finding)
    return findings


def discover_accounts():
    """Scan local CLIs. Pure reads; no request is ever sent here."""
    findings = []
    for provider, command in PROVIDER_CLIS.items():
        cli_path = shutil.which(command)
        version = _cli_version(command) if cli_path is not None else None
        finding = _base_finding(provider, cli_path, version)
        # launchd's PATH lacks user-local install dirs, so connections
        # must store the absolute path or ticks can't find the CLI.
        if finding["installed"]:
            if provider == "claude-code":
                finding["authFound"], finding["authDetail"] = _claude_auth_found()
            else:
                finding["authFound"], finding["authDetail"] = _codex_auth_found()
        findings.append(finding)
        findings.extend(_aweswitch_findings(provider, cli_path, version))
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
