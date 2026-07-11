---
ID: brief-162-harness-update-propagation
Branch: brief-162-harness-update-propagation
Status: queued
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#20", "#57"]
Depends-on: none
Tags: [harness, loop-update, propagation, templates]
---

# Brief: harness-update propagation — `loop update` doesn't actually propagate

!!! abstract "Intent"
    When the harness improves, active projects can't absorb the change: `loop
    update` never refreshes project prompt copies, and there's no invokable,
    discoverable path to pull harness updates — the real path is a five-command
    incantation buried in a doc. One mechanism: template/harness updates have no
    working propagation edge into live projects.

## The mechanism

- **#20 — `loop update` never refreshes project prompt copies — template fixes
  silently don't propagate.**
- **#57 — No invokable, discoverable way for an active project to absorb harness
  updates — 'loop update' doesn't propagate, the real path is a 5-command
  incantation in a doc.**

#20 is the specific broken behavior; #57 is the general absence it reveals. Same
mechanism: no reliable propagation command. Fix `loop update` to actually refresh
project copies and make it the discoverable path.

## Holistic over symptom

Make `loop update` the one invokable propagation edge, then #20's silent no-op and
#57's missing path close together.

## Outputs

- `closeout.md` — the propagation fix and per-issue confirmation. Close #20 #57
  with the merge SHA.
- `review.md` — gate runbook.
