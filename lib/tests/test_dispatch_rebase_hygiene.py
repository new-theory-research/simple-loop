#!/usr/bin/env python3
"""Tests for progress.json reset on worker dispatch (brief-124 cycle 1).

Covers the bug where git rebase pulls in a different brief's progress.json
from main, causing the worker to read the wrong brief's state.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from actions import ensure_progress_for_brief


class TestEnsureProgressForBrief(unittest.TestCase):

    def _progress_path(self, tmp: Path, brief_id: str) -> Path:
        p = tmp / ".loop" / "state" / "progress.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ── Missing progress.json ────────────────────────────────────────────────

    def test_missing_progress_returns_initialized(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            progress = self._progress_path(tmp, "brief-124-test")

            result = ensure_progress_for_brief(
                str(progress), "brief-124-test", ".loop/briefs/brief-124-test.md"
            )
            self.assertEqual(result, "initialized")

    def test_missing_progress_writes_correct_brief(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            progress = self._progress_path(tmp, "brief-124-test")

            ensure_progress_for_brief(
                str(progress), "brief-124-test", ".loop/briefs/brief-124-test.md"
            )

            with open(progress) as f:
                data = json.load(f)
            self.assertEqual(data["brief"], "brief-124-test")
            self.assertEqual(data["iteration"], 0)
            self.assertEqual(data["status"], "running")
            self.assertEqual(data["tasks_completed"], [])
            self.assertEqual(data["tasks_remaining"], [])

    # ── Stale progress.json (rebase inheritance) ─────────────────────────────

    def test_stale_brief_returns_reset(self):
        """Wrong brief field (rebase pulled in brief-119's progress.json) → reset."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            progress = self._progress_path(tmp, "brief-124-test")
            progress.write_text(json.dumps({
                "brief": "brief-119-hive-buzz-view",
                "iteration": 3,
                "status": "running",
                "tasks_completed": ["old-task"],
                "tasks_remaining": [],
                "learnings": [],
            }))

            result = ensure_progress_for_brief(
                str(progress), "brief-124-test", ".loop/briefs/brief-124-test.md"
            )
            self.assertEqual(result, "reset:brief-119-hive-buzz-view")

    def test_stale_brief_writes_fresh_progress(self):
        """After reset, file reflects dispatched brief at iteration 0."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            progress = self._progress_path(tmp, "brief-124-test")
            progress.write_text(json.dumps({
                "brief": "brief-119-hive-buzz-view",
                "iteration": 5,
                "status": "running",
                "tasks_completed": ["stale-task"],
                "tasks_remaining": [],
                "learnings": ["stale learning"],
            }))

            ensure_progress_for_brief(
                str(progress), "brief-124-test", ".loop/briefs/brief-124-test.md"
            )

            with open(progress) as f:
                data = json.load(f)
            self.assertEqual(data["brief"], "brief-124-test")
            self.assertEqual(data["iteration"], 0)
            self.assertEqual(data["tasks_completed"], [])
            self.assertEqual(data["learnings"], [])

    # ── Correct progress.json — don't lose worker progress ──────────────────

    def test_correct_brief_returns_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            progress = self._progress_path(tmp, "brief-124-test")
            progress.write_text(json.dumps({
                "brief": "brief-124-test",
                "iteration": 2,
                "status": "running",
                "tasks_completed": ["task-a"],
                "tasks_remaining": ["task-b"],
                "learnings": ["learned X"],
            }))

            result = ensure_progress_for_brief(
                str(progress), "brief-124-test", ".loop/briefs/brief-124-test.md"
            )
            self.assertEqual(result, "unchanged")

    def test_correct_brief_preserves_all_fields(self):
        """Correct brief → file not modified; existing progress preserved."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            progress = self._progress_path(tmp, "brief-124-test")
            original = {
                "brief": "brief-124-test",
                "iteration": 4,
                "status": "running",
                "tasks_completed": ["task-a", "task-b"],
                "tasks_remaining": ["task-c"],
                "learnings": ["learned X", "learned Y"],
            }
            progress.write_text(json.dumps(original))

            ensure_progress_for_brief(
                str(progress), "brief-124-test", ".loop/briefs/brief-124-test.md"
            )

            with open(progress) as f:
                data = json.load(f)
            self.assertEqual(data["iteration"], 4)
            self.assertEqual(data["tasks_completed"], ["task-a", "task-b"])
            self.assertEqual(data["learnings"], ["learned X", "learned Y"])

    # ── Corrupt progress.json — treat as stale, reset ───────────────────────

    def test_corrupt_progress_resets(self):
        """Corrupt (unparseable) JSON → reset rather than leaving worker confused."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            progress = self._progress_path(tmp, "brief-124-test")
            progress.write_text('{"brief": "brief-124-test", invalid json}')

            result = ensure_progress_for_brief(
                str(progress), "brief-124-test", ".loop/briefs/brief-124-test.md"
            )
            self.assertIn(result, ("initialized", "reset:"))
            with open(progress) as f:
                data = json.load(f)
            self.assertEqual(data["brief"], "brief-124-test")
            self.assertEqual(data["iteration"], 0)


if __name__ == "__main__":
    unittest.main()
