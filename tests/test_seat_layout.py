import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from seat_layout import jiangpu_libraries, normalize_layout
from seat_core import Engine, Problem, Store
from live_provider import LiveProvider


class LayoutTests(unittest.TestCase):
    def layout(self, seats, opened=True):
        return normalize_layout({"lib_id": 122783, "is_open": opened,
                                 "lib_layout": {"max_x": 60, "max_y": 42, "seats": seats}},
                                {"name": "示例馆室", "campus": "江浦校区"}, 1800000000)

    def test_only_confirmed_school_free_combination_is_available(self):
        pairs = [(1, False), (2, True), (3, True), (4, True), (1, True),
                 (3, False), (9, False), (None, False), (1, None), (True, False), (1, 0)]
        seats = [{"key": str(i), "name": str(i), "seat_status": code, "status": status}
                 for i, (code, status) in enumerate(pairs)]
        result = self.layout(seats)
        self.assertEqual(result["available_count"], 1)
        self.assertEqual([s["state"] for s in result["seats"][:5]],
                         ["available", "occupied", "occupied", "occupied", "unknown"])

    def test_closed_room_and_incomplete_status_never_offer_free_seats(self):
        seats = [{"key": "0,0", "name": "001", "seat_status": 1, "status": False}]
        self.assertEqual(self.layout(seats, False)["available_count"], 0)
        self.assertEqual(self.layout([{"key": "a", "name": "001", "available": True}])["available_count"], 0)

    def test_coordinate_zero_preserved_opaque_keys_not_parsed_and_bad_values_ignored(self):
        result = self.layout([{"key": "2,8", "name": "159", "x": 8, "y": 2,
                               "seat_status": 1, "status": False},
                              {"key": "zero", "name": "000", "x": 0, "y": "0"},
                              {"key": "bad", "name": "bad", "x": float("nan"), "y": True}])
        self.assertEqual((result["seats"][0]["x"], result["seats"][0]["y"]), (8, 2))
        self.assertEqual((result["seats"][1]["x"], result["seats"][1]["y"]), (0, 0))
        self.assertIsNone(result["seats"][2]["x"])
        self.assertIsNone(result["seats"][2]["y"])

    def test_missing_names_duplicate_seats_and_untrusted_totals(self):
        result = self.layout([{"key": "a", "name": "001", "seat_status": 1, "status": False},
                              {"key": "a", "name": "001", "seat_status": 1, "status": False},
                              {"key": "decor", "type": "wall"}])
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(result["total_count"], 1)

    def test_jiangpu_contains_both_yifu_and_pujiang_excludes_other_venues(self):
        data = {"libGroups": [{"id": 1, "group_name": "逸夫图书馆"},
                              {"id": 2, "group_name": "浦江图书馆"},
                              {"id": 3, "group_name": "朗读亭"},
                              {"id": 4, "group_name": "丁家桥校区"}],
                "libs": [{"lib_id": i, "lib_group_id": i, "lib_name": "示例馆室", "is_open": True}
                         for i in range(1, 6)]}
        result = jiangpu_libraries(data)
        self.assertEqual([r["id"] for r in result], [1, 2])
        self.assertEqual({r["campus"] for r in result}, {"江浦校区"})
        self.assertEqual(jiangpu_libraries({"libs": data["libs"]}), [])

    def test_live_adapter_rejects_venue_outside_catalog_before_layout_request(self):
        class Worker:
            def call(self, *args, **kwargs):
                raise AssertionError("must not request another campus")
        live = LiveProvider(Worker())
        live.catalog = [{"id": 122783}]
        import time
        live.catalog_at = time.monotonic()
        with self.assertRaises(Problem):
            live.seatmap(999)

    def test_live_adapter_preserves_school_coordinates_and_only_offers_free_seats(self):
        class Worker:
            def call(self, method, **kwargs):
                if "libLayout" in kwargs["payload"]["query"]:
                    body = {"libs": [{"lib_id": 122783, "is_open": True, "lib_layout": {
                        "max_x": 60, "max_y": 42, "seats": [
                            {"key": "2,2", "name": "168", "x": 2, "y": 2, "seat_status": 3, "status": True},
                            {"key": "2,8", "name": "159", "x": 8, "y": 2, "seat_status": 1, "status": False}]}}]}
                else:
                    body = {"libGroups": [{"id": 1, "group_name": "逸夫图书馆"}],
                            "libs": [{"lib_id": 122783, "lib_group_id": 1, "is_open": True}]}
                return {"data": {"userAuth": {"reserve": body}}}
        result = LiveProvider(Worker()).seatmap(122783)
        self.assertEqual((result["max_x"], result["max_y"]), (60, 42))
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(result["seats"][1]["key"], "2,8")


class AvailabilityTests(unittest.TestCase):
    def test_new_tasks_reject_occupied_and_unknown_but_existing_task_can_keep_seat(self):
        temp = ROOT.parents[1] / "tmp" / ROOT.name / "tests"
        temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp) as directory:
            store = Store(Path(directory) / "test.sqlite3")
            try:
                engine = Engine(store)
                for seat in ("1-007", "1-011"):
                    with self.assertRaises(Problem):
                        engine.create({"name": "不可选", "lib": 1, "seat": seat})
                t = engine.create({"name": "正常任务", "lib": 1, "seat": "1-001"})
                engine.action(t["id"], "start")
                engine.tick()
                with self.assertRaises(Problem):
                    engine.create({"name": "重复持座", "lib": 1, "seat": "1-001"})
                engine.action(t["id"], "pause")
                engine.update(t["id"], {"name": "保留持座并编辑", "lib": 1, "seat": "1-001"})
                self.assertEqual(engine.get_task(t["id"])["name"], "保留持座并编辑")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
