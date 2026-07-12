#!/usr/bin/env python3
"""brief-165 — apiary v0: guard, caps, dedup, received_at, ring trim, HTTP.

The store is deliberately dumb. These tests pin the one place it says no (the
coordination guard) and the loss-tolerant bounds, plus a live HTTP round-trip.
"""

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "apiary"))
import apiary  # noqa: E402


def _mem_conn():
    return apiary.init_db(":memory:")


class IngestCoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mem_conn()
        self.logs = []

    def _log(self, msg):
        self.logs.append(msg)

    def test_stores_and_stamps_received_at(self):
        summary = apiary.ingest(
            self.conn,
            [{"ts": "2026-07-11T10:00:00Z", "session": "titania", "action": "edit",
              "box": "lady-titania", "id": "lady-titania:j:0"}],
            now_iso="2026-07-11T10:00:02Z",
        )
        self.assertEqual(summary["stored"], 1)
        events = apiary.fetch_since(self.conn, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["received_at"], "2026-07-11T10:00:02Z")
        self.assertEqual(events[0]["box"], "lady-titania")
        self.assertIn("cursor", events[0])

    def test_coordination_verb_rejected_and_not_stored(self):
        # The money guard: a claim never reaches storage and never reaches a reader.
        for verb in ("claim", "gate", "merge-decide", "MERGE_DECIDE"):
            with self.assertRaises(apiary.BatchError) as cm:
                apiary.ingest(self.conn, [{"action": verb, "id": f"x:{verb}"}], log=self._log)
            self.assertEqual(cm.exception.status, 422)
        self.assertEqual(apiary.fetch_since(self.conn, 0), [])
        self.assertTrue(any("REJECTED coordination" in m for m in self.logs))

    def test_coordination_verb_fails_whole_batch(self):
        # One coordination event poisons the whole POST — nothing stored.
        with self.assertRaises(apiary.BatchError):
            apiary.ingest(
                self.conn,
                [{"action": "edit", "id": "ok:1"}, {"action": "claim", "id": "bad:1"}],
            )
        self.assertEqual(apiary.fetch_since(self.conn, 0), [])

    def test_observational_dispatch_and_merged_pass(self):
        # The reconciliation: dispatch/merged as presence observations are allowed.
        summary = apiary.ingest(
            self.conn,
            [{"action": "dispatch", "id": "d:1"}, {"event": "merged", "id": "m:1"}],
        )
        self.assertEqual(summary["stored"], 2)

    def test_dedup_on_id_belt(self):
        ev = {"action": "edit", "id": "dup:same-offset"}
        s1 = apiary.ingest(self.conn, [ev])
        s2 = apiary.ingest(self.conn, [ev])  # exact replay (at-least-once)
        self.assertEqual(s1["stored"], 1)
        self.assertEqual(s2["stored"], 0)
        self.assertEqual(s2["deduped"], 1)
        self.assertEqual(len(apiary.fetch_since(self.conn, 0)), 1)

    def test_batch_event_count_cap_413(self):
        big = [{"action": "edit", "id": f"n:{i}"} for i in range(apiary.MAX_BATCH_EVENTS + 1)]
        with self.assertRaises(apiary.BatchError) as cm:
            apiary.ingest(self.conn, big)
        self.assertEqual(cm.exception.status, 413)

    def test_oversize_event_dropped_not_wedged(self):
        # A 17 KB poison line is skipped; the good event beside it still stores.
        poison = {"action": "edit", "id": "poison", "detail": "x" * (17 * 1024)}
        good = {"action": "edit", "id": "good"}
        summary = apiary.ingest(self.conn, [poison, good], log=self._log)
        self.assertEqual(summary["stored"], 1)
        self.assertEqual(summary["skipped_poison"], 1)
        ids = [e["id"] for e in apiary.fetch_since(self.conn, 0)]
        self.assertEqual(ids, ["good"])

    def test_fetch_since_advances_by_cursor(self):
        apiary.ingest(self.conn, [{"action": "edit", "id": "a"}])
        apiary.ingest(self.conn, [{"action": "edit", "id": "b"}])
        first = apiary.fetch_since(self.conn, 0)
        self.assertEqual(len(first), 2)
        after = apiary.fetch_since(self.conn, first[0]["cursor"])
        self.assertEqual([e["id"] for e in after], ["b"])

    def test_ring_trims_by_count(self):
        # Shrink the cap for the test rather than inserting 100k rows.
        orig = apiary.RING_MAX_EVENTS
        apiary.RING_MAX_EVENTS = 3
        try:
            for i in range(6):
                apiary.ingest(self.conn, [{"action": "edit", "id": f"r:{i}"}])
            remaining = apiary.fetch_since(self.conn, 0)
            self.assertLessEqual(len(remaining), 3)
            self.assertEqual(remaining[-1]["id"], "r:5")  # newest survives
        finally:
            apiary.RING_MAX_EVENTS = orig


class HttpRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mem_conn()
        self.token = "test-token"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), apiary.make_handler(self.conn, self.token))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, events, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/events",
            data=json.dumps(events).encode(),
            headers={"Content-Type": "application/json",
                     apiary.TOKEN_HEADER: token if token is not None else self.token},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=5)

    def _get(self, since=0):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/events?since={since}",
            headers={apiary.TOKEN_HEADER: self.token},
        )
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    def test_post_then_get(self):
        resp = self._post([{"action": "edit", "id": "h:1", "box": "boxA"}])
        self.assertEqual(resp.status, 200)
        body = self._get()
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["box"], "boxA")
        self.assertIn("received_at", body["events"][0])

    def test_bad_token_401(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post([{"action": "edit", "id": "z"}], token="wrong")
        self.assertEqual(cm.exception.code, 401)

    def test_coordination_422_over_http(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post([{"action": "claim", "id": "c:1"}])
        self.assertEqual(cm.exception.code, 422)
        self.assertEqual(self._get()["events"], [])


if __name__ == "__main__":
    unittest.main()
