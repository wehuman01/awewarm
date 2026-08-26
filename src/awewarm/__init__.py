"""awewarm — keep AI coding-plan subscription windows warm."""

import subprocess
from pathlib import Path

__version__ = "0.7.0"


def _checkout_root():
    """The repo root above src/ when the package runs from a checkout."""
    return Path(__file__).resolve().parents[2]


def running_from_checkout():
    """True when this code runs straight out of a source checkout.

    That is the case for `pip install -e .`: the package directory lives
    inside the repo, next to its pyproject.toml — a wheel install never does.
    pip's recorded metadata freezes at install time while `__version__`
    tracks the checkout, so the two can drift; callers use this to mark or
    guard the editable case."""
    return (_checkout_root() / "pyproject.toml").is_file()


def display_version():
    """What `-v` prints: the version, marked when running from a checkout."""
    if not running_from_checkout():
        return __version__
    label = "editable"
    try:
        git = subprocess.run(
            ["git", "-C", str(_checkout_root()), "describe", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except Exception:
        git = ""
    if git:
        label += f", git {git}"
    return f"{__version__} ({label})"
