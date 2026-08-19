import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from helpers import account_connection

from awewarm import schedule
from awewarm.config import default_conn_state

TZ = ZoneInfo("Asia/Taipei")
WEDNESDAY = date(2026, 8, 19)  # a weekday
SATURDAY = date(2026, 8, 22)


def at(day, hhmm, tz=TZ):
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


class ParseTests(unittest.TestCase):
    def test_parse_ts_roundtrip(self):
        moment = at(WEDNESDAY, "06:35")
        self.assertEqual(schedule.parse_ts(schedule.iso(moment)), moment)

    def test_parse_ts_accepts_z_suffix(self):
        self.assertIsNotNone(schedule.parse_ts("2026-08-19T06:35:00Z"))

    def test_parse_ts_rejects_naive_and_garbage(self):
        self.assertIsNone(schedule.parse_ts("2026-08-19T06:35:00"))
        self.assertIsNone(schedule.parse_ts("nope"))
        self.assertIsNone(schedule.parse_ts(None))


class FixedTests(unittest.TestCase):
    def plan(self, conn_state, now, **kwargs):
        return schedule.plan_actions(account_connection(**kwargs), conn_state, now)

    def test_fires_exactly_at_slot(self):
        actions = self.plan(default_conn_state(), at(WEDNESDAY, "06:35"))
        self.assertEqual(
            actions,
            [{"type": "activate", "reason": "fixed", "slot": "06:35", "slotAt": at(WEDNESDAY, "06:35")}],
        )

    def test_nothing_before_slot(self):
        actions = self.plan(default_conn_state(), at(WEDNESDAY, "06:34"), mode="fixed")
        self.assertEqual(actions, [])

    def test_fires_within_catchup_window(self):
        actions = self.plan(default_conn_state(), at(WEDNESDAY, "07:00"), mode="fixed")
        self.assertEqual(actions[0]["type"], "activate")
        self.assertEqual(actions[0]["slot"], "06:35")

    def test_beyond_catchup_marks_skipped_and_never_fires(self):
        conn_state = default_conn_state()
        now = at(WEDNESDAY, "08:00")  # 85 minutes after the slot
        actions = self.plan(conn_state, now, mode="fixed")
        self.assertEqual(
            actions,
            [{"type": "skip-slot", "slot": "06:35", "why": "past-catchup"}],
        )
        schedule.record_skip(conn_state, now, "06:35", "past-catchup")
        self.assertEqual(self.plan(conn_state, now, mode="fixed"), [])

    def test_completed_slot_not_refired_same_day(self):
        conn_state = default_conn_state()
        conn = account_connection()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "06:35"), "fixed", slot="06:35")
        self.assertEqual(self.plan(conn_state, at(WEDNESDAY, "07:00")), [])

    def test_second_slot_fires_independently(self):
        conn_state = default_conn_state()
        conn = account_connection(fixed_at=("06:35", "11:40"))
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "06:35"), "fixed", slot="06:35")
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "11:40"))
        self.assertEqual(actions[0]["slot"], "11:40")

    def test_weekday_rule_skips_saturday(self):
        actions = self.plan(default_conn_state(), at(SATURDAY, "06:35"), mode="fixed")
        self.assertEqual(actions, [])

    def test_every_day_rule_fires_saturday(self):
        actions = self.plan(default_conn_state(), at(SATURDAY, "06:35"), mode="fixed", days="every-day")
        self.assertEqual(actions[0]["type"], "activate")

    def test_recent_success_satisfies_nearby_slot(self):
        conn_state = default_conn_state()
        conn = account_connection(fixed_at=("06:45", "11:40"))
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "06:30"), "interval")
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "06:45"))
        self.assertEqual(
            [a for a in actions if a["type"] == "skip-slot"],
            [{"type": "skip-slot", "slot": "06:45", "why": "recently-activated"}],
        )
        self.assertTrue(all(a["type"] != "activate" for a in actions))

    def test_unsorted_slot_list_activates_earliest_due(self):
        conn = account_connection(fixed_at=("16:45", "06:35"), days="every-day")
        actions = schedule.plan_actions(conn, default_conn_state(), at(WEDNESDAY, "07:00"))
        activates = [a for a in actions if a["type"] == "activate"]
        self.assertEqual(len(activates), 1)
        self.assertEqual(activates[0]["slot"], "06:35")


