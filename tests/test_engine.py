import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from seat_core import ACTIVE, COOLDOWN, Engine, Problem, Store
from live_provider import LiveProvider, expiry

TEMP = ROOT.parents[1] / "tmp" / ROOT.name / "tests"
TEMP.mkdir(parents=True, exist_ok=True)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEMP)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.clock = [1800000000.0]
        self.store = Store(self.path)
        self.engine = Engine(self.store, clock=lambda: self.clock[0])
        self.provider = self.engine.providers["demo"]
        self.calls = []
        for method in ("reserve", "cancel"):
            original = getattr(self.provider, method)

            def wrapped(*args, _original=original, _method=method):
                self.calls.append((_method, args[0], self.clock[0]))
                self.assertTrue(any(t["pending"] and t["pending"]["role"] == args[0]
                                    for t in self.store.snapshot()["tasks"]))
                return _original(*args)
            setattr(self.provider, method, wrapped)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def task(self, **changes):
        body = {"name": "模拟学习", "lib": 1, "seat": "1-001", "lead": 300}
        body.update(changes)
        t = self.engine.create(body)
        self.engine.action(t["id"], "start")
        self.engine.tick()
        return self.engine.get_task(t["id"])

    def due(self, t):
        self.clock[0] = t["next"] + .01

    def test_ten_rotations_immediate_takeover_and_cooldown(self):
        t = self.task()
        for i in range(10):
            self.due(t)
            self.engine.tick()
            self.assertEqual(t["rotations"], i + 1)
            cancel, reserve = self.calls[-2:]
            self.assertEqual(cancel[0], "cancel")
            self.assertEqual(reserve[0], "reserve")
            self.assertNotEqual(cancel[1], reserve[1])
            self.assertEqual(cancel[2], reserve[2])
            self.assertGreaterEqual(self.engine.ready_at("demo", cancel[1]), self.clock[0] + COOLDOWN)

    def test_wait_for_recipient_before_releasing(self):
        t = self.task()
        self.store.data["cooldowns"]["demo:B"] = t["next"] + 100
        self.due(t)
        self.engine.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(t["owner"], "A")
        self.due(t)
        self.engine.tick()
        self.assertEqual(t["owner"], "B")

    def test_impossible_cooldown_preserves_reservation(self):
        t = self.task()
        self.store.data["cooldowns"]["demo:B"] = t["expires"] + 60
        self.engine.tick()
        self.assertEqual(t["status"], "attention")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("A", self.store.data["demo_seats"])

    def test_other_reservation_and_offline_recipient_never_release(self):
        t = self.task()
        self.store.data["demo_seats"]["B"] = {"lib": 1, "seat": "1-002", "expires": t["expires"]}
        self.due(t)
        self.engine.tick()
        self.assertEqual(t["status"], "attention")
        self.assertEqual(len(self.calls), 1)

    def test_offline_recipient_preserves_holder(self):
        t = self.task()
        original = self.provider.observe

        def offline():
            states = original()
            states["B"]["ready"] = False
            return states
        self.provider.observe = offline
        self.due(t)
        self.engine.tick()
        self.assertEqual(t["status"], "recovering")
        self.assertEqual(len(self.calls), 1)

    def test_restart_reconciles_without_duplicate_reserve(self):
        t = self.task()
        id = t["id"]
        self.store.close()
        self.store = Store(self.path)
        restarted = Engine(self.store, clock=lambda: self.clock[0])
        self.assertEqual(restarted.get_task(id)["status"], "recovering")
        restarted.tick()
        self.assertEqual(restarted.get_task(id)["owner"], "A")
        self.assertEqual(restarted.get_task(id)["status"], "running")
        self.assertEqual(len(self.store.data["demo_seats"]), 1)

    def test_cancel_applied_but_response_lost_no_blind_repeat(self):
        t = self.task()
        original = self.provider.cancel

        def lost(role):
            original(role)
            raise Problem("模拟网络中断")
        self.provider.cancel = lost
        self.due(t)
        self.engine.tick()
        self.assertEqual(t["status"], "recovering")
        count = len(self.calls)
        self.engine.tick()
        self.assertEqual(t["status"], "attention")
        self.assertEqual(len(self.calls), count)
        self.assertGreater(self.engine.ready_at("demo", "A"), self.clock[0])

    def test_cancel_not_applied_no_blind_repeat(self):
        t = self.task()
        def failed(role):
            raise Problem("模拟超时")
        self.provider.cancel = failed
        self.due(t)
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(t["status"], "attention")
        self.assertEqual(t["owner"], "A")
        self.assertEqual(len(self.calls), 1)

    def test_reserve_applied_but_reply_lost_adopts_holder(self):
        original = self.provider.reserve
        def lost(role, task):
            original(role, task)
            raise Problem("模拟超时")
        self.provider.reserve = lost
        t = self.task()
        self.assertEqual(t["status"], "recovering")
        self.engine.tick()
        self.assertEqual(t["owner"], "A")
        self.assertEqual(t["status"], "running")
        self.assertEqual(len(self.calls), 1)

    def test_pause_edit_finish_and_rerun(self):
        t = self.task()
        self.engine.action(t["id"], "pause")
        self.due(t)
        self.engine.tick()
        self.assertEqual(len(self.calls), 1)
        self.engine.update(t["id"], {"name": "改名", "lib": 1, "seat": "1-001", "lead": 180})
        self.assertEqual(t["name"], "改名")
        with self.assertRaises(Problem):
            self.engine.update(t["id"], {"name": "换座", "lib": 2, "seat": "2-001"})
        self.engine.action(t["id"], "finish")
        self.assertEqual(len(self.calls), 1)
        new = self.engine.action(t["id"], "rerun")
        self.assertNotEqual(new["id"], t["id"])
        self.assertEqual(new["status"], "draft")

    def test_schedule_end_and_conflicts(self):
        t = self.task(start=self.clock[0] + 100, end=self.clock[0] + 200)
        self.assertEqual(t["status"], "scheduled")
        self.assertEqual(len(self.calls), 0)
        second = self.engine.create({"name": "另一个", "lib": 1, "seat": "1-002"})
        with self.assertRaises(Problem):
            self.engine.action(second["id"], "start")
        self.clock[0] += 101
        self.engine.tick()
        self.assertEqual(t["owner"], "A")
        self.clock[0] += 100
        self.engine.tick()
        self.assertEqual(t["status"], "completed")
        self.assertEqual(len(self.calls), 1)

    def test_stale_seat_rejected(self):
        with self.assertRaises(Problem):
            self.engine.create({"name": "旧座位", "lib": 2, "seat": "1-001"})

    def test_external_release_detected_and_cooldown_persisted(self):
        t = self.task()
        self.store.data["demo_seats"].clear()
        self.engine.tick()
        self.assertEqual(t["status"], "attention")
        self.assertGreater(self.store.snapshot()["cooldowns"]["demo:A"], self.clock[0])

    def test_unverified_reserve_requires_manual_resume(self):
        self.provider.reserve = lambda role, task: None
        t = self.task()
        self.assertEqual(t["status"], "recovering")
        self.engine.tick()
        self.assertEqual(t["status"], "attention")
        self.assertIsNone(t["pending"])

    def test_finish_pending_operation_reconciles_without_mutation(self):
        t = self.task()
        t["pending"] = {"kind": "cancel", "role": "A"}
        t["status"] = "recovering"
        self.engine.action(t["id"], "finish")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["owner"], "A")
        self.assertIsNone(t["pending"])
        self.assertEqual(len(self.calls), 1)

    def test_end_time_does_not_hide_uncertain_operation(self):
        t = self.task(end=self.clock[0] + 1)
        t["pending"] = {"kind": "reserve", "role": "A", "handoff": False}
        self.clock[0] += 2
        self.engine.tick()
        self.assertEqual(t["status"], "attention")
        self.assertIsNotNone(t["pending"])
        other = self.engine.create({"name": "另一个任务", "lib": 1, "seat": "1-002"})
        with self.assertRaises(Problem):
            self.engine.action(other["id"], "start")


