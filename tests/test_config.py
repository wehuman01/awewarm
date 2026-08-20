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

    def test_account_builtin_window_survives_roundtrip(self):
        # The flat format cannot carry evidence; the builtin verified window
        # must be re-derived on load instead of downgraded to user-confirmed.
        data = config.empty_config()
        data["connections"]["claude-code"] = account_connection()
        config.save_config(data)
        window = config.load_config()["connections"]["claude-code"]["window"]
        self.assertEqual(window["status"], "verified")
        self.assertEqual(window["evidence"], "builtin-provider")
        self.assertEqual(window["durationMinutes"], 300)
        self.assertEqual(window["startRule"], "first-successful-request")

    def test_account_custom_window_not_upgraded_to_builtin(self):
        data = config.empty_config()
        conn = account_connection(window_status="user-confirmed")
        conn["window"]["durationMinutes"] = 240
        data["connections"]["claude-code"] = conn
        config.save_config(data)
        window = config.load_config()["connections"]["claude-code"]["window"]
        self.assertEqual(window["status"], "user-confirmed")
        self.assertEqual(window["durationMinutes"], 240)

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
    def test_valid_account_has_no_errors(self):
        self.assertEqual(config.connection_errors(account_connection(), "acct"), [])

    def test_valid_plan_fixed_with_unknown_window(self):
        self.assertEqual(config.connection_errors(plan_connection(), "plan"), [])

    def test_interval_locked_without_verified_window(self):
        errors = config.connection_errors(plan_connection(mode="interval"), "plan")
        self.assertTrue(any("--window" in e for e in errors))

    def test_interval_account_locked_without_duration(self):
        conn = account_connection(window_status="user-confirmed", mode="interval")
        conn["window"]["durationMinutes"] = None
        self.assertTrue(config.connection_errors(conn, "acct"))

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
            "schedule": {"mode": "fixed",
                         "fixed": {"at": ["06:00"], "days": "every-day", "skipIfActivatedWithinMinutes": 30},
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
            "mode": "fixed", "times": ["06:00"], "days": "every-day",
            "schedule": {"wakeWhenAsleep": True},
        })

    def test_load_expands_flat_v2(self):
        Path(config.config_path()).write_text(json.dumps({
            "version": 2,
            "connections": {"glm": {
                "label": "glm", "url": "https://x.example/v4", "apiKey": "$GLM_KEY",
                "model": "GLM-5-Turbo", "windowMinutes": 300,
                "mode": "fixed", "times": ["06:00"], "days": "every-day",
            }},
        }))
        conn = config.load_config()["connections"]["glm"]
        self.assertEqual(conn["kind"], "subscription")
        self.assertEqual(conn["transport"]["baseUrl"], "https://x.example/v4")
        self.assertEqual(conn["auth"]["apiKeyRef"], "$GLM_KEY")
        self.assertEqual(conn["window"]["durationMinutes"], 300)
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["06:00"])
        self.assertEqual(conn["catchup"], {"attempts": 5, "withinMinutes": 30})
        self.assertEqual(conn["degradeAfterNodes"], 3)
        self.assertEqual(config.connection_errors(conn, "glm"), [])

    def test_hybrid_flat_migrated_to_fixed_on_load(self):
        Path(config.config_path()).write_text(json.dumps({
            "version": 2,
            "connections": {"glm": {
                "label": "glm", "url": "https://x.example/v4", "apiKey": "file:glm",
                "model": "GLM-5-Turbo", "windowMinutes": 300,
                "mode": "hybrid", "times": ["06:00"], "days": "every-day",
            }},
        }))
        conn = config.load_config()["connections"]["glm"]
        self.assertEqual(conn["schedule"]["mode"], "fixed")
        # the file is rewritten so the migration sticks
        on_disk = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(on_disk["connections"]["glm"]["mode"], "fixed")

    def test_hybrid_v1_nested_migrated_on_load(self):
        v1_conn = self._v1_conn()
        v1_conn["schedule"]["mode"] = "hybrid"
        Path(config.config_path()).write_text(json.dumps(
            {"version": 1, "global": {}, "connections": {"glm": v1_conn}}
        ))
        conn = config.load_config()["connections"]["glm"]
        self.assertEqual(conn["schedule"]["mode"], "fixed")

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
        conn["settings"] = {"catchupAttempts": 2, "catchupMinutes": 1441, "degradeAfterNodes": 5}
        conn["catchup"] = {"attempts": 2, "withinMinutes": 1441}
        conn["degradeAfterNodes"] = 5
        conf = config.empty_config()
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        loaded = config.load_config()["connections"]["glm"]
        self.assertEqual(loaded["catchup"], {"attempts": 2, "withinMinutes": 1441})
        self.assertEqual(loaded["degradeAfterNodes"], 5)
        file = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(file["connections"]["glm"]["settings"], {"catchupAttempts": 2, "catchupMinutes": 1441, "degradeAfterNodes": 5})

    def test_global_settings_inherited_by_connections(self):
        conf = config.empty_config()
        conf["settings"] = {"catchupMinutes": 45, "catchupAttempts": 2, "degradeAfterNodes": 6}
        conf["connections"]["glm"] = self._v1_conn()
        config.save_config(conf)
        file = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(file["settings"], {"catchupMinutes": 45, "catchupAttempts": 2, "degradeAfterNodes": 6})
        self.assertNotIn("settings", file["connections"]["glm"])
        loaded = config.load_config()["connections"]["glm"]
        self.assertEqual(loaded["catchup"], {"attempts": 2, "withinMinutes": 45})
        self.assertEqual(loaded["degradeAfterNodes"], 6)

    def test_connection_override_beats_global_settings(self):
        conf = config.empty_config()
        conf["settings"] = {"catchupMinutes": 45, "catchupAttempts": 2}
        conn = self._v1_conn()
        conn["settings"] = {"catchupMinutes": 60}
        conn["catchup"] = {"attempts": 2, "withinMinutes": 60}
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        file = json.loads(Path(config.config_path()).read_text())
        # only the knob that actually differs from the global block persists on the connection
        self.assertEqual(file["connections"]["glm"]["settings"], {"catchupMinutes": 60})
        loaded = config.load_config()["connections"]["glm"]
        self.assertEqual(loaded["catchup"], {"attempts": 2, "withinMinutes": 60})

    def test_flat_knobs_migrated_into_settings_on_load(self):
        Path(config.config_path()).write_text(json.dumps({
            "version": 2,
            "connections": {"glm": {
                "label": "glm", "url": "https://x.example/v4", "apiKey": "file:glm",
                "mode": "fixed", "times": ["06:00"], "days": "every-day",
                "catchupMinutes": 60, "catchupAttempts": 2, "degradeAfterNodes": 4,
            }},
        }))
        conn = config.load_config()["connections"]["glm"]
        self.assertEqual(conn["catchup"], {"attempts": 2, "withinMinutes": 60})
        self.assertEqual(conn["degradeAfterNodes"], 4)
        # the file is rewritten so the migration sticks: per-connection values stay
        # per-connection, and the top-level block is materialized with its defaults
        file = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(file["settings"], config.default_settings())
        self.assertEqual(file["connections"]["glm"]["settings"], {"catchupMinutes": 60, "catchupAttempts": 2, "degradeAfterNodes": 4})
        self.assertNotIn("catchupMinutes", file["connections"]["glm"])
        self.assertNotIn("degradeAfterNodes", file["connections"]["glm"])

    def test_settings_block_always_written(self):
        conf = config.empty_config()
        conf["connections"]["glm"] = self._v1_conn()
        config.save_config(conf)
        file = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(file["settings"], config.default_settings())
        self.assertNotIn("settings", file["connections"]["glm"])
        loaded = config.load_config()["connections"]["glm"]
        self.assertEqual(loaded["catchup"], {"attempts": 5, "withinMinutes": 30})
        self.assertEqual(loaded["degradeAfterNodes"], 3)

    def test_disabled_flag_roundtrips(self):
        conn = self._v1_conn()
        conn["enabled"] = False
        conf = config.empty_config()
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["glm"]
        self.assertFalse(on_disk["enabled"])


