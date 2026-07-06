#!/usr/bin/env python3
"""Tests for merge() dirty-tree autostash (issue #28, Python path).

RECEIPT (2026-07-05/06): ft-002 and ft-003 auto-merges looped on
'git merge <branch> returned non-zero exit status 2' for minutes
before hand-landing. Root cause: the main working tree is routinely
dirty (runtime-events.jsonl modified + untracked .loop/ daemon
artifacts). git merge refuses with exit 2 over those uncommitted
changes. The daemon.sh SYNC path already solved this with a
conditional stash (issue #28 fix); the Python merge() path never
got the same treatment.

WHY these tests are load-bearing (engineering rule 7):
  - Acceptance criterion 1: reproduce the EXACT failure on unfixed
    master, prove the fix resolves it. A test that only runs on
    fixed code can't demonstrate the regression guard works.
  - Criteria 2–4: data preservation, clean-tree no-op, and
    preservation of existing gate/conflict logic must each have an
    independent assertion — not just "it didn't crash".

All tests use real git repos (same pattern as
test_requeue_gate_bypass_integration.py) so they exercise the actual
subprocess git path, not a mock.
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

BRIEF = "ft-002"
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


def _setup_project(tmp: Path, brief: str = BRIEF, branch_touches_events: bool = True):
    """Build a minimal git project with main + brief branch, card, events.

    When branch_touches_events=True (the default): the brief branch includes a
    version of runtime-events.jsonl (committed via the branch, as dispatch does
    via plumbing). This means a dirty runtime-events.jsonl on main's working
    tree will cause 'git merge' to exit 2 — "local changes would be overwritten"
    — which is the exact failure mode in the ft-002/ft-003 incident.

    When branch_touches_events=False: the brief branch only adds work.txt.
    The clean-tree scenario uses this to ensure no runtime-events conflict.

    Returns (project, origin) paths. Both are real local repos so
    push/pull succeed without network access.
    """
    origin = tmp / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-q", "-b", "main")

    project = tmp / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@t")
    _git(project, "config", "user.name", "t")
    _git(project, "remote", "add", "origin", str(origin))

    loop_state = project / ".loop" / "state"
    loop_state.mkdir(parents=True)

    if branch_touches_events:
        # Seed commit on main — includes runtime-events.jsonl so the branch can
        # diverge on it (required to reproduce the exit-2 dirty-tree block).
        (loop_state / "runtime-events.jsonl").write_text(
            '{"ts":"2026-07-05T00:00:00Z","event":"dispatched","brief":"' + brief + '"}\n'
        )
        (project / "README").write_text("seed\n")
        _git(project, "add", "README", ".loop/state/runtime-events.jsonl")
        _git(project, "commit", "-q", "-m", "seed")
        _git(project, "push", "-u", "origin", "main", "-q")

        # Brief branch: work commit + runtime-events.jsonl update. This is what
        # makes the dirty-tree block fire: the branch touches the same file that
        # main's working tree has modified.
        _git(project, "checkout", "-b", brief)
        (project / "work.txt").write_text("work output\n")
        (loop_state / "runtime-events.jsonl").write_text(
            '{"ts":"2026-07-05T00:00:00Z","event":"dispatched","brief":"' + brief + '"}\n'
            '{"ts":"2026-07-05T01:00:00Z","event":"completed","brief":"' + brief + '"}\n'
        )
        _git(project, "add", "work.txt", ".loop/state/runtime-events.jsonl")
        _git(project, "commit", "-q", "-m", f"[worker] {brief} task done")
        _git(project, "push", "-u", "origin", brief, "-q")
    else:
        # Clean-tree variant: branch only adds work.txt. No runtime-events
        # conflict possible. Used to verify the no-stash path.
        (project / "README").write_text("seed\n")
        _git(project, "add", "README")
        _git(project, "commit", "-q", "-m", "seed")
        _git(project, "push", "-u", "origin", "main", "-q")

        _git(project, "checkout", "-b", brief)
        (project / "work.txt").write_text("work output\n")
        _git(project, "add", "work.txt")
        _git(project, "commit", "-q", "-m", f"[worker] {brief} task done")
        _git(project, "push", "-u", "origin", brief, "-q")

    # Back to main
    _git(project, "checkout", "main")

    # Card: Status: active, Auto-merge: true
    card_dir = project / "wiki" / "briefs" / "cards" / brief
    card_dir.mkdir(parents=True)
    (card_dir / "index.md").write_text(
        f"---\nID: {brief}\nStatus: active\nAuto-merge: true\n---\n\n# {brief}\n"
    )
    _git(project, "add", f"wiki/briefs/cards/{brief}/index.md")
    _git(project, "commit", "-q", "-m", f"loop: card status -> active for {brief}")
    _git(project, "push", "origin", "main", "-q")

    # .loop scaffold (dirs already created by seed commit setup above)
    loop_dir = project / ".loop"
    loop_dir.mkdir(parents=True, exist_ok=True)
    state_dir = loop_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (loop_dir / "config.sh").write_text("GIT_REMOTE=origin\nGIT_MAIN_BRANCH=main\n")

    return project


def _write_events(project: Path, brief: str = BRIEF):
    """Write runtime-events.jsonl placing brief in pending_merges.

    Overwrites the file (which is tracked on main from _setup_project),
    making main's working tree dirty relative to HEAD — exactly the
    daemon steady state.
    """
    events = [
        {"ts": "2026-07-05T00:00:00Z", "event": "dispatched",
         "brief": brief, "branch": brief},
        {"ts": "2026-07-05T01:00:00Z", "event": "completed",
         "brief": brief, "kind": "complete", "auto_merge": True},
        {"ts": "2026-07-05T01:00:01Z", "event": "approved", "brief": brief},
    ]
    events_path = project / ".loop" / "state" / "runtime-events.jsonl"
    with open(events_path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _write_pending_merge(project: Path, brief: str = BRIEF):
    pm = project / ".loop" / "state" / "pending-merge.json"
    with open(pm, "w") as f:
        json.dump({"brief": brief, "branch": brief, "title": brief}, f)


def _write_log(project: Path):
    log_path = project / ".loop" / "state" / "log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")


def _make_dirty_tree(project: Path, brief: str = BRIEF):
    """Dirty the main working tree exactly as the daemon does:
      - Modify runtime-events.jsonl (tracked, modified — this is what causes
        'git merge' to exit 2: the branch also commits this file, so git
        refuses to overwrite the working-tree dirty version)
      - Create untracked .loop/ daemon artifacts (these do NOT block the merge
        but must survive the autostash+merge cycle, per Rule 10)
    """
    events_path = project / ".loop" / "state" / "runtime-events.jsonl"
    with open(events_path, "a") as f:
        f.write(json.dumps({
            "ts": "2026-07-05T02:00:00Z", "event": "_dirty_test_marker",
            "brief": brief,
        }) + "\n")

    # Untracked artifacts — the ones that cause the real incident
    evals_dir = project / ".loop" / "state" / "evaluations"
    evals_dir.mkdir(parents=True, exist_ok=True)
    (evals_dir / f"{brief}-eval.md").write_text(
        f"# Evaluation for {brief}\n\nSome evaluation content.\n"
    )
    (project / ".loop" / "state" / f"stewardship-log-2026-07-05.md").write_text(
        "Stewardship log entry\n"
    )
    (project / ".loop" / "state" / "last-queen-success.json").write_text(
        json.dumps({"ts": "2026-07-05T02:00:00Z", "brief": brief}) + "\n"
    )


class TestMergeDirtyTreeAutostash(unittest.TestCase):
    """Criterion 1: dirty tree autostash — merge succeeds where it previously failed.

    We can't actually run the PRE-FIX code here (we're testing the fixed version),
    but we DO prove the setup reproduces the failure condition by asserting that
    WITHOUT the autostash (a bare git merge on a dirty tree) git would refuse,
    and that WITH our code the merge completes. This gives the mutation-discriminator:
    the test ONLY passes on the autostash code path.
    """

    def _make_dirty_pending_project(self):
        tmp = Path(tempfile.mkdtemp())
        project = _setup_project(tmp)
        _write_log(project)
        _write_events(project)
        write_running_json(str(project))
        _make_dirty_tree(project)
        _write_pending_merge(project)
        return project, tmp

    def test_bare_git_merge_fails_on_dirty_tree(self):
        """Prove the failure mode: bare 'git merge' exits non-zero on a dirty tree.

        This is the mutation discriminator — if this assertion fails, the setup
        doesn't actually reproduce the incident. The test suite would be testing
        a clean tree, not the dirty-tree scenario.
        """
        project, tmp = self._make_dirty_pending_project()
        try:
            # Verify tree is actually dirty
            status = _git(project, "status", "--porcelain", "--untracked-files=all")
            self.assertTrue(
                status.stdout.strip(),
                "pre-condition: working tree must be dirty before we test the fix"
            )

            # Bare git merge on the dirty tree should fail (exit 2 — the actual incident)
            merge_bare = _git(project, "merge", BRIEF, "--no-ff",
                              "-m", "test bare merge", check=False)
            self.assertNotEqual(
                merge_bare.returncode, 0,
                "bare 'git merge' on a dirty tree must fail — this is the incident we're fixing. "
                "If it passes, the test setup doesn't reproduce the actual failure condition."
            )
        finally:
            # Abort any in-progress merge so git state is clean for teardown
            _git(project, "merge", "--abort", check=False)
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)

    def test_merge_succeeds_with_dirty_tree(self):
        """After the fix: merge() autostashes, merges, and returns True."""
        project, tmp = self._make_dirty_pending_project()
        try:
            paths = init_paths(str(project))
            result = merge(paths)

            self.assertTrue(
                result,
                "merge() must succeed (return True) even when the working tree is dirty. "
                "A dirty tree with a clean-mergeable branch is the normal daemon steady state."
            )

            # Work commit must be in main
            log = _git(project, "log", "main", "--oneline").stdout
            self.assertIn(
                "task done", log,
                "worker's commit must land in main after merge"
            )
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)


class TestMergeDirtyTreeDataPreservation(unittest.TestCase):
    """Criterion 2: untracked daemon artifacts survive the autostash+merge+restore."""

    def test_untracked_files_survive_after_merge(self):
        """Untracked .loop/ files present before merge are still present after."""
        tmp = Path(tempfile.mkdtemp())
        project = _setup_project(tmp)
        _write_log(project)
        _write_events(project)
        write_running_json(str(project))
        _make_dirty_tree(project)
        _write_pending_merge(project)

        # Record which untracked files we expect to survive
        eval_file = project / ".loop" / "state" / "evaluations" / f"{BRIEF}-eval.md"
        stewardship_file = project / ".loop" / "state" / f"stewardship-log-2026-07-05.md"
        queen_file = project / ".loop" / "state" / "last-queen-success.json"

        self.assertTrue(eval_file.exists(), "pre-condition: eval file must exist")
        self.assertTrue(stewardship_file.exists(), "pre-condition: stewardship file must exist")
        self.assertTrue(queen_file.exists(), "pre-condition: queen file must exist")

        try:
            paths = init_paths(str(project))
            result = merge(paths)
            self.assertTrue(result, "merge must succeed")

            # Untracked files must survive the autostash+merge+restore cycle
            self.assertTrue(
                eval_file.exists(),
                "evaluation file must survive autostash+merge+restore (Rule 10: no silent discard)"
            )
            self.assertTrue(
                stewardship_file.exists(),
                "stewardship log must survive autostash+merge+restore"
            )
            self.assertTrue(
                queen_file.exists(),
                "last-queen-success.json must survive autostash+merge+restore"
            )
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)


class TestMergeCleanTreeUnchanged(unittest.TestCase):
    """Criterion 3: when the tree is clean, merge() behaves byte-for-byte as before.

    No stash is taken; the merge runs directly; the result is the same as the
    pre-fix code path.
    """

    def test_clean_tree_no_stash_taken(self):
        """Clean tree: merge succeeds, no stash created."""
        tmp = Path(tempfile.mkdtemp())
        # branch_touches_events=False: branch only adds work.txt, no
        # runtime-events.jsonl on the branch, so a clean main has no conflict.
        project = _setup_project(tmp, branch_touches_events=False)
        _write_log(project)

        # For this clean-tree test, we need brief in pending_merges. The events
        # file is not tracked on main (branch_touches_events=False), so writing
        # it and committing it keeps the tracked tree clean.
        loop_state = project / ".loop" / "state"
        loop_state.mkdir(parents=True, exist_ok=True)
        _write_events(project)
        write_running_json(str(project))

        # Do NOT dirty the tree — this is the clean-tree test
        _write_pending_merge(project)

        # Verify tree is actually clean (no dirty tracked files)
        tracked_dirty = _git(project, "status", "--porcelain",
                              "--untracked-files=no").stdout.strip()
        self.assertEqual(
            tracked_dirty, "",
            "pre-condition: no dirty tracked files for clean-tree test"
        )

        try:
            # Record stash list before
            stash_before = _git(project, "stash", "list").stdout.strip()

            paths = init_paths(str(project))
            result = merge(paths)

            stash_after = _git(project, "stash", "list").stdout.strip()

            self.assertTrue(result, "merge must succeed on clean tree")

            self.assertEqual(
                stash_before, stash_after,
                "no stash should be created/left when tree is clean (no tracked dirty files)"
            )

            log = _git(project, "log", "main", "--oneline").stdout
            self.assertIn("task done", log, "worker commit must land in main")
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)


class TestMergeExistingGatesPreserved(unittest.TestCase):
    """Criterion 4: existing merge() logic is not regressed by the autostash.

    4a. portal#50 gate: brief not in pending_merges[] → refused even with dirty tree.
    4b. Real content conflict → git merge --abort, returns False, even with dirty tree.
    """

    def test_portal50_gate_still_refuses_with_dirty_tree(self):
        """portal#50 gate must reject a brief not in pending_merges[] even if the tree is dirty.

        An autostash must NOT bypass the gate — the gate runs before the stash
        in the code flow, so this is already true by construction. But we test
        it explicitly to catch any future reordering.
        """
        tmp = Path(tempfile.mkdtemp())
        project = _setup_project(tmp)
        _write_log(project)

        # Events: brief is in awaiting_review, NOT pending_merges
        events = [
            {"ts": "2026-07-05T00:00:00Z", "event": "dispatched",
             "brief": BRIEF, "branch": BRIEF},
            {"ts": "2026-07-05T01:00:00Z", "event": "completed",
             "brief": BRIEF, "kind": "complete", "auto_merge": False},
        ]
        events_path = project / ".loop" / "state" / "runtime-events.jsonl"
        with open(events_path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        write_running_json(str(project))

        # Now dirty the tree
        _make_dirty_tree(project)

        # Write a stale pending-merge.json (this is the portal#50 scenario)
        _write_pending_merge(project)

        try:
            paths = init_paths(str(project))
            result = merge(paths)

            self.assertFalse(
                result,
                "merge() must refuse (return False) when brief is in awaiting_review, "
                "even if the working tree is dirty. The autostash must not bypass the gate."
            )

            # Branch must not be merged
            log = _git(project, "log", "main", "--oneline").stdout
            self.assertNotIn("task done", log, "no merge should have happened")
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)

    def test_real_content_conflict_aborts_with_dirty_tree(self):
        """Real content conflict → merge --abort, returns False, stash restored.

        Set up a conflict: same file edited differently on main and branch.
        Additionally dirty a NON-regenerable tracked file (work-notes.txt)
        before calling merge(). The autostash must restore that file after the
        conflict abort — previously the stash pop fired WHILE the merge was
        in-progress, failed silently, then merge --abort discarded the dirty
        content and left the stash dangling. Three assertions verify the fix:
          (a) stash list is EMPTY after merge() returns
          (b) dirty tracked content is restored to the working tree
          (c) no unmerged index entries remain (git status clean)
        """
        tmp = Path(tempfile.mkdtemp())

        # Custom setup: introduce a real conflict
        origin = tmp / "origin.git"
        origin.mkdir()
        _git(origin, "init", "--bare", "-q", "-b", "main")

        project = tmp / "project"
        project.mkdir()
        _git(project, "init", "-q", "-b", "main")
        _git(project, "config", "user.email", "t@t")
        _git(project, "config", "user.name", "t")
        _git(project, "remote", "add", "origin", str(origin))

        # Seed: shared file that will conflict, plus a non-regenerable tracked
        # file that we'll dirty to prove stash restore works on the conflict path.
        (project / "conflict.txt").write_text("original content\n")
        (project / "work-notes.txt").write_text("original notes\n")
        _git(project, "add", "conflict.txt", "work-notes.txt")
        _git(project, "commit", "-q", "-m", "seed")
        _git(project, "push", "-u", "origin", "main", "-q")

        # Branch: edit conflict.txt one way (work-notes.txt untouched on branch)
        _git(project, "checkout", "-b", BRIEF)
        (project / "conflict.txt").write_text("branch version\n")
        _git(project, "add", "conflict.txt")
        _git(project, "commit", "-q", "-m", f"[worker] {BRIEF} task done")
        _git(project, "push", "-u", "origin", BRIEF, "-q")

        # Main: edit conflict.txt a different way (creates real conflict)
        _git(project, "checkout", "main")
        (project / "conflict.txt").write_text("main version\n")
        _git(project, "add", "conflict.txt")
        _git(project, "commit", "-q", "-m", "main: conflicting edit")
        _git(project, "push", "origin", "main", "-q")

        # Card + .loop
        card_dir = project / "wiki" / "briefs" / "cards" / BRIEF
        card_dir.mkdir(parents=True)
        (card_dir / "index.md").write_text(
            f"---\nID: {BRIEF}\nStatus: active\nAuto-merge: true\n---\n\n# {BRIEF}\n"
        )
        _git(project, "add", f"wiki/briefs/cards/{BRIEF}/index.md")
        _git(project, "commit", "-q", "-m", f"loop: card status -> active for {BRIEF}")
        _git(project, "push", "origin", "main", "-q")

        loop_dir = project / ".loop"
        loop_dir.mkdir(parents=True)
        state_dir = loop_dir / "state"
        state_dir.mkdir(parents=True)
        (loop_dir / "config.sh").write_text("GIT_REMOTE=origin\nGIT_MAIN_BRANCH=main\n")

        _write_log(project)
        _write_events(project)
        write_running_json(str(project))

        # (a) Dirty a NON-regenerable tracked file. runtime-events.jsonl is
        # regenerated by project_running() after merge, which masks data loss.
        # work-notes.txt is committed on main but never touched by the merge or
        # any post-merge codepath — loss here is real and permanent.
        DIRTY_CONTENT = "DIRTY LOCAL — must survive conflict abort\n"
        (project / "work-notes.txt").write_text(DIRTY_CONTENT)

        # Also dirty the tree via _make_dirty_tree (untracked files + events)
        _make_dirty_tree(project)
        _write_pending_merge(project)

        try:
            paths = init_paths(str(project))
            result = merge(paths)

            self.assertFalse(
                result,
                "merge() must return False for a real content conflict, even with dirty tree autostash"
            )

            # (b) Stash list must be EMPTY — no dangling stash left behind.
            stash_list = _git(project, "stash", "list").stdout.strip()
            self.assertEqual(
                stash_list, "",
                "stash list must be empty after conflict abort — a dangling stash means "
                "dirty tracked content was not restored (Rule 10 violation)"
            )

            # (c) Dirty tracked content must be restored in the working tree.
            restored = (project / "work-notes.txt").read_text()
            self.assertEqual(
                restored, DIRTY_CONTENT,
                "dirty tracked file (work-notes.txt) must be restored after conflict abort; "
                "if it shows 'original notes', the stash pop fired before abort and the "
                "merge --abort discarded the dirty content"
            )

            # git must be in a clean state (no in-progress merge, no unmerged markers)
            status_out = _git(project, "status", "--short").stdout
            self.assertNotIn(
                "UU", status_out,
                "no unmerged paths should remain after conflict abort"
            )

            # Branch must still exist (no merge completed)
            branches = _git(project, "branch", "--list", BRIEF).stdout.strip()
            self.assertIn(BRIEF, branches, "brief branch must survive a merge conflict abort")
        finally:
            import shutil
            shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
