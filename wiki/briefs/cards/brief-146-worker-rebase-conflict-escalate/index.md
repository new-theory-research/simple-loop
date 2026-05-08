---
ID: brief-146-worker-rebase-conflict-escalate
Branch: brief-146-worker-rebase-conflict-escalate
Status: queued
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - lib/daemon.sh
  - lib/actions.py
Depends-on: _none_
Tags: [harness, daemon, multi-tenant, escalation]
---

# Brief: worker rebase conflict → clean escalate signal + visible Pending row

!!! abstract "Intent"
    When the worker's per-cycle rebase onto `${GIT_REMOTE}/${GIT_MAIN_BRANCH}` fails (conflict against a teammate's commit), surface it as a first-class escalate that hive's Pending panel renders with `kind=rebase-conflict`. Today the rebase aborts and `move_to_awaiting_review` runs, but no `escalate.json` is written, so the row may render as a plain awaiting-review entry without the "human resolution required" framing. The fix is to make rebase-conflict route through the same escalate path other structural failures use.

## Motivation

Mattie hosts a hackathon Saturday 2026-05-09 with a teammate pushing directly to `portal/main` (no daemon, no briefs — just a regular git user). When his commit lands while a worker is mid-cycle, the next tick's rebase will conflict. Today's behaviour (`lib/daemon.sh:333-343`):

```
git -C "$WORKTREE_DIR" rebase --abort 2>/dev/null || true
daemon_log "WORKER: rebase failed for $branch (conflicts) → routed to awaiting_review"
python3 "$DAEMON_LIB_DIR/actions.py" move-to-awaiting-review "$brief_id" "$PROJECT_DIR" \
    rebase-blocked "rebase conflict against main — human resolution required"
notify "$brief_id: rebase conflict → routed to awaiting_review"
```

Two gaps:

1. **No `escalate.json`** — hive's `PendingReason::Escalate` row only fires off `signals/escalate.json` (`crates/hive/src/state.rs:1779`). A `move-to-awaiting-review kind=rebase-blocked` lands in `awaiting_review[]` with `PendingReason::AwaitingReview` (amber `~`) instead of escalate (coral `!`). Mattie's eyes drop to the wrong row during the hackathon.
2. **No diagnostic captured** — the conflicting paths + the teammate's commit SHA never reach the operator. They have to `cd $WORKTREE_DIR && git status` to figure out what blocked.

The card frontmatter is already `kind=rebase-blocked` — that contract holds. This brief just adds the escalate-signal write and surfaces the conflict context.

## Starting context

!!! info "Pointers — read in this order"
    1. `lib/daemon.sh:327-343` — current rebase block + abort + move-to-awaiting-review path
    2. `lib/actions.py:576-661` — `move_to_awaiting_review`; how `kind` flows into `awaiting_review[]` entry
    3. `lib/actions.py:1200-1280` — `push_with_escalate` writes `signals/escalate.json` (existing template for shape)
    4. `crates/hive/src/state.rs:1762-1841` — Pending panel reads `signals/*.json` first, then awaiting_review
    5. `wiki/briefs/cards/brief-145-loop-install-service-reads-main-branch/index.md` — recent small-CLI brief shape
    6. `~/new-theory/portal/wiki/operating-docs/incidents/2026-05-03-harness-pain-points.md` — context on multi-source-of-truth failure modes (frames why we want one canonical signal here)

## Scope

### In

- **Capture conflict context before abort.** Before `git rebase --abort`, run `git -C "$WORKTREE_DIR" diff --name-only --diff-filter=U` to get the conflicted paths and `git -C "$WORKTREE_DIR" log -1 --format='%h %an %s' "${GIT_REMOTE}/${GIT_MAIN_BRANCH}"` for the teammate's HEAD commit info.
- **Write `signals/escalate.json`** with `reason="rebase_conflict_against_main"`, `brief=$brief_id`, `kind="rebase-conflict"`, `conflicted_paths=[…]`, `main_head="<sha> <author> <subject>"`, `worktree="$WORKTREE_DIR"`. Reuse the dict shape that `push_with_escalate` writes (`lib/actions.py:1252-1266`).
- **Keep the `move-to-awaiting-review kind=rebase-blocked` call.** The escalate signal is additive — don't remove the awaiting_review routing. Hive renders the escalate row first; awaiting_review remains the durable record.
- **Notification text** updates to name the conflicting file count + teammate SHA: `"$brief_id: rebase conflict ($N files) vs ${main_head_short} → escalate"`.

