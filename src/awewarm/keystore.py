"""API key storage: secrets.json next to the config.

Pasted keys land in ~/.config/awewarm/secrets.json (0600) so every process —
including the launchd scheduler, which has no shell environment — can read
them. `${ENV_VAR}` refs were removed after they proved unreadable from launchd;
`keychain:` refs from older releases are migrated into secrets.json on first
load — the Keychain code was removed after it was found to truncate stored keys.
"""
import json
import os
import re
import subprocess
import sys

from .config import config_path, die


def secrets_path():
    return os.path.join(os.path.dirname(os.path.abspath(config_path())), "secrets.json")


def _read_secrets():
    try:
        with open(secrets_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_secrets(data):
    path = secrets_path()
    had_file = os.path.exists(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    if not had_file:
        os.chmod(path, 0o600)


def store_api_key(conn_id, api_key):
    """Store a pasted API key in secrets.json; returns the apiKeyRef."""
    if not api_key or any(ch in api_key for ch in "\r\n"):
        die("API key must be a single non-empty line")
    data = _read_secrets()
    data[conn_id] = api_key
    _write_secrets(data)
    stored = load_api_key(f"file:{conn_id}")
    if stored != api_key:
        die("stored API key failed read-back verification — nothing saved reliably")
    return f"file:{conn_id}"


def load_api_key(api_key_ref):
    """Resolve an apiKeyRef to a secret, or None when unavailable."""
    if not api_key_ref:
        return None
    if api_key_ref.startswith("file:"):
        return _read_secrets().get(api_key_ref.split(":", 1)[1]) or None
    if api_key_ref.startswith("keychain:"):
        return _migrate_keychain_ref(api_key_ref)
    if re.fullmatch(r"\$\{?[A-Za-z0-9_]+\}?", api_key_ref):
        die(
            f"env-var refs are no longer supported ({api_key_ref!r}) — the launchd\n"
            "scheduler cannot read shell variables.\n"
            "fix: store the key with: awewarm config set <connection> --api-key <key>"
        )
    die(f"unrecognized apiKeyRef: {api_key_ref!r}\nfix: use file:<conn-id>")


def _migrate_keychain_ref(ref):
    """One-time move of a legacy keychain: ref into secrets.json."""
    if sys.platform != "darwin":
        return None
    service = ref.split(":", 1)[1]
    conn_id = service.split("/", 1)[1] if "/" in service else service
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    key = proc.stdout.strip()
    if not key:
        return None
    data = _read_secrets()
    data[conn_id] = key
    _write_secrets(data)
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return key


def delete_api_key(conn_id, api_key_ref=None):
    """Best-effort removal: secrets.json entry plus any legacy keychain item."""
    data = _read_secrets()
    if conn_id in data:
        del data[conn_id]
        _write_secrets(data)
    if api_key_ref and api_key_ref.startswith("keychain:") and sys.platform == "darwin":
        try:
            subprocess.run(
                ["security", "delete-generic-password", "-s", api_key_ref.split(":", 1)[1]],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass
