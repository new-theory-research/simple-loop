#!/usr/bin/env python3
"""Regression tests for issue #29 — dispatch projection divergence.

On dispatch, actions.py flipped a brief card's Status: queued -> active via
git-plumbing (lib/git_plumbing.py), which commits directly to
refs/heads/<main> through a throwaway temp index — by design it never
touches project_dir's real .git/index or working tree (git_plumbing.py:6-9).
That's correct when project_dir has drifted onto some other branch
(brief-150, the case the plumbing was built for), but project_dir normally
IS checked out to main_branch — the daemon's ordinary layout — and there,
skipping the sync left the on-disk card and the real index at the OLD
"queued" content while HEAD (the ref the plumbing just moved) said "active":
a staged divergence. lib/queue.py:36 (_parse_card_status) and
lib/state.py:174-177 (_walk_cards) both read the card straight off disk, so
they kept reporting the just-dispatched brief as queued/dispatchable forever,
and running.json.active stayed empty. The queen re-invoked every tick,
burning a full invocation per tick for zero progress (2026-06-29 escalation;
receipts: fleet-003/004/005a and ft-002, 2026-07-05 ~20:30).

These tests exercise actions.dispatch() end-to-end against a throwaway repo +
bare remote and assert the post-dispatch invariant the fix restores:
working-tree card status, HEAD card status, and running.json.active must all
agree — plus that a failed projection write aborts the dispatch loudly
instead of reporting success on a half-projected state (rule 10).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from actions import dispatch, init_paths  # noqa: E402


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
}


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check, capture_output=True, text=True, env=_GIT_ENV,
    )


_CARD_TEMPLATE = """---
ID: {brief_id}
Status: queued
Parallel-safe: true
---