### Out

- **No auto-resolution.** Don't run `git rebase --strategy-option=ours` or any merge-driver fallback — the whole point is that taste/intent decisions need a human.
- **No card-status flip.** `kind=rebase-blocked` already lands the brief in `awaiting_review[]`; the card's `Status:` flow is untouched.
- **No new awaiting-review kind.** `rebase-blocked` (existing) and `rebase-conflict` (new escalate kind) coexist; we don't rename either. The escalate signal carries the richer kind for hive labeling; `awaiting_review[]` keeps `kind=rebase-blocked` for backwards compat.
- **No teammate-detection heuristics.** Don't filter "is this commit from a known daemon author?" — that's brief-147's job.

### Residue

- A follow-up could harmonize `kind=rebase-blocked` (awaiting_review) with `kind=rebase-conflict` (escalate) into one taxonomy. Out of scope here.
- A follow-up could add an `escalate-resume` signal that retriggers the worker after the human resolves — also out of scope; today's flow is `loop reject` or manual rebase + `loop set-status active`.

## Cycle plan

- Cycle 1 (`loop-coder`, sonnet) — implement: capture conflict context in `lib/daemon.sh` before `rebase --abort`; emit `escalate.json` via a small helper added to `lib/actions.py` (`emit_rebase_conflict_escalate(paths, brief_id, conflicted_paths, main_head)`); update notify string. Validator runs after.
- Cycle 2 (`loop-coder`, sonnet, cushion) — test: synthetic rebase conflict in `scripts/test-flow-v2.sh` (or new `scripts/test-rebase-conflict.sh`); assert `escalate.json` exists with the expected fields, awaiting_review[] has `kind=rebase-blocked`.

## Verification

```bash
# Synthetic repro — in a sandbox project:
# 1. Stand up a brief on branch X, commit one cycle file
# 2. From outside the worker, push a conflicting change to main
# 3. Trigger one daemon tick

cat .loop/state/signals/escalate.json | jq '.reason, .kind, .conflicted_paths, .main_head'
# expect: "rebase_conflict_against_main", "rebase-conflict", [...], "<sha> <author> <subject>"

cat .loop/state/running.json | jq '.awaiting_review[] | select(.brief == "<id>") | .kind'
# expect: "rebase-blocked"

hive  # Pending panel shows row with `!` (coral, escalate) for the brief
```

## Escalation triggers

- **`escalate.json` already exists** when the rebase fails (e.g. queen wrote one in a prior tick) — don't clobber. Append the conflict context as a `chained_failures[]` field, log `WORKER: rebase conflict during open escalate — chained`, and continue. If chaining shape gets gnarly, escalate the brief itself.
- **`git diff --name-only --diff-filter=U` returns empty** after a failed rebase — means the conflict mode isn't path-level (binary merge, hook failure, etc.). Write the escalate with `conflicted_paths=[]` and `note="non-path conflict; see worker log"`. Don't block on perfect diagnostics.
- **Test fixture can't synthesize a rebase conflict** in CI within 2 cycles — escalate the brief; ship the production code change without the test, file a follow-up to add the fixture once the harness has a multi-actor repro pattern.

## Anti-patterns

- Don't auto-resolve conflicts — escalate is the contract.
- Don't remove the awaiting_review routing — the escalate is additive, not a replacement.
- Don't extend the escalate-kind enum silently — `rebase-conflict` is the only new value here.

## Artifact

- Patch to `lib/daemon.sh` (rebase failure block).
- New helper in `lib/actions.py` (escalate emitter).
- Test in `scripts/test-flow-v2.sh` or `scripts/test-rebase-conflict.sh`.
- `closeout.md` + `review.md` per contracts.

## What this unlocks

Hackathon teammate's commits become safe in the multi-tenant sense: when his work conflicts with an in-flight cycle, hive lights up coral (`!`) in Pending with the conflicting paths and his SHA visible. Mattie can decide in seconds whether to land his change first or rebase the brief manually — no archaeology pass, no swallowed failure.