class IntervalTests(unittest.TestCase):
    def interval_conn(self, **kwargs):
        return account_connection(mode="interval", fixed_at=(), **kwargs)

    def test_no_anchor_fires_first_anchor(self):
        actions = schedule.plan_actions(self.interval_conn(), default_conn_state(), at(WEDNESDAY, "09:00"))
        self.assertEqual(actions, [{"type": "activate", "reason": "first-anchor"}])

    def test_not_due_yet(self):
        conn_state = default_conn_state()
        conn = self.interval_conn()
        success = at(WEDNESDAY, "07:05")
        schedule.record_success(conn_state, conn, success, "interval")
        # due = 07:05 + 300min + 75s (+ jitter) ≈ 12:06:45
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "09:00")), [])

    def test_due_fires_renewal(self):
        conn_state = default_conn_state()
        conn = self.interval_conn()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:05"), "interval")
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "12:10"))
        self.assertEqual(actions[0]["reason"], "interval")

    def test_degraded_interval_blocked(self):
        conn_state = default_conn_state()
        conn = self.interval_conn()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:05"), "interval")
        conn_state["intervalDisabledAt"] = schedule.iso(at(WEDNESDAY, "08:00"))
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "12:10")), [])

    def test_compute_next_due_formula(self):
        conn = self.interval_conn()
        success = at(WEDNESDAY, "07:05")
        due = schedule.compute_next_due(conn, success, jitter_seconds=0)
        # 07:05 + 300 min = 12:05, plus 75 s of grace = 12:06:15
        self.assertEqual(due, at(WEDNESDAY, "12:05") + timedelta(seconds=75))
        due_jittered = schedule.compute_next_due(conn, success, jitter_seconds=10)
        self.assertEqual(due_jittered, due + timedelta(seconds=10))

    def test_compute_next_due_random_jitter_within_bounds(self):
        conn = self.interval_conn()
        success = at(WEDNESDAY, "07:05")
        base = schedule.compute_next_due(conn, success, jitter_seconds=0)
        for _ in range(20):
            due = schedule.compute_next_due(conn, success)
            self.assertTrue(base <= due <= base + timedelta(seconds=30))


class ModeSeparationTests(unittest.TestCase):
    def test_fixed_mode_ignores_interval_renewal(self):
        # a past success does not chain anything in fixed mode: nextDueAt stays
        # unset and slots fire purely by wall-clock
        conn = account_connection(mode="fixed")
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "00:00"), "manual")
        self.assertIsNone(conn_state["nextDueAt"])
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "06:35"))
        self.assertEqual([a["reason"] for a in actions], ["fixed"])

    def test_interval_mode_ignores_fixed_slots(self):
        # a configured slot time alone must not fire in interval mode
        conn = account_connection(mode="interval", fixed_at=("06:35",))
        actions = schedule.plan_actions(conn, default_conn_state(), at(WEDNESDAY, "06:35"))
        self.assertEqual([a["reason"] for a in actions], ["first-anchor"])


class FailurePolicyTests(unittest.TestCase):
    def test_three_failures_degrade_and_success_rearms(self):
        conn = account_connection(mode="interval", fixed_at=())
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:00"), "interval")
        for i in range(3):
            schedule.record_failure(conn_state, at(WEDNESDAY, "12:07") + timedelta(minutes=i), "interval", "boom")
        self.assertIsNotNone(conn_state["intervalDisabledAt"])
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "13:00"), "manual")
        self.assertIsNone(conn_state["intervalDisabledAt"])
        self.assertEqual(conn_state["consecutiveFailures"], 0)

    def test_two_failures_do_not_degrade(self):
        conn_state = default_conn_state()
        for i in range(2):
            schedule.record_failure(conn_state, at(WEDNESDAY, "12:07") + timedelta(minutes=i), "interval", "boom")
        self.assertIsNone(conn_state["intervalDisabledAt"])

    def test_retry_throttle_after_failure(self):
        conn = account_connection()
        conn_state = default_conn_state()
        conn_state["lastResult"] = "failure"
        conn_state["lastAttemptAt"] = schedule.iso(at(WEDNESDAY, "06:40"))
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "06:43")), [])
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "06:47"))
        self.assertEqual(actions[0]["type"], "activate")

    def test_degraded_interval_probes_after_cooldown(self):
        conn = account_connection(mode="interval", fixed_at=())
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:00"), "interval")
        for i in range(3):
            schedule.record_failure(conn_state, at(WEDNESDAY, "12:07") + timedelta(minutes=i), "interval", "boom")
        disabled_at = schedule.parse_ts(conn_state["intervalDisabledAt"])
        # frozen inside the 300-min cooldown, free to probe one minute after it
        frozen = disabled_at + timedelta(minutes=299)
        self.assertEqual(schedule.plan_actions(conn, conn_state, frozen), [])
        thawed = disabled_at + timedelta(minutes=301)
        actions = schedule.plan_actions(conn, conn_state, thawed)
        self.assertEqual([a["reason"] for a in actions], ["interval"])

    def test_failed_probe_refreezes_for_another_cooldown(self):
        conn = account_connection(mode="interval", fixed_at=())
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:00"), "interval")
        conn_state["intervalDisabledAt"] = schedule.iso(at(WEDNESDAY, "08:00"))
        conn_state["consecutiveFailures"] = 3  # already degraded; this is the probe
        schedule.record_failure(conn_state, at(WEDNESDAY, "13:01"), "interval", "boom")
        re_frozen = schedule.parse_ts(conn_state["intervalDisabledAt"])
        self.assertEqual(re_frozen, at(WEDNESDAY, "13:01"))
        self.assertEqual(
            schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "13:10")), []
        )
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "18:02"))
        self.assertEqual([a["reason"] for a in actions], ["interval"])

    def test_history_capped(self):
        conn = account_connection(mode="interval", fixed_at=())
        conn_state = default_conn_state()
        for i in range(30):
            schedule.record_failure(conn_state, at(WEDNESDAY, "09:00") + timedelta(minutes=i), "interval", "x")
        self.assertEqual(len(conn_state["history"]), 20)


