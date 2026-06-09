#!/usr/bin/env python3
"""Tests for the delivered gate and superseded projector routing (brief-237).

Three goldens:
  1. "A brief can't claim done while its code exists only on the worker's machine."
     (lineage: brief-230)
  2. "Work that shipped through another door closes in one command."
     (lineage: brief-300, 2026-06-09) — implemented in Mechanism B (next cycle)
  3. "Portal-only briefs feel nothing."

Goldens 1 and 3 are covered here. Golden 2 is added in the Mechanism B cycle.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from actions import _parse_target_repo, _check_delivered_gate  # noqa: E402
from state import project_running_json  # noqa: E402


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
