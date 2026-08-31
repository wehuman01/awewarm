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

    def test_catchup_crosses_midnight(self):
        conn_state = default_conn_state()
        actions = self.plan(
            conn_state,
            at(SATURDAY, "00:05"),
            mode="fixed",
            fixed_at=("23:50",),
            days="every-day",
        )
        self.assertEqual(actions[0]["type"], "activate")
        self.assertEqual(actions[0]["slotAt"], at(SATURDAY - timedelta(days=1), "23:50"))

    def test_weekday_catchup_from_friday_fires_on_saturday(self):
        actions = self.plan(
            default_conn_state(),
            at(SATURDAY, "00:05"),
            mode="fixed",
            fixed_at=("23:50",),
            days="weekday",
        )
        self.assertEqual(actions[0]["type"], "activate")
        self.assertEqual(actions[0]["slotAt"], at(SATURDAY - timedelta(days=1), "23:50"))

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

    def test_start_gate_holds_slot_then_fires_within_catchup(self):
        conn_state = default_conn_state()
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "07:00"))
        conn = account_connection()  # slot 06:35
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "06:36")), [])
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "07:00"))
        self.assertEqual(actions[0]["type"], "activate")
        self.assertEqual(actions[0]["slot"], "06:35")

    def test_start_gate_beyond_catchup_skips_slot_after_lift(self):
        conn_state = default_conn_state()
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "09:00"))
        actions = self.plan(conn_state, at(WEDNESDAY, "09:00"))  # 06:35 is 145 min past
        self.assertEqual(
            actions,
            [{"type": "skip-slot", "slot": "06:35", "why": "past-catchup"}],
        )

    def test_start_gate_clears_on_fixed_success(self):
        conn_state = default_conn_state()
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "07:00"))
        schedule.record_success(conn_state, account_connection(), at(WEDNESDAY, "07:00"), "fixed", slot="06:35")
        self.assertIsNone(conn_state.get("deferUntil"))

    def test_next_due_fixed_capped_by_gate(self):
        conn_state = default_conn_state()
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "07:00"))
        moment, kind = schedule.next_due(account_connection(), conn_state, at(WEDNESDAY, "06:00"))
        self.assertEqual(moment, at(WEDNESDAY, "07:00"))
        self.assertEqual(kind, "fixed (deferred)")


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

    def test_start_gate_blocks_first_anchor(self):
        conn_state = default_conn_state()
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "09:00"))
        self.assertEqual(schedule.plan_actions(self.interval_conn(), conn_state, at(WEDNESDAY, "08:59")), [])

    def test_start_gate_blocks_due_chain(self):
        conn_state = default_conn_state()
        conn = self.interval_conn()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "02:00"), "interval")
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "09:00"))  # set after: success clears it
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "08:00")), [])
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "09:30"))
        self.assertEqual(actions[0]["reason"], "interval")

    def test_start_gate_clears_on_success(self):
        conn_state = default_conn_state()
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "09:00"))
        schedule.record_success(conn_state, self.interval_conn(), at(WEDNESDAY, "09:01"), "first-anchor")
        self.assertIsNone(conn_state.get("deferUntil"))

    def test_user_anchor_clears_start_gate(self):
        conn_state = default_conn_state()
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "09:00"))
        schedule.apply_user_anchor(conn_state, self.interval_conn(), at(WEDNESDAY, "13:27"))
        self.assertIsNone(conn_state.get("deferUntil"))

    def test_next_due_shows_deferred_first_anchor(self):
        conn_state = default_conn_state()
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "09:00"))
        moment, kind = schedule.next_due(self.interval_conn(), conn_state, at(WEDNESDAY, "08:00"))
        self.assertEqual(moment, at(WEDNESDAY, "09:00"))
        self.assertIn("first anchor", kind)

    def test_next_due_chain_capped_by_gate(self):
        conn_state = default_conn_state()
        conn = self.interval_conn()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "02:00"), "interval")
        conn_state["deferUntil"] = schedule.iso(at(WEDNESDAY, "09:00"))
        moment, kind = schedule.next_due(conn, conn_state, at(WEDNESDAY, "08:00"))
        self.assertEqual(moment, at(WEDNESDAY, "09:00"))
        self.assertEqual(kind, "interval")

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
    def interval_conn(self, **kwargs):
        return account_connection(mode="interval", fixed_at=(), **kwargs)

    def fixed_conn(self, **kwargs):
        return account_connection(mode="fixed", **kwargs)

    def interval_node(self, due):
        return {"key": f"interval {schedule.iso(due)}", "dueAt": due}

    def fixed_node(self, day, hhmm):
        return {
            "key": f"{day.strftime('%Y-%m-%d')} {hhmm}",
            "dueAt": at(day, hhmm),
            "slot": hhmm,
        }

    def lose_node(self, conn, conn_state, node, first_attempt_at, attempts=5):
        kind = "fixed" if "slot" in node else "interval"
        for i in range(attempts):
            schedule.record_failure(
                conn_state, conn, first_attempt_at + timedelta(minutes=5 * i),
                kind, "boom", node=node,
            )

    def test_three_lost_interval_nodes_degrade(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:00"), "interval")
        for n in range(3):
            due = at(WEDNESDAY, "12:05") + timedelta(hours=6 * n)
            self.lose_node(conn, conn_state, self.interval_node(due), due)
            if n < 2:
                self.assertIsNone(conn_state["degradedAt"])
        self.assertIsNotNone(conn_state["degradedAt"])
        self.assertEqual(conn_state["failedNodes"], 3)

    def test_two_lost_nodes_do_not_degrade(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        for n in range(2):
            due = at(WEDNESDAY, "12:05") + timedelta(hours=6 * n)
            self.lose_node(conn, conn_state, self.interval_node(due), due)
        self.assertIsNone(conn_state["degradedAt"])
        self.assertEqual(conn_state["failedNodes"], 2)

    def test_success_rearms_whole_ladder(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:00"), "interval")
        for n in range(3):
            due = at(WEDNESDAY, "12:05") + timedelta(hours=6 * n)
            self.lose_node(conn, conn_state, self.interval_node(due), due)
        conn_state["degradedFailedNodes"] = 2
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "20:00"), "manual")
        self.assertIsNone(conn_state["degradedAt"])
        self.assertEqual(conn_state["failedNodes"], 0)
        self.assertEqual(conn_state["degradedFailedNodes"], 0)

    def test_degraded_interval_waits_one_window_then_probes(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:00"), "interval")
        degraded_at = at(WEDNESDAY, "18:29")
        for n in range(3):
            due = at(WEDNESDAY, "12:05") + timedelta(hours=2 * n)
            self.lose_node(conn, conn_state, self.interval_node(due), due)
        conn_state["degradedAt"] = schedule.iso(degraded_at)  # entered here
        conn_state["nextProbeAt"] = None
        still_frozen = degraded_at + timedelta(minutes=299)
        self.assertEqual(schedule.plan_actions(conn, conn_state, still_frozen), [])
        thawed = degraded_at + timedelta(minutes=301)
        actions = schedule.plan_actions(conn, conn_state, thawed)
        self.assertEqual([a["reason"] for a in actions], ["interval"])

    def test_failed_degraded_probe_pushes_next_probe(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        conn_state["degradedAt"] = schedule.iso(at(WEDNESDAY, "08:00"))
        probe_at = at(WEDNESDAY, "13:00")
        schedule.record_failure(
            conn_state, conn, probe_at, "interval", "boom",
            node=self.interval_node(probe_at),
        )
        self.assertEqual(conn_state["degradedFailedNodes"], 1)
        self.assertEqual(
            schedule.parse_ts(conn_state["nextProbeAt"]),
            probe_at + timedelta(minutes=300),
        )
        self.assertEqual(schedule.plan_actions(conn, conn_state, probe_at + timedelta(minutes=10)), [])

    def test_degraded_interval_three_failed_probes_auto_disable(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        conn_state["degradedAt"] = schedule.iso(at(WEDNESDAY, "08:00"))
        for n in range(3):
            probe_at = at(WEDNESDAY, "13:00") + timedelta(hours=5 * n)
            schedule.record_failure(
                conn_state, conn, probe_at, "interval", "boom",
                node=self.interval_node(probe_at),
            )
        self.assertIsNotNone(conn_state["autoDisabledAt"])
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(date(2026, 8, 20), "09:00")), [])

    def test_fixed_node_lost_marks_slot_skipped(self):
        conn = self.fixed_conn()
        conn_state = default_conn_state()
        self.lose_node(conn, conn_state, self.fixed_node(WEDNESDAY, "06:35"), at(WEDNESDAY, "06:35"))
        self.assertEqual(conn_state["failedNodes"], 1)
        self.assertIn("06:35", conn_state["skippedSlots"]["2026-08-19"])
        # the lost slot never refires, even inside its catch-up window
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "06:50")), [])

    def test_fixed_three_lost_nodes_degrade_then_single_shot(self):
        conn = self.fixed_conn(fixed_at=("06:35", "11:40", "16:45", "21:50"), days="every-day")
        conn_state = default_conn_state()
        for hhmm in ("06:35", "11:40", "16:45"):
            self.lose_node(conn, conn_state, self.fixed_node(WEDNESDAY, hhmm), at(WEDNESDAY, hhmm))
        self.assertIsNotNone(conn_state["degradedAt"])
        # degraded: the next slot fires exactly once
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "21:50"))
        self.assertEqual([a["reason"] for a in actions], ["fixed"])
        schedule.record_failure(
            conn_state, conn, at(WEDNESDAY, "21:50"), "fixed", "boom",
            node=self.fixed_node(WEDNESDAY, "21:50"),
        )
        self.assertEqual(conn_state["degradedFailedNodes"], 1)
        self.assertIn("21:50", conn_state["skippedSlots"]["2026-08-19"])
        # single shot: no catch-up retry inside the window
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "21:58")), [])

    def test_interval_node_deadline_expires_into_node_lost(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:00"), "interval")
        due = at(WEDNESDAY, "12:05")
        node = self.interval_node(due)
        schedule.record_failure(conn_state, conn, due, "interval", "boom", node=node)
        schedule.record_failure(conn_state, conn, due + timedelta(minutes=5), "interval", "boom", node=node)
        late_tick = due + timedelta(minutes=31)
        self.assertEqual(
            schedule.plan_actions(conn, conn_state, late_tick),
            [{"type": "node-lost"}],
        )
        schedule.close_lost_node(conn_state, conn, late_tick, "catch-up window expired")
        self.assertEqual(conn_state["failedNodes"], 1)
        self.assertIsNone(conn_state["nodeKey"])

    def test_fixed_slot_expired_without_attempts_is_not_a_lost_node(self):
        conn = self.fixed_conn()
        conn_state = default_conn_state()  # machine slept through the slot
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "07:10"))
        self.assertEqual(actions, [{"type": "skip-slot", "slot": "06:35", "why": "past-catchup"}])
        self.assertNotIn("lost", actions[0])  # tick skips the ladder move
        self.assertEqual(conn_state["failedNodes"], 0)

    def test_fixed_failed_slot_expires_with_lost_flag(self):
        conn = self.fixed_conn()
        conn_state = default_conn_state()
        node = self.fixed_node(WEDNESDAY, "06:35")
        schedule.record_failure(conn_state, conn, at(WEDNESDAY, "06:36"), "fixed", "boom", node=node)
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "07:10"))
        self.assertEqual(actions[0]["type"], "skip-slot")
        self.assertTrue(actions[0].get("lost"))

    def test_fixed_failed_slot_closes_after_deadline_crosses_midnight(self):
        friday = SATURDAY - timedelta(days=1)
        conn = self.fixed_conn(fixed_at=("23:50",), days="every-day")
        conn_state = default_conn_state()
        node = self.fixed_node(friday, "23:50")
        schedule.record_failure(
            conn_state, conn, at(friday, "23:55"), "fixed", "boom", node=node
        )
        actions = schedule.plan_actions(conn, conn_state, at(SATURDAY, "00:21"))
        self.assertTrue(actions[0].get("lost"))
        schedule.dispatch_actions(conn, conn_state, at(SATURDAY, "00:21"), lambda *_: None)
        self.assertIsNone(conn_state["nodeKey"])
        self.assertIn("23:50", conn_state["skippedSlots"][friday.isoformat()])

    def test_manual_failure_never_counts(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        for i in range(6):
            schedule.record_failure(conn_state, conn, at(WEDNESDAY, "12:07") + timedelta(minutes=i), "manual", "boom")
        self.assertIsNone(conn_state["nodeKey"])
        self.assertEqual(conn_state["failedNodes"], 0)
        self.assertIsNone(conn_state["degradedAt"])

    def test_legacy_state_migrates_on_plan(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        schedule.record_success(conn_state, conn, at(WEDNESDAY, "07:00"), "interval")
        conn_state["intervalDisabledAt"] = schedule.iso(at(WEDNESDAY, "08:00"))
        conn_state["consecutiveFailures"] = 3
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "12:10")), [])
        self.assertEqual(conn_state["degradedAt"], schedule.iso(at(WEDNESDAY, "08:00")))
        self.assertNotIn("intervalDisabledAt", conn_state)
        self.assertNotIn("consecutiveFailures", conn_state)

    def test_retry_throttle_after_failure(self):
        conn = self.fixed_conn()
        conn_state = default_conn_state()
        conn_state["lastResult"] = "failure"
        conn_state["lastAttemptAt"] = schedule.iso(at(WEDNESDAY, "06:40"))
        self.assertEqual(schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "06:43")), [])
        actions = schedule.plan_actions(conn, conn_state, at(WEDNESDAY, "06:47"))
        self.assertEqual(actions[0]["type"], "activate")

    def test_history_capped(self):
        conn = self.interval_conn()
        conn_state = default_conn_state()
        for i in range(30):
            schedule.record_failure(conn_state, conn, at(WEDNESDAY, "09:00") + timedelta(minutes=i), "interval", "x")
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
            schedule.grid_times("06:35", 300),
            ["02:55", "06:35", "11:40", "16:45", "21:50"],
        )

    def test_grid_from_late_night_reset_covers_full_day(self):
        # reset 01:14 → five windows reach 21:34
        self.assertEqual(
            schedule.grid_times("01:14", 300), ["01:14", "06:19", "11:24", "16:29", "21:34"]
        )

    def test_halfday_window_gives_two_slots(self):
        self.assertEqual(schedule.grid_times("09:00", 720), ["09:00", "21:05"])

    def test_late_anchor_wraps_past_midnight(self):
        self.assertEqual(
            schedule.grid_times("23:00", 300),
            ["04:05", "09:10", "14:15", "19:20", "23:00"],
        )

    def test_short_window_grid_fills_the_whole_day(self):
        # The old range(8) guard cut short-window grids off mid-day.
        self.assertEqual(
            schedule.grid_times("00:00", 120),
            ["00:00", "02:05", "04:10", "06:15", "08:20", "10:25",
             "12:30", "14:35", "16:40", "18:45", "20:50", "22:55"],
        )

    def test_short_windows_return_no_grid(self):
        self.assertEqual(schedule.grid_times("06:00", 60), [])

    def test_invalid_anchor_returns_no_grid(self):
        self.assertEqual(schedule.grid_times("6am", 300), [])
        self.assertEqual(schedule.grid_times(None, 300), [])
