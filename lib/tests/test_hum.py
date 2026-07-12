#!/usr/bin/env python3
"""brief-165 — hum shipper: byte-cursor tail, stable ids, poison skip,
at-least-once (cursor persists only after a successful POST), offline buffering.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "hum"))
import hum  # noqa: E402


def _write_journal(project_dir, name, lines):
    d = os.path.join(project_dir, ".loop", "state")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "a") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")
    return path


class TailJournalTest(unittest.TestCase):
    def test_stamps_box_and_stable_id_from_offset(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_journal(d, "runtime-events.jsonl",
                                  [{"ts": "t1", "event": "dispatched", "brief": "b-1"}])
            events, cursor = hum.tail_journal(path, 0, "boxA", "runtime-events.jsonl")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["box"], "boxA")
            self.assertEqual(events[0]["id"], "boxA:runtime-events.jsonl:0")
            self.assertEqual(cursor, os.path.getsize(path))

    def test_id_is_stable_across_reread_same_offset(self):
        # The at-least-once contract's backbone: re-reading from the same cursor
        # yields byte-identical ids, so a replay dedups.
        with tempfile.TemporaryDirectory() as d:
            path = _write_journal(d, "runtime-events.jsonl",
                                  [{"a": 1}, {"a": 2}])
            e1, _ = hum.tail_journal(path, 0, "boxA", "runtime-events.jsonl")
            e2, _ = hum.tail_journal(path, 0, "boxA", "runtime-events.jsonl")
            self.assertEqual([e["id"] for e in e1], [e["id"] for e in e2])

    def test_partial_trailing_line_left_unconsumed(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".loop", "state"))
            path = os.path.join(d, ".loop", "state", "runtime-events.jsonl")
            with open(path, "w") as f:
                f.write('{"a":1}\n{"a":2}')  # second line has no newline yet
            events, cursor = hum.tail_journal(path, 0, "boxA", "runtime-events.jsonl")
            self.assertEqual(len(events), 1)
            self.assertEqual(cursor, len('{"a":1}\n'))  # stops before partial line

    def test_poison_oversize_skipped_but_consumed(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".loop", "state"))
            path = os.path.join(d, ".loop", "state", "runtime-events.jsonl")
            poison = '{"x":"' + "y" * (17 * 1024) + '"}'
            with open(path, "w") as f:
                f.write(poison + "\n" + '{"good":1}\n')
            logs = []
            events, cursor = hum.tail_journal(path, 0, "boxA", "runtime-events.jsonl",
                                              log=logs.append)
            self.assertEqual(len(events), 1)  # only the good line shipped
            self.assertEqual(events[0]["good"], 1)
            self.assertEqual(cursor, os.path.getsize(path))  # poison consumed, not wedged
            self.assertTrue(any("poison" in m for m in logs))

    def test_unparseable_line_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".loop", "state"))
            path = os.path.join(d, ".loop", "state", "runtime-events.jsonl")
            with open(path, "w") as f:
                f.write("not json\n" + '{"good":1}\n')
            events, _ = hum.tail_journal(path, 0, "boxA", "runtime-events.jsonl",
                                        log=lambda m: None)
            self.assertEqual([e.get("good") for e in events], [1])


class RunOnceTest(unittest.TestCase):
    def _cfg(self, project_dir, sink):
        return {
            "box": "boxA", "project_dir": project_dir,
            "apiary_url": "http://unused", "token": "t",
            "journals": ["runtime-events.jsonl"], "heartbeat": False,
        }

    def test_cursor_persists_only_after_successful_post(self):
        with tempfile.TemporaryDirectory() as d:
            _write_journal(d, "runtime-events.jsonl", [{"a": 1}, {"a": 2}])
            cfg = {"box": "boxA", "project_dir": d, "apiary_url": "x", "token": "t",
                   "journals": ["runtime-events.jsonl"], "heartbeat": False}

            # First: POST fails → cursor must NOT advance (offline buffering).
            orig = hum.post_batch
            hum.post_batch = lambda *a, **k: False
            try:
                delivered = hum.run_once(cfg, {"heartbeat_ts": None}, log=lambda m: None)
            finally:
                hum.post_batch = orig
            self.assertEqual(delivered, 0)
            self.assertEqual(hum.read_cursor(d, "runtime-events.jsonl"), 0)

            # Then: POST succeeds → cursor advances past all consumed bytes.
            sent = []
            hum.post_batch = lambda url, tok, evs, **k: (sent.extend(evs) or True)
            try:
                delivered = hum.run_once(cfg, {"heartbeat_ts": None}, log=lambda m: None)
            finally:
                hum.post_batch = orig
            self.assertEqual(delivered, 2)
            self.assertEqual([e["id"] for e in sent],
                             ["boxA:runtime-events.jsonl:0",
                              f"boxA:runtime-events.jsonl:{len(json.dumps({'a': 1})) + 1}"])
            self.assertGreater(hum.read_cursor(d, "runtime-events.jsonl"), 0)

    def test_replay_after_crash_resends_same_ids(self):
        # Simulate crash-between-POST-and-cursor-persist: POST "succeeds" (peer
        # captured the batch) but we abort before persisting. Restart re-sends the
        # SAME ids — harmless, dedup covers it downstream.
        with tempfile.TemporaryDirectory() as d:
            _write_journal(d, "runtime-events.jsonl", [{"a": 1}])
            cfg = {"box": "boxA", "project_dir": d, "apiary_url": "x", "token": "t",
                   "journals": ["runtime-events.jsonl"], "heartbeat": False}
            captures = []

            orig = hum.post_batch
            # POST captures then reports failure so the cursor does NOT advance
            # (models the crash: bytes reached the peer, cursor never persisted).
            hum.post_batch = lambda url, tok, evs, **k: (captures.append([e["id"] for e in evs]) or False)
            try:
                hum.run_once(cfg, {"heartbeat_ts": None}, log=lambda m: None)
                hum.run_once(cfg, {"heartbeat_ts": None}, log=lambda m: None)  # restart/replay
            finally:
                hum.post_batch = orig
            self.assertEqual(captures[0], captures[1])  # identical ids on replay


class HeartbeatSnapshotTest(unittest.TestCase):
    def test_ships_on_change_only(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".loop", "state"))
            path = os.path.join(d, ".loop", "state", "heartbeat.json")
            with open(path, "w") as f:
                json.dump({"ts": "2026-07-11T10:00:00Z", "pid": 1, "last_event": "tick"}, f)
            ev, ts = hum.snapshot_heartbeat(path, "boxA", None)
            self.assertIsNotNone(ev)
            self.assertEqual(ev["id"], "boxA:heartbeat.json:2026-07-11T10:00:00Z")
            # Same ts → no re-ship.
            ev2, ts2 = hum.snapshot_heartbeat(path, "boxA", ts)
            self.assertIsNone(ev2)


if __name__ == "__main__":
    unittest.main()
