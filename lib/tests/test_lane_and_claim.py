#!/usr/bin/env python3
"""brief-151 — lane partition + atomic cross-box claim.

Three goldens, each encoding WHY (engineering rule 7):

  i.   --lane X enumerates ONLY lane-X briefs (fail-closed on unlabeled).
  ii.  two daemons, one repo+lane, NEVER both execute one brief — the
       load-bearing contention test. Run sequential AND interleaved-in-threads,
       looped N times, against a real bare remote. The loser creates NO
       worktree (asserted with a mirror of the actions.py dispatch gate).
  iii. no --lane → enumerate_dispatchable is byte-for-byte unchanged — the
       Program: field is never read; additive-only invariant (non-negotiable,
       brief escalation trigger).

WHY golden ii is load-bearing: today nothing stops two daemons against one
remote from grabbing one brief — both branch (the branch name *is* the brief
id), both spawn a worker, and the losing push fails non-fatally so neither backs
off. Two `True` winners (or zero) here means the lease expression is wrong and
silent double-execution corruption is back; that is the brief's top escalation
trigger, not a retry-and-paper situation.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from queue import enumerate_dispatchable  # noqa: E402
from claim import claim_brief, release_claim, _ref_for  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=check, capture_output=True, text=True,
    )


# ── Fixtures ────────────────────────────────────────────────────────────────

def _write_card(cards_dir, brief_id, status="queued", program=None):
    """Write a card with optional Program: lane label."""
    card_dir = os.path.join(cards_dir, brief_id)
    os.makedirs(card_dir, exist_ok=True)
    body = ["---", f"ID: {brief_id}", f"Status: {status}"]
    if program is not None:
        body.append(f"Program: {program}")
    body += ["---", "", f"# {brief_id}", ""]
    with open(os.path.join(card_dir, "index.md"), "w") as f:
        f.write("\n".join(body))


def _write_goals(state_dir, order):
    lines = ["# Goals\n\n## Queued next\n\n"]
    for i, bid in enumerate(order, 1):
        lines.append(f"{i}. **{bid}** — description\n")
    with open(os.path.join(state_dir, "goals.md"), "w") as f:
        f.writelines(lines)


# ── Golden i — lane filter ──────────────────────────────────────────────────

class TestGoldenILaneFilter(unittest.TestCase):
    """--lane X keeps ONLY Program: X cards, in goals order; an unlabeled card
    is fail-closed (excluded) so a lane queen never silently grabs a brief that
    declared no lane."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(cards_dir)
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir)
        # Two alpha, two beta, one unlabeled.
        _write_card(cards_dir, "brief-201-a1", program="alpha")
        _write_card(cards_dir, "brief-202-a2", program="alpha")
        _write_card(cards_dir, "brief-203-b1", program="beta")
        _write_card(cards_dir, "brief-204-b2", program="beta")
        _write_card(cards_dir, "brief-205-none")  # no Program: field
        # goals order interleaves lanes so "in goals.md order" is a real check.
        _write_goals(state_dir, [
            "brief-203-b1", "brief-201-a1", "brief-204-b2",
            "brief-202-a2", "brief-205-none",
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lane_alpha_returns_only_alpha_in_goals_order(self):
        result = [c["brief"] for c in enumerate_dispatchable(self.tmp, lane="alpha")]
        # goals order is b1, a1, b2, a2, none → alpha subset is a1 then a2.
        self.assertEqual(result, ["brief-201-a1", "brief-202-a2"])

    def test_beta_and_unlabeled_absent_from_alpha(self):
        result = [c["brief"] for c in enumerate_dispatchable(self.tmp, lane="alpha")]
        self.assertNotIn("brief-203-b1", result)
        self.assertNotIn("brief-204-b2", result)
        self.assertNotIn("brief-205-none", result)  # fail-closed

    def test_lane_is_case_insensitive(self):
        # Program: is lowercased on read; --lane upper still matches.
        result = [c["brief"] for c in enumerate_dispatchable(self.tmp, lane="ALPHA")]
        self.assertEqual(result, ["brief-201-a1", "brief-202-a2"])

    def test_unknown_lane_returns_empty(self):
        self.assertEqual(enumerate_dispatchable(self.tmp, lane="gamma"), [])


# ── Golden ii — contention (load-bearing) ───────────────────────────────────

class TestGoldenIIContention(unittest.TestCase):
    """One bare remote + two clones racing one claim ref. Exactly one True."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote.git")
        _git(self.tmp, "init", "--bare", "remote.git")
        self.clone_a = os.path.join(self.tmp, "clone_a")
        self.clone_b = os.path.join(self.tmp, "clone_b")
        _git(self.tmp, "clone", "--quiet", self.remote, "clone_a")
        _git(self.tmp, "clone", "--quiet", self.remote, "clone_b")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remote_claim_refs(self):
        """List claim refs present on the bare remote."""
        out = _git(self.remote, "for-each-ref", "--format=%(refname)",
                   "refs/claims/").stdout
        return [r for r in out.splitlines() if r]

    def test_sequential_one_true_one_false(self):
        """First claimer wins (True); the second sees the ref and loses
        (False) — the lease rejection, not an exception."""
        bid = "brief-210-seq"
        self.assertTrue(claim_brief(self.clone_a, bid, self.remote))
        self.assertFalse(claim_brief(self.clone_b, bid, self.remote))
        # Exactly one claim ref minted on the remote.
        self.assertEqual(self._remote_claim_refs(), [_ref_for(bid)])

    def test_release_then_reclaim_succeeds(self):
        """release_claim deletes the ref so a re-queued brief is re-claimable."""
        bid = "brief-211-release"
        self.assertTrue(claim_brief(self.clone_a, bid, self.remote))
        self.assertFalse(claim_brief(self.clone_b, bid, self.remote))
        self.assertTrue(release_claim(self.clone_a, bid, self.remote))
        self.assertEqual(self._remote_claim_refs(), [])
        # Now the previously-losing daemon can claim it.
        self.assertTrue(claim_brief(self.clone_b, bid, self.remote))

    def test_interleaved_threads_exactly_one_winner(self):
        """The real race: two daemons call claim_brief for the SAME ref at the
        same time, looped N times on fresh refs. Each round MUST have exactly
        one True and one False — never two winners (corruption) or zero
        (deadlock). A barrier maximizes overlap of the two git pushes."""
        N = 12
        for i in range(N):
            bid = f"brief-22{i:02d}-race"
            start = threading.Barrier(2)
            results = {}

            def worker(clone, key):
                start.wait()  # release both threads into the push together
                results[key] = claim_brief(clone, bid, self.remote)

            ta = threading.Thread(target=worker, args=(self.clone_a, "a"))
            tb = threading.Thread(target=worker, args=(self.clone_b, "b"))
            ta.start(); tb.start()
            ta.join(); tb.join()

            wins = [k for k, v in results.items() if v is True]
            losses = [k for k, v in results.items() if v is False]
            self.assertEqual(len(wins), 1, f"round {i}: winners={results}")
            self.assertEqual(len(losses), 1, f"round {i}: {results}")
            # The winner's ref is on the remote (refs accumulate across rounds
            # since each round uses a fresh, unreleased bid — so check presence,
            # not that it's the only ref ever minted).
            self.assertIn(_ref_for(bid), self._remote_claim_refs(),
                          f"round {i}: winner's claim ref must exist on remote")

    def test_loser_creates_no_worktree(self):
        """Mirror of the actions.py dispatch gate (lib/actions.py:1409-1424):
        claim FIRST, and create a worktree ONLY on a True claim. The loser must
        never reach worktree creation — claim-first is the whole point."""
        bid = "brief-230-gate"
        worktrees_created = []
        lock = threading.Lock()

        def dispatch_gate(clone, tag):
            # Faithful to actions.dispatch(): if the claim is not won, return
            # without ever calling ensure_worktree().
            if not claim_brief(clone, bid, self.remote):
                return False
            with lock:
                worktrees_created.append(tag)  # stands in for ensure_worktree()
            return True

        start = threading.Barrier(2)
        outcomes = {}

        def worker(clone, tag):
            start.wait()
            outcomes[tag] = dispatch_gate(clone, tag)

        ta = threading.Thread(target=worker, args=(self.clone_a, "a"))
        tb = threading.Thread(target=worker, args=(self.clone_b, "b"))
        ta.start(); tb.start()
        ta.join(); tb.join()

        # Exactly one dispatch proceeded → exactly one worktree.
        self.assertEqual(len(worktrees_created), 1, outcomes)
        self.assertEqual(sum(1 for v in outcomes.values() if v), 1, outcomes)

    def test_non_contention_failure_is_fail_loud(self):
        """A push failing for ANY reason other than the lease (here: a bogus
        remote) MUST raise, never return False — so dispatch aborts and never
        falls through to worktree creation on an unverified claim (rule 10)."""
        bogus = os.path.join(self.tmp, "does-not-exist.git")
        with self.assertRaises(RuntimeError):
            claim_brief(self.clone_a, "brief-240-bogus", bogus)


# ── Golden iii — no --lane is byte-for-byte unchanged ───────────────────────

class TestGoldenIIIAdditiveInvariant(unittest.TestCase):
    """enumerate_dispatchable(dir) (no lane) is identical to the pre-patch
    behavior: the Program: field is NEVER read, every queued card is a
    candidate. Asserted against an independently-computed legacy expectation on
    a mixed fixture (cards WITH and WITHOUT Program:)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(cards_dir)
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir)
        # Mixed: programmed + unlabeled, queued + non-queued.
        self.cards = [
            ("brief-301-a", "queued", "alpha"),
            ("brief-302-b", "queued", "beta"),
            ("brief-303-none", "queued", None),
            ("brief-304-draft", "draft", "alpha"),     # excluded by status
            ("brief-305-active", "active", None),       # excluded by status
        ]
        for bid, status, program in self.cards:
            _write_card(cards_dir, bid, status=status, program=program)
        # goals order across the queued ones.
        _write_goals(state_dir, ["brief-302-b", "brief-303-none", "brief-301-a"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_lane_matches_legacy_candidate_list(self):
        """The legacy contract: ALL queued cards, in goals order, regardless of
        Program:. Computed here independently of enumerate_dispatchable."""
        expected = ["brief-302-b", "brief-303-none", "brief-301-a"]
        result = [c["brief"] for c in enumerate_dispatchable(self.tmp)]
        self.assertEqual(result, expected)

    def test_no_lane_equals_lane_none_explicit(self):
        """Default arg and explicit lane=None are the same path."""
        self.assertEqual(
            enumerate_dispatchable(self.tmp),
            enumerate_dispatchable(self.tmp, lane=None),
        )

    def test_program_field_does_not_change_no_lane_result(self):
        """Snapshot equality: adding/removing Program: on a card must not move
        the no-lane candidate set (Program is never read when lane is None)."""
        before = enumerate_dispatchable(self.tmp)
        # Flip an unlabeled card to carry a Program, and a labeled one to drop it.
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        _write_card(cards_dir, "brief-303-none", status="queued", program="gamma")
        _write_card(cards_dir, "brief-301-a", status="queued", program=None)
        after = enumerate_dispatchable(self.tmp)
        self.assertEqual(before, after)


# ── Empty/whitespace lane coerces to no-filter (brief-152) ──────────────────

class TestEmptyLaneIsNoFilter(unittest.TestCase):
    """brief-152: a daemon with no lane exports an empty LOOP_LANE and the queen
    invokes `queue.py . --lane "$LOOP_LANE"` — i.e. `--lane ""`. An empty (or
    whitespace-only) lane MUST mean "no filter," byte-for-byte identical to no
    --lane at all — NOT the literal "" key, which would fail-closed against every
    unlabeled card and silently empty the single-daemon queue. This is the
    load-bearing backward-compat guarantee (same class as golden iii)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(cards_dir)
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir)
        # The single-daemon reality: labeled and unlabeled cards side by side.
        _write_card(cards_dir, "brief-401-a", program="alpha")
        _write_card(cards_dir, "brief-402-none")          # no Program: field
        _write_card(cards_dir, "brief-403-b", program="beta")
        _write_goals(state_dir, ["brief-402-none", "brief-401-a", "brief-403-b"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_lane_equals_no_lane(self):
        """`--lane ""` ≡ no `--lane`: the success-criterion golden equality."""
        self.assertEqual(
            enumerate_dispatchable(self.tmp, lane=""),
            enumerate_dispatchable(self.tmp),
        )

    def test_whitespace_lane_equals_no_lane(self):
        """A whitespace-only lane is just as degenerate as empty → no filter."""
        self.assertEqual(
            enumerate_dispatchable(self.tmp, lane="   "),
            enumerate_dispatchable(self.tmp),
        )

    def test_empty_lane_includes_unlabeled_card(self):
        """The fail-closed trap, named: empty lane must NOT drop the unlabeled
        brief that `--lane ""` → lane_key="" would have excluded."""
        result = [c["brief"] for c in enumerate_dispatchable(self.tmp, lane="")]
        self.assertIn("brief-402-none", result)

    def test_nonempty_lane_still_fail_closed(self):
        """Guard: the empty-lane fix must NOT loosen a real lane — alpha still
        excludes the unlabeled and beta cards (151 semantics preserved)."""
        result = [c["brief"] for c in enumerate_dispatchable(self.tmp, lane="alpha")]
        self.assertEqual(result, ["brief-401-a"])


if __name__ == "__main__":
    unittest.main()
