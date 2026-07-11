# Evaluation — brief-153-requeue-gate-hold

**Verdict:** HOLD for human approval (do NOT auto-merge). Work is sound.
**Evaluated:** 2026-06-29 (queen heartbeat, trigger no_active)
**Gate:** `Auto-merge: false` + `Human-gate: review` → Mattie approves the merge, not the queen.

## What shipped

Diff (`git diff master...brief-153-requeue-gate-hold --stat`) is **test-only**:

- `lib/tests/test_requeue_gate_hold.py` (+185, new) — 7 regression tests
- card `review.md`, `closeout.md`, `reviews/…cycle-1.md` — review artifacts
- `.loop/state/progress.json` — worker bookkeeping

No production code under the brief's Edit-surface (`lib/actions.py`, `lib/state.py`,
`lib/daemon.sh`) changed.

## Why test-only is the right close

The brief expected a code fix for the gate-bypass (a re-queued `Human-gate: review`
brief silently auto-merging on its second completion — portal#50). Investigation
(worker + scav validator) found the bypass class **already closed** by
generation-scoped projection (`state._current_generation`, brief-249): a fresh
`dispatched` event on re-queue invalidates the stale gen-1 approval, so re-completion
re-buckets into `awaiting_review` instead of `pending_merges`. The durable
contribution here is the missing **regression guard** that pins that behavior so a
future refactor can't silently reopen it. A redundant code edit would be theater.

Key tests:
- `test_requeue_after_merge_reholds_does_not_auto_merge` — re-completed brief lands
  in `awaiting_review`, not `pending_merges`/`history`.
- `test_auto_merge_true_still_merges_on_recompletion` — no over-correction.
- `test_stale_gen1_approved_is_ignored_after_redispatch` — pins the mechanism.

## Verification (per validator, not re-run by queen)

- `pytest lib/tests/test_requeue_gate_hold.py -q` → 7 passed
- `pytest lib/tests/ -q` → 173 passed (was 166; +7)
- `test-flow-v2.sh` → 163 passed / 52 failed, byte-identical to baseline with the
  new file removed (pre-existing/environmental).

## Residual (documented, not a blocker)

Generation-scoping assumes a fresh `dispatched` event fires on *every* re-queue. A
belt-and-suspenders assertion in `actions.dispatch` (fail loudly if a re-dispatch
skips the `dispatched` event) is a reasonable follow-up — captured in closeout, not
required for this merge.

## Queen decision

Leave in `awaiting_review`. **Not** writing `pending-merge.json` — doing so would
bypass the human gate, which is the exact failure mode this brief guards against.
Validator recommendation is **approve**; Mattie's call:
`loop approve brief-153-requeue-gate-hold`.
