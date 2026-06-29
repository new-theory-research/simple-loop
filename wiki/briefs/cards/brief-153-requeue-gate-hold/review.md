---
title: "brief-153 review — re-queue gate hold"
brief: brief-153-requeue-gate-hold
category: review
status: awaiting-mattie
recommendation: approve
---

# Review gate — re-queued human-gate brief must re-hold

!!! abstract "TL;DR"
    **What shipped:** see [closeout.md](closeout.md). One-line: the gate-bypass
    is already closed by generation-scoped projection; this brief adds the
    missing regression guard (`lib/tests/test_requeue_gate_hold.py`, 7 tests).

    **Your part:** confirm "no production code changed, test-only" is the right
    disposition for this class, then approve (~3 min).

!!! success "Why it matters"
    A re-queued `Auto-merge: false` / `Human-gate: review` brief previously
    auto-merged on its second completion with nobody approving (portal#50). The
    girder that prevents that is now load-bearing under test — a future refactor
    can't silently reopen it.

## What's gated on you

- Decide whether a **test-only** resolution is acceptable here, given the brief
  was written expecting a code fix. The investigation found the fix already
  present (`state._current_generation`, brief-249); the durable add is the guard.
- Approve → the regression test merges to `master`.

Worker can't self-approve a `Human-gate: review` brief.

## How to verify (≈3 min)

```bash
# 1. The regression guard passes
python3 -m pytest lib/tests/test_requeue_gate_hold.py -q     # → 7 passed

# 2. Full unit suite green (was 166; +7)
python3 -m pytest lib/tests/ -q                              # → 173 passed

# 3. Flow harness: identical to baseline (no new failures)
bash scripts/test-flow-v2.sh 2>&1 | tail -3                  # → 163 passed, 52 failed (pre-existing)
```

The 52 `test-flow-v2.sh` failures are pre-existing/environmental — confirmed
byte-for-byte identical with the new test file removed (closeout § Pass criteria).

## What "works" looks like

- `test_requeue_after_merge_reholds_does_not_auto_merge` — re-completed brief
  buckets into `awaiting_review`, not `pending_merges`/`history`.
- `test_auto_merge_true_still_merges_on_recompletion` — no over-correction;
  a real auto-merge brief still merges on re-completion.
- `test_stale_gen1_approved_is_ignored_after_redispatch` — pins the mechanism:
  the dispatch boundary is what invalidates the stale approval.

## Resolution options

| Option | When to pick | Action |
|---|---|---|
| **Approve** | Test-only guard is the right close for an already-fixed class | `loop approve brief-153-requeue-gate-hold` |
| **Iterate** | You want a belt-and-suspenders assertion in `actions.dispatch` (fail if re-dispatch skips the `dispatched` event) | re-queue with that task |
| **Reject** | You disagree the underlying class is closed | `loop reject brief-153-requeue-gate-hold` |

## Scav recommendation

**Approve.** The bypass class is closed and the empirical probe + 7 tests prove
it across all four criteria, including the no-over-correction guard. The only
judgment call is accepting a test-only resolution; the alternative (a redundant
code edit) would be theater. The one real residue — generation-scoping assumes a
fresh `dispatched` event on every re-queue — is documented in closeout as a
candidate follow-up assertion, not a blocker.

## References

- [Brief index](index.md)
- [closeout.md](closeout.md) — full forensic record + pass criteria
