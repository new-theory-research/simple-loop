---
ID: brief-156-gate-audit-model
Branch: brief-156-gate-audit-model
Status: draft
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#16", "#26", "#48", "#52"]
Depends-on: none
Tags: [harness, gate, approval, audit, escalation]
---

# Brief: the gate/audit model — approvals that record no actor and gates that don't hold

!!! abstract "Intent"
    The approval/gate girder records timestamps but not *who* decided, lets
    `Auto-merge: true` silently override a `Human-gate: review`, and lets a waived
    task bounce a brief between states forever. One mechanism: the gate model
    treats approval as a flag flip, not an accountable, witnessed decision that
    actually satisfies the cycle-gate.

## The mechanism

- **#16 — Make escalation resolution capture the human's literal decision +
  witness — a renamed filename is not evidence.**
- **#26 — `Auto-merge: true` overrides `Human-gate: review` — brief merges itself
  while daemon reports 'human approval required'.** The gate bypass; highest
  severity of the four.
- **#48 — `loop approve` records no approver identity — only timestamps.**
- **#52 — `approve --waive` doesn't satisfy the cycle-gate: waived tasks still read
  as tasks_remaining, bouncing briefs active↔awaiting_review forever.**

Root cause across all four: approval/gate state is a set of booleans and
timestamps with no accountable actor and no authoritative "this gate is satisfied"
predicate. Fix the model — record the deciding actor + witness, make
`Human-gate: review` dominate `Auto-merge`, and make a waive genuinely clear the
cycle-gate — rather than patching each symptom.

## Holistic over symptom

#26 (self-merge bypass) and #52 (waive-loop) are the sharp edges, but #16 and #48
are the same missing thing: the gate has no notion of *who* and no single satisfied
predicate. Redesign the model; the four resolve together.

## Guards

- Zero edits to `lib/daemon.sh`/`lib/queue.py` from triage. The eventual fix lives
  in the approval path (`lib/actions.py` / approve command), a future worker's job.

## Outputs

- `closeout.md` — the gate-model change and per-issue confirmation. Close #16 #26
  #48 #52 with the merge SHA.
- `review.md` — gate runbook.
