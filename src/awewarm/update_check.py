"""Background update reminder for awewarm.

Checks PyPI at most once a day (cached next to the config) and only for
interactive commands — never for `awewarm tick`, which the background scheduler
invokes every minute, and never for `awewarm self-update` itself. Network failures
back off for a few hours so an offline machine does not retry on every command.
"""
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__
from .config import config_path

CHECK_INTERVAL_S = 24 * 60 * 60
REMIND_INTERVAL_S = 24 * 60 * 60
RETRY_INTERVAL_S = 6 * 60 * 60
WAIT_TIMEOUT_S = 10  # upper bound for the 5s PyPI timeout at exit


def _parse_version(value):
    try:
        parts = re.findall(r"\d+", value)
        return tuple(int(x) for x in parts[:3]) if parts else (0,)
    except (ValueError, AttributeError):
        return (0,)


def version_gte(current, latest):
    return _parse_version(current) >= _parse_version(latest)


def _cache_path():
    return config_path().parent / "update-check.json"


def _load_cache(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _save_cache(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def get_pypi_latest(package="awewarm"):
    url = f"https://pypi.org/pypi/{package}/json"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.loads(response.read())
    return data["info"]["version"]


def _should_skip(args):
    if any(flag in args for flag in ("-h", "--help", "-v", "-V", "--version")):
        return True
    # `awewarm run` (user-facing, real requests) and `awewarm tick` (the
    # scheduler, once a minute) and `self-update` (about to replace the
    # installed package) and `serve` (long-running) should never hit PyPI.
    return bool(args) and args[0] in ("run", "tick", "self-update", "serve")


def check_async(args):
    """Start an update check in a background thread.

    Returns a callable that yields a reminder string or None. Safe to call
    from any entry point; skipped commands return a no-op immediately.
    """
    if os.environ.get("AWEWARM_NO_UPDATE_CHECK") == "1":
        return lambda: None
    if _should_skip(args):
        return lambda: None

    result = [None]
    done = threading.Event()

    def _run():
        try:
            result[0] = _check()
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()

    def get_result():
        done.wait(timeout=WAIT_TIMEOUT_S)
        return result[0]

    return get_result


def _check():
    cache_path = _cache_path()
    cache = _load_cache(cache_path) or {}
    now = time.time()

    if now < cache.get("nextCheckAt", 0):
        latest = cache.get("latestVersion")
    else:
        try:
            latest = get_pypi_latest()
        except Exception:
            # Backoff instead of retrying on the next command.
            _save_cache(cache_path, {**cache, "nextCheckAt": now + RETRY_INTERVAL_S, "latestVersion": None})
            return None
        _save_cache(cache_path, {**cache, "nextCheckAt": now + CHECK_INTERVAL_S, "latestVersion": latest})

    if not latest or version_gte(__version__, latest):
        return None
    if now - cache.get("lastReminded", 0) < REMIND_INTERVAL_S:
        return None

    _save_cache(cache_path, {**cache, "nextCheckAt": now + CHECK_INTERVAL_S, "latestVersion": latest, "lastReminded": now})
    return f"Update available: {__version__} → {latest}. Run `awewarm self-update` to update."