class LocationTests(IsolatedTestCase):
    def _write_conn(self, conn):
        conf = config.empty_config()
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        return conf

    def test_location_defaults_to_local(self):
        self._write_conn(plan_connection())
        self.assertEqual(config.load_config()["connections"]["glm"]["location"], "local")
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["glm"]
        self.assertNotIn("location", on_disk)  # local is the unwritten default

    def test_remote_location_roundtrips_through_flat_format(self):
        conn = plan_connection()
        conn["location"] = "remote"
        self._write_conn(conn)
        self.assertEqual(config.load_config()["connections"]["glm"]["location"], "remote")
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["glm"]
        self.assertEqual(on_disk["location"], "remote")

    def test_remote_location_rejects_cli_accounts(self):
        conn = account_connection()
        conn["location"] = "remote"
        self.assertIn("cannot be remote", config.connection_errors(conn, "claude")[0])

    def test_unknown_location_is_rejected(self):
        conn = plan_connection()
        conn["location"] = "the-moon"
        self.assertIn("location", config.connection_errors(conn, "glm")[0])


class RemoteBlockTests(IsolatedTestCase):
    def test_remote_block_roundtrips(self):
        conf = config.empty_config()
        conf["remote"] = {"url": "https://warm.example.com", "tokenRef": "file:remote-token"}
        conf["connections"]["glm"] = plan_connection()
        config.save_config(conf)
        self.assertEqual(
            config.load_config()["remote"],
            {"url": "https://warm.example.com", "tokenRef": "file:remote-token"},
        )
        self.assertEqual(json.loads(Path(config.config_path()).read_text())["remote"]["url"],
                         "https://warm.example.com")

    def test_invalid_remote_block_refuses_to_save(self):
        conf = config.empty_config()
        conf["remote"] = {"url": "warm.example.com", "tokenRef": "file:remote-token"}
        conf["connections"]["glm"] = plan_connection()
        with self.assertRaises(SystemExit):
            config.save_config(conf)
        self.assertEqual(config.remote_errors(conf["remote"])[0], config.remote_errors({"url": "warm.example.com", "tokenRef": "file:remote-token"})[0])
