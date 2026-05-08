#!/usr/bin/env python3
"""Tests for emit_rebase_conflict_escalate (brief-146).

Covers:
  - escalate.json written with expected fields
  - chaining into existing escalate.json
  - empty conflicted_paths adds a note
  - awaiting_review[] entry carries kind=rebase-blocked via the state projector
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from actions import emit_rebase_conflict_escalate, init_paths
from state import append_event, project_running_json


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_project(tmp: Path) -> Path:
    loop = tmp / ".loop"
    (loop / "state" / "signals").mkdir(parents=True)
    (loop / "worktrees").mkdir(parents=True)
    return tmp


def make_card(tmp: Path, brief_id: str, status: str = "active") -> None:
    card_dir = tmp / "wiki" / "briefs" / "cards" / brief_id
    card_dir.mkdir(parents=True)
    (card_dir / "index.md").write_text(
        f"---\nID: {brief_id}\nBranch: {brief_id}\nStatus: {status}\nAuto-merge: false\n---\n"
    )


# ── emit_rebase_conflict_escalate ─────────────────────────────────────────────

class TestEmitRebaseConflictEscalate(unittest.TestCase):

    def _run(self, tmp: Path, brief_id: str = "brief-146-test",
             main_head: str = "abc1234 alice fix: typo in README",
             conflicted_paths: list = None):
        if conflicted_paths is None:
            conflicted_paths = ["lib/foo.py", "lib/bar.py"]
        paths = init_paths(str(tmp))
        return emit_rebase_conflict_escalate(paths, brief_id, main_head, conflicted_paths)

    def _read_escalate(self, tmp: Path) -> dict:
        p = tmp / ".loop" / "state" / "signals" / "escalate.json"
        with open(p) as f:
            return json.load(f)

    # ── Happy path ────────────────────────────────────────────────────────────

    def test_returns_true(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            result = self._run(Path(d))
            self.assertTrue(result)

    def test_escalate_json_created(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d))
            p = Path(d) / ".loop" / "state" / "signals" / "escalate.json"
            self.assertTrue(p.exists())

    def test_reason_field(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d))
            data = self._read_escalate(Path(d))
            self.assertEqual(data["reason"], "rebase_conflict_against_main")

    def test_kind_field(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d))
            data = self._read_escalate(Path(d))
            self.assertEqual(data["kind"], "rebase-conflict")

    def test_brief_field(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d), brief_id="brief-146-test")
            data = self._read_escalate(Path(d))
            self.assertEqual(data["brief"], "brief-146-test")

    def test_conflicted_paths_field(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d), conflicted_paths=["lib/foo.py", "lib/bar.py"])
            data = self._read_escalate(Path(d))
            self.assertEqual(data["conflicted_paths"], ["lib/foo.py", "lib/bar.py"])

    def test_main_head_field(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d), main_head="abc1234 alice fix: typo")
            data = self._read_escalate(Path(d))
            self.assertEqual(data["main_head"], "abc1234 alice fix: typo")

    def test_timestamp_present(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d))
            data = self._read_escalate(Path(d))
            self.assertIn("timestamp", data)
            self.assertRegex(data["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    # ── Empty conflicted_paths ────────────────────────────────────────────────

    def test_empty_paths_adds_note(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d), conflicted_paths=[])
            data = self._read_escalate(Path(d))
            self.assertEqual(data["conflicted_paths"], [])
            self.assertIn("note", data)
            self.assertIn("non-path conflict", data["note"])

    def test_non_empty_paths_no_note(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(Path(d))
            self._run(Path(d), conflicted_paths=["lib/foo.py"])
            data = self._read_escalate(Path(d))
            self.assertNotIn("note", data)

    # ── Chaining ──────────────────────────────────────────────────────────────

    def test_chaining_preserves_existing_escalate(self):
        """Second call appends to chained_failures[] instead of clobbering."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            # Write an existing escalate.json (as queen might have written)
            existing = {
                "type": "push_blocked",
                "reason": "push_conflict",
                "brief": "brief-146-test",
                "kind": "push-conflict",
                "chained_failures": [],
            }
            escalate_path = tmp / ".loop" / "state" / "signals" / "escalate.json"
            escalate_path.write_text(json.dumps(existing) + "\n")

            self._run(tmp)

            data = self._read_escalate(tmp)
            # Original type preserved
            self.assertEqual(data["type"], "push_blocked")
            # Rebase conflict appended in chained_failures
            self.assertEqual(len(data["chained_failures"]), 1)
            chained = data["chained_failures"][0]
            self.assertEqual(chained["reason"], "rebase_conflict_against_main")
            self.assertEqual(chained["kind"], "rebase-conflict")

    def test_chaining_accumulates_multiple_failures(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            existing = {"type": "push_blocked", "chained_failures": [{"x": 1}]}
            escalate_path = tmp / ".loop" / "state" / "signals" / "escalate.json"
            escalate_path.write_text(json.dumps(existing) + "\n")

            self._run(tmp)

            data = self._read_escalate(tmp)
            self.assertEqual(len(data["chained_failures"]), 2)

    def test_chaining_returns_true(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            existing = {"type": "other", "chained_failures": []}
            (tmp / ".loop" / "state" / "signals" / "escalate.json").write_text(
                json.dumps(existing) + "\n"
            )
            result = self._run(tmp)
            self.assertTrue(result)


# ── awaiting_review[] kind via state projector ────────────────────────────────

class TestAwaitingReviewKindRebaseBlocked(unittest.TestCase):
    """Verify that a completed event with kind=rebase-blocked lands in
    awaiting_review[] with the right kind via the state projector."""

    def test_rebase_blocked_kind_in_awaiting_review(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-146-test"
            make_card(tmp, brief_id, status="active")

            append_event(str(tmp), "dispatched", brief_id,
                         branch=brief_id,
                         brief_file=f"wiki/briefs/cards/{brief_id}/index.md",
                         worker_slot=0, throttle=1, parallel_safe=False,
                         edit_surface=[])
            append_event(str(tmp), "completed", brief_id,
                         kind="rebase-blocked",
                         reason="rebase conflict against main — human resolution required",
                         auto_merge=False)

            state = project_running_json(str(tmp))

            self.assertEqual(state["active"], [], "brief should not be active")
            self.assertEqual(len(state["awaiting_review"]), 1)
            entry = state["awaiting_review"][0]
            self.assertEqual(entry["brief"], brief_id)
            self.assertEqual(entry["kind"], "rebase-blocked")

    def test_rebase_blocked_reason_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-146-test"
            make_card(tmp, brief_id, status="active")

            append_event(str(tmp), "dispatched", brief_id,
                         branch=brief_id,
                         brief_file=f"wiki/briefs/cards/{brief_id}/index.md",
                         worker_slot=0, throttle=1, parallel_safe=False,
                         edit_surface=[])
            append_event(str(tmp), "completed", brief_id,
                         kind="rebase-blocked",
                         reason="rebase conflict against main — human resolution required",
                         auto_merge=False)

            state = project_running_json(str(tmp))
            entry = state["awaiting_review"][0]
            self.assertIn("rebase conflict", entry.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
