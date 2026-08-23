import os
import plistlib
import subprocess
import sys
import unittest
from unittest import mock

from helpers import IsolatedTestCase

from awewarm import config as cfg
from awewarm import install


def ok_run(returncode=0, stderr="", stdout=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


# os.getuid does not exist on Windows; the darwin-mocked tests still reach it.
posix_uid = mock.patch("awewarm.install.os.getuid", return_value=501, create=True)


class BuildPlistTests(IsolatedTestCase):
    def test_plist_shape(self):
        plist = install.build_plist("/usr/local/bin/awewarm")
        self.assertEqual(plist["Label"], install.LABEL)
        self.assertEqual(plist["ProgramArguments"], ["/usr/local/bin/awewarm", "tick"])
        self.assertEqual(plist["StartInterval"], 60)
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["StandardOutPath"].endswith("launchd.log"))

    def test_plist_includes_calendar_entries(self):
        entries = [{"Hour": 6, "Minute": 35, "Weekday": [1, 2, 3, 4, 5]}]
        self.assertEqual(install.build_plist("/x", entries)["StartCalendarInterval"], entries)

    def test_plist_omits_calendar_key_when_no_entries(self):
        self.assertNotIn("StartCalendarInterval", install.build_plist("/x"))
        self.assertNotIn("StartCalendarInterval", install.build_plist("/x", []))

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
        argvs = [call.args[0] for call in run.call_args_list]
        create = next(argv for argv in argvs if "/Create" in argv)
        self.assertEqual(create[0], "schtasks")
        for flag in ("/Create", "/SC", "MINUTE", "/TN"):
            self.assertIn(flag, create)
        self.assertEqual(create[create.index("/TN") + 1], install.LABEL)
        # /TR embeds the exe in quotes so paths with spaces survive
        self.assertEqual(create[create.index("/TR") + 1], '"C:\\Users\\x\\Scripts\\awewarm.exe" tick')
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


