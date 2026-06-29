#!/usr/bin/env python3
"""Integration test for portal#50: re-queued human-gate brief bypasses gate on
second completion when a stale pending-merge.json is present.

RECEIPT (2026-06-28): fleet-001 (Auto-merge:false / Human-gate:review) held
correctly in awaiting_review on first completion, was re-queued for a fix pass
(card Status flipped to queued), completed again — daemon logged
'move-to-awaiting-review fleet-001 (human approval required)' immediately
followed by 'cleanup: card status -> merged' and 'Merged fleet-001 to main'.
Approved-by was None; merge_sha was set. Bug: a stale pending-merge.json from a
prior approval cycle (or failed merge) fires the legacy daemon merge path even
when the brief is now in awaiting_review (not pending_merges).

WHY this test is load-bearing (engineering rule 7):
  merge() is the gate-bypass vector. The fix adds a check INSIDE merge() that
  refuses to execute when the brief in pending-merge.json is NOT in pending_merges[]
  of the current projected running.json. Tests that bypass only the projector (like
  brief-153's tests) cannot catch this — the projector can be correct while merge()
  still fires because pending-merge.json is a filesystem signal, not a running.json
  bucket. This test exercises the ACTUAL code path: it writes pending-merge.json,
  writes a running.json with fleet-001 in awaiting_review (not pending_merges), and
  calls merge() — then asserts it refuses before touching git.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from actions import init_paths, merge  # noqa: E402
from state import write_running_json  # noqa: E402

BRIEF = "fleet-001"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": os.environ.get("HOME", "/tmp"),
}


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check, capture_output=True, text=True, env=GIT_ENV,
    )


def _setup_project(tmp: Path):
    """Build a minimal git project with main + fleet-001 branch, card, events.

    Creates a local bare repo as 'origin' so push succeeds in tests.
    """
    # Bare 'remote' repo (origin)
    origin = tmp / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-q", "-b", "main")

    project = tmp / "project"
    project.mkdir()

    # Init working repo
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@t")
    _git(project, "config", "user.name", "t")
    _git(project, "remote", "add", "origin", str(origin))

    # Seed commit on main
    (project / "README").write_text("seed\n")
    _git(project, "add", "README")
    _git(project, "commit", "-q", "-m", "seed")
    _git(project, "push", "-u", "origin", "main", "-q")

    # Create fleet-001 branch with some work
    _git(project, "checkout", "-b", BRIEF, check=True)
    (project / "work.txt").write_text("work\n")
    _git(project, "add", "work.txt")
    _git(project, "commit", "-q", "-m", f"[worker] {BRIEF} cycle 1 task done")
    _git(project, "push", "-u", "origin", BRIEF, "-q")

    # Back to main
    _git(project, "checkout", "main")

    # Create card: Status: active, Auto-merge: false
    card_dir = project / "wiki" / "briefs" / "cards" / BRIEF
    card_dir.mkdir(parents=True)
    (card_dir / "index.md").write_text(
        f"---\nID: {BRIEF}\nStatus: active\nAuto-merge: false\nHuman-gate: review\n---\n\n# {BRIEF}\n"
    )
    _git(project, "add", f"wiki/briefs/cards/{BRIEF}/index.md")
    _git(project, "commit", "-q", "-m", f"loop: card status -> active for {BRIEF}")
    _git(project, "push", "origin", "main", "-q")

    # Write .loop/config.sh so actions.py picks up GIT_MAIN_BRANCH
    loop_dir = project / ".loop"
    loop_dir.mkdir(parents=True)
    state_dir = loop_dir / "state"
    state_dir.mkdir(parents=True)
    (loop_dir / "config.sh").write_text('GIT_REMOTE=origin\nGIT_MAIN_BRANCH=main\n')

    return project


def _write_events(project: Path, events: list):
    """Write runtime-events.jsonl."""
    events_path = project / ".loop" / "state" / "runtime-events.jsonl"
    with open(events_path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _write_stale_pending_merge(project: Path):
    """Write a pending-merge.json as if it came from a prior approval cycle that
    did NOT complete (merge failed, daemon restarted, or was otherwise left stale).
    This is the artefact that fires the legacy daemon merge path."""
    pm = project / ".loop" / "state" / "pending-merge.json"
    with open(pm, "w") as f:
        json.dump({"brief": BRIEF, "branch": BRIEF, "title": BRIEF}, f)
        f.write("\n")


def _write_log(project: Path):
    """Write an empty log.jsonl so actions.py log_action doesn't crash."""
    lf = project / ".loop" / "state" / "log.jsonl"
    lf.write_text("")


