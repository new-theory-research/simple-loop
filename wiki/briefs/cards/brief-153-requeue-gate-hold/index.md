---
ID: brief-153-requeue-gate-hold
Branch: brief-153-requeue-gate-hold
Status: active
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Edit-surface:
  - lib/actions.py
  - lib/state.py
  - lib/daemon.sh
  - lib/tests/test_lane_and_claim.py
Depends-on: _none_
Tags: [harness, daemon, merge, gate, safety]
---

# Brief: a re-queued human-gate brief must re-hold, not auto-merge (gate bypass)

!!! abstract "Intent"
    An `Auto-merge: false` + `Human-gate: review` brief that is **re-queued** for a
    fix pass merged to main on its second completion with `approved_by: None` — the
    human-gate silently bypassed. Re-completion must re-assert the hold exactly like
    the first completion. This is the merge/approval girder failing; fix it.
    (portal#50.)

## Plain version

`fleet-001` was `Auto-merge: false` / `Human-gate: review`. First completion held
correctly in `awaiting_review` (gate worked). It was re-queued for a fix pass (card
`Status:` → `queued`). After the fix cycle, the **second** move-to-awaiting-review
immediately auto-merged:

```
move-to-awaiting-review fleet-001 (human approval required)
cleanup: card status → merged for fleet-001
Merged fleet-001 to main
```

`running.json` receipt: `auto_merge: False` + `approved_by: None` + a set
`merge_sha` — it merged with nobody approving. Re-queuing leaves a stale
merge/approval intent (an `approved`/merge event from the first pass, or a
projection that replays it) that fires on re-completion; the daemon doesn't
re-assert the `Auto-merge: false` hold on the second pass. Same state-replay
family as the queue-recovery-runbook Case 3 (re-queue replaying an old event), but
here it replays an *approval/merge* intent, not a bounce.

## Investigate first (Rule 6)

Ground the exact replay before changing code:
- How does the merge path decide to auto-merge vs hold? Trace `lib/actions.py`
  (the move-to-awaiting-review / merge logic) and `lib/auto_merge.py`.
- How is approval represented? Find where `approved`/`approved_by` /
  `human_approval_required_for_merge` events are written and read
  (`lib/state.py` projector, `runtime-events.jsonl`).
- Why does a re-queue (card `Status:` flip to `queued`, new dispatch) NOT
  invalidate a prior approval/merge intent? The fix likely lives at the boundary
  between "this brief was approved" and "which dispatch/generation was it approved
  for." Approval must be scoped to the *current* dispatch generation, not the brief
  id — a generation-scoped check, mirroring the generation-scoped projector fix
  that solved the brief-249 re-queue bounce.

## The fix (direction — confirm against the investigation)

On completion, the merge decision must require an approval event that is **newer
than the latest dispatch** of the brief (i.e. approval belongs to this cycle). A
re-queue starts a new dispatch generation, so any prior approval no longer
satisfies the gate → the brief re-enters `awaiting_review` and waits. Equivalently:
clear/ignore carried-over approval+merge intent when a brief is re-dispatched.
`Auto-merge: false` must be re-read from the *current* card on every completion.

## Success criteria (the repro must now hold)

1. File a brief `Auto-merge: false`, `Human-gate: review`. Complete it → it holds
   in `awaiting_review` (unchanged).
2. Re-queue it (flip card `Status:` → `queued`, add a task). Let it re-complete →
   it **holds in `awaiting_review` again** with `approved_by: None` and **no
   `merge_sha`**. It must NOT auto-merge.
3. After re-completion, an explicit approval (`loop approve`) merges it — proving
   the gate still *works*, only the bypass is closed.
4. A genuinely `Auto-merge: true` brief still auto-merges on first AND re-completion
   (don't over-correct into blocking legitimate auto-merge).
5. Add a regression test encoding the repro (steps 1–2) in `lib/tests/`.
   `python3 -m pytest lib/tests/ -q` green; `bash scripts/test-flow-v2.sh` adds the
   case without new failures vs the master baseline.

## Guards

- Do NOT weaken legitimate `Auto-merge: true` behavior (criterion 4).
- Approval scoping must be generation/dispatch-scoped, not a blanket "ignore all
  prior approvals" that breaks normal approve→merge.
- No `conductor` naming.

## Outputs

- `closeout.md` — the exact replay mechanism found, the fix, the repro now holding.
- `review.md` — gate runbook; link closeout.