class WindowsWakeTests(IsolatedTestCase):
    def _fixed_config(self, times=("06:35",), wake=True):
        config = cfg.empty_config()
        config["connections"]["c1"] = CalendarEntriesTests._conn(list(times), wake=wake)
        return config

    def test_build_wake_ps1_registers_one_task_per_slot(self):
        entries = install.calendar_entries(self._fixed_config(("06:35", "11:40")))
        script = install.build_wake_ps1("C:\\Program Files\\awewarm.exe", entries)
        self.assertIn("New-ScheduledTaskSettingsSet -WakeToRun", script)
        self.assertIn("'06:35', '11:40'", script)
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn(install.WAKE_TASK_PREFIX, script)
        self.assertIn("'C:\\Program Files\\awewarm.exe'", script)

    def test_wake_defaults_off_when_the_key_is_absent(self):
        # wake is opt-in: a schedule without wakeWhenAsleep never arms a wake
        self.assertEqual(install.calendar_entries(self._fixed_config(("06:35",), wake=None)), [])

    @mock.patch("awewarm.install._schtasks")
    def test_wake_task_times_parses_csv(self, schtasks):
        schtasks.return_value = ok_run(
            stdout='"\\com.awewarm.scheduler.wake-0635","06:35:00","Ready"\n'
                   '"\\Other Task","01:00:00","Running"\n'
        )
        self.assertEqual(install.wake_task_times(), {"0635"})

    @mock.patch("awewarm.install._schtasks")
    def test_wake_task_times_none_when_query_fails(self, schtasks):
        schtasks.side_effect = OSError("no schtasks")
        self.assertIsNone(install.wake_task_times())

    @mock.patch("awewarm.install._schtasks")
    def test_sync_noop_when_in_sync(self, schtasks):
        schtasks.return_value = ok_run(stdout='"\\com.awewarm.scheduler.wake-0635","x","y"\n')
        with mock.patch("awewarm.install._run_powershell") as powershell:
            self.assertFalse(install.sync_windows_wake(self._fixed_config(("06:35",))))
            powershell.assert_not_called()

    @mock.patch("awewarm.install._schtasks")
    @mock.patch("awewarm.install.resolve_exe", return_value="C:\\awewarm.exe")
    def test_sync_registers_missing_tasks(self, exe, schtasks):
        schtasks.return_value = ok_run(stdout="")  # none installed yet
        with mock.patch("awewarm.install._run_powershell", return_value=ok_run()) as powershell:
            self.assertTrue(install.sync_windows_wake(self._fixed_config(("06:35",))))
            script = powershell.call_args[0][0]
            self.assertIn("'06:35'", script)

    @mock.patch("awewarm.install._schtasks")
    def test_sync_deletes_stray_tasks(self, schtasks):
        schtasks.return_value = ok_run(stdout='"\\com.awewarm.scheduler.wake-2200","x","y"\n')
        with mock.patch("awewarm.install._run_powershell") as powershell:
            # wake opted out → desired is empty, the stray must go
            self.assertTrue(install.sync_windows_wake(self._fixed_config(("06:35",), wake=False)))
            powershell.assert_not_called()
        argv = schtasks.call_args[0][0]
        self.assertIn("/Delete", argv)
        self.assertIn("com.awewarm.scheduler.wake-2200", argv)

    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch(
        "awewarm.install._schtasks",
        return_value=ok_run(stdout='"\\com.awewarm.scheduler.wake-0635","x","y"\n'),
    )
    def test_uninstall_removes_wake_tasks(self, schtasks):
        self.assertTrue(install.uninstall_scheduler())
        deletes = [call.args[0] for call in schtasks.call_args_list]
        wake_delete = install.WAKE_TASK_PREFIX + "0635"
        self.assertTrue(any("/Delete" in argv and wake_delete in argv for argv in deletes))

    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch("awewarm.install._schtasks")
    def test_self_heal_syncs_wake_drift(self, schtasks):
        schtasks.side_effect = [
            ok_run(stdout="awewarm.exe tick"),  # tick task query
            ok_run(stdout='"\\com.awewarm.scheduler.wake-2200","x","y"\n'),  # wake query
        ]
        with mock.patch("awewarm.install.sync_windows_wake") as sync:
            install._maybe_self_heal_job(self._fixed_config(("06:35",)))
            sync.assert_called_once()

    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch("awewarm.install._schtasks", return_value=ok_run())
    def test_refresh_wake_win32_syncs(self, schtasks):
        with mock.patch("awewarm.install.sync_windows_wake", return_value=True) as sync:
            self.assertTrue(install.refresh_wake(self._fixed_config()))
            sync.assert_called_once()

    @mock.patch("awewarm.install.sys.platform", "win32")
    @mock.patch("awewarm.install._schtasks", return_value=ok_run(returncode=1))
    def test_refresh_wake_noop_without_tick_task(self, schtasks):
        with mock.patch("awewarm.install.sync_windows_wake") as sync:
            self.assertFalse(install.refresh_wake(self._fixed_config()))
            sync.assert_not_called()


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
        argvs = [call.args[0] for call in run.call_args_list]
        delete = next(argv for argv in argvs if "/Delete" in argv)
        self.assertIn(install.LABEL, delete)


class LinuxUnitTests(IsolatedTestCase):
    def test_service_exec_start_and_env(self):
        text = install.build_service("/home/x/.local/bin/awewarm")
        self.assertIn("ExecStart=/home/x/.local/bin/awewarm tick", text)
        self.assertIn("Type=oneshot", text)
        # sparse user-manager env gets AWEWARM_* and PATH baked in, like the plist
        self.assertIn(f'Environment="AWEWARM_CONFIG={cfg.config_path()}"', text)
        self.assertIn('Environment="PATH=', text)

    def test_timer_cadence(self):
        text = install.build_timer()
        self.assertIn(f"OnUnitActiveSec={install.TICK_SECONDS}s", text)
        self.assertIn("OnStartupSec=1min", text)
        self.assertIn("WantedBy=timers.target", text)

    def test_timer_persists_across_reboot(self):
        # a server rebooted mid-window gets its missed tick fired at boot
        self.assertIn("Persistent=true", install.build_timer())

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
        self.assertIn("ExecStart=/home/x/.local/bin/awewarm tick", timer.parent.joinpath("awewarm.service").read_text())
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


