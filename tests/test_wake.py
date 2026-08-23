"""RTC wake layer: target-set computation, pmset convergence, sudoers grant."""
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from helpers import IsolatedTestCase, plan_connection

from awewarm import config as cfg, install
from awewarm.cli import cli

TZ = ZoneInfo("Asia/Taipei")
FRIDAY = datetime(2026, 8, 21, 12, 0, tzinfo=TZ)
SATURDAY = datetime(2026, 8, 22, 12, 0, tzinfo=TZ)
SUNDAY = datetime(2026, 8, 23, 12, 0, tzinfo=TZ)


def invoke(*args, **kwargs):
    kwargs.setdefault("prog_name", "awewarm")
    return CliRunner().invoke(cli, *args, **kwargs)


def ok_run(returncode=0, stderr="", stdout=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def _config(**connections):
    # these tests exercise the wake layer, so connections opt in here unless
    # one explicitly opted out (wake is off by default since 0.4.5)
    for conn in connections.values():
        conn["schedule"].setdefault("wakeWhenAsleep", True)
    return {"version": 2, "connections": connections}


def _interval_state(next_due=None, **extra):
    conn = {
        "lastActivationAt": "2026-08-21T05:50:00+08:00",
        "nextDueAt": next_due,
    }
    conn.update(extra)
    return {"connections": {"glm": conn}}


class CanonicalSpecTests(unittest.TestCase):
    def test_two_and_four_digit_years_match(self):
        self.assertEqual(
            install._canonical_spec("08/22/26 06:35:00"),
            install._canonical_spec("08/22/2026 06:35:00"),
        )

    def test_normalizes_padding(self):
        self.assertEqual(install._canonical_spec("8/2/2026 6:35"), "08/02/2026 06:35:00")

    def test_garbage_returns_none(self):
        self.assertIsNone(install._canonical_spec("every day"))


class WakeSpecsTests(IsolatedTestCase):
    def test_fixed_expands_today_and_tomorrow_day_agnostic(self):
        # Saturday 04:00 with a weekday-only rule: today and tomorrow both
        # arm — the tick decides active days, an off-day wake is a no-op dark
        # wake that re-arms the following day, keeping the chain unbroken.
        early_saturday = datetime(2026, 8, 22, 4, 0, tzinfo=TZ)
        config = _config(glm=plan_connection(fixed_at=("05:50",)))
        specs = install.wake_specs(config, {"connections": {}}, early_saturday)
        keys = {(m.date(), m.hour, m.minute) for m, _kind in specs}
        self.assertEqual(
            keys, {(SATURDAY.date(), 5, 50), (SUNDAY.date(), 5, 50)}
        )
        self.assertTrue(all(kind == "fixed" for _m, kind in specs))

    def test_fixed_excludes_today_slots_already_past(self):
        config = _config(glm=plan_connection(fixed_at=("06:35",)))
        specs = install.wake_specs(config, {"connections": {}}, FRIDAY)
        keys = {(m.date(), m.hour, m.minute) for m, _kind in specs}
        self.assertEqual(keys, {(SATURDAY.date(), 6, 35)})

    def test_interval_contributes_next_due(self):
        conn = plan_connection(mode="interval", window_status="user-confirmed", duration=300)
        config = _config(glm=conn)
        state = _interval_state(next_due="2026-08-22T05:50:00+08:00")
        specs = install.wake_specs(config, state, FRIDAY)
        self.assertEqual(
            [(m.strftime("%m-%d %H:%M"), kind) for m, kind in specs],
            [("08-22 05:50", "interval")],
        )

    def test_interval_auto_disabled_contributes_nothing(self):
        conn = plan_connection(mode="interval", window_status="user-confirmed", duration=300)
        config = _config(glm=conn)
        state = _interval_state(
            next_due="2026-08-22T05:50:00+08:00",
            autoDisabledAt="2026-08-20T00:00:00+08:00",
        )
        self.assertEqual(install.wake_specs(config, state, FRIDAY), [])

    def test_interval_first_anchor_is_not_armed(self):
        # Fires at the next tick while awake; there is no future moment to arm.
        conn = plan_connection(mode="interval", window_status="user-confirmed", duration=300)
        config = _config(glm=conn)
        self.assertEqual(install.wake_specs(config, {"connections": {}}, FRIDAY), [])

    def test_remote_disabled_and_opted_out_are_excluded(self):
        remote = plan_connection(fixed_at=("05:50",))
        remote["location"] = "remote"
        disabled = plan_connection(fixed_at=("06:35",))
        disabled["enabled"] = False
        opted_out = plan_connection(fixed_at=("07:35",))
        opted_out["schedule"]["wakeWhenAsleep"] = False
        config = _config(
            remote_conn=remote, disabled_conn=disabled, opted_out=opted_out,
            glm=plan_connection(fixed_at=("08:35",)),
        )
        keys = {(m.hour, m.minute) for m, _kind in install.wake_specs(config, {"connections": {}}, FRIDAY)}
        self.assertEqual(keys, {(8, 35)})

    def test_slots_dedupe_across_connections_and_sort(self):
        config = _config(
            glm=plan_connection(fixed_at=("08:35", "05:50")),
            doubao=plan_connection(fixed_at=("05:50", "07:35")),
        )
        moments = [m for m, _kind in install.wake_specs(config, {"connections": {}}, FRIDAY)]
        hours = [(m.hour, m.minute) for m in moments if m.date() == SATURDAY.date()]
        self.assertEqual(hours, [(5, 50), (7, 35), (8, 35)])

    def test_event_limit_caps_the_set(self):
        slots = tuple(f"{h:02d}:00" for h in range(10))
        config = _config(glm=plan_connection(fixed_at=slots))
        # Saturday 00:30: today's 01:00-09:00 (9) + all of tomorrow (10) = 19
        early_saturday = datetime(2026, 8, 22, 0, 30, tzinfo=TZ)
        self.assertEqual(
            len(install.wake_specs(config, {"connections": {}}, early_saturday)),
            install.WAKE_EVENT_LIMIT,
        )

    def test_invalid_connection_is_skipped(self):
        # interval mode locked until the window is known — no wake specs, no crash
        conn = plan_connection(mode="interval", window_status="unknown", duration=None)
        config = _config(glm=conn)
        self.assertEqual(install.wake_specs(config, {"connections": {}}, FRIDAY), [])


class SyncWakeEventsTests(IsolatedTestCase):
    def _fixed_config(self):
        return _config(glm=plan_connection(fixed_at=("06:35",)))

    def test_arms_desired_events(self):
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries", return_value=set()) as live, \
             mock.patch("awewarm.install._sudo_pmset", return_value=True) as sudo:
            state = {"connections": {}}
            self.assertTrue(install.sync_wake_events(self._fixed_config(), state, FRIDAY))
            armed = [argv for argv in (call.args[0] for call in sudo.call_args_list)]
            self.assertEqual(armed, [["schedule", "wakeorpoweron", "08/22/26 06:35:00"]])
            live.assert_called_once()
        self.assertEqual(state["wakeEvents"], ["08/22/26 06:35:00"])
        # ledger persisted for the next convergence pass
        self.assertEqual(install.load_state().get("wakeEvents"), state["wakeEvents"])

    def test_in_sync_still_reads_live_to_sweep_orphans(self):
        # the ledger matching desired no longer short-circuits: orphans only
        # show up in `pmset -g sched`, so every pass reads live once
        state = {"connections": {}, "wakeEvents": ["08/22/26 06:35:00"]}
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries",
                        return_value={("08/22/2026 06:35:00", "wakeorpoweron", "pmset")}) as live, \
             mock.patch("awewarm.install._sudo_pmset") as sudo:
            self.assertFalse(install.sync_wake_events(self._fixed_config(), state, FRIDAY))
            live.assert_called_once()
            sudo.assert_not_called()

    def test_cancels_stale_ledger_entry(self):
        stale = "08/25/26 09:00:00"
        state = {"connections": {}, "wakeEvents": [stale, "08/22/26 06:35:00"]}
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries",
                        return_value={(install._canonical_spec(spec), "wakeorpoweron", "pmset")
                                      for spec in state["wakeEvents"]}), \
             mock.patch("awewarm.install._sudo_pmset", return_value=True) as sudo:
            self.assertTrue(install.sync_wake_events(self._fixed_config(), state, FRIDAY))
            argvs = [call.args[0] for call in sudo.call_args_list]
            self.assertIn(["schedule", "cancel", "wakeorpoweron", stale], argvs)
        self.assertNotIn(stale, state["wakeEvents"])

    def test_drops_consumed_event_without_cancelling(self):
        consumed = "08/20/26 06:35:00"  # armed yesterday, already fired
        state = {"connections": {}, "wakeEvents": [consumed, "08/22/26 06:35:00"]}
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries", return_value=set()) as live, \
             mock.patch("awewarm.install._sudo_pmset", return_value=True) as sudo:
            install.sync_wake_events(self._fixed_config(), state, FRIDAY)
            for argv in (call.args[0] for call in sudo.call_args_list):
                self.assertNotIn(consumed, argv)
            live.assert_called_once()
        self.assertNotIn(consumed, state["wakeEvents"])

    def test_retracks_live_event_missing_from_ledger(self):
        wire = "08/22/26 06:35:00"
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries",
                        return_value={(install._canonical_spec(wire), "wakeorpoweron", "pmset")}), \
             mock.patch("awewarm.install._sudo_pmset", return_value=True) as sudo:
            state = {"connections": {}}
            install.sync_wake_events(self._fixed_config(), state, FRIDAY)
            sudo.assert_not_called()  # already armed — only re-tracked
        self.assertIn(wire, state["wakeEvents"])

    def test_arm_failure_sets_blocked_flag_and_logs_once(self):
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries", return_value=set()), \
             mock.patch("awewarm.install._sudo_pmset", return_value=False), \
             mock.patch("awewarm.install.append_log") as logged:
            state = {"connections": {}}
            install.sync_wake_events(self._fixed_config(), state, FRIDAY)
            self.assertTrue(state["wakeSyncBlocked"])
            install.sync_wake_events(self._fixed_config(), state, FRIDAY)
            logged.assert_called_once()

    def test_no_grant_is_a_silent_noop(self):
        with mock.patch("awewarm.install.wake_grant_installed", return_value=False), \
             mock.patch("awewarm.install._live_wake_entries") as live:
            state = {"connections": {}}
            self.assertFalse(install.sync_wake_events(self._fixed_config(), state, FRIDAY))
            live.assert_not_called()
        self.assertNotIn("wakeEvents", state)


