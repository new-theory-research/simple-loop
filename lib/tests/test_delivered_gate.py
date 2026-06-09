#!/usr/bin/env python3
"""Tests for the delivered gate and superseded projector routing (brief-237).

Three goldens:
  1. "A brief can't claim done while its code exists only on the worker's machine."
     (lineage: brief-230)
  2. "Work that shipped through another door closes in one command."
     (lineage: brief-300, 2026-06-09)
  3. "Portal-only briefs feel nothing."
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from actions import _parse_target_repo, _check_delivered_gate, close_as_delivered, init_paths  # noqa: E402
from state import project_running_json, write_running_json  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_card(tmp: Path, brief_id: str, frontmatter: str) -> str:
    """Write a minimal card index.md and return its path."""
    card_dir = tmp / "wiki" / "briefs" / "cards" / brief_id
    card_dir.mkdir(parents=True, exist_ok=True)
    card_path = card_dir / "index.md"
    card_path.write_text(f"---\nID: {brief_id}\n{frontmatter}---\n\n# {brief_id}\n")
    return str(card_path)


def _make_progress(tmp: Path, brief_id: str, progress: dict):
    """Write progress.json in the expected worktree location."""
    wt_state = tmp / ".loop" / "worktrees" / brief_id / ".loop" / "state"
    wt_state.mkdir(parents=True, exist_ok=True)
    (wt_state / "progress.json").write_text(json.dumps(progress))


def _paths(tmp: Path) -> dict:
    return {
        "project_dir": str(tmp),
        "loop_dir": str(tmp / ".loop"),
        "state_dir": str(tmp / ".loop" / "state"),
        "worktrees_dir": str(tmp / ".loop" / "worktrees"),
        "running_file": str(tmp / ".loop" / "state" / "running.json"),
    }


# ── Golden 1 — "A brief can't claim done while its code exists only on the worker's machine." ──
# lineage: brief-230 — daemon false-completed a cross-repo brief; code sat unpushed;
# three sessions independently re-fixed the same thing.

class TestGoldenBriefCantClaimDone(unittest.TestCase):

    # ── _parse_target_repo ──────────────────────────────────────────────

    def test_parse_single_external_repo(self):
        """Target-repo: simple-loop → ['simple-loop']."""
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-t1", "Target-repo: simple-loop\n")
            self.assertEqual(_parse_target_repo(card), ["simple-loop"])

    def test_parse_portal_only_returns_empty(self):
        """Target-repo: portal → [] (portal-only, gate should not trigger)."""
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-t2", "Target-repo: portal\n")
            self.assertEqual(_parse_target_repo(card), [])

    def test_parse_multi_repo_filters_portal(self):
        """Target-repo: nt-runway + portal → ['nt-runway'] (portal filtered out)."""
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-t3", "Target-repo: nt-runway + portal\n")
            self.assertEqual(_parse_target_repo(card), ["nt-runway"])

    def test_parse_absent_target_repo_returns_empty(self):
        """No Target-repo field → [] (portal-only assumption)."""
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-t4", "Status: active\n")
            self.assertEqual(_parse_target_repo(card), [])

    def test_parse_missing_file_returns_empty(self):
        """Nonexistent card path → [] (gate can't block when card missing)."""
        self.assertEqual(_parse_target_repo("/tmp/does-not-exist/index.md"), [])

    # ── _check_delivered_gate ────────────────────────────────────────────

    def test_missing_delivered_ref_is_refused(self):
        """External repo with no Delivered entry → (False, [error naming the repo]).

        This is the brief-230 failure mode: worker sets status=complete, daemon
        promotes, code never pushed. Gate must refuse and name the repo.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-230-replay"
            card = _make_card(tmp, brief_id, "Target-repo: mock-external-repo\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
            })
            paths = _paths(tmp)

            passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertFalse(passed, "Gate should refuse when Delivered is absent")
            self.assertEqual(len(errors), 1)
            self.assertIn("mock-external-repo", errors[0])
            self.assertIn("REFUSED", errors[0])

    def test_valid_delivered_url_passes_when_gh_absent(self):
        """External repo with Delivered URL → gate passes when gh binary absent.

        gh is best-effort: missing binary means verification is skipped and the
        gate passes. This prevents CI/offline environments from blocking merges.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-230-replay-ok"
            card = _make_card(tmp, brief_id, "Target-repo: mock-external-repo\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                "delivered": {
                    "mock-external-repo": "https://github.com/mock/repo/commit/abc123ef",
                },
            })
            paths = _paths(tmp)

            with patch("shutil.which", return_value=None):
                passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertTrue(passed, f"Gate should pass with valid Delivered URL when gh absent; errors={errors}")
            self.assertEqual(errors, [])

    def test_valid_delivered_url_verified_by_gh(self):
        """External repo with Delivered URL + gh api succeeds → gate passes."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-230-gh-ok"
            card = _make_card(tmp, brief_id, "Target-repo: some-repo\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                "delivered": {
                    "some-repo": "https://github.com/org/some-repo/commit/deadbeef",
                },
            })
            paths = _paths(tmp)

            import subprocess as _subprocess
            mock_result = _subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
            with patch("shutil.which", return_value="/usr/bin/gh"), \
                 patch("subprocess.run", return_value=mock_result):
                passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertTrue(passed, f"Gate should pass when gh api returns 0; errors={errors}")

    def test_unverifiable_delivered_url_is_refused(self):
        """External repo Delivered URL fails gh api → gate refuses with diagnostic."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-230-gh-fail"
            card = _make_card(tmp, brief_id, "Target-repo: some-repo\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                "delivered": {
                    "some-repo": "https://github.com/org/some-repo/commit/notexist",
                },
            })
            paths = _paths(tmp)

            import subprocess as _subprocess
            mock_result = _subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Not Found")
            with patch("shutil.which", return_value="/usr/bin/gh"), \
                 patch("subprocess.run", return_value=mock_result):
                passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertFalse(passed, "Gate should refuse when gh api returns non-zero")
            self.assertEqual(len(errors), 1)
            self.assertIn("REFUSED", errors[0])

    def test_multiple_external_repos_all_must_have_delivered(self):
        """Two external repos — missing one means refused, error names the missing one."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-multi"
            card = _make_card(tmp, brief_id, "Target-repo: repo-a + repo-b\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                "delivered": {
                    "repo-a": "https://github.com/org/repo-a/commit/abc",
                    # repo-b deliberately absent
                },
            })
            paths = _paths(tmp)

            with patch("shutil.which", return_value=None):
                passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertFalse(passed)
            self.assertTrue(any("repo-b" in e for e in errors),
                            f"Error should name 'repo-b'; got: {errors}")


# ── Golden 3 — "Portal-only briefs feel nothing." ─────────────────────────────
# Portal-only briefs must complete exactly as before — no Delivered requirement.

class TestGoldenPortalOnlyBriefFeelsNothing(unittest.TestCase):

    def test_portal_only_brief_gate_is_not_triggered(self):
        """Target-repo: portal → gate returns (True, []) regardless of Delivered absence.

        A portal-only brief has no cross-repo work to verify; the gate must be
        a complete no-op so existing behavior is unchanged.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-portal-only"
            card = _make_card(tmp, brief_id, "Target-repo: portal\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                # no 'delivered' field — would fail if gate applied
            })
            paths = _paths(tmp)

            passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertTrue(passed, "Gate must not trigger for portal-only briefs")
            self.assertEqual(errors, [], "No errors expected for portal-only brief")

    def test_absent_target_repo_field_gate_is_not_triggered(self):
        """No Target-repo field → treated as portal-only, gate not triggered."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-no-target"
            card = _make_card(tmp, brief_id, "Status: active\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
            })
            paths = _paths(tmp)

            passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertTrue(passed)
            self.assertEqual(errors, [])


# ── Golden 2 — "Work that shipped through another door closes in one command." ──
# lineage: brief-300, 2026-06-09 — work landed via design-director land (ab80d0b4)
# + brief-301's merge; ledger still pointed at stale branch; daemon retried
# conflicting merge every tick; human hand-edited running.json four times.

def _make_project_dir(tmp: Path, brief_id: str, status: str = "active") -> None:
    """Set up a minimal project tree: card + state dir."""
    card_dir = tmp / "wiki" / "briefs" / "cards" / brief_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "index.md").write_text(
        f"---\nID: {brief_id}\nBranch: {brief_id}\nStatus: {status}\n---\n\n# {brief_id}\n"
    )
    state_dir = tmp / ".loop" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)


def _write_events(tmp: Path, events: list) -> None:
    events_path = tmp / ".loop" / "state" / "runtime-events.jsonl"
    with open(events_path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


class TestGoldenWorkShippedThroughAnotherDoor(unittest.TestCase):
    """Golden 2: close_as_delivered atomically supersedes a brief from any queue state.

    The brief-300 failure mode: work shipped through another door; the daemon
    retried conflicting merges every tick; a human had to hand-edit running.json
    (four writes, error-prone, done twice in one day — 2026-06-09).
    """

    def _setup_pending_merges(self, tmp: Path, brief_id: str) -> dict:
        """Create a brief that projects into pending_merges[] (dispatched + completed + approved)."""
        _make_project_dir(tmp, brief_id)
        _write_events(tmp, [
            {"ts": "2026-06-09T09:00:00Z", "event": "dispatched", "brief": brief_id,
             "branch": brief_id, "brief_file": f"wiki/briefs/cards/{brief_id}/index.md",
             "worker_slot": 1},
            {"ts": "2026-06-09T09:10:00Z", "event": "completed", "brief": brief_id,
             "kind": "complete", "auto_merge": True},
            {"ts": "2026-06-09T09:11:00Z", "event": "approved", "brief": brief_id,
             "auto_merge": True},
        ])
        paths = init_paths(str(tmp))
        write_running_json(str(tmp))
        return paths

    def test_close_removes_from_pending_merges_to_history(self):
        """Brief in pending_merges[] → close_as_delivered → history[] with superseded status.

        Asserts the four writes happen atomically: card flipped, event appended,
        running.json updated, history[] carries delivered_via pointer.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-300-replay"
            delivered_via = "https://github.com/ScavieFae/simple-loop/commit/ab80d0b4"
            paths = self._setup_pending_merges(tmp, brief_id)

            # Verify pre-condition: brief is in pending_merges
            pre = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in pre["pending_merges"]},
                          "Pre-condition: brief should be in pending_merges before close")

            result = close_as_delivered(paths, brief_id, delivered_via, reason="landed via design-director")

            self.assertTrue(result, "close_as_delivered must return True")

            # Card status flipped to superseded
            card_path = tmp / "wiki" / "briefs" / "cards" / brief_id / "index.md"
            card_text = card_path.read_text()
            self.assertIn("Status: superseded", card_text, "Card must have Status: superseded")

            # Superseded event appended to runtime-events.jsonl
            events_path = tmp / ".loop" / "state" / "runtime-events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
            sup_events = [e for e in events if e.get("event") == "superseded" and e.get("brief") == brief_id]
            self.assertEqual(len(sup_events), 1, "Exactly one superseded event must be appended")
            self.assertEqual(sup_events[0]["delivered_via"], delivered_via)

            # running.json history[] carries the entry; pending_merges[] is empty for this brief
            rc = project_running_json(str(tmp))
            history_briefs = {e["brief"] for e in rc["history"]}
            self.assertIn(brief_id, history_briefs, "Brief must appear in history[] after close")
            pending_briefs = {e["brief"] for e in rc["pending_merges"]}
            self.assertNotIn(brief_id, pending_briefs, "Brief must not remain in pending_merges[] after close")

            history_entry = next(e for e in rc["history"] if e["brief"] == brief_id)
            self.assertEqual(history_entry["status"], "superseded")
            self.assertEqual(history_entry["delivered_via"], delivered_via)

    def test_close_from_awaiting_review(self):
        """Brief in awaiting_review[] → close_as_delivered also works (gap that caused 2026-06-09 hand-edit)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-300-awaiting"
            _make_project_dir(tmp, brief_id)
            _write_events(tmp, [
                {"ts": "2026-06-09T09:00:00Z", "event": "dispatched", "brief": brief_id,
                 "branch": brief_id, "brief_file": f"wiki/briefs/cards/{brief_id}/index.md",
                 "worker_slot": 1},
                {"ts": "2026-06-09T09:10:00Z", "event": "completed", "brief": brief_id,
                 "kind": "complete", "auto_merge": False},
            ])
            paths = init_paths(str(tmp))
            write_running_json(str(tmp))

            pre = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in pre["awaiting_review"]})

            delivered_via = "https://github.com/ScavieFae/simple-loop/pull/15"
            result = close_as_delivered(paths, brief_id, delivered_via)

            self.assertTrue(result)
            rc = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in rc["history"]})
            self.assertNotIn(brief_id, {e["brief"] for e in rc["awaiting_review"]})

    def test_close_is_idempotent(self):
        """Re-running close_as_delivered on an already-superseded brief returns True, no extra event."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-300-idempotent"
            delivered_via = "https://github.com/ScavieFae/simple-loop/commit/abc123ef"
            paths = self._setup_pending_merges(tmp, brief_id)

            # First close
            result1 = close_as_delivered(paths, brief_id, delivered_via, reason="first close")
            self.assertTrue(result1)

            events_path = tmp / ".loop" / "state" / "runtime-events.jsonl"
            events_after_first = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
            sup_count_after_first = sum(
                1 for e in events_after_first
                if e.get("event") == "superseded" and e.get("brief") == brief_id
            )

            # Second close — idempotent re-run
            result2 = close_as_delivered(paths, brief_id, delivered_via, reason="second close")
            self.assertTrue(result2, "Idempotent re-run must return True")

            events_after_second = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
            sup_count_after_second = sum(
                1 for e in events_after_second
                if e.get("event") == "superseded" and e.get("brief") == brief_id
            )
            self.assertEqual(sup_count_after_first, sup_count_after_second,
                             "Idempotent re-run must not append another superseded event")


