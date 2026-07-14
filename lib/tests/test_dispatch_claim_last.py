#!/usr/bin/env python3
"""brief-160 — dispatch claims LAST (after the init-commit lands, before the
branch push). The acceptance surface for piece 1 part 1:

  - happy path: a successful dispatch leaves a claim ref owned by THIS box.
  - the #62 / starved-window case: a failure BEFORE the claim (worktree/init
    step) leaves NO claim ref on the remote — nothing to strand, nothing to reap.
  - race loser aborts clean: a foreign box already holds the claim → dispatch
    returns False, pushes NO branch, tears down its local worktree, and leaves
    the foreign claim untouched.
  - crash-retry idempotency: THIS box already holds the claim (a prior attempt
    crashed mid-dispatch) → dispatch resumes and completes rather than aborting.
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

import actions  # noqa: E402
from actions import dispatch, init_paths  # noqa: E402
from claim import claim_brief, claim_owner, claim_box, _ref_for  # noqa: E402

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
}


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          check=check, capture_output=True, text=True, env=_GIT_ENV)


_CARD = """---
ID: {brief_id}
Status: queued
Parallel-safe: true
---

# {brief_id}
"""


def _seed_project(tmp, brief_id):
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
        f.write(_CARD.format(brief_id=brief_id))

    state_dir = os.path.join(project_dir, ".loop", "state")
    os.makedirs(state_dir)
    with open(os.path.join(state_dir, "running.json"), "w") as f:
        json.dump({"active": [], "awaiting_review": [], "pending_merges": [],
                   "history": [], "completed_pending_eval": []}, f)
    with open(os.path.join(state_dir, "pending-dispatch.json"), "w") as f:
        json.dump({"brief": brief_id, "branch": brief_id,
                   "brief_file": card_repo_path, "notes": ""}, f)

    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "--quiet", "-m", "seed")
    _git(project_dir, "push", "--quiet", "-u", "origin", "main")
    return project_dir, remote, init_paths(project_dir)


class ClaimLastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.brief = "brief-960-claim-last"
        self.project_dir, self.remote, self.paths = _seed_project(self.tmp, self.brief)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remote_claim_refs(self):
        out = _git(self.remote, "for-each-ref", "--format=%(refname)",
                   "refs/claims/").stdout
        return sorted(r for r in out.splitlines() if r)

    def _remote_has_branch(self, branch):
        return bool(_git(self.remote, "for-each-ref",
                         f"refs/heads/{branch}").stdout.strip())

    def _worktree_exists(self):
        return os.path.exists(os.path.join(self.paths["worktrees_dir"], self.brief))

    # ── happy path ──────────────────────────────────────────────────────
    def test_success_leaves_claim_owned_by_this_box(self):
        self.assertTrue(dispatch(self.paths))
        self.assertEqual(self._remote_claim_refs(), [_ref_for(self.brief)])
        self.assertEqual(claim_owner(self.project_dir, self.brief, self.remote),
                         claim_box())
        self.assertTrue(self._remote_has_branch(self.brief))

    # ── #62: failure before the claim leaves no ref ─────────────────────
    def test_failure_before_claim_leaves_no_claim_ref(self):
        """The starved-window wedge cannot occur: if the worktree/init step
        fails, the claim was never pushed."""
        orig = actions.ensure_worktree
        actions.ensure_worktree = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("simulated worktree failure"))
        try:
            with self.assertRaises(RuntimeError):
                dispatch(self.paths)
        finally:
            actions.ensure_worktree = orig
        self.assertEqual(self._remote_claim_refs(), [],
                         "a pre-claim failure must leave NO claim ref to strand the brief")

    # ── race loser aborts clean ─────────────────────────────────────────
    def test_foreign_claim_loser_aborts_clean(self):
        # A different box grabbed the claim first.
        self.assertTrue(claim_brief(self.project_dir, self.brief, self.remote,
                                    box="other-box"))
        self.assertFalse(dispatch(self.paths))
        # No branch pushed, worktree torn down, pending-dispatch consumed.
        self.assertFalse(self._remote_has_branch(self.brief),
                         "loser must NOT push its branch")
        self.assertFalse(self._worktree_exists(),
                         "loser must tear down its local worktree")
        self.assertFalse(os.path.exists(self.paths["pending_dispatch"]))
        # The foreign claim is left exactly as it was — never reaped.
        self.assertEqual(self._remote_claim_refs(), [_ref_for(self.brief)])
        self.assertEqual(claim_owner(self.project_dir, self.brief, self.remote),
                         "other-box")

    # ── crash-retry resumes ─────────────────────────────────────────────
    def test_own_claim_retry_resumes_and_completes(self):
        # This box already holds the claim from a prior crashed attempt.
        self.assertTrue(claim_brief(self.project_dir, self.brief, self.remote))
        # Dispatch sees its own claim, resumes, and completes.
        self.assertTrue(dispatch(self.paths))
        self.assertTrue(self._remote_has_branch(self.brief))
        with open(self.paths["running_file"]) as f:
            rc = json.load(f)
        self.assertIn(self.brief, {e.get("brief") for e in rc.get("active", [])})
        self.assertEqual(self._remote_claim_refs(), [_ref_for(self.brief)])


if __name__ == "__main__":
    unittest.main()
