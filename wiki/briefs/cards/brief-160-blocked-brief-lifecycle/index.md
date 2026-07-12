---
ID: brief-160-blocked-brief-lifecycle
Branch: brief-160-blocked-brief-lifecycle
Status: draft
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#15", "#27", "#39", "#58", "#59", "#71"]
Depends-on: none
Tags: [harness, blocked, parked, escalation, lifecycle, busy-loop]
---

# Brief: the blocked/parked brief lifecycle — invisible, re-invoked, un-unblockable

!!! abstract "Intent"
    A brief that can't proceed has no coherent lifecycle. It gets re-invoked in a
    busy-loop, wedges with no escalation surface, is falsely parked on a trivial
    conflict, has no first-class unblock path, and leaves a stale `active` card so
    cleared parks stay invisible. One mechanism: the harness has no first-class
    parked/blocked-brief state — so blocked briefs are simultaneously mishandled in
    five ways.

## The mechanism

- **#27 — Queen re-invokes blocked-in-active briefs in an infinite loop (human-gate
  blindness) — burns 30–70min queens, wedges daemon.** The busy-loop.
- **#15 — delivered-gate refusals are log-only — wedged briefs need an escalation
  surface.** The wedge with nowhere to surface.
- **#39 — parked briefs (status:blocked) have no first-class unblock path — and the
  docs don't say progress.json is read from the ref, not the worktree.**
- **#59 — Follow-up to #55/#56: loop the progress.json auto-resolve for
  multi-commit branches; close test fidelity gap.** The false park, now scoped to
  the multi-commit case #55's fix left open.
- **#58 — Parking a brief (rebase-blocked/awaiting_review) leaves card Status:
  active — cleared parks stay invisible to the queue.** The stale-status blind spot.

Root cause: "blocked/parked" isn't a real state with a surface, an unblock path,
and a card-status projection. Give it one — then the queen stops re-invoking it
(#27), wedges surface (#15), unblock is first-class (#39), trivial conflicts
auto-resolve (#59), and cleared parks reappear in the queue (#58).

## Mechanism history (closed member — prose only)

**#55** (rebase-blocked false park; closed — its single-commit fix merged) is the
parent of #59; the multi-commit auto-resolve + test-fidelity gap it left open is
#59, carried here. #55 is named as history and does not enter `Issues:` frontmatter.

## Holistic over symptom

Design the parked-brief lifecycle once — state + surface + unblock path + accurate
card status. The five issues are facets of the same missing state machine.

## Outputs

- `closeout.md` — the lifecycle design and per-issue confirmation. Close #15 #27
  #39 #58 #59 with the merge SHA.
- `review.md` — gate runbook.
