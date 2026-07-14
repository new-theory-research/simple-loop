#!/usr/bin/env python3
"""brief-160 — claim box-identity + reconciliation against the live world.

The claims-lifecycle invariant is: refs/claims/<brief> exists ⟺ the brief is
active on the CLAIMING box. These goldens exercise the reconciliation half:

  - a claim now records which box minted it (box=<host>), readable via
    claim_owner without disturbing tracked refs.
  - reconcile_claims releases OWN-box orphans (claim present, brief not in the
    live working set) LOUDLY, and NEVER reaps a foreign or unknown-owner claim
    (the "never reap on local ignorance" law) — those are observed only.
  - legacy (pre-160) claims that carry only the host:pid:ns nonce still resolve
    to their host, so an own-box legacy orphan is still reapable.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import claim as _claim  # noqa: E402
from claim import (  # noqa: E402
    claim_brief, claim_owner, list_remote_claims, reconcile_claims,
    release_claim, _ref_for,
)


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args],
                          check=check, capture_output=True, text=True)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote.git")
        _git(self.tmp, "init", "--bare", "remote.git")
        self.clone = os.path.join(self.tmp, "clone")
        _git(self.tmp, "clone", "--quiet", self.remote, "clone")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remote_claim_refs(self):
        out = _git(self.remote, "for-each-ref", "--format=%(refname)",
                   "refs/claims/").stdout
        return sorted(r for r in out.splitlines() if r)

    # ── box identity ────────────────────────────────────────────────────
    def test_owner_records_this_box(self):
        self.assertTrue(claim_brief(self.clone, "brief-1", self.remote))
        self.assertEqual(claim_owner(self.clone, "brief-1", self.remote),
                         _claim.claim_box())

    def test_owner_none_when_no_claim(self):
        self.assertIsNone(claim_owner(self.clone, "brief-absent", self.remote))

    def test_explicit_box_is_read_back(self):
        self.assertTrue(claim_brief(self.clone, "brief-2", self.remote, box="boxzilla"))
        self.assertEqual(claim_owner(self.clone, "brief-2", self.remote), "boxzilla")

    def test_list_remote_claims(self):
        claim_brief(self.clone, "brief-a", self.remote)
        claim_brief(self.clone, "brief-b", self.remote)
        self.assertEqual(sorted(list_remote_claims(self.clone, self.remote)),
                         ["brief-a", "brief-b"])

    # ── reconciliation policy ───────────────────────────────────────────
    def test_own_box_orphan_released_loudly(self):
        claim_brief(self.clone, "brief-orphan", self.remote)  # minted by this box
        logged = []
        actions = reconcile_claims(self.clone, self.remote, working_brief_ids=[],
                                   log=logged.append)
        self.assertEqual([a["reason"] for a in actions], ["orphan_claim_released"])
        self.assertEqual(logged, actions)  # loud: every action was surfaced
        self.assertEqual(self._remote_claim_refs(), [])  # ref actually gone

    def test_live_claim_left_alone(self):
        claim_brief(self.clone, "brief-live", self.remote)
        actions = reconcile_claims(self.clone, self.remote,
                                   working_brief_ids=["brief-live"])
        self.assertEqual(actions, [])
        self.assertEqual(self._remote_claim_refs(), [_ref_for("brief-live")])

    def test_pending_merge_counts_as_working(self):
        # A brief mid-merge (pending_merges) is still worked by this box — its
        # claim must survive reconciliation.
        claim_brief(self.clone, "brief-merging", self.remote)
        actions = reconcile_claims(self.clone, self.remote,
                                   working_brief_ids=["brief-merging"])
        self.assertEqual(actions, [])
        self.assertEqual(self._remote_claim_refs(), [_ref_for("brief-merging")])

    def test_foreign_claim_observed_never_reaped(self):
        claim_brief(self.clone, "brief-foreign", self.remote, box="other-box")
        actions = reconcile_claims(self.clone, self.remote, working_brief_ids=[],
                                   this_box="this-box")
        self.assertEqual([a["reason"] for a in actions], ["foreign_claim_observed"])
        self.assertEqual(actions[0]["box"], "other-box")
        # NEVER reaped — the law.
        self.assertEqual(self._remote_claim_refs(), [_ref_for("brief-foreign")])

    def test_unknown_box_claim_observed_never_reaped(self):
        # Mint a claim ref with a subject that carries no parseable owner.
        empty_tree = subprocess.run(
            ["git", "-C", self.clone, "mktree"], input="",
            capture_output=True, text=True).stdout.strip()
        sha = subprocess.run(
            ["git", "-C", self.clone, "-c", "user.name=x", "-c", "user.email=x@y",
             "commit-tree", empty_tree, "-m", "opaque claim no owner"],
            capture_output=True, text=True).stdout.strip()
        _git(self.clone, "push", self.remote, f"{sha}:{_ref_for('brief-opaque')}")
        self.assertEqual(claim_owner(self.clone, "brief-opaque", self.remote), "")
        actions = reconcile_claims(self.clone, self.remote, working_brief_ids=[])
        self.assertEqual([a["reason"] for a in actions],
                         ["unknown_box_claim_observed"])
        self.assertEqual(self._remote_claim_refs(), [_ref_for("brief-opaque")])

    def test_legacy_nonce_only_claim_resolves_to_host(self):
        # brief-151 mint format: "claim <brief> <host>:<pid>:<ns>" (no box=).
        import socket
        host = socket.gethostname()
        empty_tree = subprocess.run(
            ["git", "-C", self.clone, "mktree"], input="",
            capture_output=True, text=True).stdout.strip()
        sha = subprocess.run(
            ["git", "-C", self.clone, "-c", "user.name=x", "-c", "user.email=x@y",
             "commit-tree", empty_tree, "-m", f"claim brief-legacy {host}:999:123456"],
            capture_output=True, text=True).stdout.strip()
        _git(self.clone, "push", self.remote, f"{sha}:{_ref_for('brief-legacy')}")
        self.assertEqual(claim_owner(self.clone, "brief-legacy", self.remote), host)
        # Own-box legacy orphan is reapable.
        actions = reconcile_claims(self.clone, self.remote, working_brief_ids=[],
                                   this_box=host)
        self.assertEqual([a["reason"] for a in actions], ["orphan_claim_released"])
        self.assertEqual(self._remote_claim_refs(), [])

    def test_mixed_batch(self):
        claim_brief(self.clone, "brief-live", self.remote)
        claim_brief(self.clone, "brief-orphan", self.remote)
        claim_brief(self.clone, "brief-foreign", self.remote, box="elsewhere")
        actions = reconcile_claims(self.clone, self.remote,
                                   working_brief_ids=["brief-live"],
                                   this_box=_claim.claim_box())
        by_reason = {a["brief"]: a["reason"] for a in actions}
        self.assertEqual(by_reason, {
            "brief-orphan": "orphan_claim_released",
            "brief-foreign": "foreign_claim_observed",
        })
        self.assertEqual(self._remote_claim_refs(),
                         [_ref_for("brief-foreign"), _ref_for("brief-live")])

    def test_unreachable_remote_is_noop(self):
        bogus = os.path.join(self.tmp, "nope.git")
        self.assertEqual(reconcile_claims(self.clone, bogus, working_brief_ids=[]), [])


if __name__ == "__main__":
    unittest.main()
