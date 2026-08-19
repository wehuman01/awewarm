import os
import plistlib
import unittest
from unittest import mock

from helpers import IsolatedTestCase

from awewarm import config as cfg
from awewarm import install


def ok_run(returncode=0, stderr=""):
    return mock.Mock(returncode=returncode, stdout="", stderr=stderr)


# os.getuid does not exist on Windows; the darwin-mocked tests still reach it.
posix_uid = mock.patch("awewarm.install.os.getuid", return_value=501, create=True)


class BuildPlistTests(IsolatedTestCase):
    def test_plist_shape(self):
        plist = install.build_plist("/usr/local/bin/awewarm")
        self.assertEqual(plist["Label"], install.LABEL)
        self.assertEqual(plist["ProgramArguments"], ["/usr/local/bin/awewarm", "run"])
        self.assertEqual(plist["StartInterval"], 60)
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["StandardOutPath"].endswith("launchd.log"))

    def test_plist_passes_awewarm_env_through(self):
        plist = install.build_plist("/usr/local/bin/awewarm")
        env = plist["EnvironmentVariables"]
        self.assertEqual(
            {key: value for key, value in env.items() if key.startswith("AWEWARM_")},
            {
                "AWEWARM_CONFIG": str(self.tmp_path / "config.json"),
                "AWEWARM_STATE": str(self.tmp_path / "state.json"),
                "AWEWARM_LOG": str(self.tmp_path / "awewarm.log"),
                "AWEWARM_PLIST": str(self.tmp_path / "agent.plist"),
                "AWEWARM_SYSTEMD_DIR": str(self.tmp_path / "systemd-user"),
            },
        )

    def test_plist_carries_path_for_cli_resolution(self):
        # launchd's default PATH lacks user-local install dirs; the installer
        # must propagate the current PATH or CLIs like `claude` never resolve.
        plist = install.build_plist("/usr/local/bin/awewarm")
        self.assertEqual(plist["EnvironmentVariables"].get("PATH"), os.environ["PATH"])

    def test_plist_path_env_override(self):
        self.assertEqual(install.plist_path(), self.tmp_path / "agent.plist")


class InstallTests(IsolatedTestCase):
    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.shutil.which", return_value="/usr/local/bin/awewarm")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_install_writes_plist_and_bootstraps(self, run, which, uid):
        plist = install.install_scheduler()
        self.assertTrue(plist.exists())
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["Label"], install.LABEL)
        self.assertTrue(install.scheduler_installed())
        argvs = [call[0][0] for call in run.call_args_list]
        self.assertTrue(any("bootout" in argv for argv in argvs))
        self.assertTrue(any("bootstrap" in argv for argv in argvs))

    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.shutil.which", return_value=None)
    def test_install_without_entry_point_dies(self, which):
        with self.assertRaises(SystemExit):
            install.install_scheduler()

    @mock.patch("awewarm.install.sys.platform", "linux")
    @mock.patch("awewarm.install.shutil.which", return_value=None)
    def test_install_without_systemctl_dies_with_cron_hint(self, which):
        with self.assertRaises(SystemExit) as raised:
            install.install_scheduler()
        self.assertIn("cron", str(raised.exception))

    @mock.patch("awewarm.install.sys.platform", "freebsd")
    def test_install_refuses_unsupported_platform(self):
        with self.assertRaises(SystemExit):
            install.install_scheduler()

    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.shutil.which", return_value="/usr/local/bin/awewarm")
    @mock.patch("awewarm.install.subprocess.run")
    def test_install_falls_back_to_legacy_load(self, run, which, uid):
        run.side_effect = [
            ok_run(),  # bootout
            ok_run(returncode=1, stderr="bootstrap failed"),  # bootstrap
            ok_run(),  # legacy load
        ]
        plist = install.install_scheduler()
        self.assertTrue(plist.exists())

    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.shutil.which", return_value="/usr/local/bin/awewarm")
    @mock.patch("awewarm.install.subprocess.run")
    def test_install_all_launchctl_failures_die(self, run, which, uid):
        run.side_effect = [
            ok_run(),
            ok_run(returncode=1, stderr="bootstrap failed"),
            ok_run(returncode=1, stderr="load failed"),
        ]
        with self.assertRaises(SystemExit):
            install.install_scheduler()


class UninstallTests(IsolatedTestCase):
    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_uninstall_removes_plist(self, run, uid):
        plist = install.plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(b"placeholder")
        self.assertTrue(install.uninstall_scheduler())
        self.assertFalse(plist.exists())
        self.assertFalse(install.scheduler_installed())

    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_uninstall_when_absent(self, run, uid):
        self.assertFalse(install.uninstall_scheduler())