# {brief_id}
"""


def _seed_project(tmp, brief_id):
    """Build a bare remote + a project_dir clone with one queued card,
    pending-dispatch.json, and an empty running.json — the minimum fixture
    actions.dispatch() needs. Returns (project_dir, card_repo_path, paths)."""
    remote = os.path.join(tmp, "remote.git")
    _git(tmp, "init", "--quiet", "--bare", "remote.git")

    project_dir = os.path.join(tmp, "project")
    os.makedirs(project_dir)
    _git(project_dir, "init", "--quiet", "-b", "main")
    _git(project_dir, "remote", "add", "origin", remote)

    card_repo_path = f"wiki/briefs/cards/{brief_id}/index.md"
    card_path = os.path.join(project_dir, card_repo_path)
    os.makedirs(os.path.dirname(card_path))
    with open(card_path, "w") as f:
        f.write(_CARD_TEMPLATE.format(brief_id=brief_id))

    state_dir = os.path.join(project_dir, ".loop", "state")
    os.makedirs(state_dir)
    with open(os.path.join(state_dir, "running.json"), "w") as f:
        json.dump({"active": [], "awaiting_review": [], "pending_merges": [],
                   "history": [], "completed_pending_eval": []}, f)
    with open(os.path.join(state_dir, "pending-dispatch.json"), "w") as f:
        json.dump({
            "brief": brief_id,
            "branch": brief_id,
            "brief_file": card_repo_path,
            "notes": "",
        }, f)

    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "--quiet", "-m", "seed")
    _git(project_dir, "push", "--quiet", "-u", "origin", "main")

    return project_dir, card_repo_path, init_paths(project_dir)


def _card_status(content):
    for line in content.splitlines():
        if line.strip().lower().startswith("status:"):
            return line.split(":", 1)[1].strip().lower()
    return None


class TestDispatchProjectionInvariant(unittest.TestCase):
    """End-to-end: dispatch() must leave working tree, HEAD, and
    running.json in agreement (issue #29)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.brief_id = "brief-900-projection-test"
        self.project_dir, self.card_repo_path, self.paths = _seed_project(
            self.tmp, self.brief_id
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _working_tree_card_status(self):
        with open(os.path.join(self.project_dir, self.card_repo_path)) as f:
            return _card_status(f.read())

    def _head_card_status(self):
        content = _git(self.project_dir, "show", f"HEAD:{self.card_repo_path}").stdout
        return _card_status(content)

    def test_dispatch_succeeds(self):
        self.assertTrue(dispatch(self.paths))

    def test_working_tree_head_and_running_json_agree(self):
        """The core regression: pre-fix, HEAD flipped to active while the
        working-tree card (and index) stayed at queued forever — the exact
        divergence queue.py/state.py fall for."""
        dispatch(self.paths)

        self.assertEqual(self._head_card_status(), "active")
        self.assertEqual(
            self._working_tree_card_status(), "active",
            "working-tree card reverted to queued post-dispatch — the "
            "issue #29 divergence",
        )

        with open(self.paths["running_file"]) as f:
            rc = json.load(f)
        active_ids = {e.get("brief") for e in rc.get("active", [])}
        self.assertIn(
            self.brief_id, active_ids,
            "running.json.active stayed empty post-dispatch",
        )

    def test_no_staged_divergence_on_card_after_dispatch(self):
        """Pre-fix, `git status` showed the card as a staged modification —
        the real index (and working tree) still at 'queued' vs the
        newly-advanced HEAD at 'active'. Post-fix, index and worktree must
        match the new HEAD exactly: no staged or unstaged diff on the card."""
        dispatch(self.paths)

        staged = _git(self.project_dir, "diff", "--cached", "--", self.card_repo_path).stdout
        unstaged = _git(self.project_dir, "diff", "--", self.card_repo_path).stdout
        self.assertEqual(staged, "", f"card staged-diverged from HEAD: {staged!r}")
        self.assertEqual(unstaged, "", f"card working-tree diverged from index: {unstaged!r}")

    def test_queue_py_no_longer_sees_dispatched_brief_as_dispatchable(self):
        """The observable symptom: queue.py reads the working tree directly.
        Pre-fix it kept listing the just-dispatched brief as dispatchable,
        which is what drove the queen's every-tick re-invocation."""
        dispatch(self.paths)

        loop_queue_path = os.path.join(_LIB_DIR, "queue.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("loop_queue_test", loop_queue_path)
        loop_queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loop_queue)

        dispatchable = loop_queue.enumerate_dispatchable(self.project_dir)
        ids = {c["brief"] for c in dispatchable}
        self.assertNotIn(self.brief_id, ids)


class TestDispatchFailsLoudOnProjectionWriteFailure(unittest.TestCase):
    """Rule 10: a failed projection write must abort the dispatch loudly, not
    report success on a half-projected state."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.brief_id = "brief-901-fail-loud"
        self.project_dir, self.card_repo_path, self.paths = _seed_project(
            self.tmp, self.brief_id
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_failure_raises_and_leaves_pending_dispatch(self):
        import actions

        original = actions._sync_worktree_file_from_head
        actions._sync_worktree_file_from_head = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("simulated sync failure")
        )
        try:
            with self.assertRaises(RuntimeError):
                dispatch(self.paths)
        finally:
            actions._sync_worktree_file_from_head = original

        # No silent success: the queue file must still be there so the
        # daemon's built-in dispatch retry-once (lib/daemon.sh) re-attempts,
        # rather than the dispatch being reported done on a half-projected
        # state.
        self.assertTrue(os.path.exists(self.paths["pending_dispatch"]))

    def test_projection_assertion_raises_if_invariant_fails(self):
        """Direct unit check on the assertion helper itself: if the card
        still reads 'queued' post-write, it must raise, never pass silently."""
        import actions

        actions.dispatch(self.paths)
        # Sabotage the now-active card back to queued on disk, simulating a
        # write that silently failed to take — the assertion must catch it.
        card_path = os.path.join(self.project_dir, self.card_repo_path)
        with open(card_path, "w") as f:
            f.write(_CARD_TEMPLATE.format(brief_id=self.brief_id))

        with self.assertRaises(RuntimeError):
            actions._assert_dispatch_projection(
                self.paths, self.project_dir, "main", self.brief_id,
                self.card_repo_path,
            )


if __name__ == "__main__":
    unittest.main()
