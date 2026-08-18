import json
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner
from helpers import IsolatedTestCase, account_connection, plan_connection

import awewarm
from awewarm import config as cfg
from awewarm.cli import cli

RUNNER = CliRunner()


def invoke(*args, **kwargs):
    kwargs.setdefault("prog_name", "awewarm")
    return RUNNER.invoke(cli, *args, **kwargs)


def output_of(result):
    """Combined stdout+stderr across click versions (mix_stderr was removed in 8.2)."""
    text = result.output
    try:
        text += result.stderr
    except (ValueError, AttributeError):
        pass
    return text


def write_config(conn=None, conn_id="claude-code-main"):
    data = cfg.empty_config()
    if conn is not None:
        data["connections"][conn_id] = conn
    cfg.save_config(data)
    return data


class SurfaceTests(IsolatedTestCase):
    def test_help_usage_and_commands(self):
        result = invoke(["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Usage: awewarm [OPTIONS] COMMAND [ARGS]...", result.output)
        self.assertIn("-v, --version", result.output)
        for command in ("init", "discover", "status", "run", "activate", "verify", "install"):
            self.assertIn(command, result.output)

    def test_version_prints_bare_number(self):
        result = invoke(["-v"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output.strip(), awewarm.__version__)

    def test_entry_point_and_dependency_pinned(self):
        data = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        self.assertIn('awewarm = "awewarm.cli:main"', data)
        self.assertIn('dependencies = ["click>=8.1"]', data)


class DiscoverCommandTests(IsolatedTestCase):
    @mock.patch("awewarm.discover.discover_accounts")
    def test_discover_prints_findings(self, discover_accounts):
        discover_accounts.return_value = [
            {
                "provider": "claude-code",
                "label": "Claude Code",
                "cliCommand": "claude",
                "installed": True,
                "version": "1.0.66",
                "authFound": True,
                "authDetail": "keychain: Claude Code-credentials",
                "builtinWindow": {
                    "status": "verified",
                    "startRule": "first-successful-request",
                    "durationMinutes": 300,
                    "evidence": "builtin-provider",
                },
            }
        ]
        result = invoke(["discover"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Claude Code CLI found: 1.0.66", result.output)
        self.assertIn("5 hours", result.output)


class InitTests(IsolatedTestCase):
    @mock.patch("awewarm.discover.discover_accounts")
    def test_init_adds_claude_account_hybrid(self, discover_accounts):
        discover_accounts.return_value = [
            {
                "provider": "claude-code",
                "label": "Claude Code",
                "cliCommand": "claude",
                "cliPath": "/Users/x/.local/bin/claude",
                "installed": True,
                "version": "1.0.66",
                "authFound": True,
                "authDetail": "file",
                "builtinWindow": {
                    "status": "verified",
                    "startRule": "first-successful-request",
                    "durationMinutes": 300,
                    "evidence": "builtin-provider",
                },
            },
            {
                "provider": "codex",
                "label": "Codex",
                "cliCommand": "codex",
                "cliPath": None,
                "installed": False,
                "version": None,
                "authFound": False,
                "authDetail": None,
                "builtinWindow": {"status": "unknown", "startRule": "unknown", "durationMinutes": None, "evidence": "none"},
            },
        ]
        # manage? (enter=default y) / mode (enter=default hybrid) / time / days / install? no
        result = invoke(["init"], input="\n\n\n\nn\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        data = cfg.load_config()
        self.assertIn("claude-code", data["connections"])
        conn = data["connections"]["claude-code"]
        self.assertEqual(conn["schedule"]["mode"], "hybrid")
        self.assertEqual(conn["window"]["durationMinutes"], 300)
        # absolute path, not the bare name — launchd ticks can't resolve bare
        # names from user-local install dirs
        self.assertEqual(conn["transport"]["cliCommand"], "/Users/x/.local/bin/claude")
        self.assertIn("mode hybrid", result.output)


class AddPlanTests(IsolatedTestCase):
    INPUT = "\n".join(
        [
            "GLM Coding Plan",
            "https://open.bigmodel.cn/api/anthropic",
            "tok-123",
            "",  # plan URL optional
            "",  # protocol -> default 3 (anthropic)
            "glm-4.7",
            "",  # warm-up mode -> default 1 (fixed)
            "",  # fixed time -> default 06:35
            "",  # days -> default weekday
        ]
    ) + "\n"

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=False)
    @mock.patch("awewarm.transport.send_activation")
    def test_add_plan_happy_path_fixed_mode(self, send, keychain_available):
        send.return_value = {"ok": True, "detail": "ok"}
        result = invoke(["add", "plan"], input=self.INPUT)
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Authentication accepted", result.output)
        data = cfg.load_config()
        (conn_id, conn), = data["connections"].items()
        self.assertEqual(conn_id, "glm-coding-plan")
        self.assertEqual(conn["kind"], "subscription")
        self.assertEqual(conn["transport"]["kind"], "anthropic-messages")
        self.assertEqual(conn["schedule"]["mode"], "fixed")
        self.assertEqual(conn["window"]["status"], "unknown")
        self.assertEqual(conn["auth"]["tokenRef"], "${AWEWARM_TOKEN_GLM_CODING_PLAN}")
        self.assertIn("Keychain unavailable", result.output)

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=False)
    @mock.patch("awewarm.transport.send_activation")
    def test_add_plan_accepts_multiple_fixed_times(self, send, keychain_available):
        send.return_value = {"ok": True, "detail": "ok"}
        multi = self.INPUT.replace("glm-4.7\n\n\n", "glm-4.7\n\n16:45, 06:35, 11:40\n")
        result = invoke(["add", "plan"], input=multi)
        self.assertEqual(result.exit_code, 0, output_of(result))
        (conn_id, conn), = cfg.load_config()["connections"].items()
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35", "11:40", "16:45"])

    @mock.patch("awewarm.transport.send_activation")
    def test_add_plan_endpoint_failure_decline_aborts(self, send):
        send.return_value = {"ok": False, "detail": "HTTP 401: bad key"}
        result = invoke(["add", "plan"], input=self.INPUT + "n\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("aborted", output_of(result))
        self.assertFalse(cfg.config_path().exists())


class RunTests(IsolatedTestCase):
    def always_due_conn(self):
        # 00:00 slot, every day, catch-up window longer than a day, so the
        # first run of any test is always "due" regardless of wall clock.
        conn = account_connection(mode="fixed", fixed_at=("00:00",), days="every-day")
        conn["schedule"]["fixed"]["catchUpWindowMinutes"] = 1441
        return conn

    def test_dry_run_plans_without_sending_or_writing(self):
        write_config(self.always_due_conn())
        with mock.patch("awewarm.transport.send_activation") as send:
            result = invoke(["run", "--dry-run"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("[dry-run] would activate", result.output)
        send.assert_not_called()
        self.assertFalse(cfg.state_path().exists())

    @mock.patch("awewarm.transport.send_activation")
    def test_run_fires_and_completes_slot(self, send):
        send.return_value = {"ok": True, "detail": "ok"}
        write_config(self.always_due_conn())
        result = invoke(["run"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("activated", result.output)
        state = cfg.load_state()
        self.assertIsNotNone(state["connections"]["claude-code-main"]["lastActivationAt"])
        second = invoke(["run"])
        self.assertIn("nothing due", second.output)
        self.assertEqual(send.call_count, 1)

    @mock.patch("awewarm.transport.send_activation")
    def test_run_failure_recorded_not_fatal(self, send):
        send.return_value = {"ok": False, "detail": "claude not found in PATH"}
        write_config(self.always_due_conn())
        result = invoke(["run"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        state = cfg.load_state()
        self.assertEqual(state["connections"]["claude-code-main"]["lastResult"], "failure")
        self.assertIn("failed", result.output)

    @mock.patch("awewarm.transport.send_activation")
    def test_run_skips_disabled_connection(self, send):
        conn = self.always_due_conn()
        conn["enabled"] = False
        write_config(conn)
        result = invoke(["run"])
        self.assertIn("nothing due", result.output)
        send.assert_not_called()


class ActivateTests(IsolatedTestCase):
    def test_activate_requires_confirm(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["activate", "claude-code-main"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--confirm", output_of(result))

    @mock.patch("awewarm.transport.send_activation")
    def test_activate_with_confirm_records_manual(self, send):
        send.return_value = {"ok": True, "detail": "ok"}
        write_config(account_connection(mode="fixed"))
        result = invoke(["activate", "claude-code-main", "--confirm"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        state = cfg.load_state()
        self.assertEqual(state["connections"]["claude-code-main"]["history"][-1]["kind"], "manual")

    def test_activate_unknown_connection(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["activate", "nope", "--confirm"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unknown connection", output_of(result))


class VerifyTests(IsolatedTestCase):
    def test_verify_without_flags_only_prints_guidance(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        with mock.patch("awewarm.transport.send_activation") as send:
            result = invoke(["verify", "glm-coding-plan"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--user-confirm", result.output)
        send.assert_not_called()

    def test_verify_user_confirm_unlocks_interval(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = RUNNER.invoke(
            cli, ["verify", "glm-coding-plan", "--user-confirm", "--duration", "300"]
        )
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm-coding-plan"]
        self.assertEqual(conn["window"]["status"], "user-confirmed")
        self.assertEqual(conn["window"]["durationMinutes"], 300)

    def test_verify_user_confirm_requires_duration(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["verify", "glm-coding-plan", "--user-confirm"])
        self.assertNotEqual(result.exit_code, 0)


class LifecycleTests(IsolatedTestCase):
    def test_enable_interval_blocked_until_window_confirmed(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        blocked = invoke(["enable", "glm-coding-plan", "--mode", "interval"])
        self.assertNotEqual(blocked.exit_code, 0)
        self.assertIn("verify", output_of(blocked))
        data = cfg.load_config()
        data["connections"]["glm-coding-plan"]["window"] = {
            "status": "user-confirmed",
            "startRule": "unknown",
            "durationMinutes": 300,
            "evidence": "user-confirmed",
        }
        cfg.save_config(data)
        allowed = invoke(["enable", "glm-coding-plan", "--mode", "interval"])
        self.assertEqual(allowed.exit_code, 0, output_of(allowed))
        self.assertEqual(cfg.load_config()["connections"]["glm-coding-plan"]["schedule"]["mode"], "interval")

    def test_disable_and_status(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["disable", "claude-code-main"])
        self.assertEqual(result.exit_code, 0)
        status = invoke(["status"])
        self.assertIn("disabled", status.output)

    @mock.patch("awewarm.keychain.delete_token")
    def test_remove_deletes_everything(self, delete_token):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        cfg.save_state({"version": 1, "connections": {"glm-coding-plan": cfg.default_conn_state()}})
        result = invoke(["remove", "glm-coding-plan"], input="y\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertEqual(cfg.load_config()["connections"], {})
        self.assertEqual(cfg.load_state()["connections"], {})
        delete_token.assert_called_once_with("glm-coding-plan")


class TimesTests(IsolatedTestCase):
    def test_show_without_args(self):
        write_config(account_connection(mode="hybrid"))
        result = invoke(["times", "claude-code-main"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("06:35", result.output)
        self.assertIn("weekday", result.output)

    def test_set_multiple_times_sorted_and_deduped(self):
        write_config(account_connection(mode="hybrid"))
        result = invoke(["times", "claude-code-main", "16:45", "06:35", "16:45", "11:40"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35", "11:40", "16:45"])

    def test_invalid_time_dies_without_saving(self):
        write_config(account_connection(mode="hybrid"))
        result = invoke(["times", "claude-code-main", "6:35"])
        self.assertNotEqual(result.exit_code, 0)
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35"])

    def test_interval_mode_connection_notes_mode_switch(self):
        conn = account_connection(mode="interval")
        write_config(conn)
        result = invoke(["times", "claude-code-main", "06:35", "11:40"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("--mode", result.output)
        saved = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(saved["schedule"]["fixed"]["at"], ["06:35", "11:40"])
        self.assertEqual(saved["schedule"]["fixed"]["days"], "weekday")


class StatusTests(IsolatedTestCase):
    def test_status_empty_suggests_onboarding(self):
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("No connections yet", result.output)

    def test_status_shows_connection_summary(self):
        write_config(account_connection(mode="hybrid"))
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Claude Code (claude-code-main) — connected", result.output)
        self.assertIn("Mode: hybrid", result.output)
        self.assertIn("300 minutes, verified", result.output)
        self.assertIn("Scheduler: not installed", result.output)


class InspectTests(IsolatedTestCase):
    def test_inspect_json_redacts_token_ref(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["inspect", "glm-coding-plan", "--json"])
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        conn = payload["config"]["connections"]["glm-coding-plan"]
        self.assertEqual(conn["auth"]["tokenRef"], "<redacted>")
        self.assertEqual(payload["scheduler"]["installed"], False)


if __name__ == "__main__":
    unittest.main()
