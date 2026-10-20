import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_provider import CANCEL, INDEX, LiveProvider
from seat_core import Problem


def reply(data):
    return {"data": {"userAuth": {"reserve": data}}}


class Worker:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class CancelTests(unittest.TestCase):
    def provider(self, *responses):
        worker = Worker(responses)
        provider = LiveProvider(worker)
        provider.tokens["A"] = "temporary-test-token"
        return provider, worker

    def test_error_after_release_is_reconciled_without_second_cancel(self):
        provider, worker = self.provider(
            {"errors": [{"message": "response construction failed"}]},
            reply({"reserve": None, "getSToken": "refreshed-test-token"}),
        )
        provider.cancel("A")
        self.assertEqual([c[1]["payload"]["query"] for c in worker.calls], [CANCEL, INDEX])
        self.assertEqual(provider.tokens["A"], "refreshed-test-token")

    def test_lost_reply_then_confirmed_empty_is_accepted(self):
        provider, worker = self.provider(
            Problem("transport interrupted"),
            reply({"reserve": None, "getSToken": "test-token"}),
        )
        provider.cancel("A")
        self.assertEqual(len(worker.calls), 2)

    def test_success_reply_with_remaining_reservation_blocks_takeover(self):
        provider, _ = self.provider(
            reply({"reserveCancle": {"mins": 1}}),
            reply({"reserve": {"lib_id": 122783, "seat_key": "2,8"},
                   "getSToken": "test-token"}),
        )
        with self.assertRaises(Problem):
            provider.cancel("A")

    def test_failed_observation_does_not_mean_released(self):
        provider, worker = self.provider(
            reply({"reserveCancle": None}), Problem("read timed out"),
        )
        with self.assertRaises(Problem):
            provider.cancel("A")
        self.assertEqual(len(worker.calls), 2)

    def test_missing_reservation_field_or_login_proof_blocks_takeover(self):
        for actual in ({"getSToken": "test-token"}, {"reserve": None}):
            with self.subTest(actual=actual):
                provider, _ = self.provider(reply({"reserveCancle": None}), reply(actual))
                with self.assertRaises(Problem):
                    provider.cancel("A")

    def test_missing_cancel_token_never_sends_mutation(self):
        provider, worker = self.provider()
        provider.tokens.clear()
        with self.assertRaises(Problem):
            provider.cancel("A")
        self.assertEqual(worker.calls, [])


class ReservationRequestTests(unittest.TestCase):
    def test_request_matches_school_seat_map_operation_and_empty_captcha(self):
        worker = Worker([reply({"reserueSeat": True})])
        LiveProvider(worker).reserve("A", {"lib": 122783, "seat": "2,8"})
        payload = worker.calls[0][1]["payload"]
        self.assertEqual(payload["operationName"], "reserueSeat")
        self.assertIn("reserueSeat(", payload["query"])
        self.assertEqual(payload["variables"], {
            "libId": 122783, "seatKey": "2,8", "captchaCode": "", "captcha": ""})

    def test_business_error_does_not_try_a_different_mutation(self):
        worker = Worker([{"errors": [{"message": "captcha required"}]}])
        with self.assertRaises(Problem):
            LiveProvider(worker).reserve("A", {"lib": 122783, "seat": "2,8"})
        self.assertEqual(len(worker.calls), 1)


if __name__ == "__main__":
    unittest.main()
