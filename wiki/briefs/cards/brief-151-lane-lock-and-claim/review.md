---
title: "brief-151 review — make a program lane a real lock"
brief: brief-151-lane-lock-and-claim
category: review
escalated_at: 2026-06-28
status: awaiting-mattie
recommendation: approve-then-reinstall
---

# Review gate — brief-151: lane partition + atomic claim

!!! abstract "TL;DR"
    **What shipped:** see [closeout.md](closeout.md) — a program lane is now an
    enforced partition (`--lane`), and a brief is claimed via a pushed git ref
    **before** any worktree exists, so two daemons on one repo+lane can never
    both run one brief.

    **Your part:** review the diff (~15 min), then **reinstall + restart the
    daemon** to make it live. This brief edits the daemon's own dispatch/queue
    code — the change is inert until reinstalled. ~5 min.

!!! success "Why it matters"
    This is the keystone of `remote-queens` (portal `wiki/specs/remote-queens/`).
    Until now nothing stopped two daemons against one remote from grabbing one
    brief — both branch, both spawn a worker, the losing push failed *silently*.
    This converts that silent double-execution corruption into clean, visible,
    fail-loud contention.

## What's gated on you

- **Human-gate: review** — read the diff and decide. Worker can't merge
  (Auto-merge: false) and **must not** — this is a self-modification brief.
- **Reinstall + restart** is the load-bearing manual step. The worker ran in a
  worktree and edited a *copy*; the live daemon won't show `--lane`/claim
  behavior until `install.sh` re-copies `lib/` and the service restarts. (Mirrors
  brief-150.) The worker cannot restart the service it runs under.

## Prerequisites

!!! danger "Do not auto-merge; do not expect live behavior pre-reinstall"
    The running daemon executes the *installed* copy of `lib/`. Merging the branch
    changes the repo, not the running process. New behavior goes live only after
    reinstall + restart.

!!! info "Tooling"
    git ≥ 2.x with `--force-with-lease` empty-expect support (verified on 2.50.1).
    `python3` for the test suite.

## Runbook

### Phase 1 — Review the diff

**blocking.** ~15 min.

```bash
git -C ~/claude-projects/simple-loop log --oneline master..brief-151-lane-lock-and-claim
git -C ~/claude-projects/simple-loop diff master...brief-151-lane-lock-and-claim -- lib/ install.sh
```

Focus on `lib/claim.py` (the atomic primitive) and the claim-before-worktree gate
in `lib/actions.py:dispatch()` (~line 1407). Confirm the claim push happens
*before* `ensure_worktree`.

### Phase 2 — Re-run the gate tests yourself

**requires_focus.** ~2 min.

```bash
cd ~/claude-projects/simple-loop   # or the worktree
python3 -m pytest lib/tests/test_lane_and_claim.py -v
python3 -m pytest lib/tests/ lib/queue_test.py -q
```

Expect 12 green in the first, 190 passed in the second. (`test-flow-v2.sh` shows a
**pre-existing** 152/54 baseline — see closeout.md; it is unchanged by this brief.)

### Phase 3 — Merge, reinstall, restart

**blocking.** ~5 min.

```bash
# after approving the diff:
git -C ~/claude-projects/simple-loop checkout master
git -C ~/claude-projects/simple-loop merge --no-ff brief-151-lane-lock-and-claim
bash ~/claude-projects/simple-loop/install.sh   # re-copies lib/ incl. claim.py
# restart the daemon service per your normal restart path
```

## What "works" looks like

- A daemon started with `--lane <name>` only dispatches cards whose `Program:`
  matches that lane; unlabeled cards are skipped by it (fail-closed).
- With no `--lane`, dispatch is identical to today (additive-only).
- When two daemons race one brief, exactly one proceeds; the other logs
  `loop: brief <id> already claimed — skipping` and creates no worktree.

## Alternatives if a gate fails

!!! note "If `--force-with-lease` empty-expect is rejected on a target box's git"
    Escalate before substituting the claim primitive (brief Residue: lease
    portability). Do not swap to a card-status-flip variant unilaterally — that
    was explicitly deferred.

## Resolution options

| Option | When to pick | Action |
|---|---|---|
| **Approve** | Diff is clean, tests green | Merge → `install.sh` → restart |
| **Iterate** | Small diff nit | Comment on the card; re-queue a follow-up |
| **Reject** | Design concern with git-ref claim | Set card `Status: rejected`; worker releases the claim ref |

## Scav recommendation

**Approve, then reinstall.**

The load-bearing contention test (golden ii) passes under barrier-synchronized
threads across 12 rounds, and in the process caught a genuine cycle-2 bug: the
loser's rejection message under *real* concurrency differs from the sequential
case, and was being misclassified as fail-loud. That fix is in this cycle and is
the difference between "clean visible contention" and "scary error log on every
race." The additive invariant (golden iii) holds byte-for-byte, so the no-`--lane`
single-daemon path is unchanged. The only manual risk is forgetting the reinstall
— the diff is correct but inert until then.

## If something breaks mid-runbook

Capture and stop:

- The exact command + full output
- `git -C <repo> for-each-ref refs/claims/` (any leaked claim refs)
- Daemon log lines around the dispatch tick

Drop it in `wiki/briefs/cards/brief-151-lane-lock-and-claim/review-failure-2026-06-28.md`
+ ping me.

## References

- [Brief index](index.md)
- [closeout.md](closeout.md) — what shipped + pass criteria + lessons
