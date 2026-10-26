"""Host-side heartbeat evidence survives loss of the guest collector."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import driver


class HeartbeatHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(driver, "OUT", self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)

    def post(self, body, endpoint="heartbeat"):
        handler = object.__new__(driver.Handler)
        handler.path = "/" + endpoint
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.do_POST()
        self.assertEqual(handler.wfile.getvalue(), b"ok")

    def records(self, name="heartbeat-history.jsonl"):
        with open(os.path.join(self.tmp.name, name), encoding="utf-8") as stream:
            return [json.loads(line) for line in stream]

    def test_two_posts_retain_both_timestamps_and_tails(self):
        self.post(b"first sample")
        self.post(b"second sample")
        with open(os.path.join(self.tmp.name, "heartbeat"), "rb") as stream:
            self.assertEqual(stream.read(), b"second sample")
        records = self.records()
        self.assertEqual([item["tail"] for item in records], ["first sample", "second sample"])
        self.assertLessEqual(records[0]["received_at"], records[1]["received_at"])
        self.assertGreater(records[0]["received_at"], 0)

    def test_history_rotation_retains_complete_bounded_records(self):
        with mock.patch.object(driver, "HEARTBEAT_HISTORY_MAX_BYTES", 512):
            for i in range(20):
                self.post((str(i) + ":" + "x" * 80).encode())
        names = [name for name in os.listdir(self.tmp.name) if name.startswith("heartbeat-history")]
        self.assertEqual(set(names), {"heartbeat-history.jsonl", "heartbeat-history.previous.jsonl"})
        for name in names:
            self.assertLessEqual(os.path.getsize(os.path.join(self.tmp.name, name)), 512)
            self.assertTrue(self.records(name))
        self.assertTrue(self.records()[-1]["tail"].startswith("19:"))

    def test_journal_failure_does_not_turn_a_live_post_into_a_stall(self):
        with mock.patch.object(driver, "record_heartbeat", side_effect=OSError("journal unavailable")):
            self.post(b"still alive")
        with open(os.path.join(self.tmp.name, "heartbeat"), "rb") as stream:
            self.assertEqual(stream.read(), b"still alive")

    def test_next_post_recovers_a_partial_record_from_failed_append(self):
        self.post(b"before disk failure")
        with open(os.path.join(self.tmp.name, "heartbeat-history.jsonl"), "ab") as stream:
            stream.write(b'{"received_at":' + b"x" * 5000)
        self.post(b"after disk recovery")
        self.assertEqual(
            [item["tail"] for item in self.records()],
            ["before disk failure", "after disk recovery"],
        )
