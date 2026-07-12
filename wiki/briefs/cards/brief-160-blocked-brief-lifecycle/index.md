---
ID: brief-160-blocked-brief-lifecycle
Branch: brief-160-blocked-brief-lifecycle
Status: queued
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#27", "#39", "#58", "#59", "#71", "#62", "#83", "#84"]
Depends-on: none
Tags: [harness, blocked, parked, claims, lifecycle, invariant]
---

# Brief: the blocked/parked brief lifecycle — claims that leak, parks that hide, boards that lie

!!! abstract "Intent"
    A brief that can't proceed has no coherent lifecycle, and the root is a missing
    invariant: **a claim ref exists ⟺ its brief is active on the claiming box.**
    Claims are minted at one site (`lib/claim.py`, brief-151) but released only on
    the happy path, so every abnormal exit leaks one — and a leaked claim wedges the
    queen (re-invocation churn, `#27`), while the brief it stranded stays invisible
    (`#58`), un-unblockable (`#39`), and unaccounted-for across a daemon restart
    (`#71`). One mechanism, four remaining pieces: make the claim invariant real,
    give parked a first-class state, reconcile the board on startup, and finish the
    progress.json auto-resolve (`#58`/`#59`). The escalation half of this card already
    shipped — see *Delivered* below; this brief is the queue-state half.

## The mechanism

The claim ref is the queue's only cross-box truth, and it is the one piece of state
with no lifecycle. Tonight's leak tally — every entry a queue misbehavior traced to
the same missing invariant:

- **rq-001's June claim blocked its own re-run** — a claim outlived its brief and
  refused the re-dispatch.
- **three merged briefs never released** — happy-path merge left the ref standing.
- **the starved-window wedge** — the claim was pushed *before* dispatch's init-commit
  landed (reviewer-64); the worker died in the window, recovery impossible without
  manual ref surgery.
- **serve-009 leaked via sweep auto-route** — the queen saw "already claimed," silently
  skipped down the list (`lib/actions.py:1685` `claim_skip`), and the stranded brief
  never re-entered the queue.

Mechanism: claims are created at dispatch (`lib/actions.py:1675`, `claim_brief`) and
released only on terminal success (`_release_claim_quiet`, called at
`lib/actions.py:1030/1132/1212/2216`). Every *other* exit — park, sweep auto-route,
over-budget, failed dispatch, crash, daemon restart — leaks the ref. A leaked ref is
indistinguishable from a live one, so the queen either re-invokes churn (`#27`) or
skips a brief that will never come back.

## Delivered (done-context — not in active scope)

These shipped while this card sat open; they are named so the remaining scope is exact,
not to be re-built:

- **`#15` — repeated-failure escalation (closed, merge `84ef6b8`).** Delivered-gate
  refusals now park + escalate instead of logging silently. **This closed the `#27`
  busy-loop's human-gate-blindness facet** per its reviewer — the queen no longer
  re-invokes a brief that has escalated. `#27` remains open only for its *claim-leak*
  facet (re-invocation churn from a stale ref), which the claims invariant below closes.
- **hive's Decide/Anomalies shelf split** — parked/unclassifiable state now renders
  honestly; piece 2 populates it with a real `parked` status rather than inventing the
  surface.
- **`queue_stuck` detection** exists; piece 3 turns its "0 cycles" tell into a startup
  check rather than a human glance.

## The fix (four pieces)

### 1. The claims-lifecycle invariant (centerpiece)

**Invariant: `refs/claims/<brief>` exists ⟺ the brief is active on the claiming box.**
The infrastructure (`lib/claim.py` `claim_brief`/`release_claim`) exists; it has no
enforcement. Build:

1. **Claim LAST at dispatch — after the init-commit lands.** Move the `claim_brief`
   call (`lib/actions.py:1675`) to *after* the init-commit is confirmed
   (`_init_commit_already_landed`, `lib/actions.py:1279`). Kills the starved-window
   wedge class: if the worktree/init-commit step fails, no ref was ever pushed, so
   there is nothing to leak and nothing to reap.
2. **Release-or-transfer in the SAME operation at every exit.** Grep every transition
   that removes a brief from `active[]` and pair it with a claim release/transfer in
   the same call: `move_to_pending_merges` (`lib/actions.py:905`),
   `move_to_awaiting_review` (`:936`), the sweep auto-route path (`lib/sweep.py`),
   over-budget, failed-dispatch, and manual-recovery. The release is not a follow-up
   step that a later branch can skip — it is part of the move.
3. **Startup repair + sweep verify the invariant and release loudly.** Extend the
   daemon's `startup_repair` pass (`lib/daemon.sh:192`) and `lib/sweep.py` to check
   each `refs/claims/<brief>` against live `active[]` + process state; a ref with no
   live brief is released with a logged action (never a silent `git update-ref -d`).
4. **Box-aware liveness — never reap on local ignorance.** Once multi-box, verify
   liveness via apiary heartbeats before releasing: a remote box's active claim is
   NOT stale just because this box can't see its process. Local suspicion never reaps
   a remote box's living claim.