class OrphanReclaimTests(IsolatedTestCase):
    """Live pmset debris no ledger tracks — fallout of converging-sync races."""

    def test_reclaims_pmset_event_no_schedule_wants(self):
        # the fully opted-out regime: desired empty, ledger empty, debris armed
        state = {"connections": {}}
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries",
                        return_value={("08/23/2026 07:00:00", "wakeorpoweron", "pmset")}), \
             mock.patch("awewarm.install._sudo_pmset", return_value=True) as sudo, \
             mock.patch("awewarm.install.append_log") as logged:
            self.assertTrue(install.sync_wake_events({"connections": {}}, state, FRIDAY))
            self.assertEqual(
                sudo.call_args.args[0],
                ["schedule", "cancel", "wakeorpoweron", "08/23/26 07:00:00"],
            )
            self.assertIn("re-arm", logged.call_args.args[1])
        self.assertEqual(state.get("wakeEvents", []), [])

    def test_system_events_and_plain_wakes_are_untouched(self):
        state = {"connections": {}}
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries", return_value={
                 ("08/22/2026 01:55:33", "wake", "com.apple.alarm.calaccessd"),
                 ("08/22/2026 07:00:00", "wakeorpoweron", "com.apple.powerd"),
             }), \
             mock.patch("awewarm.install._sudo_pmset") as sudo:
            self.assertFalse(install.sync_wake_events({"connections": {}}, state, FRIDAY))
            sudo.assert_not_called()

    def test_failed_reclaim_adopts_orphan_into_ledger(self):
        state = {"connections": {}}
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries",
                        return_value={("08/23/2026 07:00:00", "wakeorpoweron", "pmset")}), \
             mock.patch("awewarm.install._sudo_pmset", return_value=False):
            self.assertFalse(install.sync_wake_events({"connections": {}}, state, FRIDAY))
        self.assertEqual(state["wakeEvents"], ["08/23/26 07:00:00"])
        self.assertTrue(state["wakeSyncBlocked"])

    def test_just_cancelled_ledger_entry_is_not_reclaimed(self):
        # the stale-cancel and the orphan sweep share one pmset snapshot —
        # an entry cancelled moments ago must not be cancelled twice
        stale = "08/25/26 09:00:00"
        state = {"connections": {}, "wakeEvents": [stale]}
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install.scheduler_installed", return_value=True), \
             mock.patch("awewarm.install._live_wake_entries",
                        return_value={("08/25/2026 09:00:00", "wakeorpoweron", "pmset")}), \
             mock.patch("awewarm.install._sudo_pmset", return_value=True) as sudo:
            self.assertTrue(install.sync_wake_events({"connections": {}}, state, FRIDAY))
            sudo.assert_called_once()
        self.assertNotIn(stale, state.get("wakeEvents", []))


class SudoersTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        # install_wake_grant reads the username through os.getuid + pwd —
        # both POSIX-only, like the posix_uid patch in test_install; fake
        # them so Windows runners take the same path.
        getuid = mock.patch("awewarm.install.os.getuid", return_value=501, create=True)
        getuid.start()
        self.addCleanup(getuid.stop)
        fake_pwd = SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_name="tester"))
        pwd_module = mock.patch.dict(sys.modules, {"pwd": fake_pwd})
        pwd_module.start()
        self.addCleanup(pwd_module.stop)

    def test_rule_is_scoped_to_wake_events_only(self):
        self.assertEqual(
            install.sudoers_rule("peng"),
            "peng ALL=(root) NOPASSWD: /usr/bin/pmset schedule wakeorpoweron *, "
            "/usr/bin/pmset schedule cancel wakeorpoweron *\n",
        )

    def test_install_validates_then_places_the_file(self):
        with mock.patch("awewarm.install._sudo_cmd", return_value=True) as sudo:
            self.assertTrue(install.install_wake_grant())
            argvs = [call.args[0] for call in sudo.call_args_list]
            self.assertIn("visudo", argvs[0])
            self.assertEqual(argvs[0][0], "visudo")
            self.assertEqual(argvs[0][1], "-cf")
            place = argvs[1]
            self.assertEqual(place[0], "install")
            self.assertIn("0440", place)
            self.assertEqual(place[-1], str(install.SUDOERS_PATH))

    def test_install_dies_when_visudo_rejects(self):
        with mock.patch("awewarm.install._sudo_cmd", return_value=False):
            with self.assertRaises(SystemExit):
                install.install_wake_grant()

    def test_uninstall_removes_the_file(self):
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install._sudo_cmd", return_value=True) as sudo:
            self.assertTrue(install.uninstall_wake_grant())
            self.assertEqual(
                sudo.call_args.args[0], ["rm", "-f", str(install.SUDOERS_PATH)]
            )

    def test_uninstall_noop_when_absent(self):
        with mock.patch("awewarm.install.wake_grant_installed", return_value=False), \
             mock.patch("awewarm.install._sudo_cmd") as sudo:
            self.assertFalse(install.uninstall_wake_grant())
            sudo.assert_not_called()


