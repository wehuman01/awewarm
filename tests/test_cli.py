import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from helpers import (
    IsolatedTestCase,
    account_connection,
    plan_connection,
    start_http_server,
    stop_http_server,
)

import awewarm
from awewarm import config as cfg, keystore, remote, schedule, server
from awewarm.cli import cli, main
from awewarm.locking import process_lock

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
    # command rows sit at a 2-space indent; wrapped descriptions indent deeper
    return [
        line.split()[0]
        for line in lines[start + 1:]
        if line.startswith("  ") and not line.startswith("   ")
    ]


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
    def test_help_shows_exactly_nine_commands(self):
        result = invoke(["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Usage: awewarm [OPTIONS] COMMAND [ARGS]...", result.output)
        self.assertIn("-v, --version", result.output)
        self.assertEqual(
            command_names(result.output),
            ["config", "discover", "init", "remote", "run", "scheduler", "self-update", "serve", "status"],
        )

    def test_legacy_command_names_are_hidden(self):
        result = invoke(["--help"])
        names = command_names(result.output)
        for legacy in ("add", "activate", "verify", "enable", "anchor", "disable", "times", "remove", "install", "uninstall", "inspect"):
            self.assertNotIn(legacy, names)

    def test_group_help_lists_subcommands(self):
        self.assertEqual(command_names(invoke(["config", "--help"]).output), ["add", "edit", "path", "remove", "set", "settings", "show", "template"])
        self.assertEqual(command_names(invoke(["scheduler", "--help"]).output), ["install", "uninstall"])
        self.assertEqual(command_names(invoke(["remote", "--help"]).output), ["connect", "disconnect", "push"])

    def test_hub_commands_moved_to_the_hub_package(self):
        # hub serving/administration is the separate awewarm-hub package now;
        # both tombstones must name the replacement command verbatim.
        result = invoke(["hub", "invite"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("awewarm-hub", output_of(result))
        self.assertIn("awewarm-hub invite", output_of(result))
        result = invoke(["hub", "list", "users"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("awewarm-hub list users", output_of(result))

    def test_serve_hub_flag_moved_to_the_hub_package(self):
        result = invoke(["serve", "--hub"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("awewarm-hub serve", output_of(result))

    def test_update_command_is_gone(self):
        # `update` was replaced by `self-update` outright — no hidden alias.
        result = invoke(["update"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such command", result.output)

    def test_version_marks_a_source_checkout(self):
        result = invoke(["-v"])
        self.assertEqual(result.exit_code, 0)
        # tests import the package from this repo, so -v says editable
        self.assertTrue(result.output.strip().startswith(awewarm.__version__))
        self.assertIn("editable", result.output)

    @mock.patch("awewarm.running_from_checkout", return_value=False)
    def test_version_is_bare_outside_a_checkout(self, _checkout):
        self.assertEqual(awewarm.display_version(), awewarm.__version__)

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
    @mock.patch("awewarm.transport.send_activation")
    def test_init_adds_claude_account_fixed(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
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
        # manage? (enter=y) / mode (fixed) / time / reset / grid (accept) / days / install? no
        result = invoke(["init"], input="\n\n\n\n\n\nn\nn\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        data = cfg.load_config()
        self.assertIn("claude-code", data["connections"])
        conn = data["connections"]["claude-code"]
        self.assertEqual(conn["schedule"]["mode"], "fixed")
        self.assertEqual(conn["window"]["durationMinutes"], 300)
        # absolute path, not the bare name — launchd ticks can't resolve bare
        # names from user-local install dirs
        self.assertEqual(conn["transport"]["cliCommand"], "/Users/x/.local/bin/claude")
        self.assertIn("mode fixed", result.output)
        self.assertIn("Activation test passed", result.output)
        send.assert_called_once()


class ConfigAddPlanTests(IsolatedTestCase):
    INPUT = "\n".join(
        [
            "GLM Coding Plan",
            "3",  # protocol -> Anthropic Messages (no default; must be explicit)
            "https://open.bigmodel.cn/api/anthropic",
            "tok-123",
            "glm-4.7",
            "",  # warm-up mode -> default 1 (fixed)
            "",  # window duration -> default 300
            "",  # fixed time -> default 06:35
            "",  # reset anchor -> defaults to the entered time
            "",  # grid accept -> full-day grid from 06:35
            "",  # days -> default every-day (grid accepted)
            "",  # wake when asleep -> default yes (macOS prompt)
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
        self.assertEqual(conn["window"]["status"], "user-confirmed")
        self.assertEqual(conn["window"]["durationMinutes"], 300)
        self.assertEqual(
            conn["schedule"]["fixed"]["at"], ["06:35", "11:40", "16:45", "21:50"]
        )
        self.assertEqual(conn["schedule"]["fixed"]["days"], "every-day")
        self.assertEqual(conn["auth"]["apiKeyRef"], "file:glm-coding-plan")
        self.assertIn("API key stored in", result.output)

    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_add_plan_accepts_multiple_fixed_times(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        multi = self.INPUT.replace(
            "glm-4.7\n\n\n\n\n\n\n\n", "glm-4.7\n\n\n16:45, 06:35, 11:40\n\n\n"
        )
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
    @mock.patch("awewarm.transport.send_activation")
    def test_menu_readds_a_removed_account(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
        discover_accounts.return_value = [claude_finding()]
        # menu 1 (Claude Code) / mode / times / reset / grid (accept) / days / window open? (no)
        result = invoke(["config", "add"], input="1\n\n\n\n\n\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code"]
        self.assertEqual(conn["kind"], "account")
        self.assertEqual(conn["schedule"]["mode"], "fixed")
        self.assertEqual(conn["transport"]["cliCommand"], "/Users/x/.local/bin/claude")

    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_menu_endpoint_choice_runs_plan_flow(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
        discover_accounts.return_value = [claude_finding()]
        endpoint_input = "\n".join(["2", "GLM", "1", "http://x/v4", "k", "glm-4.7", "", "", "", "", "", "", ""]) + "\n"
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
        result = invoke(["config", "add"], input="2\nGLM\n1\nhttp://x/v4\nk\nglm-4.7\n\n\n\n\n\n\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("already managed", result.output)

    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_unauthenticated_account_gets_login_hint(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
        discover_accounts.return_value = [claude_finding(authFound=False)]
        result = invoke(["config", "add"], input="GLM\n1\nhttp://x/v4\nk\nm\n\n\n\n\n\n\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("claude auth login", result.output)
        self.assertIn("adding a subscription endpoint", result.output)


class WakePromptTests(IsolatedTestCase):
    @mock.patch("awewarm.cli.sys.platform", "darwin")
    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_plan_fixed_wake_prompt_defaults_to_no(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        # mode 1 / window / times / reset / grid declined / days / wake default (Enter)
        result = invoke(["config", "add"], input=(
            "GLM\n1\nhttp://x/v4\nk\nglm-4.7\n1\n\n06:00\n\nn\n\n\n"
        ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm"]
        self.assertFalse(conn["schedule"]["wakeWhenAsleep"])

    @mock.patch("awewarm.cli.sys.platform", "darwin")
    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_plan_fixed_wake_confirmed_records_true(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        result = invoke(["config", "add"], input=(
            "GLM\n1\nhttp://x/v4\nk\nglm-4.7\n1\n\n06:00\n\nn\n\ny\n"
        ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm"]
        self.assertTrue(conn["schedule"]["wakeWhenAsleep"])

    @mock.patch("awewarm.cli.sys.platform", "darwin")
    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_plan_fixed_wake_declined_records_false(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        result = invoke(["config", "add"], input=(
            "GLM\n1\nhttp://x/v4\nk\nglm-4.7\n1\n\n06:00\n\nn\n\nn\n"
        ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm"]
        self.assertFalse(conn["schedule"]["wakeWhenAsleep"])

    @mock.patch("awewarm.cli.sys.platform", "linux")
    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_linux_skips_wake_prompt_and_follows_layer_defaults(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        # no wake input line at all — Linux must not ask
        result = invoke(["config", "add"], input=(
            "GLM\n1\nhttp://x/v4\nk\nglm-4.7\n1\n\n06:00\n\nn\n\n"
        ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertNotIn("asleep?", result.output)
        conn = cfg.load_config()["connections"]["glm"]
        self.assertFalse(conn["schedule"]["wakeWhenAsleep"])

    def test_config_set_wake_flag_round_trip(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--no-wake"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertFalse(conn["schedule"]["wakeWhenAsleep"])
        result = invoke(["config", "set", "claude-code-main", "--wake"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertTrue(conn["schedule"]["wakeWhenAsleep"])

    @mock.patch("awewarm.cli.sys.platform", "linux")
    def test_config_set_wake_notes_no_effect_on_linux(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--wake"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("cannot wake", result.output)

    def test_settings_show_wake_line(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("wake when asleep:", result.output)


class RunTests(IsolatedTestCase):
    def always_due_conn(self):
        # 00:00 slot, every day, catch-up window longer than a day, so the
        # first run of any test is always "due" regardless of wall clock.
        conn = account_connection(mode="fixed", fixed_at=("00:00",), days="every-day")
        conn["catchup"] = {"attempts": 5, "withinMinutes": 1441}
        return conn

    def test_run_requires_force_without_tty(self):
        # The confirm prompt needs a terminal; a non-tty `run` without
        # --force must die before sending anything or writing state.
        write_config(self.always_due_conn())
        with mock.patch("awewarm.transport.send_activation") as send:
            result = invoke(["run"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("requires --force", output_of(result))
        send.assert_not_called()
        self.assertFalse(cfg.state_path().exists())

    @mock.patch("awewarm.transport.send_activation")
    def test_run_fires_every_enabled_connection(self, send):
        send.return_value = {"ok": True, "detail": "ok"}
        write_config(self.always_due_conn())
        result = invoke(["run", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("activated", result.output)
        state = cfg.load_state()
        self.assertIsNotNone(state["connections"]["claude-code-main"]["lastActivationAt"])
        second = invoke(["run", "--force"])
        self.assertIn("1 of 1 activated", second.output)
        self.assertEqual(send.call_count, 2)

    @mock.patch("awewarm.transport.send_activation")
    def test_run_failure_recorded_not_fatal(self, send):
        send.return_value = {"ok": False, "detail": "claude not found in PATH"}
        write_config(self.always_due_conn())
        result = invoke(["run", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        state = cfg.load_state()
        self.assertEqual(state["connections"]["claude-code-main"]["lastResult"], "failure")
        self.assertIn("0 of 1 activated", result.output)

    @mock.patch("awewarm.transport.send_activation")
    def test_run_skips_disabled_connection(self, send):
        conn = self.always_due_conn()
        conn["enabled"] = False
        write_config(conn)
        result = invoke(["run", "--force"])
        self.assertIn("No enabled connections", result.output)
        send.assert_not_called()

    @mock.patch("awewarm.transport.send_activation")
    def test_run_all_skips_auto_disabled(self, send):
        write_config(self.always_due_conn())
        state = cfg.empty_state()
        cs = cfg.conn_state(state, "claude-code-main")
        cs["autoDisabledAt"] = "2026-08-19T06:35:00+08:00"
        cfg.save_state(state)
        result = invoke(["run", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("auto-disabled after repeated failures", result.output)
        self.assertIn("0 of 1 activated", result.output)
        send.assert_not_called()


class LadderStatusTests(IsolatedTestCase):
    def seed_state(self, **fields):
        write_config(account_connection(mode="fixed"))
        state = cfg.empty_state()
        cs = cfg.conn_state(state, "claude-code-main")
        cs.update(fields)
        cfg.save_state(state)
        return cs

    def test_status_failing_shows_health_line(self):
        self.seed_state(
            nodeKey="2026-08-19 06:35", nodeDueAt="2026-08-19T06:35:00+08:00",
            nodeAttempts=2, failedNodes=1,
            lastResult="failure", lastError="HTTP 500",
        )
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("(claude-code-main) — failing", result.output)
        self.assertIn("Health: failing — 1/3 nodes lost, catch-up attempt 2/5", result.output)

    def test_status_degraded_shows_single_shot(self):
        self.seed_state(degradedAt="2026-08-19T18:29:00+08:00", degradedFailedNodes=1)
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("(claude-code-main) — degraded", result.output)
        self.assertIn("Mode: fixed (single-shot after failures)", result.output)
        self.assertIn("Health: degraded — one shot per node (1/3 lost)", result.output)

    def test_status_auto_disabled_shows_resume_hint(self):
        self.seed_state(autoDisabledAt="2026-08-19T22:00:00+08:00")
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("(claude-code-main) — auto-disabled", result.output)
        self.assertIn("resume with: awewarm config set claude-code-main --on", result.output)
        self.assertIn("Next due: none (auto-disabled)", result.output)

    def test_user_disabled_still_wins_display(self):
        conn = account_connection(mode="fixed")
        conn["enabled"] = False
        write_config(conn)
        state = cfg.empty_state()
        cs = cfg.conn_state(state, "claude-code-main")
        cs["autoDisabledAt"] = "2026-08-19T22:00:00+08:00"
        cfg.save_state(state)
        result = invoke(["status"])
        self.assertIn("(claude-code-main) — disabled", result.output)
        self.assertNotIn("auto-disabled", result.output)

    def test_on_resets_the_ladder(self):
        self.seed_state(
            nodeKey="2026-08-19 06:35", nodeAttempts=3, failedNodes=3,
            degradedAt="2026-08-19T18:29:00+08:00", degradedFailedNodes=2,
            autoDisabledAt="2026-08-19T22:00:00+08:00",
        )
        result = invoke(["config", "set", "claude-code-main", "--on"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("failure counters reset", result.output)
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertIsNone(cs["degradedAt"])
        self.assertIsNone(cs["autoDisabledAt"])
        self.assertIsNone(cs["nodeKey"])
        self.assertEqual(cs["failedNodes"], 0)

    def test_legacy_interval_disabled_state_shows_degraded(self):
        self.seed_state(
            lastActivationAt="2026-08-19T07:05:00+08:00",
            nextDueAt="2026-08-19T12:06:15+08:00",
            intervalDisabledAt="2026-08-19T08:00:00+08:00",
        )
        conn = account_connection(mode="interval", fixed_at=())
        write_config(conn)
        result = invoke(["status"])
        self.assertIn("(claude-code-main) — degraded", result.output)
        self.assertIn("probing after failures", result.output)


class CatchupFlagTests(IsolatedTestCase):
    def test_flags_persist_and_show(self):
        write_config(account_connection(mode="fixed"))
        result = invoke([
            "config", "set", "claude-code-main",
            "--catchup-attempts", "3", "--catchup-minutes", "60", "--degrade-after-nodes", "5",
        ])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Catch-up for claude-code-main: 3 attempts within 60 minutes", result.output)
        self.assertIn("Degrade after 5 consecutive lost nodes", result.output)
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["catchup"], {"attempts": 3, "withinMinutes": 60})
        self.assertEqual(conn["degradeAfterNodes"], 5)
        shown = invoke(["config", "set", "claude-code-main"])
        self.assertIn("catch-up: 3 attempts within 60 minutes", shown.output)
        self.assertIn("degrade after nodes: 5", shown.output)

    def test_global_settings_command_sets_defaults(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "settings", "--catchup-minutes", "60", "--catchup-attempts", "2"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("2 attempts within 60 minutes", result.output)
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["catchup"], {"attempts": 2, "withinMinutes": 60})
        self.assertEqual(conn["degradeAfterNodes"], 3)
        shown = invoke(["config", "settings"])
        self.assertIn("catch-up: 2 attempts within 60 minutes", shown.output)
        self.assertIn("degrade after nodes: 3", shown.output)

    def test_connection_override_wins_over_global(self):
        write_config(account_connection(mode="fixed"))
        invoke(["config", "settings", "--catchup-minutes", "60"])
        invoke(["config", "set", "claude-code-main", "--catchup-minutes", "15"])
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["catchup"]["withinMinutes"], 15)
        self.assertEqual(conn["catchup"]["attempts"], 5)
        file = json.loads(Path(cfg.config_path()).read_text())
        self.assertEqual(file["settings"]["catchupMinutes"], 60)
        flat = file["connections"]["local"]["claude-code-main"]
        self.assertEqual({k: v for k, v in flat.items() if k not in ("label", "cli", "model")},
                         {"catchupMinutes": 15, "schedule": {"mode": "fixed"}})

    def test_out_of_range_rejected(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--catchup-minutes", "3"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("between 5 and 240", output_of(result))
        result = invoke(["config", "set", "claude-code-main", "--catchup-attempts", "0"])
        self.assertNotEqual(result.exit_code, 0)
        result = invoke(["config", "set", "claude-code-main", "--degrade-after-nodes", "11"])
        self.assertNotEqual(result.exit_code, 0)
        result = invoke(["config", "settings", "--catchup-minutes", "3"])
        self.assertNotEqual(result.exit_code, 0)


class TickLadderTests(IsolatedTestCase):
    def tick_at(self, moment, send_result):
        with mock.patch("awewarm.cli._now") as now, mock.patch("awewarm.transport.send_activation") as send:
            now.return_value = moment
            send.return_value = send_result
            result = invoke(["tick"])
        return result, send

    def test_tick_failure_opens_node_and_throttles(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        write_config(account_connection(mode="fixed", fixed_at=("06:35",), days="every-day"))
        tz = ZoneInfo("Asia/Shanghai")
        _, first = self.tick_at(datetime(2026, 8, 19, 6, 36, tzinfo=tz), {"ok": False, "detail": "HTTP 500"})
        self.assertEqual(first.call_count, 1)
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(cs["nodeAttempts"], 1)
        self.assertEqual(cs["failedNodes"], 0)
        self.assertIsNotNone(cs["nodeKey"])
        # inside the 5-minute throttle nothing refires
        _, throttled = self.tick_at(datetime(2026, 8, 19, 6, 38, tzinfo=tz), {"ok": False, "detail": "HTTP 500"})
        self.assertEqual(throttled.call_count, 0)
        # past the throttle the catch-up retry fires
        _, retried = self.tick_at(datetime(2026, 8, 19, 6, 42, tzinfo=tz), {"ok": False, "detail": "HTTP 500"})
        self.assertEqual(retried.call_count, 1)
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(cs["nodeAttempts"], 2)

    def test_tick_degraded_fixed_slot_single_shot(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        write_config(account_connection(mode="fixed", fixed_at=("06:35",), days="every-day"))
        state = cfg.empty_state()
        cs = cfg.conn_state(state, "claude-code-main")
        cs["degradedAt"] = "2026-08-18T21:50:00+08:00"
        cfg.save_state(state)
        tz = ZoneInfo("Asia/Shanghai")
        _, first = self.tick_at(datetime(2026, 8, 19, 6, 36, tzinfo=tz), {"ok": False, "detail": "HTTP 429"})
        self.assertEqual(first.call_count, 1)
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(cs["degradedFailedNodes"], 1)
        self.assertIn("06:35", cs["skippedSlots"]["2026-08-19"])
        # single shot: the retry throttle window passing must NOT refire
        _, later = self.tick_at(datetime(2026, 8, 19, 6, 50, tzinfo=tz), {"ok": False, "detail": "HTTP 429"})
        self.assertEqual(later.call_count, 0)
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(cs["degradedFailedNodes"], 1)

    def test_tick_auto_disabled_is_silent(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        write_config(account_connection(mode="fixed", fixed_at=("06:35",), days="every-day"))
        state = cfg.empty_state()
        cs = cfg.conn_state(state, "claude-code-main")
        cs["autoDisabledAt"] = "2026-08-18T22:00:00+08:00"
        cfg.save_state(state)
        tz = ZoneInfo("Asia/Shanghai")
        result, send = self.tick_at(datetime(2026, 8, 19, 6, 36, tzinfo=tz), {"ok": True, "detail": "ok"})
        self.assertIn("nothing due", result.output)
        self.assertEqual(send.call_count, 0)


class RunConnectionTests(IsolatedTestCase):
    @mock.patch("awewarm.transport.send_activation")
    def test_run_id_fires_with_force(self, send):
        send.return_value = {"ok": True, "detail": "ok"}
        write_config(account_connection(mode="fixed"))
        result = invoke(["run", "claude-code-main", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        state = cfg.load_state()
        self.assertEqual(state["connections"]["claude-code-main"]["history"][-1]["kind"], "manual")
        send.assert_called_once()

    def test_run_id_requires_force_without_tty(self):
        # The old --dry-run flag is gone; the non-tty guard is what now keeps
        # a scripted `run <id>` from firing anything.
        write_config(account_connection(mode="fixed"))
        with mock.patch("awewarm.transport.send_activation") as send:
            result = invoke(["run", "claude-code-main"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("requires --force", output_of(result))
        send.assert_not_called()
        self.assertFalse(cfg.state_path().exists())

    def test_run_unknown_connection(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["run", "nope", "--force"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unknown connection", output_of(result))

    def anchored_interval_conn(self):
        write_config(account_connection(mode="interval", fixed_at=()))
        state = cfg.empty_state()
        cs = cfg.conn_state(state, "claude-code-main")
        cs["lastActivationAt"] = "2026-08-19T10:00:00+08:00"
        cs["nextDueAt"] = "2026-08-19T15:01:15+08:00"
        cfg.save_state(state)

    def test_manual_run_keeps_next_due_by_default(self):
        self.anchored_interval_conn()
        with mock.patch("awewarm.cli._now") as now, mock.patch("awewarm.transport.send_activation") as send:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            send.return_value = {"ok": True, "detail": "ok"}
            result = invoke(["run", "claude-code-main", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(cs["nextDueAt"], "2026-08-19T15:01:15+08:00")
        self.assertIn("15:01", result.output)

    def test_manual_run_reset_due_recomputes(self):
        self.anchored_interval_conn()
        with mock.patch("awewarm.cli._now") as now, mock.patch("awewarm.transport.send_activation") as send:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            send.return_value = {"ok": True, "detail": "ok"}
            result = invoke(["run", "claude-code-main", "--reset-due", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        cs = cfg.load_state()["connections"]["claude-code-main"]
        due = schedule.parse_ts(cs["nextDueAt"])
        # 12:00 + 300 min + 75 s grace + up to 30 s jitter
        self.assertEqual(due.strftime("%H:%M"), "17:01")
        self.assertGreaterEqual(due.second, 15)


class AccountAddTestTests(IsolatedTestCase):
    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_account_test_failure_decline_aborts(self, send, discover_accounts):
        send.return_value = {"ok": False, "detail": "claude exited 1"}
        discover_accounts.return_value = [claude_finding()]
        # menu 1 / mode / times / reset / grid (accept) / days / wake / save anyway? no
        result = invoke(["config", "add"], input="1\n\n\n\n\n\n\nn\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Activation test failed", result.output)
        self.assertIn("aborted", output_of(result))
        self.assertEqual(cfg.load_config()["connections"], {})
        send.assert_called_once()

    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_account_test_failure_accept_saves(self, send, discover_accounts):
        send.return_value = {"ok": False, "detail": "claude exited 1"}
        discover_accounts.return_value = [claude_finding()]
        # menu 1 / mode / times / reset / grid (accept) / days / wake / save anyway? yes / window open? no
        result = invoke(["config", "add"], input="1\n\n\n\n\n\n\ny\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Activation test failed", result.output)
        conn = cfg.load_config()["connections"]["claude-code"]
        self.assertEqual(conn["kind"], "account")


class WindowSetTests(IsolatedTestCase):
    def test_set_without_flags_shows_settings(self):
        write_config(account_connection(mode="fixed"))
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


class HideTests(IsolatedTestCase):
    def _write_two(self):
        data = cfg.empty_config()
        conn = account_connection(mode="fixed")
        conn["hide"] = True
        data["connections"]["claude-code-main"] = conn
        data["connections"]["glm-coding-plan"] = plan_connection(mode="fixed")
        cfg.save_config(data)

    def test_status_listing_omits_hidden(self):
        self._write_two()
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("claude-code-main", result.output)
        self.assertIn("glm-coding-plan", result.output)

    def test_status_single_ask_still_shows_hidden(self):
        self._write_two()
        result = invoke(["status", "claude-code-main"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("claude-code-main", result.output)

    def test_status_json_listing_omits_hidden(self):
        self._write_two()
        result = invoke(["status", "--json"])
        self.assertEqual(result.exit_code, 0)
        view = json.loads(result.output)
        self.assertNotIn("claude-code-main", view["config"]["connections"])
        self.assertIn("glm-coding-plan", view["config"]["connections"])

    def test_hide_and_show_flags_round_trip(self):
        write_config(account_connection(mode="fixed"))
        hidden = invoke(["config", "set", "claude-code-main", "--hide"])
        self.assertEqual(hidden.exit_code, 0, output_of(hidden))
        self.assertIn("hidden from status", hidden.output)
        self.assertTrue(cfg.load_config()["connections"]["claude-code-main"]["hide"])
        self.assertIn('"hide": true', cfg.config_path().read_text())
        shown = invoke(["config", "set", "claude-code-main", "--show"])
        self.assertEqual(shown.exit_code, 0, output_of(shown))
        self.assertFalse(cfg.load_config()["connections"]["claude-code-main"]["hide"])

    def test_settings_show_hidden_state(self):
        write_config(account_connection(mode="fixed"))
        invoke(["config", "set", "claude-code-main", "--hide"])
        result = invoke(["config", "set", "claude-code-main"])
        self.assertIn("hidden from status: true", result.output)

    def test_all_hidden_listing_says_so(self):
        data = cfg.empty_config()
        conn = account_connection(mode="fixed")
        conn["hide"] = True
        data["connections"]["claude-code-main"] = conn
        cfg.save_config(data)
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--show", result.output)


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

    def test_api_key_env_option_removed(self):
        result = invoke(["config", "set", "glm-coding-plan", "--api-key-env", "GLM_API_KEY"])
        self.assertNotEqual(result.exit_code, 0)

    def test_config_add_stores_pasted_key_in_secrets(self):
        send = mock.MagicMock(return_value={"ok": True, "detail": "ok"})
        with mock.patch("awewarm.transport.send_activation", send), \
                mock.patch("awewarm.discover.discover_accounts", return_value=[]):
            result = invoke(["config", "add"], input=(
                "GLM Plan\n1\nhttps://open.bigmodel.cn/api/coding/paas/v4\n"
                "sk-pasted-key\nglm-4.7\n1\n\n06:00\n\nn\n1\n\n"
            ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm-plan"]
        self.assertEqual(conn["auth"]["apiKeyRef"], "file:glm-plan")
        self.assertEqual(keystore.load_api_key("file:glm-plan"), "sk-pasted-key")



class SetTimesTests(IsolatedTestCase):
    def test_set_multiple_times_sorted_and_deduped(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--times", "16:45,06:35,16:45,11:40"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35", "11:40", "16:45"])

    def test_times_accepts_space_separated_values(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--times", "16:45 06:35"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:35", "16:45"])

    def test_invalid_time_dies_without_saving(self):
        write_config(account_connection(mode="fixed"))
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
        write_config(account_connection(mode="fixed"))
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Claude Code (claude-code-main) — connected", result.output)
        self.assertIn("Mode: fixed", result.output)
        self.assertIn("Times: 06:35 (weekday)", result.output)
        self.assertNotIn("Window:", result.output)
        self.assertIn("Scheduler: not installed", result.output)
        self.assertNotIn("Last result:", result.output)

    def test_status_shows_last_failure_detail(self):
        write_config(account_connection(mode="fixed"))
        state = cfg.empty_state()
        cs = cfg.conn_state(state, "claude-code-main")
        cs["lastResult"] = "failure"
        cs["lastError"] = "HTTP 401 unauthorized"
        cs["lastAttemptAt"] = "2026-08-19T06:40:00+08:00"
        cfg.save_state(state)
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Last result: failure (2026-08-19 06:40)", result.output)
        self.assertIn("— HTTP 401 unauthorized", result.output)

    def test_status_fixed_detail_keeps_window(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["status", "claude-code-main"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Times: 06:35 (weekday)", result.output)
        self.assertIn("Window: 300 minutes, verified (evidence: builtin-provider)", result.output)

    def test_status_interval_shows_window_not_times(self):
        write_config(account_connection(mode="interval"))
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Mode: interval", result.output)
        self.assertIn("Window: 300 minutes, verified", result.output)
        self.assertNotIn("Times:", result.output)

    def test_status_interval_detail_keeps_fixed_times(self):
        write_config(account_connection(mode="interval"))
        result = invoke(["status", "claude-code-main"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Window: 300 minutes, verified (evidence: builtin-provider)", result.output)
        self.assertIn("Fixed times: 06:35 (weekday)", result.output)

    def test_status_single_connection_shows_detail(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["status", "glm-coding-plan"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Transport: anthropic-messages", result.output)
        self.assertIn("evidence:", result.output)
        self.assertIn("Times: 06:35", result.output)
        self.assertIn("Next due:", result.output)

    def test_status_disabled_connection_shows_no_due_moment(self):
        conn = account_connection(mode="fixed")
        conn["enabled"] = False
        write_config(conn)
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("(claude-code-main) — disabled", result.output)
        self.assertIn("Next due: none (disabled)", result.output)
        self.assertNotIn("(fixed)", result.output)  # no moment that will never fire

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

    def test_config_template_prints_valid_json(self):
        result = invoke(["config", "template"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        parsed = json.loads(result.output)
        self.assertEqual(parsed["version"], 3)


class SchedulerCommandTests(IsolatedTestCase):
    @mock.patch("awewarm.install.install_scheduler")
    def test_scheduler_install(self, install_scheduler):
        install_scheduler.return_value = "/Library/LaunchAgents/com.awewarm.scheduler.plist"
        result = invoke(["scheduler", "install"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Scheduler installed", result.output)

    @mock.patch("awewarm.install.install_scheduler")
    def test_scheduler_install_skips_the_gate_with_a_local_connection(self, install_scheduler):
        install_scheduler.return_value = "/Library/LaunchAgents/com.awewarm.scheduler.plist"
        write_config(plan_connection())
        result = invoke(["scheduler", "install"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("no connections for a local scheduler", output_of(result))
        install_scheduler.assert_called_once()

    @mock.patch("awewarm.install.install_scheduler")
    def test_install_without_local_connections_prints_the_note_when_scripted(self, install_scheduler):
        # CliRunner's stdin is not a tty: a script gets the notice and the install
        install_scheduler.return_value = "/Library/LaunchAgents/com.awewarm.scheduler.plist"
        result = invoke(["scheduler", "install"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("note: no connections for a local scheduler", output_of(result))
        self.assertIn("Scheduler installed", result.output)
        install_scheduler.assert_called_once()

    @mock.patch("awewarm.install.install_scheduler")
    def test_install_asks_first_when_everything_is_delegated(self, install_scheduler):
        conn = plan_connection()
        conn["location"] = "remote"  # the server's serve ticks it, not this machine
        write_config(conn)
        with mock.patch("awewarm.cli._stdin_is_interactive", return_value=True):
            result = invoke(["scheduler", "install"], input="n\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Install the scheduler anyway?", result.output)
        self.assertIn("aborted — nothing installed", result.output)
        install_scheduler.assert_not_called()

    @mock.patch("awewarm.install.install_scheduler")
    def test_install_gate_confirm_proceeds_on_yes(self, install_scheduler):
        install_scheduler.return_value = "/Library/LaunchAgents/com.awewarm.scheduler.plist"
        with mock.patch("awewarm.cli._stdin_is_interactive", return_value=True):
            result = invoke(["scheduler", "install"], input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Scheduler installed", result.output)
        install_scheduler.assert_called_once()

    @mock.patch("awewarm.install.uninstall_scheduler", return_value=False)
    def test_scheduler_uninstall_when_absent(self, _uninstall):
        result = invoke(["scheduler", "uninstall"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("was not installed", result.output)


class UpdateTests(IsolatedTestCase):
    @mock.patch("awewarm.cli.get_pypi_latest", return_value="0.0.1")
    def test_check_when_up_to_date(self, _pypi):
        result = invoke(["self-update", "--check"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("up to date", result.output)

    @mock.patch("awewarm.cli.subprocess.run")
    @mock.patch("awewarm.cli.get_pypi_latest", return_value="9.9.9")
    def test_check_shows_latest_without_updating(self, _pypi, run):
        result = invoke(["self-update", "--check"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("9.9.9", result.output)
        run.assert_not_called()

    @mock.patch("awewarm.cli.subprocess.run")
    @mock.patch("awewarm.cli.running_from_checkout", return_value=False)
    @mock.patch("awewarm.cli.get_pypi_latest", return_value="9.9.9")
    def test_update_runs_pip(self, _pypi, _checkout, run):
        run.return_value = mock.Mock(returncode=0)
        result = invoke(["self-update"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        command = " ".join(run.call_args[0][0])
        self.assertIn("awewarm", command)
        self.assertNotIn("pipx", command)

    @mock.patch("awewarm.cli.subprocess.run")
    @mock.patch("awewarm.cli.running_from_checkout", return_value=True)
    @mock.patch("awewarm.cli.get_pypi_latest", return_value="9.9.9")
    def test_update_refuses_on_a_source_checkout(self, _pypi, _checkout, run):
        result = invoke(["self-update"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("source checkout", output_of(result))
        self.assertIn("pip install -e", output_of(result))
        run.assert_not_called()

    @mock.patch("awewarm.cli.get_pypi_latest", side_effect=OSError("offline"))
    def test_update_dies_on_network_failure(self, _pypi):
        result = invoke(["self-update", "--check"])
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
        write_config(account_connection(mode="fixed"))
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

    @mock.patch("awewarm.transport.send_activation")
    def test_legacy_activate_fires_directly(self, send):
        send.return_value = {"ok": True, "detail": "ok"}
        write_config(account_connection(mode="fixed"))
        result = invoke(["activate", "claude-code-main"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("moved to", output_of(result))
        self.assertIn("run claude-code-main", output_of(result))
        state = cfg.load_state()
        self.assertEqual(state["connections"]["claude-code-main"]["history"][-1]["kind"], "manual")

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

    @mock.patch("awewarm.cli.check_async", return_value=lambda: None)
    def test_tick_exits_cleanly_when_another_process_holds_the_lock(self, _check):
        with process_lock(timeout_seconds=0):
            self.assertIsNone(main(["tick"]))

    @mock.patch("awewarm.cli.check_async", return_value=lambda: None)
    def test_interactive_command_reports_a_busy_lock(self, _check):
        immediate_lock = lambda **_kwargs: process_lock(timeout_seconds=0)
        with process_lock(timeout_seconds=0), \
             mock.patch("awewarm.cli.local_process_lock", side_effect=immediate_lock):
            with self.assertRaises(SystemExit) as ctx:
                main(["status"])
        self.assertIn("another awewarm command is still running", str(ctx.exception))

    @mock.patch("awewarm.cli.check_async", return_value=lambda: None)
    @mock.patch("awewarm.cli.local_process_lock", side_effect=AssertionError("help must not lock"))
    def test_subcommand_help_bypasses_the_local_lock(self, _lock, _check):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["status", "--help"])
        self.assertEqual(ctx.exception.code, 0)


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
        self.assertIn("--mode interval", result.output)

    def test_mode_and_anchor_combine_in_one_call(self):
        write_config(plan_connection(mode="fixed", window_status="user-confirmed", duration=300))
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "set", "claude-code-main", "--mode", "interval", "--anchor", "13:27"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["mode"], "interval")
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(schedule.parse_ts(cs["nextDueAt"]).strftime("%H:%M"), "13:28")


class StartTests(IsolatedTestCase):
    def interval_conn(self):
        return write_config(
            plan_connection(mode="interval", fixed_at=(), window_status="user-confirmed", duration=300)
        )

    def test_start_defers_until_later_today(self):
        self.interval_conn()
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "set", "claude-code-main", "--start", "13:27"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("deferred until", result.output)
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(schedule.parse_ts(cs["deferUntil"]).strftime("%H:%M"), "13:27")

    def test_start_rolls_to_tomorrow_when_passed(self):
        self.interval_conn()
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "set", "claude-code-main", "--start", "06:00"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        cs = cfg.load_state()["connections"]["claude-code-main"]
        defer = schedule.parse_ts(cs["deferUntil"])
        self.assertEqual(defer.strftime("%m-%d %H:%M"), "08-20 06:00")

    def test_start_rejects_bad_format(self):
        self.interval_conn()
        result = invoke(["config", "set", "claude-code-main", "--start", "25:00"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("HH:MM", result.output)

    def test_start_defers_fixed_slots(self):
        write_config(plan_connection(mode="fixed", fixed_at=("16:00",)))
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "set", "claude-code-main", "--start", "16:05"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("deferred until today 16:05", result.output)
        self.assertIn("fixed slots", result.output)
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(schedule.parse_ts(cs["deferUntil"]).strftime("%H:%M"), "16:05")

    def test_start_keeps_fixed_mode(self):
        write_config(plan_connection(mode="fixed", fixed_at=("23:00",), window_status="user-confirmed", duration=300))
        result = invoke(["config", "set", "claude-code-main", "--start", "23:30"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("fixed slots", result.output)
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["mode"], "fixed")
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["23:00"])

    def test_mode_and_start_combine_in_one_call(self):
        write_config(plan_connection(mode="fixed", window_status="user-confirmed", duration=300))
        with mock.patch("awewarm.cli._now") as now:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now.return_value = datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            result = invoke(["config", "set", "claude-code-main", "--mode", "interval", "--start", "13:27"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(conn["schedule"]["mode"], "interval")
        cs = cfg.load_state()["connections"]["claude-code-main"]
        self.assertEqual(schedule.parse_ts(cs["deferUntil"]).strftime("%H:%M"), "13:27")


class ConfigSetWakeRefreshTests(IsolatedTestCase):
    @mock.patch("awewarm.cli.sys.platform", "darwin")
    @mock.patch("awewarm.cli.install.refresh_wake", return_value=False)
    @mock.patch("awewarm.cli.install.scheduler_installed", return_value=True)
    def test_schedule_edit_refreshes_installed_wake(self, _installed, refresh):
        write_config(account_connection(mode="fixed", fixed_at=("06:35",)))
        result = invoke(["config", "set", "claude-code-main", "--times", "07:00"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        refresh.assert_called_once()

    @mock.patch("awewarm.cli.sys.platform", "darwin")
    @mock.patch("awewarm.cli.install.refresh_wake")
    def test_no_refresh_without_installed_scheduler(self, refresh):
        # scheduler_installed() reads the isolated temp env → not installed
        write_config(account_connection(mode="fixed", fixed_at=("06:35",)))
        result = invoke(["config", "set", "claude-code-main", "--times", "07:00"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        refresh.assert_not_called()

    @mock.patch("awewarm.cli.sys.platform", "linux")
    @mock.patch("awewarm.cli.install.refresh_wake")
    def test_no_refresh_off_darwin(self, refresh):
        write_config(account_connection(mode="fixed", fixed_at=("06:35",)))
        result = invoke(["config", "set", "claude-code-main", "--times", "07:00"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        refresh.assert_not_called()


class RunStaleAgentHealTests(IsolatedTestCase):
    @mock.patch("awewarm.transport.send_activation")
    @mock.patch("awewarm.cli.install._maybe_self_heal_job")
    def test_non_tty_fire_all_heals_stale_agent(self, heal, send):
        # pre-`tick` scheduler agents invoke `run --force`; the heal replaces
        # their job definition so they stop fire-all-ing every minute
        send.return_value = {"ok": True, "detail": "ok"}
        write_config(account_connection(mode="fixed"))
        result = invoke(["run", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        heal.assert_called_once()
        # the fire itself still happens — run --force means fire all
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class DayGridTests(IsolatedTestCase):
    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_reset_time_anchors_full_day_grid(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
        discover_accounts.return_value = [claude_finding()]
        # menu 1 / mode fixed / times / reset 01:14 / grid accept / days
        result = invoke(["config", "add"], input="1\n\n06:00\n01:14\n\n\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code"]
        self.assertEqual(
            conn["schedule"]["fixed"]["at"],
            ["01:14", "06:19", "11:24", "16:29", "21:34"],
        )
        self.assertEqual(conn["schedule"]["fixed"]["days"], "every-day")

    @mock.patch("awewarm.discover.discover_accounts")
    @mock.patch("awewarm.transport.send_activation")
    def test_declined_grid_keeps_entered_time(self, send, discover_accounts):
        send.return_value = {"ok": True, "detail": "ok"}
        discover_accounts.return_value = [claude_finding()]
        # menu 1 / mode fixed / times / reset skipped / grid declined / days
        result = invoke(["config", "add"], input="1\n\n06:00\n\nn\n\n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["claude-code"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:00"])
        self.assertEqual(conn["schedule"]["fixed"]["days"], "weekday")

    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_plan_fixed_asks_window_and_grids(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        # name/protocol/url/key/model / mode 1 / window 300 / times 01:14 /
        # reset skipped (entered time anchors) / grid accept / days default
        result = invoke(["config", "add"], input=(
            "GLM\n1\nhttp://x/v4\nk\nglm-4.7\n1\n300\n01:14\n\n\n\n\n"
        ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm"]
        self.assertEqual(conn["schedule"]["mode"], "fixed")
        self.assertEqual(conn["window"]["status"], "user-confirmed")
        self.assertEqual(conn["window"]["durationMinutes"], 300)
        self.assertEqual(
            conn["schedule"]["fixed"]["at"], ["01:14", "06:19", "11:24", "16:29", "21:34"]
        )
        self.assertEqual(conn["schedule"]["fixed"]["days"], "every-day")

    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_plan_fixed_window_defaults_to_300(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        # window left empty -> default 300; grid declined keeps the entered time
        result = invoke(["config", "add"], input=(
            "GLM\n1\nhttp://x/v4\nk\nglm-4.7\n1\n\n06:00\n\nn\n\n\n"
        ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm"]
        self.assertEqual(conn["window"]["status"], "user-confirmed")
        self.assertEqual(conn["window"]["durationMinutes"], 300)
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:00"])
        self.assertEqual(conn["schedule"]["fixed"]["days"], "weekday")

    @mock.patch("awewarm.discover.discover_accounts", return_value=[])
    @mock.patch("awewarm.transport.send_activation")
    def test_plan_mode3_is_interval_without_fixed_prompts(self, send, _discover):
        send.return_value = {"ok": True, "detail": "ok"}
        # name/protocol/url/key/model / mode 3 / duration 300 / window open? no
        result = invoke(["config", "add"], input=(
            "GLM\n1\nhttp://x/v4\nk\nglm-4.7\n3\n300\nn\n"
        ))
        self.assertEqual(result.exit_code, 0, output_of(result))
        conn = cfg.load_config()["connections"]["glm"]
        self.assertEqual(conn["schedule"]["mode"], "interval")
        self.assertEqual(conn["window"]["durationMinutes"], 300)


class ConfigDuplicateTests(IsolatedTestCase):
    def plan_with_key(self, conn_id="glm"):
        conn = plan_connection()
        conn["auth"]["apiKeyRef"] = keystore.store_api_key(conn_id, "sk-test")
        write_config(conn, conn_id=conn_id)
        return conn

    def test_duplicate_copies_config_and_key_under_a_fresh_id(self):
        self.plan_with_key()
        result = invoke(["config", "set", "glm", "--duplicate"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("duplicated as glm-copy", result.output)
        data = cfg.load_config()
        original, clone = data["connections"]["glm"], data["connections"]["glm-copy"]
        self.assertEqual(clone["kind"], "subscription")
        self.assertEqual(clone["schedule"]["fixed"]["at"], original["schedule"]["fixed"]["at"])
        self.assertEqual(clone["auth"]["apiKeyRef"], "file:glm-copy")  # own ref, not shared
        self.assertEqual(keystore.load_api_key("file:glm-copy"), "sk-test")
        self.assertEqual(keystore.load_api_key("file:glm"), "sk-test")  # original keeps its own
        self.assertNotEqual(clone, original)  # a change to one never leaks into the other

    def test_duplicate_takes_the_next_free_suffix(self):
        self.plan_with_key()
        invoke(["config", "set", "glm", "--duplicate"])
        result = invoke(["config", "set", "glm", "--duplicate"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("glm-copy2", result.output)

    def test_duplicate_rejects_other_flags(self):
        self.plan_with_key()
        result = invoke(["config", "set", "glm", "--duplicate", "--times", "07:00"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("only with --remote/--local", output_of(result))
        self.assertNotIn("glm-copy", cfg.load_config()["connections"])  # rejected upfront, nothing created

    def test_duplicate_of_an_account_without_a_key_works(self):
        write_config(account_connection())
        result = invoke(["config", "set", "claude-code-main", "--duplicate"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        clone = cfg.load_config()["connections"]["claude-code-main-copy"]
        self.assertIsNone(clone["auth"]["apiKeyRef"])


class RemoteDelegationTests(IsolatedTestCase):
    """Delegation end to end against a real in-process awewarm serve."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server_dir = Path(tmp.name) / "server"
        self.start_server()

    def start_server(self):
        self.warm, self.httpd = server.make_server(self.server_dir, "127.0.0.1", 0)
        self.server_thread = start_http_server(self.httpd)
        self.addCleanup(stop_http_server, self.httpd, self.server_thread)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.token = remote.generate_token()
        remote.claim(self.url, self.token)
        remote.store_token(self.token)

    def paired_config(self, conn, conn_id="glm"):
        data = cfg.empty_config()
        data["remote"] = {"url": self.url, "tokenRef": "file:remote-token"}
        conn = json.loads(json.dumps(conn))
        conn["auth"]["apiKeyRef"] = keystore.store_api_key(conn_id, "sk-test")
        data["connections"][conn_id] = conn
        cfg.save_config(data)
        return data

    def delegate(self, conn=None, conn_id="glm"):
        self.paired_config(conn or plan_connection(fixed_at=("23:58",)), conn_id=conn_id)
        result = invoke(["config", "set", conn_id, "--remote"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        return result

    def server_view(self):
        return remote.fetch_state(self.url, self.token)

    def test_config_set_remote_delegates(self):
        result = self.delegate()
        self.assertIn("delegated", output_of(result))
        entry = self.server_view()["connections"]["glm"]
        self.assertTrue(entry["config"]["timezone"])  # IANA name traveled with the push
        self.assertFalse(entry["keyMissing"])
        on_disk = json.loads(Path(os.environ["AWEWARM_CONFIG"]).read_text())
        self.assertNotIn("location", on_disk["connections"]["remote"]["glm"])  # the group alone carries it

    def test_duplicate_remote_delegates_the_copy_and_disables_the_original(self):
        self.paired_config(plan_connection(fixed_at=("23:58",)))
        result = invoke(["config", "set", "glm", "--duplicate", "--remote"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("duplicated as glm-copy", output_of(result))
        self.assertIn("disabled", output_of(result))
        view = self.server_view()
        self.assertIn("glm-copy", view["connections"])  # the copy ticks server-side
        self.assertNotIn("glm", view["connections"])    # the original never was
        data = cfg.load_config()
        self.assertEqual(data["connections"]["glm-copy"]["location"], "remote")
        self.assertFalse(data["connections"]["glm"]["enabled"])  # one subscription, one ticker
        self.assertEqual(keystore.load_api_key("file:glm-copy"), "sk-test")  # key traveled

    def test_delegation_freezes_schedule_against_global_edits(self):
        # the global schedule exists and the connection follows it while local;
        # handover pins the effective times, and later global edits never move
        # the delegated connection
        data = cfg.empty_config()
        data["settings"]["schedule"] = {"times": ["09:00"]}
        data["remote"] = {"url": self.url, "tokenRef": "file:remote-token"}
        conn = plan_connection(fixed_at=("09:00",))
        conn["settings"] = {"schedule": {}}  # follows the global 09:00
        conn["auth"]["apiKeyRef"] = keystore.store_api_key("glm", "sk-test")
        data["connections"]["glm"] = conn
        cfg.save_config(data)
        result = invoke(["config", "set", "glm", "--remote"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        entry = self.server_view()["connections"]["glm"]
        self.assertEqual(entry["config"]["schedule"]["fixed"]["at"], ["09:00"])
        on_disk = json.loads(Path(os.environ["AWEWARM_CONFIG"]).read_text())
        self.assertEqual(on_disk["connections"]["remote"]["glm"]["schedule"]["times"], ["09:00"])  # pinned
        invoke(["config", "settings", "--times", "10:10"])
        entry = self.server_view()["connections"]["glm"]
        self.assertEqual(entry["config"]["schedule"]["fixed"]["at"], ["09:00"])  # server untouched
        loaded = cfg.load_config()["connections"]["glm"]
        self.assertEqual(loaded["schedule"]["fixed"]["at"], ["09:00"])  # pinned, not following global

    def test_config_set_remote_rejects_cli_accounts(self):
        self.paired_config(account_connection(), conn_id="claude")
        result = invoke(["config", "set", "claude", "--remote"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot", output_of(result))
        self.assertEqual(self.server_view()["connections"], {})

    def test_config_set_remote_requires_a_paired_server(self):
        write_config(plan_connection())
        result = invoke(["config", "set", "claude-code-main", "--remote"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no remote server connected", output_of(result))

    def test_config_set_local_pulls_server_state(self):
        self.delegate()
        self.warm.state["connections"]["glm"]["lastActivationAt"] = "2026-08-20T10:00:00+08:00"
        self.warm._save(self.warm.state_path, self.warm.state)
        result = invoke(["config", "set", "glm", "--local"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("back on local scheduling", output_of(result))
        self.assertEqual(self.server_view()["connections"], {})
        local = cfg.load_state()["connections"]["glm"]
        self.assertEqual(local["lastActivationAt"], "2026-08-20T10:00:00+08:00")
        on_disk = json.loads(Path(os.environ["AWEWARM_CONFIG"]).read_text())
        self.assertNotIn("location", on_disk["connections"]["local"]["glm"])

    def add_local_account(self):
        data = cfg.load_config()
        data["connections"]["claude-code-main"] = account_connection(mode="fixed")
        cfg.save_config(data)

    def test_status_filters_split_local_and_delegated(self):
        self.delegate()
        self.add_local_account()
        remote_view = invoke(["status", "--remote"])
        self.assertEqual(remote_view.exit_code, 0, output_of(remote_view))
        self.assertIn("awewarm server", remote_view.output)  # the health line moved here
        self.assertIn(self.url, remote_view.output)
        self.assertIn("(glm)", remote_view.output)
        self.assertNotIn("claude-code-main", remote_view.output)
        local_view = invoke(["status", "--local"])
        self.assertEqual(local_view.exit_code, 0, output_of(local_view))
        self.assertNotIn("awewarm server", local_view.output)
        self.assertIn("claude-code-main", local_view.output)
        self.assertNotIn("(glm)", local_view.output)

    def test_status_filter_mismatch_points_at_the_fix(self):
        self.delegate()
        self.add_local_account()
        result = invoke(["status", "glm", "--local"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("glm is not local", output_of(result))
        self.assertIn("awewarm config set glm --local", output_of(result))
        result = invoke(["status", "claude-code-main", "--remote"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("claude-code-main is not remote", output_of(result))

    def test_status_remote_without_pairing_is_friendly(self):
        write_config(plan_connection())
        result = invoke(["status", "--remote"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("No remote server paired", result.output)
        self.assertIn("awewarm remote connect", result.output)

    def test_status_local_with_everything_delegated(self):
        self.delegate()
        result = invoke(["status", "--local"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("every connection is delegated", result.output)
        self.assertIn("awewarm status --remote", result.output)

    def test_remote_status_alias_points_at_status_remote(self):
        self.delegate()
        result = invoke(["remote", "status"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("moved to", output_of(result))
        self.assertIn("status --remote", output_of(result))
        self.assertIn("awewarm server", result.output)  # and the new view renders

    def test_status_health_line_renders_server_moments_in_the_viewers_timezone(self):
        self.delegate()
        # a UTC box serving a UTC+8 viewer: 04:00/04:05Z must show as 12:00/12:05
        self.warm.started_at = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
        self.warm.last_tick_at = schedule.iso(datetime(2026, 8, 20, 4, 5, tzinfo=timezone.utc))
        viewer_now = datetime(2026, 8, 20, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        with mock.patch("awewarm.cli._now", return_value=viewer_now):
            result = invoke(["status", "--remote"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("up since today 12:00", result.output)
        self.assertIn("last tick today 12:05", result.output)

    def test_status_health_line_labels_a_hub_with_both_versions(self):
        self.delegate()
        view = self.server_view()
        view["hubVersion"] = "9.9.9"  # only a hub serves this; solo never does
        with mock.patch("awewarm.remote.ensure_session", return_value=view):
            result = invoke(["status", "--remote"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn(f"awewarm-hub 9.9.9 (engine awewarm {view['version']})", result.output)
        self.assertNotIn("awewarm server", result.output)

    def test_tick_skips_remote_and_rekeys_after_server_restart(self):
        self.delegate(plan_connection(fixed_at=("03:00",), days="every-day"))
        self.warm.claimed_token = None  # a restart wiped the RAM keyring+token
        self.warm.keys.clear()
        moment = datetime(2026, 8, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with mock.patch("awewarm.cli._now") as now, mock.patch("awewarm.transport.send_activation") as send:
            now.return_value = moment
            result = invoke(["tick"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        send.assert_not_called()  # the server owns glm; the local tick never fires it
        self.assertEqual(self.warm.keys.get("glm"), "sk-test")  # re-keyed on contact
        self.assertIn("glm", self.warm.config["connections"])

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_run_forwards_to_the_server(self, send):
        self.delegate()
        result = invoke(["run", "glm", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("on the remote server", output_of(result))
        send.assert_called_once()
        self.assertEqual(self.warm.state["connections"]["glm"]["history"][-1]["kind"], "manual")

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_run_clears_auto_disabled_on_the_server(self, send):
        self.delegate()
        self.warm.state["connections"]["glm"]["autoDisabledAt"] = "2026-08-20T00:00:00+08:00"
        result = invoke(["run", "glm", "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("on the remote server", output_of(result))
        self.assertIsNone(self.warm.state["connections"]["glm"]["autoDisabledAt"])

    def test_status_merges_server_truth(self):
        self.delegate()
        self.warm.state["connections"]["glm"]["lastActivationAt"] = "2026-08-20T10:00:00+08:00"
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn(f"· {self.url}", output_of(result))
        self.assertIn(f"Remote: {self.url} (1 delegated", output_of(result))

    def test_status_warns_when_the_server_lost_a_delegated_connection(self):
        self.delegate()
        remote.delete_connection(self.url, self.token, "glm")
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("missing on the server", output_of(result))

    def test_status_offline_falls_back_to_last_sync(self):
        self.delegate()
        self.assertEqual(invoke(["status"]).exit_code, 0)
        stop_http_server(self.httpd, self.server_thread)
        result = invoke(["status"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("server unreachable, showing the last sync", output_of(result))

    def test_remote_disconnect_refuses_while_delegated(self):
        self.delegate()
        result = invoke(["remote", "disconnect"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("still delegated", output_of(result))

    def test_disconnect_then_reconnect_pairs_again(self):
        self.assertEqual(invoke(["remote", "connect", self.url]).exit_code, 0)
        result = invoke(["remote", "disconnect"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertFalse(remote.healthz(self.url)["claimed"])  # the claim was released
        result = invoke(["remote", "connect", self.url])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Connected to awewarm", output_of(result))

    def test_edits_to_delegated_connection_push_through(self):
        self.delegate()
        result = invoke(["config", "set", "glm", "--times", "07:00"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Pushed to the remote server", output_of(result))
        server_times = self.server_view()["connections"]["glm"]["config"]["schedule"]["fixed"]["at"]
        self.assertEqual(server_times, ["07:00"])

    def test_edits_while_offline_stay_local_and_pending(self):
        self.delegate()
        stop_http_server(self.httpd, self.server_thread)
        result = invoke(["config", "set", "glm", "--times", "07:07"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("unreachable", output_of(result))
        state = cfg.load_state()
        self.assertIn("glm", state.get("pendingPush") or {})
        on_disk = json.loads(Path(os.environ["AWEWARM_CONFIG"]).read_text())
        self.assertEqual(on_disk["connections"]["remote"]["glm"]["schedule"]["times"], ["07:07"])


class PlainHttpConnectTests(IsolatedTestCase):
    """remote connect must warn before pairing over plaintext HTTP to a public host."""

    @mock.patch("awewarm.remote.healthz")
    def test_public_http_declined_refuses_without_contacting_the_server(self, healthz):
        result = invoke(["remote", "connect", "http://warm.example.com"], input="n\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("refusing to pair over plain HTTP", output_of(result))
        healthz.assert_not_called()
        self.assertIsNone(remote.load_token())  # nothing was stored

    @mock.patch("awewarm.remote.healthz", side_effect=remote.RemoteError("cannot reach the awewarm server"))
    def test_public_http_confirmed_proceeds_to_healthz(self, healthz):
        result = invoke(["remote", "connect", "http://warm.example.com"], input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot reach the awewarm server", output_of(result))

    @mock.patch("awewarm.remote.healthz", side_effect=remote.RemoteError("cannot reach the awewarm server"))
    def test_https_never_prompts(self, healthz):
        result = invoke(["remote", "connect", "https://warm.example.com"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot reach the awewarm server", output_of(result))
        self.assertNotIn("plain HTTP", output_of(result))

    @mock.patch("awewarm.remote.healthz", side_effect=remote.RemoteError("cannot reach the awewarm server"))
    def test_loopback_http_never_prompts(self, healthz):
        result = invoke(["remote", "connect", "http://127.0.0.1:8790"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot reach the awewarm server", output_of(result))
        self.assertNotIn("plain HTTP", output_of(result))


class PushTimezoneTests(IsolatedTestCase):
    """Delegation pushes the machine's zone with every connection."""

    def test_configured_timezone_wins(self):
        self.assertEqual(
            awewarm.cli._push_timezone({"global": {"timezone": "Asia/Taipei"}}), "Asia/Taipei"
        )

    def test_windows_style_zone_pushes_a_fixed_offset(self):
        # datetime.timezone has no .key (a Windows-style local zone): the push
        # must carry the offset, never silently fall back to UTC.
        moment = datetime(2026, 8, 21, 12, 0, tzinfo=timezone(timedelta(hours=8), "CST"))
        with mock.patch("awewarm.cli.datetime") as dt_cls:
            dt_cls.now.return_value = moment
            self.assertEqual(awewarm.cli._push_timezone({"global": {}}), "UTC+08:00")

    def test_negative_half_hour_offset_formats_with_sign(self):
        moment = datetime(2026, 8, 21, 12, 0, tzinfo=timezone(-timedelta(hours=5, minutes=30)))
        with mock.patch("awewarm.cli.datetime") as dt_cls:
            dt_cls.now.return_value = moment
            self.assertEqual(awewarm.cli._push_timezone({"global": {}}), "UTC-05:30")

    def test_pushed_offset_is_a_timezone_the_server_accepts(self):
        pushed = "UTC-05:30"
        tz = cfg.timezone_for(pushed)
        self.assertEqual(datetime(2026, 8, 21, tzinfo=tz).utcoffset(), -timedelta(hours=5, minutes=30))


class SettingsScopeTests(IsolatedTestCase):
    """`config settings [global|local|remote]` — the three layers on disk."""

    def _write_two_conns(self):
        local = account_connection(mode="fixed")
        remote_conn = plan_connection(mode="fixed")
        remote_conn["location"] = "remote"
        data = cfg.empty_config()
        data["connections"]["claude-code-main"] = local
        data["connections"]["glm"] = remote_conn
        cfg.save_config(data)

    def test_global_schedule_reaches_local_not_remote(self):
        self._write_two_conns()
        result = invoke(["config", "settings", "--times", "07:07"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("reaches local connections only", output_of(result))
        loaded = cfg.load_config()
        self.assertEqual(loaded["connections"]["claude-code-main"]["schedule"]["fixed"]["at"], ["07:07"])
        self.assertEqual(loaded["connections"]["glm"]["schedule"]["fixed"]["at"], ["06:35"])  # untouched

    def test_remote_scope_sets_remote_layer_and_marks_delegated_for_push(self):
        self._write_two_conns()
        result = invoke(["config", "settings", "remote", "--times", "08:08", "--catchup-minutes", "45"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Marked glm for re-push", output_of(result))
        self.assertIn("glm", cfg.load_state().get("pendingPush") or {})
        loaded = cfg.load_config()
        self.assertEqual(loaded["connections"]["glm"]["schedule"]["fixed"]["at"], ["08:08"])
        self.assertEqual(loaded["connections"]["glm"]["catchup"]["withinMinutes"], 45)
        self.assertEqual(loaded["connections"]["claude-code-main"]["schedule"]["fixed"]["at"], ["06:35"])

    def test_local_scope_only_moves_local_connections(self):
        self._write_two_conns()
        result = invoke(["config", "settings", "local", "--times", "09:09"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        loaded = cfg.load_config()
        self.assertEqual(loaded["connections"]["claude-code-main"]["schedule"]["fixed"]["at"], ["09:09"])
        self.assertEqual(loaded["connections"]["glm"]["schedule"]["fixed"]["at"], ["06:35"])

    def test_show_lists_the_three_layers(self):
        self._write_two_conns()
        result = invoke(["config", "settings"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        for word in ("Global", "Local", "Remote", "never follow"):
            self.assertIn(word, output_of(result))

    def test_show_prints_the_local_layer(self):
        # regression: the show path must read the same runtime shape load builds
        self._write_two_conns()
        invoke(["config", "settings", "local", "--wake"])
        result = invoke(["config", "settings"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("wake when asleep: true", output_of(result))

    def test_config_set_edit_keeps_the_local_layer_on_disk(self):
        # regression: a load → edit → save must not drop connections.local.settings
        self._write_two_conns()
        invoke(["config", "settings", "local", "--wake"])
        result = invoke(["config", "set", "claude-code-main", "--times", "07:07"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        on_disk = json.loads(Path(cfg.config_path()).read_text())
        self.assertEqual(on_disk["connections"]["local"]["settings"], {"wakeWhenAsleep": True})
        self.assertEqual(
            on_disk["connections"]["local"]["claude-code-main"]["schedule"]["times"],
            ["07:07"])

    def test_reset_clears_a_scope(self):
        self._write_two_conns()
        invoke(["config", "settings", "local", "--times", "09:09"])
        result = invoke(["config", "settings", "local", "--reset"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        loaded = cfg.load_config()
        self.assertNotIn("local", loaded["connectionDefaults"])
        self.assertEqual(loaded["connections"]["claude-code-main"]["schedule"]["fixed"]["at"], ["06:35"])


class NewKnobLayerTests(IsolatedTestCase):
    """windowMinutes/prompt/maxTokens as settings-layer knobs."""

    def test_window_minutes_layer_unlocks_interval(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["config", "settings", "--window-minutes", "300", "--mode", "interval"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("vouches for a 300-minute window", output_of(result))
        loaded = cfg.load_config()["connections"]["glm-coding-plan"]
        # the layer's mode never re-modes an existing connection — its own
        # pinned mode stands; the layer's window unlocks an explicit switch
        self.assertEqual(loaded["schedule"]["mode"], "fixed")
        result = invoke(["config", "set", "glm-coding-plan", "--mode", "interval"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        loaded = cfg.load_config()["connections"]["glm-coding-plan"]
        self.assertEqual(loaded["schedule"]["mode"], "interval")
        self.assertEqual(loaded["window"]["durationMinutes"], 300)
        self.assertFalse(cfg.connection_errors(loaded, "glm-coding-plan"))
        on_disk = json.loads(Path(cfg.config_path()).read_text())
        self.assertEqual(
            on_disk["connections"]["local"]["glm-coding-plan"]["schedule"]["mode"],
            "interval")

    def test_window_flag_persists_as_own_override(self):
        write_config(plan_connection(mode="fixed"), conn_id="glm-coding-plan")
        result = invoke(["config", "set", "glm-coding-plan", "--window", "240"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("Window recorded as 240 minutes", output_of(result))
        on_disk = json.loads(Path(cfg.config_path()).read_text())
        flat = on_disk["connections"]["local"]["glm-coding-plan"]
        self.assertNotIn("windowMinutes", flat)  # old top-level spelling gone
        self.assertEqual(flat["schedule"]["windowMinutes"], 240)
        loaded = cfg.load_config()["connections"]["glm-coding-plan"]
        self.assertEqual(loaded["window"]["durationMinutes"], 240)

    def test_prompt_and_max_tokens_layer_flags(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "settings", "--max-tokens", "8", "--prompt", "say ok"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        loaded = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(loaded["activation"]["prompt"], "say ok")
        self.assertEqual(loaded["activation"]["maxTokens"], 8)
        self.assertNotEqual(invoke(["config", "settings", "--max-tokens", "0"]).exit_code, 0)
        self.assertNotEqual(invoke(["config", "settings", "--window-minutes", "0"]).exit_code, 0)


class ProfileScheduleTests(IsolatedTestCase):
    """`config set` writes the connection's own settings; --inherit-schedule drops them."""

    def test_times_persist_as_own_overrides(self):
        write_config(account_connection(mode="fixed"))
        result = invoke(["config", "set", "claude-code-main", "--times", "07:07"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        on_disk = json.loads(Path(cfg.config_path()).read_text())
        self.assertEqual(on_disk["connections"]["local"]["claude-code-main"]["schedule"]["times"], ["07:07"])
        loaded = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(loaded["schedule"]["fixed"]["at"], ["07:07"])

    def test_inherit_schedule_drops_own_overrides(self):
        write_config(account_connection(mode="fixed"))
        invoke(["config", "settings", "--times", "08:08"])
        invoke(["config", "set", "claude-code-main", "--times", "07:07"])
        result = invoke(["config", "set", "claude-code-main", "--inherit-schedule"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("dropped its own schedule overrides", output_of(result))
        loaded = cfg.load_config()["connections"]["claude-code-main"]
        self.assertEqual(loaded["schedule"]["fixed"]["at"], ["08:08"])  # follows global now

    def test_settings_show_names_the_schedule_source(self):
        write_config(account_connection(mode="fixed"))
        shown = invoke(["config", "set", "claude-code-main"])
        self.assertIn("schedule source:", output_of(shown))
        self.assertIn("local defaults → global", output_of(shown))
