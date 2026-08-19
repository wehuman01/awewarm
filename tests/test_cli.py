import json
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner
from helpers import IsolatedTestCase, account_connection, plan_connection

import awewarm
from awewarm import config as cfg, keystore, schedule
from awewarm.cli import cli, main

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


def command_names(help_output):
    lines = help_output.splitlines()
    start = lines.index("Commands:")
    return [line.split()[0] for line in lines[start + 1:] if line.strip()]


def claude_finding(**overrides):
    finding = {
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
    }
    finding.update(overrides)
    return finding


class SurfaceTests(IsolatedTestCase):
    def test_help_shows_exactly_seven_commands(self):
        result = invoke(["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Usage: awewarm [OPTIONS] COMMAND [ARGS]...", result.output)
        self.assertIn("-v, --version", result.output)
        self.assertEqual(
            command_names(result.output),
            ["config", "discover", "init", "run", "scheduler", "status", "update"],
        )

    def test_legacy_command_names_are_hidden(self):
        result = invoke(["--help"])
        names = command_names(result.output)
        for legacy in ("add", "activate", "verify", "enable", "anchor", "disable", "times", "remove", "install", "uninstall", "inspect", "self-update"):
            self.assertNotIn(legacy, names)

    def test_group_help_lists_subcommands(self):
        self.assertEqual(command_names(invoke(["config", "--help"]).output), ["add", "edit", "path", "remove", "set", "show"])
        self.assertEqual(command_names(invoke(["scheduler", "--help"]).output), ["install", "uninstall"])

    def test_version_prints_bare_number(self):
        result = invoke(["-v"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output.strip(), awewarm.__version__)

    def test_entry_point_and_dependency_pinned(self):
        data = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        self.assertIn('awewarm = "awewarm.cli:main"', data)
        self.assertIn('click>=8.1', data)
        # Windows has no system tz database; zoneinfo needs the tzdata package
        self.assertIn("tzdata; platform_system == 'Windows'", data)


class DiscoverCommandTests(IsolatedTestCase):
    @mock.patch("awewarm.discover.discover_accounts")
    def test_discover_prints_findings(self, discover_accounts):
        discover_accounts.return_value = [claude_finding(cliPath=None)]
        result = invoke(["discover"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Claude Code CLI found: 1.0.66", result.output)
        self.assertIn("5 hours", result.output)


class InitTests(IsolatedTestCase):
    @mock.patch("awewarm.discover.discover_accounts")
    def test_init_adds_claude_account_hybrid(self, discover_accounts):
        discover_accounts.return_value = [
            claude_finding(),
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
        result = invoke(["init"], input="\n\n\n\n\nn\n")
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


class ConfigAddPlanTests(IsolatedTestCase):
    INPUT = "\n".join(
        [
            "GLM Coding Plan",
            "3",  # protocol -> Anthropic Messages (no default; must be explicit)
            "https://open.bigmodel.cn/api/anthropic",
            "tok-123",
            "glm-4.7",
            "",  # warm-up mode -> default 1 (fixed)
            "",  # fixed time -> default 06:35
            "",  # days -> default weekday
        ]
    ) + "\n"

    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_add_plan_happy_path_fixed_mode(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        result = invoke(["config", "add"], input=self.INPUT)
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Authentication accepted", result.output)
        data = cfg.load_config()
        (conn_id, conn), = data["connections"].items()
        self.assertEqual(conn_id, "glm-coding-plan")
        self.assertEqual(conn["kind"], "subscription")
        self.assertEqual(conn["transport"]["kind"], "anthropic-messages")
        self.assertEqual(conn["schedule"]["mode"], "fixed")
        self.assertEqual(conn["window"]["status"], "unknown")
        self.assertEqual(conn["auth"]["apiKeyRef"], "file:glm-coding-plan")
        self.assertIn("API key stored in", result.output)

    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_add_plan_accepts_multiple_fixed_times(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        multi = self.INPUT.replace("glm-4.7\n\n\n", "glm-4.7\n\n16:45, 06:35, 11:40\n")
        result = invoke(["config", "add"], input=multi)
        self.assertEqual(result.exit_code, 0, output_of(result))
        (conn_id, conn), = cfg.load_config()["connections"].items()
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35", "11:40", "16:45"])

    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_add_plan_endpoint_failure_decline_aborts(self, send, _discover):
        send.return_value = {"ok": False, "detail": "HTTP 401: bad key"}
        result = invoke(["config", "add"], input=self.INPUT + "n\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("aborted", output_of(result))
        self.assertFalse(cfg.config_path().exists())


class ConfigAddMenuTests(IsolatedTestCase):
    @mock.patch("awewarm.discover.discover_accounts")
    def test_menu_readds_a_removed_account(self, discover_accounts):
        discover_accounts.return_value = [claude_finding()]
        # menu choice 1 (Claude Code) / mode (default hybrid) / times / days / window open? (no)
        result = invoke(["config", "add"], input="1\n\n\n\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code"]
        self.assertEqual(conn["kind"], "account")
        self.assertEqual(conn["schedule"]["mode"], "hybrid")
        self.assertEqual(conn["transport"]["cliCommand"], "/Users/x/.local/bin/claude")

    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_menu_endpoint_choice_runs_plan_flow(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
        discover_accounts.return_value = [claude_finding()]
        endpoint_input = "\n".join(["2", "GLM", "1", "http://x/v4", "k", "glm-4.7", "", "", ""]) + "\n"
        result = invoke(["config", "add"], input=endpoint_input)
        self.assertEqual(result.exit_code, 0, output_of(result))
        (conn_id, conn), = cfg.load_config()["connections"].items()
        self.assertEqual(conn_id, "glm")
        self.assertEqual(conn["kind"], "subscription")

    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_menu_marks_already_managed_accounts(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
        discover_accounts.return_value = [claude_finding()]
        write_config(account_connection(mode="fixed"))  # label "Claude Code"
        result = invoke(["config", "add"], input="2\nGLM\n1\nhttp://x/v4\nk\nglm-4.7\n\n\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("already managed", result.output)

    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_unauthenticated_account_gets_login_hint(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
        discover_accounts.return_value = [claude_finding(authFound=False)]
        result = invoke(["config", "add"], input="GLM\n1\nhttp://x/v4\nk\nm\n\n\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("claude auth login", result.output)
        self.assertIn("adding a subscription endpoint", result.output)


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


class RunNowTests(IsolatedTestCase):
    def test_now_requires_confirm(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["run", "--now", "claude-code-main"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--confirm", output_of(result))

    @mock.patch("awewarm.transport.send_activation")
    def test_now_with_confirm_records_manual(self, send):
        send.return_value = {"ok": True, "detail": "ok"}
        write_config(account_connection(mode="fixed"))
        result = invoke(["run", "--now", "claude-code-main", "--confirm"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        state = cfg.load_state()
        self.assertEqual(state["connections"]["claude-code-main"]["history"][-1]["kind"], "manual")

    def test_now_unknown_connection(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["run", "--now", "nope", "--confirm"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unknown connection", output_of(result))

    def test_now_dry_run_sends_nothing(self):
        write_config(account_connection(mode="fixed"))
        with mock.patch("awewarm.transport.send_activation") as send:
            result = invoke(["run", "--now", "claude-code-main", "--confirm", "--dry-run"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("[dry-run] would activate", result.output)
        send.assert_not_called()


class WindowSetTests(IsolatedTestCase):
    def test_set_without_flags_shows_settings(self):
        write_config(account_connection(mode="hybrid"))
        with mock.patch("awewarm.transport.send_activation") as send:
            result = invoke(["config", "set", "claude-code-main"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Settings for claude-code-main", result.output)
        self.assertIn("06:35", result.output)
        self.assertIn("weekday", result.output)
        send.assert_not_called()

    def test_window_records_user_confirmation(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["config", "set", "glm-coding-plan", "--window", "300"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm-coding-plan"]
        self.assertEqual(conn["window"]["status"], "user-confirmed")
        self.assertEqual(conn["window"]["durationMinutes"], 300)

    def test_window_rejects_nonpositive_duration(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["config", "set", "glm-coding-plan", "--window", "0"])
        self.assertNotEqual(result.exit_code, 0)
        conn = cfg.load_config()["connections"]["glm-coding-plan"]
        self.assertEqual(conn["window"]["status"], "unknown")


class LifecycleTests(IsolatedTestCase):
    def test_mode_interval_blocked_until_window_confirmed(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        blocked = invoke(["config", "set", "glm-coding-plan", "--mode", "interval"])
        self.assertNotEqual(blocked.exit_code, 0)
        self.assertIn("--window", output_of(blocked))
        data = cfg.load_config()
        data["connections"]["glm-coding-plan"]["window"] = {
            "status": "user-confirmed",
            "startRule": "unknown",
            "durationMinutes": 300,
            "evidence": "user-confirmed",
        }
        cfg.save_config(data)
        allowed = invoke(["config", "set", "glm-coding-plan", "--mode", "interval"])
        self.assertEqual(allowed.exit_code, 0, output_of(allowed))
        self.assertEqual(cfg.load_config()["connections"]["glm-coding-plan"]["schedule"]["mode"], "interval")

    def test_off_and_on_toggle_with_status_reflecting(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--off"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("disabled", result.output)
        status = invoke(["status"])
        self.assertIn("disabled", status.output)
        resumed = invoke(["config", "set", "claude-code-main", "--on"])
        self.assertEqual(resumed.exit_code, 0)
        self.assertTrue(cfg.load_config()["connections"]["claude-code-main"]["enabled"])

    @mock.patch("awewarm.keystore.delete_api_key")
    def test_remove_deletes_everything(self, delete_api_key):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        cfg.save_state({"version": 1, "connections": {"glm-coding-plan": cfg.default_conn_state()}})
        result = invoke(["config", "remove", "glm-coding-plan"], input="y\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertEqual(cfg.load_config()["connections"], {})
        self.assertEqual(cfg.load_state()["connections"], {})
        delete_api_key.assert_called_once_with("glm-coding-plan", "${AWEWARM_API_KEY_GLM_CODING_PLAN}")


class ApiKeySetTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        write_config(plan_connection(), conn_id="glm-coding-plan")

    def test_api_key_stored_in_secrets_file(self):
        result = invoke(["config", "set", "glm-coding-plan", "--api-key", "sk-new-123"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("stored in", result.output)
        conn = cfg.load_config()["connections"]["glm-coding-plan"]
        self.assertEqual(conn["auth"]["apiKeyRef"], "file:glm-coding-plan")
        self.assertEqual(keystore.load_api_key("file:glm-coding-plan"), "sk-new-123")

    def test_api_key_env_ref(self):
        result = invoke(["config", "set", "glm-coding-plan", "--api-key-env", "GLM_API_KEY"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm-coding-plan"]
        self.assertEqual(conn["auth"]["apiKeyRef"], "${GLM_API_KEY}")

    def test_api_key_and_env_rejected_together(self):
        result = invoke(["config", "set", "glm-coding-plan", "--api-key", "sk", "--api-key-env", "GLM_API_KEY"])
        self.assertNotEqual(result.exit_code, 0)

    def test_config_add_accepts_env_ref_input(self):
        send = mock.MagicMock(return_value={"ok": True, "detail": "ok"})
        with mock.patch("awewarm.transport.send_activation", send), \
                mock.patch("awewarm.discover.discover_accounts", return_value=[]), \
                mock.patch.dict("os.environ", {"GLM_API_KEY": "from-env"}, clear=False):
            result = invoke(["config", "add"], input=(
                "GLM Plan\n1\nhttps://open.bigmodel.cn/api/coding/paas/v4\n"
                "${GLM_API_KEY}\nglm-4.7\n1\n06:00\n1\n"
            ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm-plan"]
        self.assertEqual(conn["auth"]["apiKeyRef"], "${GLM_API_KEY}")



class SetTimesTests(IsolatedTestCase):
    def test_set_multiple_times_sorted_and_deduped(self):
        write_config(account_connection(mode="hybrid"))
        result = invoke(["config", "set", "claude-code-main", "--times", "16:45,06:35,16:45,11:40"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35", "11:40", "16:45"])

    def test_times_accepts_space_separated_values(self):
        write_config(account_connection(mode="hybrid"))
        result = invoke(["config", "set", "claude-code-main", "--times", "16:45 06:35"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35", "16:45"])

    def test_invalid_time_dies_without_saving(self):
        write_config(account_connection(mode="hybrid"))
        result = invoke(["config", "set", "claude-code-main", "--times", "6:35"])
        self.assertNotEqual(result.exit_code, 0)
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35"])

    def test_interval_mode_connection_notes_mode_switch(self):
        conn = account_connection(mode="interval")
        write_config(conn)
        result = invoke(["config", "set", "claude-code-main", "--times", "06:35,11:40"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("--mode", result.output)
        saved = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(saved["schedule"]["fixed"]["at"], ["06:35", "11:40"])
        self.assertEqual(saved["schedule"]["fixed"]["days"], "weekday")

    def test_days_changes_without_times(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--days", "every-day"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["days"], "every-day")
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35"])

    def test_combined_flags_apply_together(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--times", "07:00,12:00", "--days", "every-day"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["07:00", "12:00"])
        self.assertEqual(conn["schedule"]["fixed"]["days"], "every-day")


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
        self.assertIn("300 minutes, user-confirmed", result.output)
        self.assertIn("Scheduler: not installed", result.output)

    def test_status_single_connection_shows_detail(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["status", "glm-coding-plan"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Transport: anthropic-messages", result.output)
        self.assertIn("evidence:", result.output)
        self.assertIn("Fixed times: 06:35", result.output)
        self.assertIn("Next due:", result.output)

    def test_status_json_redacts_api_key_ref(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["status", "glm-coding-plan", "--json"])
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        conn = payload["config"]["connections"]["glm-coding-plan"]
        self.assertEqual(conn["auth"]["apiKeyRef"], "<redacted>")
        self.assertEqual(payload["scheduler"]["installed"], False)

    def test_status_json_covers_all_connections(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["status", "--json"])
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertIn("claude-code-main", payload["config"]["connections"])


class ConfigPathTests(IsolatedTestCase):
    def test_config_path_prints_all_paths(self):
        result = invoke(["config", "path"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn(str(cfg.config_path()), result.output)
        self.assertIn(str(cfg.state_path()), result.output)
        self.assertIn(str(cfg.log_path()), result.output)


class SchedulerCommandTests(IsolatedTestCase):
    @mock.patch("awewarm.install.install_scheduler")
    def test_scheduler_install(self, install_scheduler):
        install_scheduler.return_value = "/Library/LaunchAgents/com.awewarm.scheduler.plist"
        result = invoke(["scheduler", "install"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Scheduler installed", result.output)

    @mock.patch("awewarm.install.uninstall_scheduler", return_value=False)
    def test_scheduler_uninstall_when_absent(self, _uninstall):
        result = invoke(["scheduler", "uninstall"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("was not installed", result.output)


class UpdateTests(IsolatedTestCase):
    @mock.patch("awewarm.cli.get_pypi_latest", return_value="0.0.1")
    def test_check_when_up_to_date(self, _pypi):
        result = invoke(["update", "--check"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("up to date", result.output)

    @mock.patch("awewarm.cli.subprocess.run")
    @mock.patch("awewarm.cli.get_pypi_latest", return_value="9.9.9")
    def test_check_shows_latest_without_updating(self, _pypi, run):
        result = invoke(["update", "--check"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("9.9.9", result.output)
        run.assert_not_called()

    @mock.patch("awewarm.cli.subprocess.run")
    @mock.patch("awewarm.cli.get_pypi_latest", return_value="9.9.9")
    def test_update_runs_pip(self, _pypi, run):
        run.return_value = mock.Mock(returncode=0)
        result = invoke(["update"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        command = " ".join(run.call_args[0][0])
        self.assertIn("awewarm", command)
        self.assertNotIn("pipx", command)

    @mock.patch("awewarm.cli.get_pypi_latest", side_effect=OSError("offline"))
    def test_update_dies_on_network_failure(self, _pypi):
        result = invoke(["update", "--check"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("failed to check PyPI", output_of(result))


class LegacyAliasTests(IsolatedTestCase):
    def test_alias_prints_migration_note_to_stderr(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["disable", "claude-code-main"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("moved to", output_of(result))
        self.assertIn("config set", output_of(result))

    def test_legacy_times_sets_fixed_times(self):
        write_config(account_connection(mode="hybrid"))
        result = invoke(["times", "claude-code-main", "06:35", "11:40"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35", "11:40"])

    def test_legacy_enable_and_disable(self):
        write_config(account_connection(mode="fixed"))
        self.assertEqual(invoke(["disable", "claude-code-main"]).exit_code, 0)
        self.assertFalse(cfg.load_config()["connections"]["claude-code-main"]["enabled"])
        self.assertEqual(invoke(["enable", "claude-code-main"]).exit_code, 0)
        self.assertTrue(cfg.load_config()["connections"]["claude-code-main"]["enabled"])

    def test_legacy_verify_user_confirm_sets_window(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["verify", "glm-coding-plan", "--user-confirm", "--duration", "300"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm-coding-plan"]
        self.assertEqual(conn["window"]["status"], "user-confirmed")

    def test_legacy_activate_routes_to_run_now(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["activate", "claude-code-main"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--confirm", output_of(result))

    @mock.patch("awewarm.cli.get_pypi_latest", return_value="0.0.1")
    def test_legacy_self_update_check(self, _pypi):
        result = invoke(["self-update", "--check"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("up to date", result.output)

    def test_legacy_inspect_json(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["inspect", "glm-coding-plan", "--json"])
        self.assertEqual(result.exit_code, 0)
        # the migration note (stderr) is mixed in before the JSON payload
        payload = json.loads(result.output[result.output.index("{"):])
        self.assertEqual(payload["config"]["connections"]["glm-coding-plan"]["auth"]["apiKeyRef"], "<redacted>")

    @mock.patch("awewarm.install.install_scheduler")
    def test_legacy_install_routes_to_scheduler(self, install_scheduler):
        install_scheduler.return_value = "/plist"
        result = invoke(["install"])
        self.assertEqual(result.exit_code, 0)
        install_scheduler.assert_called_once()

    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_legacy_add_plan_routes_to_config_add(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        result = invoke(["add", "plan"], input=ConfigAddPlanTests.INPUT)
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("glm-coding-plan", cfg.load_config()["connections"])


class MainReminderTests(IsolatedTestCase):
    @mock.patch("click.echo")
    @mock.patch("awewarm.cli.check_async", return_value=lambda: "Update available: 0.1 → 9.9")
    @mock.patch("sys.argv", ["awewarm", "status"])
    def test_main_prints_reminder_after_interactive_command(self, _check, echo):
        try:
            main()
        except SystemExit:
            pass
        echoed = [str(call.args[0]) for call in echo.call_args_list]
        self.assertTrue(any("Update available" in text for text in echoed), echoed)

    @mock.patch("awewarm.cli.check_async", return_value=lambda: None)
    @mock.patch("sys.argv", ["awewarm", "run"])
    def test_main_passes_argv_so_run_ticks_can_be_skipped(self, check):
        try:
            main()
        except SystemExit:
            pass
        check.assert_called_once_with(["run"])


class AddPlanUserAnchorTests(IsolatedTestCase):
    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_mode3_with_open_window_anchors_renewal(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "add"], input="\n".join([
                "GLM", "1", "http://x/v4", "k", "glm-4.7",
                "3",        # warm-up mode: configure interval manually
                "300",      # duration
                "2",        # interval only (no fixed settings prompts)
                "y",        # window already open
                "13:27",    # current window closes at
            ]) + "\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Renewal anchored", result.output)
        state = cfg.load_state()
        cs = state["connections"]["glm"]
        self.assertEqual(schedule.parse_ts(cs["lastActivationAt"]).strftime("%H:%M"), "08:27")
        self.assertEqual(schedule.parse_ts(cs["nextDueAt"]).strftime("%H:%M"), "13:28")


class AnchorTests(IsolatedTestCase):
    def anchored_conn(self):
        conn = plan_connection(mode="interval", fixed_at=())
        conn["window"] = {
            "status": "user-confirmed", "startRule": "unknown",
            "durationMinutes": 300, "evidence": "user-confirmed",
        }
        return write_config(conn)

    def test_anchor_sets_next_due_after_reset(self):
        self.anchored_conn()
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "set", "claude-code-main", "--anchor", "13:27"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("anchored", result.output)
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(schedule.parse_ts(cs["lastActivationAt"]).strftime("%H:%M"), "08:27")
        self.assertEqual(schedule.parse_ts(cs["nextDueAt"]).strftime("%H:%M"), "13:28")

    def test_anchor_rejects_past_time(self):
        self.anchored_conn()
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "set", "claude-code-main", "--anchor", "13:27"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("already passed", result.output)

    def test_anchor_requires_interval_mode(self):
        write_config(plan_connection(mode="fixed", window_status="user-confirmed", duration=300))
        result = invoke(["config", "set", "claude-code-main", "--anchor", "23:00"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--mode hybrid", result.output)

    def test_mode_and_anchor_combine_in_one_call(self):
        write_config(plan_connection(mode="fixed", window_status="user-confirmed", duration=300))
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "set", "claude-code-main", "--mode", "hybrid", "--anchor", "13:27"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["mode"], "hybrid")
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(schedule.parse_ts(cs["nextDueAt"]).strftime("%H:%M"), "13:28")


if __name__ == "__main__":
    unittest.main()
