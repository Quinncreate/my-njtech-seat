"""Task finish and manual handoff use only an isolated local simulation."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from seat_core import COOLDOWN, Engine, Problem, Store

TEMP = ROOT.parents[1] / "tmp" / ROOT.name / "tests"
TEMP.mkdir(parents=True, exist_ok=True)


class TaskControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEMP)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.clock = [1800000000.0]
        self.store = Store(self.path)
        self.engine = Engine(self.store, clock=lambda: self.clock[0])
        self.calls = []
        self.wrap_provider()

    def wrap_provider(self):
        self.provider = self.engine.providers["demo"]
        for method in ("cancel", "reserve"):
            original = getattr(self.provider, method)

            def wrapped(*args, _method=method, _original=original):
                task = next(t for t in self.store.snapshot()["tasks"] if t["pending"])
                self.assertEqual(task["pending"]["role"], args[0])
                if task["pending"]["kind"] == "finish_cancel":
                    self.assertTrue(task["finish_requested"])
                self.calls.append((_method, args[0], self.clock[0]))
                return _original(*args)
            setattr(self.provider, method, wrapped)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def task(self, *, start=True):
        created = self.engine.create({"name": "本地控制验证", "lib": 1, "seat": "1-001"})
        if start:
            self.engine.action(created["id"], "start")
            self.engine.tick()
        return self.engine.get_task(created["id"])

    def request_finish(self, t):
        self.engine.action(t["id"], "finish")
        self.engine.tick()

    def request_handoff(self, t):
        self.engine.action(t["id"], "handoff", expected_owner=t["owner"])
        self.engine.tick()

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.engine = Engine(self.store, clock=lambda: self.clock[0])
        self.wrap_provider()

    def test_finish_releases_only_holder_and_never_reserves_again(self):
        t = self.task()
        self.request_finish(t)
        self.assertEqual([c[:2] for c in self.calls], [("reserve", "A"), ("cancel", "A")])
        self.assertEqual(t["status"], "completed")
        self.assertFalse(self.store.data["demo_seats"])
        self.assertIsNone(t["owner"])
        self.assertGreater(self.engine.ready_at("demo", "A"), self.clock[0])
        self.engine.tick()
        self.assertEqual(len(self.calls), 2)

    def test_finish_paused_task_also_releases(self):
        t = self.task()
        self.engine.action(t["id"], "pause")
        self.request_finish(t)
        self.assertEqual(t["status"], "completed")
        self.assertFalse(self.store.data["demo_seats"])

    def test_finish_draft_does_not_release_another_tasks_seat(self):
        draft = self.task(start=False)
        active = self.task()
        self.request_finish(draft)
        self.assertEqual(draft["status"], "completed")
        self.assertEqual(active["owner"], "A")
        self.assertEqual(len(self.calls), 1)

    def test_finish_ignores_unrelated_reservation(self):
        t = self.task()
        foreign = {"lib": 1, "seat": "1-002", "expires": self.clock[0] + 1800}
        self.store.data["demo_seats"]["B"] = foreign.copy()
        self.request_finish(t)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(self.store.data["demo_seats"], {"B": foreign})

    def test_finish_already_released_does_not_cancel_twice(self):
        t = self.task()
        self.store.data["demo_seats"].clear()
        self.request_finish(t)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(len(self.calls), 1)

    def test_finish_offline_stops_rotation_without_claiming_released(self):
        t = self.task()
        original = self.provider.observe
        def offline():
            result = original()
            result["B"]["ready"] = False
            return result
        self.provider.observe = offline
        self.request_finish(t)
        self.assertEqual(t["status"], "attention")
        self.assertTrue(t["finish_requested"])
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.store.data["demo_seats"])
        with self.assertRaises(Problem):
            self.engine.action(t["id"], "resume")
        with self.assertRaises(Problem):
            self.engine.update(t["id"], {"name": "编辑", "lib": 1, "seat": "1-001"})
        self.provider.observe = original
        self.request_finish(t)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(len(self.calls), 2)

    def test_finish_lost_reply_is_resolved_without_repeat(self):
        t = self.task()
        original = self.provider.cancel
        def lost(role):
            original(role)
            raise Problem("模拟丢失退座响应")
        self.provider.cancel = lost
        self.request_finish(t)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(len(self.calls), 2)

    def test_finish_timeout_requires_explicit_retry_and_does_not_rebook(self):
        t = self.task()
        original = self.provider.cancel
        def timeout(role):
            self.calls.append(("cancel", role, self.clock[0]))
            raise Problem("模拟未执行退座")
        self.provider.cancel = timeout
        self.request_finish(t)
        self.assertEqual(t["status"], "attention")
        self.assertEqual(t["pending"]["kind"], "finish_cancel")
        self.clock[0] += 500
        self.engine.tick()
        self.assertEqual(len(self.calls), 2)
        self.provider.cancel = original
        self.request_finish(t)
        self.assertEqual(t["status"], "completed")
        self.assertEqual([c[0] for c in self.calls], ["reserve", "cancel", "cancel"])

    def test_restart_resolves_sent_finish_without_repeating_it(self):
        t = self.task()
        ident = t["id"]
        self.engine.action(ident, "finish")
        t["pending"] = {"kind": "finish_cancel", "role": "A", "time": self.clock[0]}
        self.store.data["demo_seats"].clear()
        self.store.save()
        self.restart()
        self.engine.tick()
        self.assertEqual(self.engine.get_task(ident)["status"], "completed")
        self.assertEqual(len(self.calls), 1)

    def test_restart_does_not_repeat_unconfirmed_finish_that_still_holds(self):
        t = self.task()
        self.engine.action(t["id"], "finish")
        t["pending"] = {"kind": "finish_cancel", "role": "A", "time": self.clock[0]}
        self.store.save()
        self.restart()
        self.engine.tick()
        self.assertEqual(self.engine.get_task(t["id"])["status"], "attention")
        self.assertEqual(len(self.calls), 1)

    def test_manual_handoff_switches_both_directions_without_wait_between_requests(self):
        t = self.task()
        for expected in ("B", "A"):
            self.clock[0] += COOLDOWN
            self.request_handoff(t)
            self.assertEqual(t["owner"], expected)
            self.assertEqual(t["status"], "running")
            self.assertEqual(self.calls[-2][0], "cancel")
            self.assertEqual(self.calls[-1][0], "reserve")
            self.assertEqual(self.calls[-2][2], self.calls[-1][2])
        self.assertEqual(t["rotations"], 2)

    def test_manual_handoff_preserves_paused_state(self):
        t = self.task()
        self.engine.action(t["id"], "pause")
        self.clock[0] += COOLDOWN
        self.request_handoff(t)
        self.assertEqual(t["owner"], "B")
        self.assertEqual(t["status"], "paused")
        count = len(self.calls)
        self.clock[0] += 1700
        self.engine.tick()
        self.assertEqual(len(self.calls), count)

    def test_manual_handoff_requires_matching_owner_and_blocks_double_submit(self):
        t = self.task()
        self.clock[0] += COOLDOWN
        with self.assertRaises(Problem):
            self.engine.action(t["id"], "handoff", expected_owner="B")
        self.engine.action(t["id"], "handoff", expected_owner="A")
        with self.assertRaises(Problem):
            self.engine.action(t["id"], "handoff", expected_owner="A")
        self.engine.tick()
        with self.assertRaises(Problem):
            self.engine.action(t["id"], "handoff", expected_owner="A")
        self.assertEqual(len(self.calls), 3)

    def test_manual_handoff_waits_for_protection_without_releasing(self):
        t = self.task()
        with self.assertRaises(Problem):
            self.request_handoff(t)
        self.clock[0] += COOLDOWN
        self.store.data["cooldowns"]["demo:B"] = self.clock[0] + 100
        with self.assertRaises(Problem):
            self.request_handoff(t)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(t["owner"], "A")

    def test_manual_handoff_never_releases_when_recipient_has_other_seat(self):
        t = self.task()
        self.clock[0] += COOLDOWN
        self.store.data["demo_seats"]["B"] = {"lib": 1, "seat": "1-002", "expires": t["expires"]}
        self.request_handoff(t)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(t["status"], "attention")

    def test_manual_cancel_timeout_does_not_blindly_retry(self):
        t = self.task()
        self.clock[0] += COOLDOWN
        def timeout(role):
            self.calls.append(("cancel", role, self.clock[0]))
            raise Problem("模拟退座超时")
        self.provider.cancel = timeout
        self.request_handoff(t)
        self.engine.tick()
        self.assertEqual(t["status"], "attention")
        self.assertEqual(t["owner"], "A")
        self.assertEqual(len(self.calls), 2)

    def test_manual_reserve_reply_lost_adopts_recipient_and_stays_paused(self):
        t = self.task()
        self.engine.action(t["id"], "pause")
        self.clock[0] += COOLDOWN
        original = self.provider.reserve
        def lost(role, task):
            original(role, task)
            raise Problem("模拟选座响应丢失")
        self.provider.reserve = lost
        self.request_handoff(t)
        self.assertEqual(t["status"], "recovering")
        self.engine.tick()
        self.assertEqual(t["status"], "paused")
        self.assertEqual(t["owner"], "B")
        self.assertEqual(t["rotations"], 1)
        self.assertEqual(len(self.calls), 3)

    def test_finish_takes_precedence_over_queued_handoff(self):
        t = self.task()
        self.clock[0] += COOLDOWN
        self.engine.action(t["id"], "handoff", expected_owner="A")
        self.request_finish(t)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.store.data["demo_seats"])

    def test_finish_conflicting_holders_does_not_send_duplicate_cancellations(self):
        t = self.task()
        self.store.data["demo_seats"]["B"] = self.store.data["demo_seats"]["A"].copy()
        self.request_finish(t)
        self.assertEqual(t["status"], "attention")
        self.assertEqual(len(self.calls), 1)

    def test_manual_handoff_offline_recipient_never_releases(self):
        t = self.task()
        self.clock[0] += COOLDOWN
        original = self.provider.observe
        def offline():
            states = original()
            states["B"]["ready"] = False
            return states
        self.provider.observe = offline
        self.request_handoff(t)
        self.engine.tick()
        self.assertEqual(t["status"], "recovering")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(t["owner"], "A")

    def test_restart_adopts_manual_recipient_and_preserves_pause(self):
        t = self.task()
        self.engine.action(t["id"], "pause")
        self.clock[0] += COOLDOWN
        self.engine.action(t["id"], "handoff", expected_owner="A")
        t["pending"] = {"kind": "reserve", "role": "B", "handoff": True}
        self.store.data["demo_seats"]["B"] = self.store.data["demo_seats"].pop("A")
        self.store.save()
        self.restart()
        self.engine.tick()
        restored = self.engine.get_task(t["id"])
        self.assertEqual(restored["owner"], "B")
        self.assertEqual(restored["status"], "paused")
        self.assertEqual(restored["rotations"], 1)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
