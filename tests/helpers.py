"""Shared test utilities: env-isolated paths and connection factories."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ENV_KEYS = ("AWEWARM_CONFIG", "AWEWARM_STATE", "AWEWARM_LOG", "AWEWARM_PLIST", "AWEWARM_SYSTEMD_DIR")


class IsolatedTestCase(unittest.TestCase):
    """Point every awewarm path at a temp dir so tests never touch the user's."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.tmp_path = base
        overrides = {
            "AWEWARM_CONFIG": str(base / "config.json"),
            "AWEWARM_STATE": str(base / "state.json"),
            "AWEWARM_LOG": str(base / "awewarm.log"),
            "AWEWARM_PLIST": str(base / "agent.plist"),
            "AWEWARM_SYSTEMD_DIR": str(base / "systemd-user"),
        }
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ.update(overrides)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def account_connection(mode="fixed", window_status="verified", fixed_at=("06:35",), days="weekday"):
    verified = window_status == "verified"
    return {
        "label": "Claude Code",
        "kind": "account",
        "enabled": True,
        "auth": {"type": "local-cli", "status": "valid", "apiKeyRef": None},
        "transport": {"kind": "claude-cli", "baseUrl": None, "cliCommand": "claude"},
        "plan": {"url": None, "label": None},
        "window": {
            "status": window_status,
            "startRule": "first-successful-request" if verified else "unknown",
            "durationMinutes": 300 if verified else None,
            "evidence": "builtin-provider" if verified else "none",
        },
        "activation": {"model": "haiku", "prompt": "Reply with exactly: ok", "maxTokens": 4},
        "catchup": {"attempts": 5, "withinMinutes": 30},
        "degradeAfterNodes": 3,
        "schedule": {
            "mode": mode,
            "fixed": {
                "at": list(fixed_at),
                "days": days,
                "skipIfActivatedWithinMinutes": 30,
            },
            "interval": {"graceSeconds": 75, "jitterSeconds": 30},
        },
    }


def plan_connection(mode="fixed", fixed_at=("06:35",), days="weekday", window_status="unknown", duration=None):
    confirmed = window_status == "user-confirmed"
    return {
        "label": "GLM Coding Plan",
        "kind": "subscription",
        "enabled": True,
        "auth": {"type": "api-key", "status": "valid", "apiKeyRef": "${AWEWARM_API_KEY_GLM_CODING_PLAN}"},
        "transport": {
            "kind": "anthropic-messages",
            "baseUrl": "https://open.bigmodel.cn/api/anthropic",
            "cliCommand": None,
        },
        "plan": {"url": "https://example.com/plan", "label": "GLM Coding Plan"},
        "window": {
            "status": window_status,
            "startRule": "unknown",
            "durationMinutes": duration,
            "evidence": "user-confirmed" if confirmed else "none",
        },
        "activation": {"model": "glm-4.7", "prompt": "Reply with exactly: ok", "maxTokens": 4},
        "catchup": {"attempts": 5, "withinMinutes": 30},
        "degradeAfterNodes": 3,
        "schedule": {
            "mode": mode,
            "fixed": {
                "at": list(fixed_at),
                "days": days,
                "skipIfActivatedWithinMinutes": 30,
            },
            "interval": {"graceSeconds": 75, "jitterSeconds": 30},
        },
    }