class TeardownTests(IsolatedTestCase):
    def _armed_state(self):
        install.save_state(
            {"connections": {}, "wakeEvents": ["08/22/26 06:35:00"]}
        )

    def test_cancels_events_and_removes_grant(self):
        self._armed_state()
        with mock.patch("awewarm.install._sudo_pmset", return_value=True) as sudo, \
             mock.patch("awewarm.install._live_wake_entries", return_value=set()), \
             mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install._sudo_cmd", return_value=True):
            self.assertEqual(install.teardown_wake_layer(), (1, 1, True))
            self.assertEqual(
                sudo.call_args.args[0],
                ["schedule", "cancel", "wakeorpoweron", "08/22/26 06:35:00"],
            )
        self.assertNotIn("wakeEvents", install.load_state())

    def test_failed_cancel_keeps_the_ledger_for_retry(self):
        self._armed_state()
        with mock.patch("awewarm.install._sudo_pmset", return_value=False), \
             mock.patch("awewarm.install._live_wake_entries", return_value=set()), \
             mock.patch("awewarm.install.wake_grant_installed", return_value=False):
            self.assertEqual(install.teardown_wake_layer(), (0, 1, False))
        self.assertEqual(install.load_state().get("wakeEvents"), ["08/22/26 06:35:00"])

    def test_uninstall_sweeps_orphans_beyond_the_ledger(self):
        # with the scheduler gone no tick ever converges again — orphans the
        # ledger never tracked must go in the same uninstall pass
        self._armed_state()
        with mock.patch("awewarm.install._sudo_pmset", return_value=True) as sudo, \
             mock.patch("awewarm.install._live_wake_entries", return_value={
                 ("08/22/2026 06:35:00", "wakeorpoweron", "pmset"),
                 ("08/23/2026 07:00:00", "wakeorpoweron", "pmset"),
             }), \
             mock.patch("awewarm.install.wake_grant_installed", return_value=True), \
             mock.patch("awewarm.install._sudo_cmd", return_value=True):
            self.assertEqual(install.teardown_wake_layer(), (2, 2, True))
            cancels = [call.args[0] for call in sudo.call_args_list]
            self.assertIn(["schedule", "cancel", "wakeorpoweron", "08/23/26 07:00:00"], cancels)
        self.assertNotIn("wakeEvents", install.load_state())