class WindowsInstallTests(IsolatedTestCase):
    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch("awewarm.install.shutil.which", return_value="C:\\Users\\x\\Scripts\\awewarm.exe")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_install_creates_minute_task(self, run, which):
        name = install.install_scheduler()
        self.assertEqual(name, install.LABEL)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "schtasks")
        for flag in ("/Create", "/SC", "MINUTE", "/TN"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("/TN") + 1], install.LABEL)
        # /TR embeds the exe in quotes so paths with spaces survive
        self.assertEqual(argv[argv.index("/TR") + 1], '"C:\\Users\\x\\Scripts\\awewarm.exe" run')
        self.assertTrue(install.scheduler_installed())

    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch("awewarm.install.shutil.which", return_value="C:\\bin\\awewarm.exe")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run(returncode=1, stderr="access denied"))
    def test_install_failure_dies_with_manual_command(self, run, which):
        with self.assertRaises(SystemExit):
            install.install_scheduler()

    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch("awewarm.install.shutil.which", return_value=None)
    def test_install_without_entry_point_dies(self, which):
        with self.assertRaises(SystemExit):
            install.install_scheduler()


class WindowsUninstallTests(IsolatedTestCase):
    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch("awewarm.install.subprocess.run")
    def test_installed_detected_via_query(self, run):
        run.return_value = ok_run()
        self.assertTrue(install.scheduler_installed())
        argv = run.call_args[0][0]
        self.assertIn("/Query", argv)
        self.assertIn(install.LABEL, argv)
        run.return_value = ok_run(returncode=1)
        self.assertFalse(install.scheduler_installed())

    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_uninstall_deletes_task(self, run):
        self.assertTrue(install.uninstall_scheduler())
        argv = run.call_args[0][0]
        self.assertIn("/Delete", argv)
        self.assertIn(install.LABEL, argv)


class LinuxUnitTests(IsolatedTestCase):
    def test_service_exec_start_and_env(self):
        text = install.build_service("/home/x/.local/bin/awewarm")
        self.assertIn("ExecStart=/home/x/.local/bin/awewarm run", text)
        self.assertIn("Type=oneshot", text)
        # sparse user-manager env gets AWEWARM_* and PATH baked in, like the plist
        self.assertIn(f'Environment="AWEWARM_CONFIG={cfg.config_path()}"', text)
        self.assertIn('Environment="PATH=', text)

    def test_timer_cadence(self):
        text = install.build_timer()
        self.assertIn(f"OnUnitActiveSec={install.TICK_SECONDS}s", text)
        self.assertIn("OnStartupSec=1min", text)
        self.assertIn("WantedBy=timers.target", text)

    def test_installed_detected_by_timer_file(self):
        with mock.patch("awewarm.install.sys.platform", "linux"):
            self.assertFalse(install.scheduler_installed())
            install.timer_path().parent.mkdir(parents=True, exist_ok=True)
            install.timer_path().write_text(install.build_timer())
            self.assertTrue(install.scheduler_installed())


class LinuxInstallTests(IsolatedTestCase):
    @mock.patch("awewarm.install.sys.platform", "linux")
    @mock.patch("awewarm.install.shutil.which", return_value="/home/x/.local/bin/awewarm")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_install_writes_units_and_enables_timer(self, run, which):
        timer = install.install_scheduler()
        self.assertTrue(timer.exists())
        self.assertTrue(install.service_path().exists())
        self.assertIn("ExecStart=/home/x/.local/bin/awewarm run", timer.parent.joinpath("awewarm.service").read_text())
        commands = [call[0][0] for call in run.call_args_list]
        self.assertTrue(any("daemon-reload" in argv for argv in commands))
        self.assertTrue(any("enable" in argv and "awewarm.timer" in argv for argv in commands))
        self.assertTrue(all(argv[:2] == ["systemctl", "--user"] for argv in commands))
        self.assertTrue(install.scheduler_installed())

    @mock.patch("awewarm.install.sys.platform", "linux")
    @mock.patch("awewarm.install.shutil.which", side_effect=lambda cmd: "/usr/bin/systemctl" if cmd == "systemctl" else None)
    def test_install_without_awewarm_exe_dies(self, which):
        with self.assertRaises(SystemExit):
            install.install_scheduler()

    @mock.patch("awewarm.install.sys.platform", "linux")
    @mock.patch("awewarm.install.shutil.which", return_value="/home/x/.local/bin/awewarm")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run(returncode=1, stderr="Failed to connect to bus: no medium found"))
    def test_install_bus_failure_hints_linger(self, run, which):
        with self.assertRaises(SystemExit) as raised:
            install.install_scheduler()
        self.assertIn("enable-linger", str(raised.exception))
        self.assertIn("cron", str(raised.exception))