class LegacyPmsetCleanupTests(IsolatedTestCase):
    """awewarm < 0.4 registered a pmset repeat wake; cancel_wake_schedule()
    removes it without touching a schedule the user replaced."""

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
        self.assertNotIn("wakeSchedule", cfg.load_state())
        # no pmset mutation, only state cleanup
        run.assert_not_called()

    @mock.patch("awewarm.install._current_repeat_line",
                return_value="Repeating power events:\n\twakepoweron at 6:30AM every day")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_cancel_matches_modern_pmset_output(self, run, line):
        # newer macOS prints the event on its own line as '6:30AM', not
        # '06:30:00' inside the header line
        state = cfg.load_state()
        state["wakeSchedule"] = {"type": install.WAKE_TYPE, "days": "MTWRFSU", "time": "06:30:00"}
        cfg.save_state(state)
        status, _ = install.cancel_wake_schedule()
        self.assertEqual(status, "cancelled")
        self.assertNotIn("wakeSchedule", cfg.load_state())

    @mock.patch("awewarm.install._current_repeat_line", return_value="Repeating power event: wake at 05:55:00 every day (MTWRF)")
    @mock.patch("awewarm.install.subprocess.run")
    def test_failed_cancel_keeps_key_for_retry(self, run, line):
        run.return_value = ok_run(returncode=1, stderr="no tty")
        self._recorded_state()
        status, _ = install.cancel_wake_schedule()
        self.assertEqual(status, "failed")
        self.assertIn("wakeSchedule", cfg.load_state())

    def test_wallclock_normalization(self):
        self.assertEqual(install._normalize_wallclock("06:30:00"), "6:30")
        self.assertEqual(install._normalize_wallclock("wakepoweron at 6:30AM every day"), "6:30")
        self.assertEqual(install._normalize_wallclock("at 7:45PM"), "19:45")
        self.assertEqual(install._normalize_wallclock("12:15AM"), "0:15")
        self.assertEqual(install._normalize_wallclock("12:30PM"), "12:30")
        self.assertIsNone(install._normalize_wallclock("no time here"))


class CalendarEntriesTests(IsolatedTestCase):
    @staticmethod
    def _conn(times, days="weekday", mode="fixed", enabled=True, wake=True):
        schedule = {"mode": mode, "fixed": {
            "at": times, "days": days,
            "skipIfActivatedWithinMinutes": cfg.DEFAULT_SKIP_IF_ACTIVATED_MINUTES,
        }, "interval": {
            "graceSeconds": cfg.DEFAULT_GRACE_SECONDS,
            "jitterSeconds": cfg.DEFAULT_JITTER_SECONDS,
        }}
        if wake is not None:
            schedule["wakeWhenAsleep"] = wake
        window = ({"status": "user-confirmed", "startRule": "unknown",
                   "durationMinutes": 300, "evidence": "user-confirmed"}
                  if mode == "interval" else
                  {"status": "unknown", "startRule": "unknown",
                   "durationMinutes": None, "evidence": "none"})
        return {
            "label": "c", "kind": cfg.KIND_ACCOUNT, "enabled": enabled,
            "auth": {"type": "local-cli", "status": "valid", "apiKeyRef": None},
            "transport": {"kind": "claude-cli", "baseUrl": None, "cliCommand": "/usr/local/bin/claude"},
            "window": window,
            "activation": {"model": None, "prompt": cfg.DEFAULT_PROMPT, "maxTokens": cfg.DEFAULT_MAX_TOKENS},
            "schedule": schedule,
        }

    def _save(self, *conns):
        data = cfg.empty_config()
        for index, conn in enumerate(conns, 1):
            data["connections"][f"c{index}"] = conn
        cfg.save_config(data)
        return cfg.load_config()

    def test_slots_wake_every_day_regardless_of_day_rule(self):
        config = self._save(self._conn(["06:35"]))
        self.assertEqual(install.calendar_entries(config), [{"Hour": 6, "Minute": 35}])

    def test_every_day_slots_omit_weekday(self):
        config = self._save(self._conn(["19:42"], days="every-day"))
        self.assertEqual(install.calendar_entries(config), [{"Hour": 19, "Minute": 42}])

    def test_wake_opt_out_interval_and_disabled_are_excluded(self):
        config = self._save(
            self._conn(["06:35"], wake=False),
            self._conn(["07:00"], mode="interval"),
            self._conn(["08:00"], enabled=False),
        )
        self.assertEqual(install.calendar_entries(config), [])

    def test_duplicate_slots_across_day_rules_collapse(self):
        # entries fire daily and the tick applies the day rule, so the same
        # slot under different day rules needs only one entry
        config = self._save(
            self._conn(["06:35"]),
            self._conn(["06:35"]),
            self._conn(["06:35"], days="every-day"),
        )
        self.assertEqual(install.calendar_entries(config), [{"Hour": 6, "Minute": 35}])

    def test_entries_sorted_by_time(self):
        config = self._save(self._conn(["11:40", "06:00"], days="every-day"))
        entries = install.calendar_entries(config)
        self.assertEqual([(e["Hour"], e["Minute"]) for e in entries], [(6, 0), (11, 40)])


