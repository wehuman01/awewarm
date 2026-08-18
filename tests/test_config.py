import json
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
        self.assertTrue(any("verify" in e for e in errors))

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
