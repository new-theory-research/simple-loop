---
ID: brief-155-dirty-tree-daemon-state
Branch: brief-155-dirty-tree-daemon-state
Status: draft
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#2", "#25", "#33", "#46", "#54", "#78"]
Depends-on: none
Tags: [harness, daemon, worktree, dirty-tree, holistic, brick-a]
---

# Brief: daemon state out of the git working tree — the day-one holistic fix

!!! abstract "Intent"
    One mechanism has spawned issue after issue for six weeks: **the daemon and
    its workers keep runtime state (progress.json, runtime-events.jsonl,
    pending-merge.json) inside the tracked git working tree.** Every dispatch,
    merge, and rebase then races the human's checkout and each other. It has been
    patched piecemeal — five separate dirty-tree fixes — instead of at the root.
    This card carries the mechanism as a whole and names **#2 (worktree isolation)
    as the day-one holistic fix** that supersedes the symptom patches.

## The mechanism

Daemon state lives on tracked paths in the same working tree a human edits and
that workers rebase against. That single design choice produces the whole family:

- **#2 — Worktree isolation: daemon and workers should not touch the human's
  working directory.** The holistic fix. Give the daemon/workers their own
  worktree(s) so runtime writes never collide with the human's checkout or with a
  sibling brief. Every other issue below is a symptom of not having this.
- **#25 — Merge pipeline: single-slot `pending-merge.json` clobbers rapid
  approvals; dirty-checkout merge failures retry silently forever.**
- **#33 — Worktree pull sites still log `diverged (0 ahead / 0 behind)` —
  issue-#28 false-positive class not ported from `sync_project_checkout`.**
- **#46 — daemon process died after a failed auto-merge (dirty
  runtime-events.jsonl) — possible regression in the 2026-07-06 install
  (e1e2100).**
- **#54 — progress.json is still tracked on main — clean-worktree rebase
  conflicts when two briefs' dispatch/merge windows overlap.**

## Mechanism history (closed members — prose only, not frontmatter)

The same mechanism was patched symptom-by-symptom before this card existed:
**#5, #28, #29** (all closed). #28 was the `diverged` false-positive whose fix
#33 shows was never fully ported; #5/#29 were earlier dirty-tree merge/rebase
patches. They are named here as history — they are closed, so they do not enter
`Issues:` frontmatter and do not count toward coverage.

## Holistic over symptom

Do **not** land another per-symptom dirty-tree patch. Land #2 — move daemon/worker
runtime state off the human's tracked working tree — and verify it dissolves the
retry loop (#25), the false `diverged` logs (#33), the dirty-file death (#46), and
the progress.json rebase conflict (#54) together. Symptom patches that predate #2
are superseded.

## Guards

- Touching `lib/daemon.sh` is expected for the real fix — but that is a *future
  worker's* job once a human flips this card to `queued`. This card is a draft.
- Prefer the isolation redesign to N more retry/guard patches.

## Outputs

- `closeout.md` — the isolation design that shipped, and confirmation that #25 #33
  #46 #54 each stop reproducing. Close all five open issues with the merge SHA.
- `review.md` — gate runbook.
