"""Read CLI login credentials when delegating an account connection.

The local login is always the source of truth: delegation reads it here and
pushes it to the server exactly like an API key; anything the server-side CLI
refreshes is discarded when the next sync re-materializes the credential.
Reads happen only inside delegation / re-push flows — discover stays
existence-only and never reads credential values.

A credential is the provider's own JSON blob (Claude Code's Keychain entry or
credentials file, Codex's auth.json), returned raw plus a short sha256
fingerprint for display and drift detection. No value ever reaches a log.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
# Codex itself reads CODEX_HOME, so honoring it here reads the same login the
# local CLI would use.
CODEX_HOME_ENV = "CODEX_HOME"


class CredentialError(Exception):
    """One unreadable login; str(exc) is user-ready and secret-free."""


class Credential:
    """A login credential: the raw JSON text plus a display fingerprint."""

    __slots__ = ("raw", "fingerprint")

    def __init__(self, raw):
        self.raw = raw
        self.fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def read_credential(conn):
    """The account connection's login credential; CredentialError when the
    local login is missing or unreadable (the message says how to fix it)."""
    kind = (conn.get("transport") or {}).get("kind")
    if kind == "claude-cli":
        return _read_claude()
    if kind == "codex-cli":
        return _read_codex()
    raise CredentialError("not a CLI account connection — no login credential to read")


def _read_text(path, what, hint):
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise CredentialError(f"{what} not found at {path}\nfix: {hint}")
    except OSError as exc:
        raise CredentialError(f"cannot read {what} at {path}: {exc}\nfix: {hint}")
    if not text:
        raise CredentialError(f"{what} at {path} is empty\nfix: {hint}")
    return text


def _read_claude():
    """Claude Code keeps its login in the macOS Keychain, elsewhere in
    ~/.claude/.credentials.json."""
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CredentialError(
                f"cannot read the Claude Code login from the macOS Keychain: {exc}\n"
                "fix: run `claude` once in your terminal to refresh the login, then re-run"
            )
        if proc.returncode != 0:
            # A Keychain ACL can make the read fail outside the app that
            # stored it — the honest fix is re-running where the user can
            # answer the prompt, or re-logging in.
            reason = (proc.stderr or "").strip().splitlines()
            raise CredentialError(
                "cannot read the Claude Code login from the macOS Keychain"
                + (f" ({reason[0][:120]})" if reason else "")
                + "\nfix: re-run in your terminal (answer the Keychain prompt), or log in again with `claude /login`"
            )
        secret = proc.stdout.strip()
        if secret:
            return Credential(secret)
    return Credential(_read_text(
        Path("~/.claude/.credentials.json").expanduser(),
        "the Claude Code login",
        "log in with `claude /login`, then re-push: awewarm remote push",
    ))


def _read_codex():
    home = os.environ.get(CODEX_HOME_ENV) or "~/.codex"
    return Credential(_read_text(
        Path(home).expanduser() / "auth.json",
        "the Codex login",
        "log in with `codex login`, then re-push: awewarm remote push",
    ))


def codex_auth(credential):
    """(access_token, account_id) inside Codex's auth.json JSON.

    Raises ValueError when the shape is not recognized — the caller turns
    that into an activation failure pointing at a re-push.
    """
    try:
        payload = json.loads(credential)
    except ValueError:
        raise ValueError(
            "credential format not recognized — log in again on the local machine, then: awewarm remote push"
        )
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        tokens = payload  # a bare tokens block is accepted too
    token = tokens.get("access_token")
    account = tokens.get("account_id")
    if not isinstance(token, str) or not token or not isinstance(account, str) or not account:
        raise ValueError(
            "credential format not recognized — log in again on the local machine, then: awewarm remote push"
        )
    return token, account


def claude_access_token(credential):
    """The accessToken inside Claude Code's credentials JSON.

    Raises ValueError when the shape is not recognized — the caller turns
    that into an activation failure pointing at a re-push.
    """
    try:
        payload = json.loads(credential)
    except ValueError:
        raise ValueError(
            "credential format not recognized — log in again on the local machine, then: awewarm remote push"
        )
    token = None
    if isinstance(payload, dict):
        block = payload.get("claudeOAuthAccessToken")
        if isinstance(block, dict):
            token = block.get("accessToken")
        if not isinstance(token, str) or not token:
            token = payload.get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError(
            "credential format not recognized — log in again on the local machine, then: awewarm remote push"
        )
    return token
