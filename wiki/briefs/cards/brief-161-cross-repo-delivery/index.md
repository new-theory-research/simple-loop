---
ID: brief-161-cross-repo-delivery
Branch: brief-161-cross-repo-delivery
Status: draft
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#35", "#36"]
Depends-on: none
Tags: [harness, cross-repo, target-repo, presence-check, merge-pipeline]
---

# Brief: cross-repo delivery — target-repo briefs strand their deliverable

!!! abstract "Intent"
    A brief whose `Target repo` differs from the portal can't be delivered
    correctly: the presence check can't find its artifacts, and the merge pipeline
    lands the bookkeeping while the actual deliverable is stranded in the target
    repo with no PR and nothing tracking it. One mechanism: the harness resolves
    artifacts and merges only against the portal card dir, not the target repo.

## The mechanism

- **#35 — Presence check resolves artifacts only against the portal card dir +
  worktree — Target-repo artifacts can never be found (latent cross-repo
  false-block).**
- **#36 — Target-repo briefs: merge pipeline lands portal bookkeeping but strands
  the deliverable in the target repo — no PR filed, nothing tracks it. Rider:
  approve events record no actor.**

Both are the cross-repo delivery path being unbuilt: presence-check and the merge
pipeline both assume a single repo. (The #36 rider — approve events record no actor
— overlaps the gate/audit mechanism in brief-156; the actor-recording fix there
covers it, so this card carries #36 for the cross-repo delivery gap specifically.)

## Holistic over symptom

Build the target-repo delivery path — artifact resolution against the target
worktree and a real PR/tracking hop in the merge pipeline — once. #35 #36 resolve
together.

## Outputs

- `closeout.md` — the cross-repo delivery path and per-issue confirmation. Close
  #35 #36 with the merge SHA.
- `review.md` — gate runbook.
