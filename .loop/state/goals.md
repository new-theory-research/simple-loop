# Goals

simple-loop's own loop, bootstrapped 2026-05-05 per brief-143's ops execution. Briefs about simple-loop's source land here, not in portal. See portal's `wiki/operating-docs/cross-repo-loops.md` for the convention.

## Active program — finish brick 0 + close the gate bypass (2026-06-28)

The May hackathon-hardening queue (briefs 142–151) is **complete**: all merged, rejected, deferred, or abandoned — see portal `wiki/programs/harness-improvements/self-dev-backlog-disposition.md`. brief-151 (lane lock + atomic claim) landed the remote-queens keystone. Two follow-ups close the gaps that grounding the brick-1 launch surfaced:

1. **Finish brick 0's lane wiring** — brief-152: `--lane` reaches the queen's brief *selection*, not just dedup. Without it a lane-scoped daemon still dispatches any queue-head brief, so brick 1 (the lane-pinned Titania queen) can't land. Blocker for remote-queens brick 1. (portal#52.)
2. **Close the re-queue gate bypass** — brief-153: a re-queued `Auto-merge: false` / `Human-gate: review` brief merged with `approved_by: None`. The merge/approval girder failing — high severity. (portal#50.)

## Queued next

1. **brief-154 (issue intake — a `loop-triage-issues` skill that turns issues into cards)** — closes the intake gap the 2026-07-11 director audit found: `loop-file-issue` files issues out, nothing reads them back, so the loop has closed 0 of 11 fixed issues while dozens sit open. New skill at `core/skills/triage-issues/SKILL.md` (mirrors `file-issue`; install.sh's `core/skills/*/` loop carries it — zero install.sh edits). It clusters open issues by root-cause mechanism (holistic over symptom), emits `Status: draft` / `Program: harness-improvements` cards with an open-issues-only `Issues:` back-link field, plus a `comment-plan.md` — no tracker writes during triage; "tracked as brief-NNN" comments post only via a gated step after human review. First run against every issue open at run time is a deliverable (cluster A cites #2 as the day-one holistic fix). No daemon/queue edits. Opus, Auto-merge: false, Human-gate: review, Parallel-safe: false. Depends-on: _none_. Canonical at `wiki/briefs/cards/brief-154-issue-intake-triage/index.md`.

## Disposition — prior queue (2026-07-11)

The two entries that sat here are resolved; kept for history:

- **brief-152 (queen lane wiring — finish brick 0)** — **merged** (merge `744eb06`, 2026-06-29; card `Status: merged`). `--lane` now reaches the queen's brief selection; single-daemon path byte-for-byte unchanged. Canonical at `wiki/briefs/cards/brief-152-queen-lane-wiring/index.md`.
- **brief-153 (re-queued human-gate brief must re-hold)** — **not-doing** (superseded). The re-queue gate concern folds into the gate/audit-model cluster (#16 #26 #48 #52) that brief-154's triage will card holistically rather than as a one-off. Canonical at `wiki/briefs/cards/brief-153-requeue-gate-hold/index.md`.
