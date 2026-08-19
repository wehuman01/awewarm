"""API key storage: macOS Keychain via `security`, or ${ENV_VAR} references.

The API key is fed to `security -i` over stdin (interactive command mode) so it
never appears in a `ps` listing. Env-var refs follow the aweswitch
convention: the config stores only a pointer, the secret stays out of disk.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys

from .config import die

ENV_REF_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def keychain_service(conn_id):
    return f"awewarm/{conn_id}"


def env_ref_for(conn_id):
    return "${AWEWARM_API_KEY_" + re.sub(r"[^A-Z0-9]+", "_", conn_id.upper()).strip("_") + "}"


def is_keychain_available():
    return sys.platform == "darwin" and shutil.which("security") is not None


def store_api_key(conn_id, api_key):
    """Store an API key; returns the apiKeyRef to keep in config."""
    if is_keychain_available():
        service = keychain_service(conn_id)
        command = (
            f"add-generic-password -U -s {shlex.quote(service)}"
            f" -a {shlex.quote(conn_id)} -w {shlex.quote(api_key)}"
        )
        try:
            proc = subprocess.run(
                ["security", "-i"], input=command + "\n", capture_output=True,
                text=True, timeout=30,
            )
            if proc.returncode == 0:
                return f"keychain:{service}"
        except (OSError, subprocess.SubprocessError):
            pass
    return env_ref_for(conn_id)


def load_api_key(api_key_ref):
    """Resolve an apiKeyRef to a secret, or None when unavailable."""
    if not api_key_ref:
        return None
    match = ENV_REF_RE.match(api_key_ref)
    if match:
        return os.environ.get(match.group(1))
    if api_key_ref.startswith("keychain:"):
        if not is_keychain_available():
            return None
        service = api_key_ref.split(":", 1)[1]
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0:
            return proc.stdout.strip() or None
        return None
    die(f"unrecognized apiKeyRef: {api_key_ref!r}\nfix: use keychain:<service> or ${{ENV_VAR_NAME}}")


def delete_api_key(conn_id):
    """Best-effort removal of a stored keychain item; env refs need no cleanup."""
    if not is_keychain_available():
        return
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", keychain_service(conn_id)],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass
