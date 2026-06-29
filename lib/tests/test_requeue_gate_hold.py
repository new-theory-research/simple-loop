#!/usr/bin/env python3
"""brief-153 — a re-queued human-gate brief must re-HOLD, not auto-merge.

The repro (portal#50): an `Auto-merge: false` / `Human-gate: review` brief that
is re-queued for a fix pass merged to main on its *second* completion with
`approved_by: None` — the human-gate silently bypassed. A stale approval/merge
intent from the first pass fired on re-completion.

WHY this test is load-bearing (engineering rule 7): the projector is the
decision layer that buckets an `active` card into awaiting_review (HOLD) vs
pending_merges (will auto-merge) vs history (already merged). The fix that
closes the bypass is generation-scoping — `_current_generation()` slices the
event log at the LAST `dispatched` event, so an `approved`/`merged` event from a
prior dispatch belongs to a previous generation and CANNOT bucket a freshly
re-dispatched brief (same family as the brief-249 re-queue bounce). A re-queue
always mints a fresh `dispatched` event (actions.dispatch), which starts a new
generation and invalidates the carried-over approval.

If a future refactor drops generation-scoping, the gen-1 `approved` event would
leak into gen-2's projection and route the re-completed brief straight into
pending_merges → auto-merge with nobody approving. These tests fail loudly the
moment that happens. They encode success criteria 1–4 of the brief.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from state import project_running_json  # noqa: E402


BRIEF = "fleet-001"


def _ev(event, ts, **kw):
    """One runtime-events.jsonl line for BRIEF."""
    return dict(event=event, brief=BRIEF, ts=ts, **kw)


def _bucket_of(out, brief=BRIEF):
    """Which running.json bucket the projector placed `brief` into (or None)."""
    for k in ("active", "awaiting_review", "pending_merges", "history"):
        if any(e.get("brief") == brief for e in out[k]):
            return k
    return None


def _entry(out, bucket, brief=BRIEF):
    return next((e for e in out[bucket] if e.get("brief") == brief), {})


class _CardProject:
    """Temp project dir with a single card; project_running_json walks the card
    for Status/Auto-merge, events are injected directly (no git/worktree)."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.card_dir = os.path.join(self.tmp, "wiki", "briefs", "cards", BRIEF)
        os.makedirs(self.card_dir)

    def write_card(self, status, auto_merge="false"):
        with open(os.path.join(self.card_dir, "index.md"), "w") as f:
            f.write(
                f"---\nID: {BRIEF}\nStatus: {status}\n"
                f"Auto-merge: {auto_merge}\nHuman-gate: review\n---\n\n# {BRIEF}\n"
            )

    def project(self, status, events, auto_merge="false"):
        self.write_card(status, auto_merge)
        return project_running_json(self.tmp, events=events)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


# Generation-1 events: dispatched → completed (held) → human approves → merged.
_GEN1 = [
    _ev("dispatched", "2026-06-01T00:00:00Z", branch=BRIEF),
    _ev("completed", "2026-06-01T01:00:00Z", kind="complete", auto_merge=False),
]
_GEN1_MERGED = _GEN1 + [
    _ev("approved", "2026-06-01T02:00:00Z"),
    _ev("merged", "2026-06-01T03:00:00Z", merge_sha="aaaa1111"),
]
# Re-queue mints a fresh `dispatched` (generation 2), then the fix cycle completes.
_REDISPATCH = [
    _ev("dispatched", "2026-06-02T00:00:00Z", branch=BRIEF),
    _ev("completed", "2026-06-02T01:00:00Z", kind="complete", auto_merge=False),
]


class TestRequeueGateHold(unittest.TestCase):

    def setUp(self):
        self.p = _CardProject()

    def tearDown(self):
        self.p.cleanup()

    # ── Criterion 1: first completion holds (unchanged) ──────────────────
    def test_first_completion_holds_in_awaiting_review(self):
        out = self.p.project("active", _GEN1)
        self.assertEqual(_bucket_of(out), "awaiting_review")
        e = _entry(out, "awaiting_review")
        self.assertFalse(e.get("auto_merge"))
        self.assertIsNone(e.get("merge_sha"))

    # ── Criterion 2 (load-bearing): re-completion RE-holds, no bypass ────
    def test_requeue_after_merge_reholds_does_not_auto_merge(self):
        """gen-1 was approved AND merged; re-queued for a fix pass. The carried
        merge/approval intent must NOT fire — re-completion holds again."""
        events = _GEN1_MERGED + _REDISPATCH
        out = self.p.project("active", events)  # card re-dispatched → active
        self.assertEqual(
            _bucket_of(out), "awaiting_review",
            "re-completed human-gate brief must HOLD, not bucket into "
            "pending_merges (auto-merge) or history (already-merged)",
        )
        e = _entry(out, "awaiting_review")
        self.assertFalse(e.get("auto_merge"), "auto_merge must be False on the hold")
        self.assertIsNone(e.get("approved_at"), "no carried approval in this generation")
        self.assertIsNone(e.get("merge_sha"), "no merge may have happened")

    def test_requeue_without_prior_approval_reholds(self):
        """Even the simpler shape — gen-1 held (never approved), re-queued —
        must re-hold on the second completion."""
        events = _GEN1 + _REDISPATCH
        out = self.p.project("active", events)
        self.assertEqual(_bucket_of(out), "awaiting_review")

    def test_stale_gen1_approved_is_ignored_after_redispatch(self):
        """The exact replay mechanism: a gen-1 `approved` event present in the
        log is invalidated by the gen-2 `dispatched` event (generation-scoping).
        Drop the re-dispatch and the SAME approved event WOULD route to
        pending_merges — proving the dispatch boundary is what closes the gate."""
        leaky = _GEN1 + [_ev("approved", "2026-06-01T02:00:00Z")]
        # No new dispatched event → still generation 1 → approved is live.
        out = self.p.project("active", leaky)
        self.assertEqual(_bucket_of(out), "pending_merges")
        # Add the re-dispatch: generation 2 starts, the stale approval is scoped out.
        out2 = self.p.project("active", leaky + _REDISPATCH)
        self.assertEqual(_bucket_of(out2), "awaiting_review")

    # ── Criterion 3: an explicit approval in THIS generation still merges ─
    def test_explicit_approval_after_recompletion_routes_to_merge(self):
        """`loop approve` after re-completion appends an `approved` event in the
        current generation → pending_merges. Proves the gate still WORKS; only
        the bypass is closed."""
        events = _GEN1_MERGED + _REDISPATCH + [
            _ev("approved", "2026-06-02T02:00:00Z"),
        ]
        out = self.p.project("active", events)
        self.assertEqual(_bucket_of(out), "pending_merges")

    # ── Criterion 4: don't over-correct — Auto-merge:true still auto-merges ─
    def test_auto_merge_true_still_merges_on_first_completion(self):
        # daemon's move-to-pending-merges appends completed(am=True) + approved
        # in the same generation.
        events = [
            _ev("dispatched", "2026-06-01T00:00:00Z", branch=BRIEF),
            _ev("completed", "2026-06-01T01:00:00Z", kind="complete", auto_merge=True),
            _ev("approved", "2026-06-01T01:00:01Z"),
        ]
        out = self.p.project("active", events, auto_merge="true")
        self.assertEqual(_bucket_of(out), "pending_merges")

    def test_auto_merge_true_still_merges_on_recompletion(self):
        events = _GEN1_MERGED + [
            _ev("dispatched", "2026-06-02T00:00:00Z", branch=BRIEF),
            _ev("completed", "2026-06-02T01:00:00Z", kind="complete", auto_merge=True),
            _ev("approved", "2026-06-02T01:00:01Z"),
        ]
        out = self.p.project("active", events, auto_merge="true")
        self.assertEqual(
            _bucket_of(out), "pending_merges",
            "a genuine Auto-merge:true brief must still auto-merge on re-completion",
        )


if __name__ == "__main__":
    unittest.main()
