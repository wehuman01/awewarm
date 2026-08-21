import json
from datetime import timedelta
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

    def test_hide_flag_roundtrips(self):
        data = config.empty_config()
        conn = account_connection()
        conn["hide"] = True
        data["connections"]["claude-code"] = conn
        config.save_config(data)
        self.assertIn('"hide": true', config.config_path().read_text())
        self.assertTrue(config.load_config()["connections"]["claude-code"]["hide"])

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

    def test_hide_must_be_boolean(self):
        conn = account_connection()
        conn["hide"] = "yes"
        errors = config.connection_errors(conn, "acct")
        self.assertTrue(any("hide" in e for e in errors))

    def test_user_confirmed_duration_unlocks_interval(self):
        conn = plan_connection(mode="interval", window_status="user-confirmed", duration=300)
        self.assertEqual(config.connection_errors(conn, "plan"), [])


class TimezoneForTests(unittest.TestCase):
    """timezone_for: IANA names, fixed "UTC±HH:MM" offsets, honest rejection."""

    def test_iana_names(self):
        self.assertEqual(config.timezone_for("Asia/Shanghai").key, "Asia/Shanghai")

    def test_fixed_offsets(self):
        self.assertEqual(config.timezone_for("UTC+08:00").utcoffset(None), timedelta(hours=8))
        self.assertEqual(
            config.timezone_for("UTC-05:30").utcoffset(None), -timedelta(hours=5, minutes=30)
        )
        self.assertEqual(config.timezone_for("UTC+00:00").utcoffset(None), timedelta(0))

    def test_rejects_malformed_names(self):
        for name in ("Mars/Olympus", "UTC+25:00", "UTC+8", "UTC+08:60", "", None, 8):
            with self.assertRaises(ValueError):
                config.timezone_for(name)


class NamingTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(config.slugify("GLM Coding Plan!"), "glm-coding-plan")
        self.assertEqual(config.slugify("  "), "plan")

    def test_unique_connection_id_appends_counter(self):
        data = config.empty_config()
        data["connections"]["glm-coding-plan"] = plan_connection()
        self.assertEqual(config.unique_connection_id(data, "GLM Coding Plan"), "glm-coding-plan-2")

    def test_unique_connection_id_never_yields_a_location_name(self):
        # "local"/"remote" are the on-disk location groups; a connection named
        # after one would be swallowed by the grouping on save
        data = config.empty_config()
        self.assertEqual(config.unique_connection_id(data, "Local"), "local-2")
        self.assertEqual(config.unique_connection_id(data, "remote"), "remote-2")