class TestRequeueStalePendingMergeBypass(unittest.TestCase):
    """Criterion 1: the bypass IS reproducible against the ACTUAL merge() code.

    Before the fix: merge() proceeds when pending-merge.json is present, even
    if the brief is in awaiting_review[] not pending_merges[] — gate bypass.
    After the fix: merge() refuses when the brief is not in pending_merges[].
    """

    def _make_project_in_awaiting_review(self):
        """
        Set up the exact state that triggered the fleet-001 bypass:
          - fleet-001 card: Status: active, Auto-merge: false
          - Events: gen1 dispatched + completed (held), gen2 dispatched + completed
          - running.json projected: fleet-001 in awaiting_review[], NOT pending_merges[]
          - stale pending-merge.json present
        """
        tmp = Path(tempfile.mkdtemp())
        project = _setup_project(tmp)
        _write_log(project)

        # Events: two dispatch+complete cycles with no approval — should stay in
        # awaiting_review after the second completion.
        events = [
            {"ts": "2026-06-01T00:00:00Z", "event": "dispatched", "brief": BRIEF, "branch": BRIEF},
            {"ts": "2026-06-01T01:00:00Z", "event": "completed", "brief": BRIEF,
             "kind": "complete", "auto_merge": False},
            {"ts": "2026-06-02T00:00:00Z", "event": "dispatched", "brief": BRIEF, "branch": BRIEF},
            {"ts": "2026-06-02T01:00:00Z", "event": "completed", "brief": BRIEF,
             "kind": "complete", "auto_merge": False},
        ]
        _write_events(project, events)
        write_running_json(str(project))

        # Verify the projector has fleet-001 in awaiting_review (not pending_merges)
        running_path = project / ".loop" / "state" / "running.json"
        with open(running_path) as f:
            rc = json.load(f)
        ar = [e.get("brief") for e in rc.get("awaiting_review", [])]
        pm = [e.get("brief") for e in rc.get("pending_merges", [])]
        self.assertIn(BRIEF, ar, "pre-condition: brief must be in awaiting_review")
        self.assertNotIn(BRIEF, pm, "pre-condition: brief must NOT be in pending_merges")

        _write_stale_pending_merge(project)
        return project, tmp

    def test_stale_pending_merge_does_not_bypass_gate(self):
        """After the fix: merge() refuses when brief is in awaiting_review not pending_merges.

        This is the load-bearing regression guard for portal#50.
        """
        project, tmp = self._make_project_in_awaiting_review()
        try:
            paths = init_paths(str(project))

            # Call merge() — this is the exact code path the daemon takes via
            # the legacy pending-merge.json check.
            result = merge(paths)

            # After the fix, merge() must refuse (return False) and NOT alter git.
            self.assertFalse(
                result,
                "merge() must refuse when brief is in awaiting_review (not pending_merges). "
                "If this assertion fails, the gate bypass is live — a re-queued "
                "Auto-merge:false brief can be merged with approved_by=None."
            )

            # The brief branch must still exist (no merge happened).
            branches = _git(project, "branch", "--list", BRIEF).stdout.strip()
            self.assertIn(BRIEF, branches,
                          "fleet-001 branch must survive — no merge occurred")

            # Main must not contain the work commit.
            log = _git(project, "log", "main", "--oneline").stdout
            self.assertNotIn("cycle 1 task done", log,
                             "worker's cycle commit must not be in main — no merge occurred")

            # pending-merge.json must be cleaned up by the guard (not left to fire again).
            pm_path = project / ".loop" / "state" / "pending-merge.json"
            self.assertFalse(
                pm_path.exists(),
                "stale pending-merge.json must be removed by the guard so it can't fire again"
            )
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)

    def test_legitimate_pending_merge_still_executes(self):
        """Criterion 3 no-over-correction: a brief that IS in pending_merges[] merges OK.

        This is the same setup but with an approved event in the current generation,
        which puts fleet-001 in pending_merges[]. merge() must proceed.
        """
        tmp = Path(tempfile.mkdtemp())
        project = _setup_project(tmp)
        _write_log(project)

        events = [
            {"ts": "2026-06-01T00:00:00Z", "event": "dispatched", "brief": BRIEF, "branch": BRIEF},
            {"ts": "2026-06-01T01:00:00Z", "event": "completed", "brief": BRIEF,
             "kind": "complete", "auto_merge": True},
            {"ts": "2026-06-01T01:00:01Z", "event": "approved", "brief": BRIEF},
        ]
        _write_events(project, events)
        write_running_json(str(project))

        running_path = project / ".loop" / "state" / "running.json"
        with open(running_path) as f:
            rc = json.load(f)
        pm = [e.get("brief") for e in rc.get("pending_merges", [])]
        self.assertIn(BRIEF, pm, "pre-condition: brief must be in pending_merges for this test")

        _write_stale_pending_merge(project)
        try:
            paths = init_paths(str(project))
            result = merge(paths)
            self.assertTrue(
                result,
                "merge() must succeed for a legitimate pending_merges entry. "
                "The fix must not block genuine auto-merge."
            )
            # Work commit must be in main now.
            log = _git(project, "log", "main", "--oneline").stdout
            self.assertIn("cycle 1 task done", log,
                          "worker's commit must now be in main after legitimate merge")
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)

    def test_stale_pending_merge_for_nonexistent_brief_is_cleared(self):
        """pending-merge.json pointing to a brief not in any bucket → cleared, not merged."""
        tmp = Path(tempfile.mkdtemp())
        project = _setup_project(tmp)
        _write_log(project)

        # No events at all → projector produces empty running.json
        write_running_json(str(project))

        # Stale pending-merge.json for a brief that's not in any active bucket
        _write_stale_pending_merge(project)
        try:
            paths = init_paths(str(project))
            result = merge(paths)
            self.assertFalse(result, "merge must refuse for brief not in pending_merges")
            pm_path = project / ".loop" / "state" / "pending-merge.json"
            self.assertFalse(pm_path.exists(), "stale file must be cleaned up")
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)


