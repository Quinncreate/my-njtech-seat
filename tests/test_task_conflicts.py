"""Old paused-task conflicts are checked only against an isolated fake provider."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from seat_core import Engine, Problem, Store

TEMP = ROOT.parents[1] / "tmp" / ROOT.name / "tests"
TEMP.mkdir(parents=True, exist_ok=True)


class TaskConflictTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEMP)
        self.store = Store(Path(self.directory.name) / "tasks.sqlite3")
        self.clock = [1800000000.0]
        self.engine = Engine(self.store, clock=lambda: self.clock[0])
        self.provider = self.engine.providers["demo"]
        self.calls = []
        for method in ("reserve", "cancel"):
            original = getattr(self.provider, method)

            def wrapped(*args, _method=method, _original=original):
                self.calls.append((_method, args[0]))
                return _original(*args)
            setattr(self.provider, method, wrapped)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def create(self, name="新任务", seat="1-002", **fields):
        task = self.engine.create({"name": name, "lib": 1, "seat": seat, **fields})
        return self.engine.get_task(task["id"])

    def paused(self, holding=False):
        old = self.create("较早的自习", "1-001")
        self.engine.action(old["id"], "start")
        if holding:
            self.engine.tick()
        self.engine.action(old["id"], "pause")
        return old

    def assert_blocked(self, new, old):
        before = list(self.calls)
        with self.assertRaises(Problem) as caught:
            self.engine.action(new["id"], "start")
        self.assertEqual(caught.exception.task_id, old["id"])
        self.assertIn(old["name"], str(caught.exception))
        self.assertIn(old["seat_name"], str(caught.exception))
        self.assertEqual(new["status"], "draft")
        self.assertEqual(self.calls, before)

    def test_empty_pause_is_retired_and_new_task_can_start(self):
        old = self.paused()
        new = self.create()
        self.engine.action(new["id"], "start")
        self.assertEqual(old["status"], "completed")
        self.assertEqual(new["status"], "recovering")
        self.assertEqual(self.calls, [])
        saved = next(t for t in self.store.snapshot()["tasks"] if t["id"] == old["id"])
        self.assertEqual(saved["status"], "completed")
        self.engine.tick()
        self.assertEqual(new["owner"], "A")
        self.assertEqual(self.calls, [("reserve", "A")])

    def test_expired_paused_reservation_does_not_keep_an_account_claim(self):
        old = self.paused(holding=True)
        self.clock[0] = old["expires"] + 1
        new = self.create()
        self.engine.action(new["id"], "start")
        self.assertEqual(old["status"], "completed")
        self.assertIsNone(old["owner"])
        self.assertEqual((old["next"], old["expires"]), (0, 0))
        self.assertEqual(self.calls, [("reserve", "A")])

    def test_live_observation_overrides_stale_free_account_cache(self):
        old = self.paused(holding=True)
        self.engine.accounts["demo"]["A"]["reservation"] = None
        self.assert_blocked(self.create(), old)
        self.assertEqual(old["status"], "paused")
        self.assertIn("A", self.store.data["demo_seats"])

    def test_fresh_free_observation_retires_stale_holder_without_cancel(self):
        old = self.paused(holding=True)
        self.store.data["demo_seats"].clear()
        new = self.create()
        self.engine.action(new["id"], "start")
        self.assertEqual(old["status"], "completed")
        self.assertEqual(self.calls, [("reserve", "A")])

    def test_offline_account_does_not_retire_paused_task(self):
        old, new = self.paused(), self.create()
        original = self.provider.observe

        def offline():
            states = original()
            states["B"]["ready"] = False
            return states
        self.provider.observe = offline
        self.assert_blocked(new, old)
        self.assertEqual(old["status"], "paused")

    def test_observation_failure_does_not_retire_paused_task(self):
        old, new = self.paused(), self.create()

        def unavailable():
            raise RuntimeError("isolated fake network failure")
        self.provider.observe = unavailable
        self.assert_blocked(new, old)
        self.assertEqual(old["status"], "paused")

    def test_pending_cancel_blocks_even_if_both_accounts_are_free(self):
        old, new = self.paused(), self.create()
        old["pending"] = {"kind": "cancel", "role": "A"}
        self.assert_blocked(new, old)
        self.assertIsNotNone(old["pending"])

    def test_unconfirmed_finish_blocks_even_if_accounts_are_free(self):
        old, new = self.paused(), self.create()
        old["finish_requested"] = True
        self.assert_blocked(new, old)
        self.assertEqual(old["status"], "paused")

    def test_unconfirmed_handoff_blocks_even_if_accounts_are_free(self):
        old, new = self.paused(), self.create()
        old["manual_handoff"] = {"from": "A", "paused": True}
        self.assert_blocked(new, old)
        self.assertEqual(old["status"], "paused")

    def test_scheduled_task_keeps_claim_and_error_names_it(self):
        old = self.create("明日自习", start=self.clock[0] + 86400)
        self.engine.action(old["id"], "start")
        self.assert_blocked(self.create(), old)
        self.assertEqual(old["status"], "scheduled")

    def test_active_conflict_does_not_partially_retire_empty_pauses(self):
        active = self.create("等待开始", start=self.clock[0] + 1000)
        self.engine.action(active["id"], "start")
        paused = self.create("旧暂停", "1-003")
        paused["status"] = "paused"
        self.assert_blocked(self.create(), active)
        self.assertEqual(paused["status"], "paused")

    def test_paused_task_in_other_mode_does_not_block(self):
        old = self.paused()
        old["mode"] = "live"
        new = self.create()
        self.engine.action(new["id"], "start")
        self.assertEqual(old["status"], "paused")
        self.assertEqual(new["status"], "recovering")

    def test_invalid_new_schedule_does_not_retire_empty_pause(self):
        old = self.paused()
        new = self.create(end=self.clock[0] + 10)
        self.clock[0] += 11
        with self.assertRaisesRegex(Problem, "结束时间已过"):
            self.engine.action(new["id"], "start")
        self.assertEqual(old["status"], "paused")

    def test_finish_conflict_identifies_the_other_task(self):
        old, new = self.paused(holding=True), self.create()
        new["status"] = "paused"
        with self.assertRaises(Problem) as caught:
            self.engine.action(new["id"], "finish")
        self.assertEqual(caught.exception.task_id, old["id"])
        self.assertEqual(old["status"], "paused")


if __name__ == "__main__":
    unittest.main()