class RefreshWakeTests(IsolatedTestCase):
    def _write_plist(self, entries):
        install.plist_path().parent.mkdir(parents=True, exist_ok=True)
        with open(install.plist_path(), "wb") as handle:
            plistlib.dump(install.build_plist("/opt/awewarm", entries), handle)

    def _fixed_config(self):
        config = cfg.empty_config()
        config["connections"]["c1"] = CalendarEntriesTests._conn(["06:35"])
        return config

    @mock.patch("awewarm.install.sys.platform", "darwin")
    def test_noop_when_entries_match(self):
        config = self._fixed_config()
        self._write_plist(install.calendar_entries(config))
        with mock.patch("awewarm.install._install_launchd") as reinstall:
            self.assertFalse(install.refresh_wake(config))
            reinstall.assert_not_called()

    @mock.patch("awewarm.install.sys.platform", "darwin")
    def test_rewrites_when_entries_drifted(self):
        config = self._fixed_config()
        self._write_plist([])  # installed before the slot existed
        with mock.patch("awewarm.install._install_launchd") as reinstall:
            self.assertTrue(install.refresh_wake(config))
            reinstall.assert_called_once()

    @mock.patch("awewarm.install.sys.platform", "darwin")
    def test_noop_without_installed_plist(self):
        self.assertFalse(install.refresh_wake(self._fixed_config()))

    @mock.patch("awewarm.install.sys.platform", "linux")
    def test_noop_off_darwin(self):
        self._write_plist([])
        self.assertFalse(install.refresh_wake(self._fixed_config()))


class SelfHealCalendarTests(IsolatedTestCase):
    def _write_plist(self, entries):
        install.plist_path().parent.mkdir(parents=True, exist_ok=True)
        with open(install.plist_path(), "wb") as handle:
            plistlib.dump(install.build_plist("/opt/awewarm", entries), handle)

    def _fixed_config(self):
        config = cfg.empty_config()
        config["connections"]["c1"] = CalendarEntriesTests._conn(["06:35"])
        return config

    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    def test_stale_calendar_triggers_reinstall(self, _uid):
        self._write_plist([])  # current command line, pre-calendar plist
        with mock.patch("awewarm.install.install_scheduler") as heal:
            install._maybe_self_heal_job(self._fixed_config())
            heal.assert_called_once()

    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    def test_current_calendar_leaves_job_alone(self, _uid):
        config = self._fixed_config()
        self._write_plist(install.calendar_entries(config))
        with mock.patch("awewarm.install.install_scheduler") as heal:
            install._maybe_self_heal_job(config)
            heal.assert_not_called()

    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.load_config")
    def test_heal_loads_config_when_not_given(self, load, _uid):
        load.return_value = self._fixed_config()
        self._write_plist([])
        with mock.patch("awewarm.install.install_scheduler") as heal:
            install._maybe_self_heal_job()
            heal.assert_called_once()
            load.assert_called_once()

    @posix_uid
    @mock.patch("awewarm.install.sys.platform", "darwin")
    def test_failed_heal_does_not_break_the_tick(self, _uid):
        # die() raises SystemExit (e.g. resolve_exe on launchd's sparse PATH);
        # a failed self-heal must not abort the tick's activations.
        self._write_plist([])  # stale job triggers the heal attempt
        with mock.patch("awewarm.install.install_scheduler", side_effect=SystemExit("awewarm: boom")):
            install._maybe_self_heal_job(self._fixed_config())  # must not raise


class WindowsImportTests(unittest.TestCase):
    """Windows has no pwd module; importing it at module top level broke
    every awewarm command there (CI regression, fixed by importing it inside
    the macOS-only install_wake_grant)."""

    def test_imports_survive_with_pwd_blocked(self):
        src = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
        )
        code = (
            "import sys; sys.path.insert(0, %r); "
            "sys.modules['pwd'] = None; import awewarm.cli" % src
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