class HousekeepingTests(unittest.TestCase):
    def test_prune_drops_old_slot_days(self):
        conn_state = default_conn_state()
        conn_state["completedSlots"]["2026-01-01"] = ["06:35"]
        conn_state["completedSlots"]["2026-08-19"] = ["06:35"]
        schedule.prune_state(conn_state, at(WEDNESDAY, "12:00"))
        self.assertNotIn("2026-01-01", conn_state["completedSlots"])
        self.assertIn("2026-08-19", conn_state["completedSlots"])


class EdgeTests(unittest.TestCase):
    def test_dst_nonexistent_slot_does_not_crash(self):
        tz = ZoneInfo("America/New_York")
        spring_forward = date(2026, 3, 8)
        conn = account_connection(mode="fixed", days="every-day")
        actions = schedule.plan_actions(conn, default_conn_state(), datetime(2026, 3, 8, 5, 0, tzinfo=tz))
        self.assertIsInstance(actions, list)

    def test_next_due_fixed_future(self):
        conn = account_connection(mode="fixed")
        due_at, kind = schedule.next_due(conn, default_conn_state(), at(WEDNESDAY, "05:00"))
        self.assertEqual((due_at, kind), (at(WEDNESDAY, "06:35"), "fixed"))

    def test_next_due_skips_today_when_past_catchup(self):
        conn = account_connection(mode="fixed")
        due_at, _ = schedule.next_due(conn, default_conn_state(), at(WEDNESDAY, "23:00"))
        self.assertEqual(due_at, at(date(2026, 8, 20), "06:35"))

    def test_next_due_ignores_already_skipped_slot(self):
        # interval mode never advertises fixed slots; renewal is the only due
        conn = account_connection(mode="interval", fixed_at=("06:35",))
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "06:30"), "interval")
        schedule.record_skip(conn_state, at(WEDNESDAY, "06:35"), "06:35", "recently-activated")
        due_at, kind = schedule.next_due(conn, conn_state, at(WEDNESDAY, "06:40"))
        self.assertEqual(kind, "interval")
        # due = 06:30 + 300 min + 75 s grace (+ up to 30 s jitter) ≈ 11:31
        self.assertGreaterEqual(due_at, at(WEDNESDAY, "11:31"))
        self.assertLess(due_at, at(WEDNESDAY, "11:32"))

    def test_next_due_picks_earliest_slot_from_unsorted_list(self):
        conn = account_connection(mode="fixed", fixed_at=("16:45", "11:40"))
        due_at, kind = schedule.next_due(conn, default_conn_state(), at(WEDNESDAY, "05:00"))
        self.assertEqual((due_at, kind), (at(WEDNESDAY, "11:40"), "fixed"))

    def test_next_due_interval_without_anchor(self):
        conn = account_connection(mode="interval", fixed_at=())
        now = at(WEDNESDAY, "09:00")
        due_at, kind = schedule.next_due(conn, default_conn_state(), now)
        self.assertEqual(due_at, now)
        self.assertIn("first anchor", kind)

    def test_next_due_degraded_interval_advertises_thaw(self):
        conn = account_connection(mode="interval", fixed_at=())
        conn_state = default_conn_state()
        conn_state["intervalDisabledAt"] = schedule.iso(at(WEDNESDAY, "08:00"))
        due_at, kind = schedule.next_due(conn, conn_state, at(WEDNESDAY, "09:00"))
        self.assertEqual(due_at, at(WEDNESDAY, "13:00"))
        self.assertIn("probing", kind)