class LinuxUninstallTests(IsolatedTestCase):
    @mock.patch("awewarm.install.sys.platform", "linux")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_uninstall_removes_units(self, run):
        install.unit_dir().mkdir(parents=True, exist_ok=True)
        install.service_path().write_text(install.build_service("/x/awewarm"))
        install.timer_path().write_text(install.build_timer())
        self.assertTrue(install.uninstall_scheduler())
        self.assertFalse(install.timer_path().exists())
        self.assertFalse(install.service_path().exists())
        commands = [call[0][0] for call in run.call_args_list]
        self.assertTrue(any("disable" in argv for argv in commands))

    @mock.patch("awewarm.install.sys.platform", "linux")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_uninstall_when_absent(self, run):
        self.assertFalse(install.uninstall_scheduler())


class WakeSpecTests(IsolatedTestCase):
    def _config(self, times, days="weekday", mode="fixed", enabled=True):
        window = {
            "status": "user-confirmed", "startRule": "unknown",
            "durationMinutes": 300, "evidence": "user-confirmed",
        } if mode == "interval" else {
            "status": "unknown", "startRule": "unknown",
            "durationMinutes": None, "evidence": "none",
        }
        cfg.save_config({"version": 2, "global": {}, "connections": {
            "c1": {
                "label": "c1", "kind": cfg.KIND_ACCOUNT, "enabled": enabled,
                "auth": {"type": "local-cli", "status": "valid", "apiKeyRef": None},
                "transport": {"kind": "claude-cli", "baseUrl": None, "cliCommand": "/usr/local/bin/claude"},
                "window": window,
                "activation": {"model": None, "prompt": cfg.DEFAULT_PROMPT, "maxTokens": cfg.DEFAULT_MAX_TOKENS},
                "schedule": {"mode": mode, "fixed": {
                    "at": times, "days": days,
                    "catchUpWindowMinutes": cfg.DEFAULT_CATCHUP_MINUTES,
                    "skipIfActivatedWithinMinutes": cfg.DEFAULT_SKIP_IF_ACTIVATED_MINUTES,
                }, "interval": {
                    "graceSeconds": cfg.DEFAULT_GRACE_SECONDS,
                    "jitterSeconds": cfg.DEFAULT_JITTER_SECONDS,
                }},
            }
        }})

    def test_earliest_slot_with_lead_and_weekday_letters(self):
        self._config(["11:40", "06:00"])
        self.assertEqual(install.build_wake_spec(cfg.load_config()), ("MTWRF", "05:55:00"))

    def test_every_day_wins_when_any_connection_uses_it(self):
        self._config(["06:00"], days="every-day")
        self.assertEqual(install.build_wake_spec(cfg.load_config()), ("MTWRFSU", "05:55:00"))

    def test_lead_wraps_past_midnight(self):
        self._config(["00:03"])
        self.assertEqual(install.build_wake_spec(cfg.load_config()), ("MTWRF", "23:58:00"))

    def test_none_without_fixed_connections(self):
        self._config(["06:00"], mode="interval")
        self.assertIsNone(install.build_wake_spec(cfg.load_config()))

    def test_disabled_connections_are_ignored(self):
        self._config(["06:00"], enabled=False)
        self.assertIsNone(install.build_wake_spec(cfg.load_config()))


class WakeScheduleTests(IsolatedTestCase):
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_set_writes_state_on_success(self, run):
        self.assertTrue(install.set_wake_schedule(("MTWRF", "05:55:00")))
        self.assertEqual(
            cfg.load_state()["wakeSchedule"],
            {"type": install.WAKE_TYPE, "days": "MTWRF", "time": "05:55:00"},
        )
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["sudo", "-n"])
        self.assertIn("wakeorpoweron", argv)

    @mock.patch("awewarm.install.subprocess.run")
    def test_set_fails_after_both_sudo_attempts(self, run):
        run.return_value = ok_run(returncode=1, stderr="no tty")
        self.assertFalse(install.set_wake_schedule(("MTWRF", "05:55:00")))
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("wakeSchedule", cfg.load_state())

    def _recorded_state(self):
        state = cfg.load_state()
        state["wakeSchedule"] = {"type": install.WAKE_TYPE, "days": "MTWRF", "time": "05:55:00"}
        cfg.save_state(state)

    @mock.patch("awewarm.install._current_repeat_line", return_value="")
    def test_cancel_without_state_is_none(self, line):
        self.assertEqual(install.cancel_wake_schedule()[0], "none")

    @mock.patch("awewarm.install._current_repeat_line", return_value="Repeating power event: wake at 05:55:00 every day (MTWRF)")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_cancel_matches_and_cancels(self, run, line):
        self._recorded_state()
        status, spec = install.cancel_wake_schedule()
        self.assertEqual(status, "cancelled")
        argv = run.call_args[0][0]
        self.assertIn("cancel", argv)
        self.assertNotIn("wakeSchedule", cfg.load_state())

    @mock.patch("awewarm.install._current_repeat_line", return_value="Repeating power event: wake at 07:30:00 every day (MTWRF)")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_cancel_skips_user_owned_schedule(self, run, line):
        self._recorded_state()
        status, _ = install.cancel_wake_schedule()
        self.assertEqual(status, "changed")
        # no pmset mutation, only state cleanup
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
