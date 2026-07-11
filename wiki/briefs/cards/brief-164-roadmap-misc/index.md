---
ID: brief-164-roadmap-misc
Branch: brief-164-roadmap-misc
Status: draft
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#1", "#3", "#4"]
Depends-on: none
Tags: [harness, roadmap, misc, field-report, capability]
---

# Brief: roadmap/misc — field report + capability items with no shared bug mechanism

!!! abstract "Intent"
    Three open issues share no root-cause bug mechanism with each other or with the
    named clusters — they are forward-looking roadmap/capability items and one field
    report. Triage's coverage rule (every open issue in exactly one card) requires
    they land somewhere; this is the honest misc card. A human reviewing it should
    likely **split these into separate real briefs** rather than treat them as one
    unit of work.

## The members

- **#1 — Field report: Nevermined hackathon (2 days, 16 briefs, 2 parallel
  daemons).** A retrospective, not a bug. Belongs on the roadmap as source material
  — many of the clustered issues trace back to observations here. Keep open as a
  reference until its observations are all carded, then close.
- **#3 — Context budget visibility: show what's loaded and how much context it
  consumes.** A capability/feature — agent-facing context observability. Distinct
  from the runtime observability cluster (brief-159), which is about daemon status
  signals, not agent context loading.
- **#4 — Module stub: code-quality (review loops, CI, simplify/debug tooling).** A
  module stub — a whole capability area, not a defect. Deserves its own program
  scope when prioritized.

## Why one card, not three (yet)

Holistic-over-symptom clusters *bugs* by mechanism; these aren't bugs sharing a
mechanism, they're independent roadmap items. Grouping them keeps coverage exact
without inventing false mechanism links. The human gate should fan them out into
proper briefs (or a program) at review — this card is a holding pen, explicitly.

## Outputs

- `closeout.md` — disposition of each (split into brief, folded into a program, or
  closed as reference). Close #1 #3 #4 as each is dispositioned, with the SHA.
- `review.md` — gate runbook; recommend the fan-out.
