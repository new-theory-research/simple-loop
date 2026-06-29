---
title: "brief-153 closeout — re-queued human-gate brief must re-hold"
brief: brief-153-requeue-gate-hold
category: closeout
status: complete
---

# Closeout — re-queued human-gate brief must re-hold, not auto-merge

!!! abstract "TL;DR"
    The gate-bypass class is **already closed in the current codebase** by
    generation-scoped projection (`state._current_generation`, the brief-249
    fix). What was missing was a regression guard. This brief adds
    `lib/tests/test_requeue_gate_hold.py` — 7 tests that encode the portal#50
    repro (criteria 1–4) and fail loudly if generation-scoping is ever removed.
    No production code changed; the girder is sound and now locked in.

## The replay mechanism (what was found)

The incident (portal#50) was an `Auto-merge: false` / `Human-gate: review`
brief (`fleet-001`) that held correctly on first completion, was re-queued for a
fix pass, then **auto-merged on its second completion** with `approved_by: None`
and a set `merge_sha`. A stale approval/merge intent from the first pass fired on
re-completion.

The merge decision lives in two layers, and both had to be traced:

1. **Daemon routing** (`lib/daemon.sh:1734–1802`). On detecting a worker
   completion, the daemon reads the `Auto-merge:` flag **fresh from the current
   card** on the brief branch (`AM_FLAG`) and routes:
   `true` → `move-to-pending-merges` (auto), else → `move-to-awaiting-review`
   (hold). For an `Auto-merge: false` card this correctly calls
   `move-to-awaiting-review` on *every* completion. This layer was never the
   bug — it re-reads the card each time.

2. **Projector bucketing** (`lib/state.py:project_running_json`). `running.json`
   is *derived* (brief-108-d): card frontmatter is truth for status, and
   `runtime-events.jsonl` is truth for runtime facts. An `active` card is bucketed
   by joining its status with its events:
   - `approved` event present → `pending_merges[]` (will auto-merge)
   - else `completed` event present → `awaiting_review[]` (HOLD)

   This is the layer where the original bypass lived: a carried-over `approved`
   event from the first pass would route the re-completed `active` card straight
   into `pending_merges` — auto-merge with nobody approving.

### Why it no longer fires

`_approved_event` and `_completed_event` read only the **current generation** of
the event log:

```python
def _current_generation(events_for_brief):
    # suffix starting at the LAST `dispatched` event
    last = None
    for i, e in enumerate(events_for_brief):
        if e.get("event") == "dispatched":
            last = i
    return events_for_brief if last is None else events_for_brief[last:]
```

A re-queue always mints a fresh `dispatched` event (`actions.dispatch`,
`lib/actions.py:1463`), which starts generation 2. The first pass's `approved`
(and `merged`) events fall **before** that boundary, so they are scoped out and
cannot bucket the re-dispatched brief. Approval is thus scoped to the *current
dispatch generation*, not the brief id — exactly the direction the brief
prescribed. The `merged` event is non-generation-scoped but is only consulted for
cards whose status is already `merged`; a re-queued card is `active`, so it never
applies.

This is the same state-replay family as the brief-249 re-queue bounce (a stale
`completed` event replaying into `awaiting_review`); that fix generalized to also
close the *approval/merge* replay this brief targets.

## What shipped

| # | Item | Landed as |
|---|---|---|
| 1 | Grounded the replay across both decision layers (daemon routing + projector) | investigation, this closeout |
| 2 | Regression guard encoding the repro (criteria 1–4) | `lib/tests/test_requeue_gate_hold.py` (7 tests) |

Branch: `brief-153-requeue-gate-hold`. No changes to `lib/actions.py`,
`lib/state.py`, or `lib/daemon.sh` — the fix already exists; only a test was added.

## Pass criteria — status

1. **First completion holds** — `test_first_completion_holds_in_awaiting_review`. ✅
2. **Re-queue re-holds, no `merge_sha`, no auto-merge** —
   `test_requeue_after_merge_reholds_does_not_auto_merge`,
   `test_requeue_without_prior_approval_reholds`,
   `test_stale_gen1_approved_is_ignored_after_redispatch`. ✅
3. **Explicit approval after re-completion still merges (gate works)** —
   `test_explicit_approval_after_recompletion_routes_to_merge`. ✅
4. **`Auto-merge: true` still auto-merges on first AND re-completion** —
   `test_auto_merge_true_still_merges_on_first_completion`,
   `test_auto_merge_true_still_merges_on_recompletion`. ✅
5. **Regression test added; suites green; no new flow failures** —
   `python3 -m pytest lib/tests/ -q` → **173 passed** (was 166; +7).
   `bash scripts/test-flow-v2.sh` → **163 passed, 52 failed**, byte-for-byte
   identical to the master baseline with the new file removed (the 52 are
   pre-existing/environmental, incl. the known brief-107 symlink test). **Zero
   new failures.** ✅

### Empirical ground truth (pre-test probe)

A direct `project_running_json` probe on a synthetic `Auto-merge: false` card
replaying `dispatched→completed→approved→merged→dispatched→completed`:

```
1) first completion bucket:        awaiting_review
   gen1 merged bucket:             history
2) RE-completion bucket:           awaiting_review   (auto_merge=False, approved_at=None, merge_sha=None)
4) auto-merge:true re-completion:  pending_merges
```

The `test_stale_gen1_approved_is_ignored_after_redispatch` test pins the
mechanism: the *same* gen-1 `approved` event routes to `pending_merges` without a
re-dispatch, and to `awaiting_review` once the gen-2 `dispatched` event is added —
proving the dispatch boundary is what closes the gate.

## Lessons / residue

- **The guard is the deliverable when the girder already holds.** The honest
  outcome was not a code edit but a test that makes the invariant load-bearing:
  if a future refactor drops `_current_generation` scoping, these 7 tests fail
  immediately instead of a brief silently auto-merging unapproved.
- **Generation-scoping depends on a fresh `dispatched` event on every re-queue.**
  That is the single load-bearing assumption. Any future re-dispatch path that
  skips appending `dispatched` (e.g. re-running a worker in an existing worktree
  without going through `actions.dispatch`) would reopen the class. Worth a lint
  or an assertion if such a path is ever added.
- This worktree has no `.loop/config.sh`; `test-flow-v2.sh` carries pre-existing
  environmental failures unrelated to this brief (baseline confirmed).
