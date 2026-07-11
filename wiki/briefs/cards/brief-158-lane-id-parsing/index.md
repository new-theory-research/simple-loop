---
ID: brief-158-lane-id-parsing
Branch: brief-158-lane-id-parsing
Status: draft
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#30", "#50"]
Depends-on: none
Tags: [harness, lane, brief-id, parsing, remote-queens]
---

# Brief: lane IDs — unlaned dispatch and the brief-NNN-only regex

!!! abstract "Intent"
    Lane awareness is half-wired: an unlaned daemon grabs another lane's briefs,
    and the brief-ID regex assumes `brief-NNN`, silently dropping lane-prefixed IDs
    (`ft-*`, `capture-*`, `rq-*`) from dependency gating and goals ranking. One
    mechanism: lane identity isn't consistently honored across dispatch and ID
    parsing.

## The mechanism

- **#30 — Unlaned daemon dispatches other lanes' briefs — laptop daemon ran rq-001
  (Program: remote-queens) locally, defeating the brief's purpose.**
- **#50 — `BRIEF_ID_RE` assumes brief-NNN — lane-prefixed IDs (ft-*, capture-*,
  rq-*) silently dropped from Depends-on gating and goals.md ranking.**

Both stem from the ID/lane model predating the lane-prefixed-ID convention. #50 is
the parser gap; #30 is the dispatch-scope gap. Reconcile the lane model once.

## Holistic over symptom

`BRIEF_ID_RE` lives in `assess.py` and is imported by `lint.py` — the canonical
brief-id shape. Widening it to accept lane prefixes and honoring lane scope at
dispatch are the same reconciliation. (Note the guard: `lib/queue.py` is
off-limits to triage; the eventual fix is a future worker's, scoped carefully
around the lane-selection code.)

## Outputs

- `closeout.md` — the lane/ID reconciliation and per-issue confirmation. Close #30
  #50 with the merge SHA.
- `review.md` — gate runbook.