class VersionTests(unittest.TestCase):
    def test_version_string_present(self):
        import awewarm

        self.assertRegex(awewarm.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()


class V2FormatTests(IsolatedTestCase):
    def test_save_compacts_to_flat_v3(self):
        conf = config.empty_config()
        conf["connections"]["glm"] = plan_connection(
            mode="fixed", fixed_at=("06:00",), days="every-day",
            window_status="user-confirmed", duration=300,
        )
        config.save_config(conf)
        on_disk = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(on_disk["version"], 3)
        self.assertEqual(on_disk["connections"]["local"]["glm"]["settings"]["schedule"], {
            "times": ["06:00"], "days": "every-day"})

    def test_load_refuses_flat_v2(self):
        Path(config.config_path()).write_text(json.dumps({
            "version": 2,
            "connections": {"glm": {
                "label": "glm", "url": "https://x.example/v4", "apiKey": "$GLM_KEY",
                "model": "GLM-5-Turbo", "windowMinutes": 300,
                "mode": "fixed", "times": ["06:00"], "days": "every-day",
            }},
        }))
        with self.assertRaises(SystemExit) as ctx:
            config.load_config()
        msg = str(ctx.exception)
        self.assertIn("older files are not upgraded automatically", msg)
        self.assertIn("config template", msg)

    def test_load_refuses_nested_v1(self):
        Path(config.config_path()).write_text(json.dumps({
            "version": 1,
            "global": {},
            "connections": {"glm": {
                "label": "glm", "kind": "subscription", "enabled": True,
                "auth": {"type": "api-key", "status": "valid", "apiKeyRef": "file:glm"},
                "transport": {"kind": "openai-chat", "baseUrl": "https://x.example/v4", "cliCommand": None},
                "plan": {"url": "https://x.example/v4", "label": "glm"},
                "window": {"status": "user-confirmed", "startRule": "unknown", "durationMinutes": 300, "evidence": "user-confirmed"},
                "activation": {"model": "GLM-5-Turbo", "prompt": "Reply with exactly: ok", "maxTokens": 4},
                "schedule": {"mode": "fixed",
                             "fixed": {"at": ["06:00"], "days": "every-day", "skipIfActivatedWithinMinutes": 30},
                             "interval": {"graceSeconds": 75, "jitterSeconds": 30}},
            }},
        }))
        with self.assertRaises(SystemExit) as ctx:
            config.load_config()
        msg = str(ctx.exception)
        self.assertIn("older files are not upgraded automatically", msg)
        self.assertIn("config template", msg)

    def test_v2_flat_knobs_refused_older_version(self):
        Path(config.config_path()).write_text(json.dumps({
            "version": 2,
            "connections": {"glm": {
                "label": "glm", "url": "https://x.example/v4", "apiKey": "file:glm",
                "model": "GLM-5-Turbo", "windowMinutes": 300,
                "mode": "fixed", "times": ["06:00"], "days": "every-day",
                "catchupMinutes": 60, "catchupAttempts": 2, "degradeAfterNodes": 4,
            }},
        }))
        with self.assertRaises(SystemExit) as ctx:
            config.load_config()
        msg = str(ctx.exception)
        self.assertIn("older files are not upgraded automatically", msg)
        self.assertIn("config template", msg)

    def test_account_roundtrip_via_cli_flag(self):
        conn = account_connection()
        conn["transport"]["cliCommand"] = "/Users/x/.local/bin/codex"
        conn["transport"]["kind"] = "codex-cli"
        conf = config.empty_config()
        conf["connections"]["cx"] = conn
        config.save_config(conf)
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["local"]["cx"]
        self.assertEqual(on_disk["cli"], "/Users/x/.local/bin/codex")
        self.assertNotIn("url", on_disk)
        loaded = config.load_config()["connections"]["cx"]
        self.assertEqual(loaded["transport"]["kind"], "codex-cli")

    def test_custom_tuning_knobs_survive_roundtrip(self):
        conn = plan_connection(mode="fixed", fixed_at=("06:00",), days="every-day")
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
        self.assertEqual(file["connections"]["local"]["glm"]["settings"], {
            "catchupAttempts": 2, "catchupMinutes": 1441, "degradeAfterNodes": 5,
            "schedule": {"times": ["06:00"], "days": "every-day"},
        })

    def test_global_settings_inherited_by_connections(self):
        conf = config.empty_config()
        conf["settings"] = {"catchupMinutes": 45, "catchupAttempts": 2, "degradeAfterNodes": 6}
        conf["connections"]["glm"] = plan_connection(mode="fixed", fixed_at=("06:00",), days="every-day")
        config.save_config(conf)
        file = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(file["settings"], {"catchupMinutes": 45, "catchupAttempts": 2, "degradeAfterNodes": 6})
        self.assertFalse(set(file["connections"]["local"]["glm"]["settings"]) - {"schedule"})
        loaded = config.load_config()["connections"]["glm"]
        self.assertEqual(loaded["catchup"], {"attempts": 2, "withinMinutes": 45})
        self.assertEqual(loaded["degradeAfterNodes"], 6)

    def test_connection_override_beats_global_settings(self):
        conf = config.empty_config()
        conf["settings"] = {"catchupMinutes": 45, "catchupAttempts": 2}
        conn = plan_connection()
        conn["settings"] = {"catchupMinutes": 60}
        conn["catchup"] = {"attempts": 2, "withinMinutes": 60}
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        file = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(file["connections"]["local"]["glm"]["settings"]["catchupMinutes"], 60)
        self.assertNotIn("catchupAttempts", file["connections"]["local"]["glm"]["settings"])
        loaded = config.load_config()["connections"]["glm"]
        self.assertEqual(loaded["catchup"], {"attempts": 2, "withinMinutes": 60})

    def test_settings_block_always_written(self):
        conf = config.empty_config()
        conf["connections"]["glm"] = plan_connection(mode="fixed", fixed_at=("06:00",), days="every-day")
        config.save_config(conf)
        file = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(file["settings"], config.default_settings())
        self.assertFalse(set(file["connections"]["local"]["glm"]["settings"]) - {"schedule"})
        loaded = config.load_config()["connections"]["glm"]
        self.assertEqual(loaded["catchup"], {"attempts": 5, "withinMinutes": 30})
        self.assertEqual(loaded["degradeAfterNodes"], 3)

    def test_disabled_flag_roundtrips(self):
        conn = plan_connection()
        conn["enabled"] = False
        conf = config.empty_config()
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["local"]["glm"]
        self.assertFalse(on_disk["enabled"])


class SettingsLayerTests(IsolatedTestCase):
    """The three settings layers (global → location → connection) and the
    rule that a delegated connection never follows the global schedule."""

    def _inherit_conn(self, **kwargs):
        conn = plan_connection(**kwargs)
        conn["settings"] = {"schedule": {}}  # explicitly nothing of its own
        return conn

    def _config_with(self, global_settings=None, local_settings=None, remote_settings=None, **conns):
        conf = config.empty_config()
        if global_settings:
            conf["settings"].update(global_settings)
        if local_settings:
            conf["connectionDefaults"]["local"] = local_settings
        if remote_settings:
            conf["connectionDefaults"]["remote"] = remote_settings
        conf["connections"].update(conns)
        config.save_config(conf)
        return config.load_config()

    def test_local_connection_inherits_global_schedule(self):
        loaded = self._config_with(
            global_settings={"schedule": {"times": ["09:00"], "days": "every-day", "wakeWhenAsleep": False}},
            glm=self._inherit_conn(),
        )
        sched = loaded["connections"]["glm"]["schedule"]
        self.assertEqual(sched["fixed"]["at"], ["09:00"])
        self.assertEqual(sched["fixed"]["days"], "every-day")
        self.assertFalse(sched["wakeWhenAsleep"])

    def test_remote_connection_never_follows_the_global_schedule(self):
        remote_conn = self._inherit_conn()
        remote_conn["location"] = "remote"
        loaded = self._config_with(
            global_settings={"schedule": {"times": ["09:00"], "days": "every-day"}},
            glm=remote_conn,
        )
        sched = loaded["connections"]["glm"]["schedule"]
        self.assertEqual(sched["fixed"]["at"], ["06:35"])  # code defaults, not global
        self.assertEqual(sched["fixed"]["days"], "weekday")

    def test_remote_layer_schedule_reaches_remote_connections(self):
        remote_conn = self._inherit_conn()
        remote_conn["location"] = "remote"
        loaded = self._config_with(
            global_settings={"schedule": {"times": ["09:00"]}},
            remote_settings={"schedule": {"times": ["08:00"], "days": "every-day"}},
            glm=remote_conn,
        )
        sched = loaded["connections"]["glm"]["schedule"]
        self.assertEqual(sched["fixed"]["at"], ["08:00"])
        self.assertEqual(sched["fixed"]["days"], "every-day")

    def test_local_layer_overrides_global_for_local_connections(self):
        loaded = self._config_with(
            global_settings={"schedule": {"times": ["09:00"]}, "catchupMinutes": 45},
            local_settings={"schedule": {"times": ["07:30"]}, "catchupMinutes": 60},
            glm=self._inherit_conn(),
        )
        conn = loaded["connections"]["glm"]
        self.assertEqual(conn["schedule"]["fixed"]["at"], ["07:30"])
        self.assertEqual(conn["catchup"]["withinMinutes"], 60)

    def test_profile_beats_location_beats_global(self):
        own = plan_connection()
        own["settings"] = {"schedule": {"times": ["05:00"]}}
        loaded = self._config_with(
            global_settings={"schedule": {"times": ["09:00"]}},
            local_settings={"schedule": {"times": ["07:30"]}},
            glm=own,
        )
        self.assertEqual(loaded["connections"]["glm"]["schedule"]["fixed"]["at"], ["05:00"])

    def test_global_knobs_still_reach_remote_connections(self):
        remote_conn = self._inherit_conn()
        remote_conn["location"] = "remote"
        loaded = self._config_with(
            global_settings={"catchupMinutes": 45},
            glm=remote_conn,
        )
        self.assertEqual(loaded["connections"]["glm"]["catchup"]["withinMinutes"], 45)

    def test_inherited_interval_falls_back_to_fixed_without_window(self):
        loaded = self._config_with(
            global_settings={"schedule": {"mode": "interval"}},
            glm=self._inherit_conn(),  # plan with an unknown window
            acct=self._inherit_conn(window_status="user-confirmed", duration=300),
        )
        self.assertEqual(loaded["connections"]["glm"]["schedule"]["mode"], "fixed")
        self.assertEqual(loaded["connections"]["acct"]["schedule"]["mode"], "interval")

    def test_own_interval_without_window_stays_interval_and_errors(self):
        conn = self._inherit_conn()
        conn["settings"]["schedule"]["mode"] = "interval"
        loaded = self._config_with(glm=conn)
        self.assertEqual(loaded["connections"]["glm"]["schedule"]["mode"], "interval")
        self.assertTrue(config.connection_errors(loaded["connections"]["glm"], "glm"))

    def test_connection_defaults_roundtrip(self):
        conf = config.empty_config()
        conf["settings"]["schedule"] = {"times": ["09:00"]}
        conf["connectionDefaults"]["local"] = {"catchupMinutes": 20}
        conf["connectionDefaults"]["remote"] = {"schedule": {"times": ["08:00"]}, "degradeAfterNodes": 4}
        conf["connections"]["glm"] = self._inherit_conn()
        config.save_config(conf)
        loaded = config.load_config()
        self.assertEqual(loaded["connectionDefaults"]["remote"]["schedule"]["times"], ["08:00"])
        self.assertEqual(loaded["connections"]["glm"]["schedule"]["fixed"]["at"], ["09:00"])
        on_disk = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(on_disk["connections"]["remote"]["settings"]["schedule"]["times"], ["08:00"])
        self.assertEqual(on_disk["connections"]["remote"]["settings"]["degradeAfterNodes"], 4)
        self.assertEqual(on_disk["connections"]["local"]["settings"]["catchupMinutes"], 20)

    def test_location_settings_survive_a_load_save_roundtrip(self):
        # the regression the nested on-disk format introduced: any
        # load → edit → save (config set, config remove, ...) must keep the
        # location layers instead of silently dropping them
        conf = config.empty_config()
        conf["connectionDefaults"]["local"] = {"schedule": {"wakeWhenAsleep": True}}
        conf["connections"]["glm"] = self._inherit_conn()
        config.save_config(conf)
        loaded = config.load_config()
        self.assertTrue(loaded["connections"]["glm"]["schedule"]["wakeWhenAsleep"])
        loaded["connections"]["glm"]["label"] = "renamed"  # any edit
        config.save_config(loaded)
        on_disk = json.loads(Path(config.config_path()).read_text())
        self.assertEqual(on_disk["connections"]["local"]["settings"],
                         {"schedule": {"wakeWhenAsleep": True}})
        reloaded = config.load_config()
        self.assertTrue(reloaded["connections"]["glm"]["schedule"]["wakeWhenAsleep"])

    def test_override_equal_to_inheritance_is_dropped(self):
        conn = self._inherit_conn()
        conn["settings"]["schedule"]["times"] = ["09:00"]  # same as global: follows it
        loaded = self._config_with(
            global_settings={"schedule": {"times": ["09:00"]}},
            glm=conn,
        )
        self.assertEqual(loaded["connections"]["glm"]["schedule"]["fixed"]["at"], ["09:00"])
        on_disk = json.loads(Path(config.config_path()).read_text())
        own = (on_disk["connections"]["local"]["glm"].get("settings") or {}).get("schedule") or {}
        self.assertNotIn("times", own)  # nothing pinned: it follows the global times

    def test_settings_block_errors_catch_typos_and_bad_values(self):
        self.assertTrue(config.settings_block_errors({"bogus": 1}, "settings"))
        self.assertTrue(config.settings_block_errors({"schedule": {"times": ["9:00"]}}, "settings"))
        self.assertTrue(config.settings_block_errors({"schedule": {"mode": "whenever"}}, "settings"))
        self.assertTrue(config.settings_block_errors({"catchupMinutes": 0}, "settings"))
        self.assertEqual(config.settings_block_errors({"schedule": {"times": ["09:00"]}}, "settings"), [])

    def test_invalid_settings_block_dies_on_load(self):
        Path(config.config_path()).write_text(json.dumps({
            "version": 3,
            "settings": {"schedule": {"times": ["9:00"]}},
            "connections": {},
        }))
        with self.assertRaises(SystemExit):
            config.load_config()


class LocationTests(IsolatedTestCase):
    def _write_conn(self, conn):
        conf = config.empty_config()
        conf["connections"]["glm"] = conn
        config.save_config(conf)
        return conf

    def test_location_defaults_to_local(self):
        self._write_conn(plan_connection())
        self.assertEqual(config.load_config()["connections"]["glm"]["location"], "local")
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["local"]["glm"]
        self.assertNotIn("location", on_disk)  # local is the unwritten default

    def test_remote_location_roundtrips_through_flat_format(self):
        conn = plan_connection()
        conn["location"] = "remote"
        self._write_conn(conn)
        self.assertEqual(config.load_config()["connections"]["glm"]["location"], "remote")
        on_disk = json.loads(Path(config.config_path()).read_text())["connections"]["remote"]["glm"]
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


class TemplateTests(unittest.TestCase):
    def test_template_file_matches_constant(self):
        template_path = Path(__file__).resolve().parents[1] / "resources" / "config.template.json"
        on_disk = json.loads(template_path.read_text())
        self.assertEqual(on_disk, json.loads(config.CONFIG_TEMPLATE))