# ── Superseded projector routing ──────────────────────────────────────────────

class TestSupersededProjectorRouting(unittest.TestCase):

    def test_superseded_card_routes_to_history(self):
        """Card Status: superseded → projected into history[] with delivered_via."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-sup-1"
            _make_card(tmp, brief_id, "Status: superseded\n")

            events = [
                {"ts": "2026-06-09T10:00:00Z", "event": "superseded", "brief": brief_id,
                 "delivered_via": "https://github.com/org/repo/pull/42", "reason": "landed via PR"},
            ]
            result = project_running_json(str(tmp), events=events)

            history_briefs = [e["brief"] for e in result["history"]]
            self.assertIn(brief_id, history_briefs, "Superseded card must appear in history[]")

            entry = next(e for e in result["history"] if e["brief"] == brief_id)
            self.assertEqual(entry["status"], "superseded")
            self.assertEqual(entry["delivered_via"], "https://github.com/org/repo/pull/42")

    def test_superseded_card_not_in_active_or_pending(self):
        """Superseded card must not appear in active[], awaiting_review[], or pending_merges[]."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-sup-2"
            _make_card(tmp, brief_id, "Status: superseded\n")

            events = [
                {"ts": "2026-06-09T10:00:00Z", "event": "superseded", "brief": brief_id,
                 "delivered_via": "https://github.com/org/repo/commit/abc123", "reason": ""},
            ]
            result = project_running_json(str(tmp), events=events)

            for bucket in ("active", "awaiting_review", "pending_merges"):
                bucket_briefs = [e.get("brief") for e in result[bucket]]
                self.assertNotIn(brief_id, bucket_briefs,
                                 f"Superseded card must not appear in {bucket}[]")


if __name__ == "__main__":
    unittest.main()