**Acceptance:** `loop why`'s `claim_ref` check (`lib/why.py:230`, check #5) already
reports stale refs — its receipts are the test. After the change, a parked/routed/
crashed brief leaves no `claim_ref` failure on the next `why` run, and a re-dispatch
of a released brief passes the check.

### 2. First-class parked state (`#39`)

Parked is a real status value, not an ad-hoc annotation. Make `parked` a first-class
`Status:` the queue understands; render it on hive's Decide/Anomalies shelf (surface
already exists — see *Delivered*); document the unblock path that replaces the escape-
hatch runbook. Update `.loop/prompts/queen.md` so the queen reads `parked` as
"do not dispatch, do not re-invoke — await unblock," not as a dispatchable candidate.
The docs must state that `progress.json` is read from the **ref, not the worktree**
(the `#39` documentation gap).

### 3. Startup board reconciliation (`#71`)

A restarting daemon accounts every `active[]` entry against live processes: re-adopt
the ones whose worker still runs, and release+re-queue the orphans **loudly** (logged
action, not silent). This is where the anomaly shelf's "0 cycles" tell becomes a
startup check instead of something a human has to notice. Builds on the
`startup_repair` pass (`lib/daemon.sh:192`) and shares the invariant-verify from
piece 1 — an orphaned `active[]` entry and a leaked claim are the same fault seen from
two sides.

### 4. progress.json auto-resolve follow-ups (`#58`, `#59`)

- **`#58`** — parking a brief (rebase-blocked / awaiting_review) must not leave the
  card at `Status: active`; the park writes the `parked` status from piece 2 so cleared
  parks become visible to the queue again.
- **`#59`** — extend the progress.json auto-resolve (from `#55`'s single-commit fix)
  to the **multi-commit branch** case, and close the test-fidelity gap so the test
  exercises a real multi-commit rebase, not a one-commit stand-in.

## Success criteria

- Every `active[]`-removing transition releases or transfers its claim in the same
  operation; a grep of `move_to_*` / auto-route / park / over-budget / failed-dispatch
  sites shows no exit path that drops a brief without touching its claim.
- Dispatch claims after the init-commit lands — a failed worktree/init step pushes no
  ref (starved-window wedge cannot occur).
- Startup + sweep verify `refs/claims/*` against live briefs and release orphans with a
  logged action; box-aware liveness gates the release once multi-box (a remote living
  claim survives a local sweep).
- `loop why`'s `claim_ref` check is clean after a park / auto-route / crash / restart,
  and a re-dispatch of a released brief passes it. (Acceptance receipts.)
- `parked` is a real `Status:` value, rendered on the hive shelf, with a documented
  unblock path; `.loop/prompts/queen.md` treats it as non-dispatchable; docs state
  progress.json is read from the ref.
- A restarting daemon re-adopts live workers and re-queues orphans loudly — no
  `active[]` entry with no process and no roster entry survives startup (`#71`).
- Parking writes `parked` (never leaves `Status: active`, `#58`); progress.json
  auto-resolve covers the multi-commit case with a fidelity test (`#59`).

## Guards

- **Never reap on local ignorance.** No release path may delete a claim ref on the sole
  basis that this box can't see the process — box-aware liveness (apiary heartbeats)
  first, once multi-box.
- **Fail loud, never silent.** Every claim release/transfer and every orphan re-queue
  emits a logged action (`log_action`). No silent `git update-ref -d`, no silent skip.
- **Release is part of the move, not a follow-up.** A claim release that lives in a
  separate later step is a leak waiting for the first early-return — pair it with the
  `active[]` removal in the same operation.
- No `conductor` naming.

## Retires

This brief's compensating watchers and manual procedures, retired on merge (added to
the watcher-retirement list):

- **Scav's `doctor` claim-variant patch** — the compensating watcher that scrubbed
  leaked claim refs after the fact.
- **Manual claim-ref surgery** — hand-deleting `refs/claims/<brief>` to recover a
  starved-window wedge.
- **The unblock escape hatch** — the escape-hatch runbook, replaced by piece 2's
  documented first-class unblock path.

## Mechanism history (closed members — prose only)

- **`#15`** (repeated-failure escalation) merged as `84ef6b8`; parks + escalates, and
  closed the `#27` busy-loop's human-gate-blindness facet. History, not frontmatter.
- **`#55`** (rebase-blocked false park) closed — its single-commit fix merged. Parent
  of `#59`; the multi-commit auto-resolve + test-fidelity gap it left open is `#59`,
  carried here in piece 4.
- **portal#56 (stale-claim reaper)** — the cross-repo follow-up named in
  `lib/actions.py:1355` ("Residue: stale-claim reaper is a follow-up"). This card's
  invariant (piece 1) makes the reaper's job structural rather than after-the-fact;
  cited here as the cross-repo tie.

## Outputs

- `closeout.md` — the claims-invariant design, the four pieces, and per-issue
  confirmation. Close `#27` `#39` `#58` `#59` `#71` with the merge SHA.
- `review.md` — gate runbook (Human-gate: review); link the `loop why` `claim_ref`
  receipts as the acceptance proof.

**Tally addendum (2026-07-12 morning):** overnight run leaked another claim on
the PARKED capture brief (released by the director's doctor) — parking is one
of the exit paths piece 1 must make release-in-same-op.