class WindowsIntervalWakeTests(IsolatedTestCase):
    def test_ps1_registers_culture_invariant_once_triggers(self):
        script = install.build_iwake_ps1(
            "/x/awewarm.exe", [datetime(2026, 8, 22, 5, 50)]
        )
        self.assertIn("'2026-08-22T05:50:00'", script)
        self.assertIn("ParseExact($t, 'yyyy-MM-ddTHH:mm:ss', $null)", script)
        self.assertIn("New-ScheduledTaskTrigger -Once -At $at", script)
        self.assertIn(install.IWAKE_TASK_PREFIX, script)
        self.assertIn("-WakeToRun", script)

    def test_sync_registers_missing_and_deletes_stale(self):
        conn = plan_connection(mode="interval", window_status="user-confirmed", duration=300)
        state = _interval_state(next_due="2026-08-22T05:50:00+08:00")
        with mock.patch("awewarm.install.sys.platform", "win32"), \
             mock.patch("awewarm.install._task_keys",
                        side_effect=[{"20260101000000"}, {"20260101000000"}]) as keys, \
             mock.patch("awewarm.install._run_powershell", return_value=ok_run()) as ps, \
             mock.patch("awewarm.install.resolve_exe", return_value="/x/awewarm.exe"), \
             mock.patch("awewarm.install._schtasks", return_value=ok_run()) as schtasks:
            self.assertTrue(install.sync_wake_events(_config(glm=conn), state, FRIDAY))
            ps.assert_called_once()
            deletes = [call.args[0] for call in schtasks.call_args_list]
            self.assertTrue(
                any("/Delete" in argv and install.IWAKE_TASK_PREFIX + "20260101000000" in argv
                    for argv in deletes)
            )
            keys.assert_called_with(install.IWAKE_TASK_PREFIX)

    def test_sync_noop_when_in_sync(self):
        conn = plan_connection(mode="interval", window_status="user-confirmed", duration=300)
        state = _interval_state(next_due="2026-08-22T05:50:00+08:00")
        with mock.patch("awewarm.install.sys.platform", "win32"), \
             mock.patch("awewarm.install._task_keys", return_value={"20260822055000"}), \
             mock.patch("awewarm.install._run_powershell") as ps:
            self.assertFalse(install.sync_wake_events(_config(glm=conn), state, FRIDAY))
            ps.assert_not_called()

    def test_fixed_only_config_arms_nothing(self):
        # fixed slots keep their static daily tasks — the dynamic layer is interval-only
        state = {"connections": {}}
        with mock.patch("awewarm.install.sys.platform", "win32"), \
             mock.patch("awewarm.install._task_keys", return_value=set()) as keys, \
             mock.patch("awewarm.install._run_powershell") as ps:
            self.assertFalse(
                install.sync_wake_events(_config(glm=plan_connection(fixed_at=("06:35",))), state, FRIDAY)
            )
            ps.assert_not_called()


class StatusFooterTests(IsolatedTestCase):
    def _write_config(self):
        data = cfg.empty_config()
        data["connections"]["glm"] = plan_connection(fixed_at=("05:50",))
        cfg.save_config(data)

    def test_footer_shows_layer_off_without_grant(self):
        self._write_config()
        with mock.patch("awewarm.install.wake_grant_installed", return_value=False):
            result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Wake layer: off — lid-closed sleep fires late (enable: awewarm scheduler install --wake)", result.output)

    def test_footer_shows_armed_events_with_grant(self):
        self._write_config()
        cfg.save_state({"connections": {}, "wakeEvents": ["08/22/26 05:50:00"]})
        with mock.patch("awewarm.install.wake_grant_installed", return_value=True):
            result = invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Wake layer: enabled — 1 RTC wake(s) armed, next 08-22 05:50", result.output)


if __name__ == "__main__":
    unittest.main()