class TestRequeueNoOverCorrection(unittest.TestCase):
    """Criterion 3: a human-approved brief (loop approve) still merges after re-queue+recompletion."""

    def test_human_approved_requeue_recompletion_merges(self):
        """gen1: held. gen2: completed + human approved in gen2 → pending_merges → merges."""
        tmp = Path(tempfile.mkdtemp())
        project = _setup_project(tmp)
        _write_log(project)

        events = [
            # Gen 1: held, no approval
            {"ts": "2026-06-01T00:00:00Z", "event": "dispatched", "brief": BRIEF, "branch": BRIEF},
            {"ts": "2026-06-01T01:00:00Z", "event": "completed", "brief": BRIEF,
             "kind": "complete", "auto_merge": False},
            # Re-queue dispatch (gen 2)
            {"ts": "2026-06-02T00:00:00Z", "event": "dispatched", "brief": BRIEF, "branch": BRIEF},
            {"ts": "2026-06-02T01:00:00Z", "event": "completed", "brief": BRIEF,
             "kind": "complete", "auto_merge": False},
            # Human approves via 'loop approve fleet-001' in gen 2
            {"ts": "2026-06-02T02:00:00Z", "event": "approved", "brief": BRIEF},
        ]
        _write_events(project, events)
        write_running_json(str(project))

        running_path = project / ".loop" / "state" / "running.json"
        with open(running_path) as f:
            rc = json.load(f)
        pm = [e.get("brief") for e in rc.get("pending_merges", [])]
        self.assertIn(BRIEF, pm, "after explicit gen-2 approval, brief must be in pending_merges")

        # Write pending-merge.json (as process_pending_merges would)
        _write_stale_pending_merge(project)
        try:
            paths = init_paths(str(project))
            result = merge(paths)
            self.assertTrue(result, "explicit gen-2 human approval must allow merge")
            log = _git(project, "log", "main", "--oneline").stdout
            self.assertIn("cycle 1 task done", log, "merged commit must be in main")
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