if __name__ == "__main__":
    unittest.main()


class WindowOverrideNoticeTests(unittest.TestCase):
    def verified(self, minutes):
        return {"status": "verified", "durationMinutes": minutes, "evidence": "builtin-provider"}

    def test_none_without_verified_window(self):
        self.assertIsNone(schedule.window_override_notice({"status": "unknown"}, 240))
        self.assertIsNone(schedule.window_override_notice(None, 240))

    def test_none_when_matching_or_invalid(self):
        self.assertIsNone(schedule.window_override_notice(self.verified(300), 300))
        self.assertIsNone(schedule.window_override_notice(self.verified(None), 240))

    def test_shorter_duration_lands_inside_old_window(self):
        notice = schedule.window_override_notice(self.verified(300), 240)
        self.assertIn("inside the still-open window", notice)
        self.assertIn("60 min cold gap", notice)

    def test_shorter_duration_covered_by_grace(self):
        # 299 min + 75s grace clears the 300-min window: still differs, warn softly.
        notice = schedule.window_override_notice(self.verified(300), 299)
        self.assertIn("every ~299 min", notice)
        self.assertNotIn("cold gap", notice)

    def test_longer_duration_warns_softly(self):
        notice = schedule.window_override_notice(self.verified(300), 360)
        self.assertIn("every ~360 min", notice)
        self.assertNotIn("cold gap", notice)


class UserAnchorTests(unittest.TestCase):
    def conn(self):
        return account_connection(mode="interval", fixed_at=())

    def test_anchor_infers_open_time_from_reset(self):
        conn = self.conn()
        cs = default_conn_state()
        reset = at(WEDNESDAY, "13:27")
        schedule.apply_user_anchor(cs, conn, reset)
        # 300-min window: opened 08:27, renewal due reset + grace (no jitter)
        self.assertEqual(schedule.parse_ts(cs["lastActivationAt"]), at(WEDNESDAY, "08:27"))
        due = schedule.parse_ts(cs["nextDueAt"])
        self.assertEqual(due, at(WEDNESDAY, "13:27") + timedelta(seconds=75))

    def test_no_first_anchor_fire_before_reset(self):
        conn = self.conn()
        cs = default_conn_state()
        schedule.apply_user_anchor(cs, conn, at(WEDNESDAY, "13:27"))
        actions = schedule.plan_actions(conn, cs, at(WEDNESDAY, "13:28") - timedelta(seconds=1))
        self.assertEqual(actions, [])

    def test_fires_right_after_reset(self):
        conn = self.conn()
        cs = default_conn_state()
        schedule.apply_user_anchor(cs, conn, at(WEDNESDAY, "13:27"))
        actions = schedule.plan_actions(conn, cs, at(WEDNESDAY, "13:29"))
        self.assertEqual([a["type"] for a in actions], ["activate"])
        self.assertEqual(actions[0]["reason"], "interval")


class GridTimesTests(unittest.TestCase):
    def test_300min_grid_from_default_anchor(self):
        self.assertEqual(
            schedule.grid_times("06:35", 300), ["06:35", "11:40", "16:45", "21:50"]
        )

    def test_grid_from_late_night_reset_covers_full_day(self):
        # reset 01:14 → five windows reach 21:34
        self.assertEqual(
            schedule.grid_times("01:14", 300), ["01:14", "06:19", "11:24", "16:29", "21:34"]
        )

    def test_halfday_window_gives_two_slots(self):
        self.assertEqual(schedule.grid_times("09:00", 720), ["09:00", "21:05"])

    def test_short_windows_return_no_grid(self):
        self.assertEqual(schedule.grid_times("06:00", 60), [])

    def test_invalid_anchor_returns_no_grid(self):
        self.assertEqual(schedule.grid_times("6am", 300), [])
        self.assertEqual(schedule.grid_times(None, 300), [])
