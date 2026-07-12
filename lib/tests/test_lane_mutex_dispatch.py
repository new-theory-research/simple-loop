#!/usr/bin/env python3
"""dispatch()-path tests for the lane mutex (issue #74, Mattie's ruling 2026-07-11).

Program: is the unit of parallelism — a lane is a single thread. dispatch()
refuses a brief whose Program: matches any active brief, BEFORE the throttle and
concurrency gates, independent of Parallel-safe/edit-surface. The refusal path
returns before any git/claim/worktree work, so these tests need only cards,
config, and a running.json — no remote.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import actions  # noqa: E402


def _write_card(project, brief_id, status="queued", program=None,
                parallel_safe=None, edit_surface=None):
    card_dir = os.path.join(project, "wiki", "briefs", "cards", brief_id)
    os.makedirs(card_dir, exist_ok=True)
    body = ["---", f"ID: {brief_id}", f"Status: {status}"]
    if program is not None:
        body.append(f"Program: {program}")
    if parallel_safe is not None:
        body.append(f"Parallel-safe: {parallel_safe}")
    if edit_surface is not None:
        body.append(f"Edit-surface: {edit_surface}")
    body += ["---", "", f"# {brief_id}", ""]
    with open(os.path.join(card_dir, "index.md"), "w") as f:
        f.write("\n".join(body))


class LaneMutexDispatchTests(unittest.TestCase):
    def setUp(self):
        self._saved_lane = os.environ.pop("LOOP_LANE", None)
        self.tmp = tempfile.mkdtemp()
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.project, ".loop", "state"))
        with open(os.path.join(self.project, ".loop", "config.sh"), "w") as f:
            f.write("GIT_REMOTE=origin\nGIT_MAIN_BRANCH=main\nTHROTTLE=3\n")
        self.paths = actions.init_paths(self.project)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._saved_lane is not None:
            os.environ["LOOP_LANE"] = self._saved_lane

    def _write_running(self, active):
        with open(self.paths["running_file"], "w") as f:
            json.dump({"active": active, "awaiting_review": [],
                       "pending_merges": [], "history": []}, f)

    def _write_pending(self, brief):
        card = os.path.join("wiki", "briefs", "cards", brief, "index.md")
        with open(self.paths["pending_dispatch"], "w") as f:
            json.dump({"brief": brief, "branch": brief, "brief_file": card}, f)

    def _events(self):
        p = os.path.join(self.project, ".loop", "state", "runtime-events.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_same_lane_held_even_parallel_safe(self):
        # THROTTLE=3 so the throttle gate would NOT block — the mutex must.
        # Both parallel-safe, disjoint surfaces: legal under the surface model,
        # forbidden under the ruling.
        _write_card(self.project, "serve-010", program="serving",
                    parallel_safe="true", edit_surface="a/")
        self._write_running([
            {"brief": "serve-005", "branch": "serve-005", "program": "serving",
             "parallel_safe": True, "edit_surface": ["b/"], "worker_slot": 0},
        ])
        self._write_pending("serve-010")
        ok = actions.dispatch(self.paths)
        self.assertFalse(ok)
        # pending-dispatch consumed (queen re-queues next tick if still wanted).
        self.assertFalse(os.path.exists(self.paths["pending_dispatch"]))
        # Loud receipt: a lane_mutex_hold runtime event naming the holder.
        holds = [e for e in self._events() if e.get("event") == "lane_mutex_hold"]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["brief"], "serve-010")
        self.assertEqual(holds[0]["held_by"], "serve-005")
        self.assertEqual(holds[0]["program"], "serving")

    def test_dedup_one_event_per_pair(self):
        _write_card(self.project, "serve-010", program="serving", parallel_safe="true")
        active = [{"brief": "serve-005", "branch": "serve-005",
                   "program": "serving", "parallel_safe": True, "worker_slot": 0}]
        # Two ticks with the same (brief, holder) pair.
        for _ in range(2):
            self._write_running(active)
            self._write_pending("serve-010")
            self.assertFalse(actions.dispatch(self.paths))
        holds = [e for e in self._events() if e.get("event") == "lane_mutex_hold"]
        self.assertEqual(len(holds), 1, "dedup must not re-emit per tick")

    def test_normalization_serving_vs_Serving(self):
        _write_card(self.project, "serve-010", program="Serving", parallel_safe="true")
        self._write_running([
            {"brief": "serve-005", "branch": "serve-005", "program": "Serving",
             "parallel_safe": True, "worker_slot": 0},
        ])
        self._write_pending("serve-010")
        self.assertFalse(actions.dispatch(self.paths))
        holds = [e for e in self._events() if e.get("event") == "lane_mutex_hold"]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["program"], "serving")

    def test_program_read_from_card_fallback(self):
        # Active entry predates the projector's `program` field → card fallback.
        _write_card(self.project, "serve-005", status="active", program="serving")
        _write_card(self.project, "serve-010", program="serving", parallel_safe="true")
        self._write_running([
            {"brief": "serve-005", "branch": "serve-005", "worker_slot": 0},
        ])
        self._write_pending("serve-010")
        self.assertFalse(actions.dispatch(self.paths))
        holds = [e for e in self._events() if e.get("event") == "lane_mutex_hold"]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["held_by"], "serve-005")

    def test_unlabeled_candidate_bypasses_mutex(self):
        # No Program: on the candidate → mutex N/A. With THROTTLE=3 and a
        # non-parallel-safe active brief, the CONCURRENCY gate (not the mutex)
        # is what holds it — proving the mutex did not fire for an unlabeled brief.
        _write_card(self.project, "serve-010")  # no Program:, absent → not parallel-safe
        self._write_running([
            {"brief": "other-001", "branch": "other-001", "program": "serving",
             "parallel_safe": False, "worker_slot": 0},
        ])
        self._write_pending("serve-010")
        self.assertFalse(actions.dispatch(self.paths))
        # No lane_mutex_hold event — the hold came from the concurrency gate.
        holds = [e for e in self._events() if e.get("event") == "lane_mutex_hold"]
        self.assertEqual(holds, [])


if __name__ == "__main__":
    unittest.main()
