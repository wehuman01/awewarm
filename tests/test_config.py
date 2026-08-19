import json
from pathlib import Path
import unittest

from helpers import IsolatedTestCase, account_connection, plan_connection

from awewarm import config


class PathTests(IsolatedTestCase):
    def test_env_overrides_apply(self):
        self.assertEqual(config.config_path(), self.tmp_path / "config.json")
        self.assertEqual(config.state_path(), self.tmp_path / "state.json")
        self.assertEqual(config.log_path(), self.tmp_path / "awewarm.log")

    def test_load_missing_returns_empty_skeleton(self):
        loaded = config.load_config()
        self.assertEqual(loaded["version"], config.CONFIG_VERSION)
        self.assertEqual(loaded["connections"], {})

    def test_load_malformed_json_dies(self):
        config.config_path().write_text("{not json")
        with self.assertRaises(SystemExit):
            config.load_config()

    def test_load_non_object_dies(self):
        config.config_path().write_text("[1, 2]")
        with self.assertRaises(SystemExit):
            config.load_config()


class RoundtripTests(IsolatedTestCase):
    def test_save_then_load_preserves_connections(self):
        data = config.empty_config()
        data["connections"]["claude-code-main"] = account_connection()
        data["connections"]["glm-plan"] = plan_connection()
        config.save_config(data)
        loaded = config.load_config()
        self.assertEqual(loaded["connections"].keys(), {"claude-code-main", "glm-plan"})

    def test_save_writes_indented_json_with_trailing_newline(self):
        data = config.empty_config()
        data["connections"]["claude-code-main"] = account_connection(mode="fixed")
        config.save_config(data)
        text = config.config_path().read_text()
        self.assertTrue(text.endswith("\n"))
        self.assertIn("\n  \"connections\"", text)

    def test_state_roundtrip_and_defaults(self):
        state = config.empty_state()
        cs = config.conn_state(state, "x")
        self.assertIsNone(cs["lastActivationAt"])
        cs["lastActivationAt"] = "2026-08-19T06:35:00+08:00"
        config.save_state(state)
        loaded = config.load_state()
        self.assertEqual(loaded["connections"]["x"]["lastActivationAt"], "2026-08-19T06:35:00+08:00")

    def test_save_refuses_invalid_connection(self):
        data = config.empty_config()
        bad = account_connection()
        bad["transport"]["kind"] = "carrier-pigeon"
        data["connections"]["bad"] = bad
        with self.assertRaises(SystemExit):
            config.save_config(data)


class ValidationTests(unittest.TestCase):
    def test_valid_account_hybrid_has_no_errors(self):
        self.assertEqual(config.connection_errors(account_connection(), "acct"), [])

    def test_valid_plan_fixed_with_unknown_window(self):
        self.assertEqual(config.connection_errors(plan_connection(), "plan"), [])

    def test_interval_locked_without_verified_window(self):
        errors = config.connection_errors(plan_connection(mode="interval"), "plan")
        self.assertTrue(any("--window" in e for e in errors))

    def test_hybrid_locked_without_duration(self):
        conn = account_connection(window_status="user-confirmed", mode="hybrid")
        conn["window"]["durationMinutes"] = None
        errors = config.connection_errors(conn, "acct")
        self.assertTrue(errors)

    def test_bad_slot_format_rejected(self):
        conn = account_connection(mode="fixed", fixed_at=("6:35",))
        errors = config.connection_errors(conn, "acct")
        self.assertTrue(any("at" in e for e in errors))

    def test_empty_fixed_at_rejected(self):
        conn = account_connection(mode="fixed", fixed_at=())
        self.assertTrue(config.connection_errors(conn, "acct"))

    def test_bad_days_rejected(self):
        conn = account_connection(mode="fixed")
        conn["schedule"]["fixed"]["days"] = "weekends"
        self.assertTrue(config.connection_errors(conn, "acct"))

    def test_subscription_requires_http_base_url(self):
        conn = plan_connection()
        conn["transport"]["baseUrl"] = "open.bigmodel.cn"
        self.assertTrue(config.connection_errors(conn, "plan"))

    def test_account_requires_cli_command(self):
        conn = account_connection()
        conn["transport"]["cliCommand"] = None
        self.assertTrue(config.connection_errors(conn, "acct"))

    def test_user_confirmed_duration_unlocks_interval(self):
        conn = plan_connection(mode="interval", window_status="user-confirmed", duration=300)
        self.assertEqual(config.connection_errors(conn, "plan"), [])


class NamingTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(config.slugify("GLM Coding Plan!"), "glm-coding-plan")
        self.assertEqual(config.slugify("  "), "plan")

    def test_unique_connection_id_appends_counter(self):
        data = config.empty_config()
        data["connections"]["glm-coding-plan"] = plan_connection()
        self.assertEqual(config.unique_connection_id(data, "GLM Coding Plan"), "glm-coding-plan-2")


class VersionTests(unittest.TestCase):
    def test_version_string_present(self):
        import awewarm

        self.assertRegex(awewarm.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()


class V2FormatTests(IsolatedTestCase):
    def _v1_conn(self):
        return {
            "label": "glm", "kind": "subscription", "enabled": True,
            "auth": {"type": "api-key", "status": "valid", "apiKeyRef": "file:glm"},
            "transport": {"kind": "openai-chat", "baseUrl": "https://x.example/v4", "cliCommand": None},
            "plan": {"url": "https://x.example/v4", "label": "glm"},
            "window": {"status": "user-confirmed", "startRule": "unknown", "durationMinutes": 300, "evidence": "user-confirmed"},
            "activation": {"model": "GLM-5-Turbo", "prompt": "Reply with exactly: ok", "maxTokens": 4},
            "schedule": {"mode": "hybrid",
                         "fixed": {"at": ["06:00"], "days": "every-day", "catchUpWindowMinutes": 45, "skipIfActivatedWithinMinutes": 30},
                         "interval": {"graceSeconds": 75, "jitterSeconds": 30}},
        }

    def test_save_compacts_to_flat_v2(self):
        conf = config.empty_config()
        conf["connections"]["glm"] = self._v1_conn()
        config.save_config(conf)
        on_disk = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(on_disk["version"], 2)
        self.assertEqual(on_disk["connections"]["glm"], {
            "label": "glm", "url": "https://x.example/v4", "protocol": "openai-chat",
            "apiKey": "file:glm", "model": "GLM-5-Turbo", "windowMinutes": 300,
            "mode": "hybrid", "times": ["06:00"], "days": "every-day",
            "schedule": {"wakeWhenAsleep": True},
        })

    def test_load_expands_flat_v2(self):
        Path(config.config_path()).write_text(json.dumps({
            "version": 2,
            "connections": {"glm": {
                "label": "glm", "url": "https://x.example/v4", "apiKey": "$GLM_KEY",
                "model": "GLM-5-Turbo", "windowMinutes": 300,
                "mode": "hybrid", "times": ["06:00"], "days": "every-day",
            }},
        }))
        conn = config.load_config()["connections"]["glm"]
        self.assertEqual(conn["kind"], "subscription")
        self.assertEqual(conn["transport"]["baseUrl"], "https://x.example/v4")
        self.assertEqual(conn["auth"]["apiKeyRef"], "$GLM_KEY")
        self.assertEqual(conn["window"]["durationMinutes"], 300)
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:00"])
        self.assertEqual(conn["schedule"]["fixed"]["catchUpWindowMinutes"], 45)
        self.assertEqual(config.connection_errors(conn, "glm"), [])

    def test_v1_file_upgraded_in_place_on_load(self):
        v1 = {"version": 1, "global": {}, "connections": {"glm": self._v1_conn()}}
        Path(config.config_path()).write_text(json.dumps(v1))
        conn = config.load_config()["connections"]["glm"]
        on_disk = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(on_disk["version"], 2)
        self.assertNotIn("transport", on_disk["connections"]["glm"])
        self.assertEqual(conn["auth"]["apiKeyRef"], "file:glm")

    def test_account_roundtrip_via_cli_flag(self):
        conn = account_connection()
        conn["transport"]["cliCommand"] = "/Users/x/.local/bin/codex"
        conn["transport"]["kind"] = "codex-cli"
        conf = config.empty_config()
        conf["connections"]["cx"] = conn
        config.save_config(conf)
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["cx"]
        self.assertEqual(on_disk["cli"], "/Users/x/.local/bin/codex")
        self.assertNotIn("url", on_disk)
        loaded = config.load_config()["connections"]["cx"]
        self.assertEqual(loaded["transport"]["kind"], "codex-cli")

    def test_custom_tuning_knobs_survive_roundtrip(self):
        conn = self._v1_conn()
        conn["schedule"]["fixed"]["catchUpWindowMinutes"] = 1441
        conf = config.empty_config()
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        loaded = config.load_config()["connections"]["glm"]
        self.assertEqual(loaded["schedule"]["fixed"]["catchUpWindowMinutes"], 1441)

    def test_disabled_flag_roundtrips(self):
        conn = self._v1_conn()
        conn["enabled"] = False
        conf = config.empty_config()
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["glm"]
        self.assertFalse(on_disk["enabled"])