class AdapterTests(unittest.TestCase):
    def test_legacy_mutation_only_retried_for_pre_execution_validation(self):
        class Worker:
            calls = 0
            def call(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"errors": [{"message": 'Cannot query field "reserveSeat" on type "Reserve".'}]}
                return {"data": {"userAuth": {"reserve": {"reserueSeat": True}}}}
        worker = Worker()
        LiveProvider(worker).reserve("A", {"lib": 1, "seat": "test"})
        self.assertEqual(worker.calls, 2)
        worker.calls = 0
        def uncertain(*args, **kwargs):
            worker.calls += 1
            raise Problem("timeout")
        worker.call = uncertain
        with self.assertRaises(Problem):
            LiveProvider(worker).reserve("A", {"lib": 1, "seat": "test"})
        self.assertEqual(worker.calls, 1)

    def test_expiry_formats_and_bad_input(self):
        expected = expiry("2026-10-19T12:00:00+08:00")
        self.assertEqual(expiry(str(expected)), expected)
        self.assertEqual(expiry(expected * 1000), expected)
        self.assertEqual(expiry(None, "2026-10-19 12:00:00"), expected)
        with self.assertRaises(Problem):
            expiry("29 分钟")

    def test_graphql_errors_and_uncertain_cancellation_are_not_success(self):
        class Worker:
            result = {"errors": [{"message": "must never escape to UI"}]}
            def call(self, *args, **kwargs):
                return self.result
        worker = Worker()
        adapter = LiveProvider(worker)
        with self.assertRaises(Problem) as caught:
            adapter.query("A", "query {}")
        self.assertNotIn("must never", str(caught.exception))
        worker.result = {"data": {"userAuth": {"reserve": {"reserveCancle": None}}}}
        adapter.tokens["A"] = "test-memory-only"
        with self.assertRaises(Problem):
            adapter.cancel("A")


if __name__ == "__main__":
    unittest.main()
