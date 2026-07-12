#!/usr/bin/env python3
"""brief-165 — the two-box money artifact, end to end, entirely local.

One local apiary (real HTTP subprocess). Two simulated boxes (two temp project
dirs, two hum instances). Proves the card's success criteria:

  * two-box render — both boxes' events reach the one floor, tagged with box;
  * kill-apiary is a no-op for work — the local journals are untouched, cursors
    do not advance, the "work" (journal appends) is unaffected;
  * offline box catches up — apiary down → buffer locally → apiary up → ship;
  * exactly once across a crash — POST-then-crash-before-cursor-persist replays
    the SAME ids; the reader collapses them to one row each.

`GET /v1/events?since=0` is exactly the call hive's `load_apiary_events` makes,
so the JSON asserted here is the data the dance floor renders.
"""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
APIARY = REPO / "apiary" / "apiary.py"
HUM = REPO / "hum" / "hum.py"
TOKEN = "two-box-token"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _state(project_dir):
    d = os.path.join(project_dir, ".loop", "state")
    os.makedirs(d, exist_ok=True)
    return d


def _append(project_dir, journal, obj):
    with open(os.path.join(_state(project_dir), journal), "a") as f:
        f.write(json.dumps(obj) + "\n")


def _get(port, since=0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/events?since={since}",
        headers={"X-Apiary-Token": TOKEN},
    )
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def _run_hum(box, project_dir, port):
    return subprocess.run(
        [sys.executable, str(HUM), "--once", "--box", box,
         "--project-dir", project_dir,
         "--apiary-url", f"http://127.0.0.1:{port}", "--token", TOKEN,
         "--no-heartbeat"],
        capture_output=True, text=True, timeout=15,
    )


class TwoBoxPresenceTest(unittest.TestCase):
    def setUp(self):
        self.port = _free_port()
        self.db = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                               f"apiary-test-{os.getpid()}-{self.port}.db")
        self._start_apiary()

    def tearDown(self):
        self._stop_apiary()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suffix)
            except FileNotFoundError:
                pass

    def _start_apiary(self):
        self.apiary = subprocess.Popen(
            [sys.executable, str(APIARY), "--db", self.db, "--port", str(self.port),
             "--host", "127.0.0.1", "--token", TOKEN],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Wait for readiness.
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/health", timeout=1)
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        raise RuntimeError("apiary did not come up")

    def _stop_apiary(self):
        if self.apiary and self.apiary.poll() is None:
            self.apiary.terminate()
            try:
                self.apiary.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.apiary.kill()

    def test_two_boxes_render_on_one_floor(self):
        import tempfile
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _append(a, "runtime-events.jsonl", {"ts": "2026-07-11T10:00:00Z", "event": "dispatched", "brief": "brief-1"})
            _append(a, "intent-journal.jsonl", {"ts": "2026-07-11T10:00:01Z", "session": "titania", "action": "git push", "detail": "branch X"})
            _append(b, "runtime-events.jsonl", {"ts": "2026-07-11T10:00:02Z", "event": "merged", "brief": "brief-2"})

            self.assertEqual(_run_hum("lady-titania", a, self.port).returncode, 0)
            self.assertEqual(_run_hum("scaviefae", b, self.port).returncode, 0)

            floor = _get(self.port)["events"]
            boxes = {e["box"] for e in floor}
            self.assertEqual(boxes, {"lady-titania", "scaviefae"}, "both boxes on one floor")
            # Every remote row carries a stable id and a server stamp.
            self.assertTrue(all(e.get("id") and e.get("received_at") for e in floor))

    def test_kill_apiary_is_a_noop_for_work(self):
        import tempfile
        with tempfile.TemporaryDirectory() as a:
            _append(a, "runtime-events.jsonl", {"ts": "t", "event": "dispatched", "brief": "brief-1"})
            self.assertEqual(_run_hum("boxA", a, self.port).returncode, 0)

            self._stop_apiary()  # apiary dies mid-run

            journal = os.path.join(_state(a), "runtime-events.jsonl")
            before = Path(journal).read_text()
            # "Work" keeps appending to the local journal, unaffected by the bus.
            _append(a, "runtime-events.jsonl", {"ts": "t2", "event": "completed", "brief": "brief-1"})
            # hum runs against a dead apiary: exits cleanly, advances no cursor.
            res = _run_hum("boxA", a, self.port)
            self.assertEqual(res.returncode, 0, "hum degrades gracefully, never crashes work")
            after = Path(journal).read_text()
            self.assertIn(before, after)
            self.assertIn("completed", after)  # the local durable record is intact
            cursor = Path(_state(a)) / "hum-cursors" / "runtime-events.jsonl.cursor"
            # cursor still points only at what the LIVE apiary acked (line 1), not
            # the line written while it was dead.
            self.assertLess(int(cursor.read_text()), len(after))

    def test_offline_box_catches_up(self):
        import tempfile
        with tempfile.TemporaryDirectory() as a:
            self._stop_apiary()  # start life with the bus down
            _append(a, "runtime-events.jsonl", {"ts": "t", "event": "dispatched", "brief": "brief-9"})
            res = _run_hum("boxA", a, self.port)
            self.assertEqual(res.returncode, 0)
            cursor = Path(_state(a)) / "hum-cursors" / "runtime-events.jsonl.cursor"
            self.assertTrue(not cursor.exists() or int(cursor.read_text()) == 0,
                            "offline: cursor never advanced, event buffered locally")

            self._start_apiary()  # reconnect
            self.assertEqual(_run_hum("boxA", a, self.port).returncode, 0)
            floor = _get(self.port)["events"]
            self.assertEqual([e["brief"] for e in floor], ["brief-9"], "buffered event caught up")

    def test_exactly_once_across_a_crash(self):
        # The sharp test: ship a batch, then crash before persisting the cursor
        # (modeled by deleting the cursor after ship). Replay re-sends the SAME
        # ids; the apiary dedups → the floor renders each event exactly once.
        import tempfile
        with tempfile.TemporaryDirectory() as a:
            _append(a, "runtime-events.jsonl", {"ts": "t", "event": "dispatched", "brief": "brief-1"})
            _append(a, "runtime-events.jsonl", {"ts": "t2", "event": "completed", "brief": "brief-1"})
            self.assertEqual(_run_hum("boxA", a, self.port).returncode, 0)

            # Crash between POST and cursor-persist: throw the cursor away so the
            # restart re-tails from 0 and re-POSTs the identical ids.
            cursor = Path(_state(a)) / "hum-cursors" / "runtime-events.jsonl.cursor"
            cursor.unlink()
            self.assertEqual(_run_hum("boxA", a, self.port).returncode, 0)  # replay

            floor = _get(self.port)["events"]
            ids = [e["id"] for e in floor]
            self.assertEqual(len(ids), len(set(ids)), "no dup ids reached the floor")
            self.assertEqual(len(floor), 2, "two events, each rendered exactly once")


if __name__ == "__main__":
    unittest.main()
