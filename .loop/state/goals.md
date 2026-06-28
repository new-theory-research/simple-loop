# Goals

simple-loop's own loop, bootstrapped 2026-05-05 per brief-143's ops execution. Briefs about simple-loop's source land here, not in portal. See portal's `wiki/operating-docs/cross-repo-loops.md` for the convention.

## Active program — finish brick 0 + close the gate bypass (2026-06-28)

The May hackathon-hardening queue (briefs 142–151) is **complete**: all merged, rejected, deferred, or abandoned — see portal `wiki/programs/harness-improvements/self-dev-backlog-disposition.md`. brief-151 (lane lock + atomic claim) landed the remote-queens keystone. Two follow-ups close the gaps that grounding the brick-1 launch surfaced:

1. **Finish brick 0's lane wiring** — brief-152: `--lane` reaches the queen's brief *selection*, not just dedup. Without it a lane-scoped daemon still dispatches any queue-head brief, so brick 1 (the lane-pinned Titania queen) can't land. Blocker for remote-queens brick 1. (portal#52.)
2. **Close the re-queue gate bypass** — brief-153: a re-queued `Auto-merge: false` / `Human-gate: review` brief merged with `approved_by: None`. The merge/approval girder failing — high severity. (portal#50.)

## Queued next

1. **brief-152 (wire `--lane` through to brief selection — finish brick 0)** — queen prompt runs `queue.py . --lane "$LOOP_LANE"`; `queue.py` treats empty lane as no-filter (single-daemon byte-for-byte unchanged); `loop install-service --lane` passthrough. Opus, Auto-merge: false, Human-gate: review. Depends-on: brief-151. Canonical at `wiki/briefs/cards/brief-152-queen-lane-wiring/index.md`.

2. **brief-153 (re-queued human-gate brief must re-hold, not auto-merge)** — approval must be scoped to the current dispatch generation; a re-queue invalidates a prior approval so the brief re-enters `awaiting_review` and waits. Repro from portal#50 must hold. Opus, Auto-merge: false, Human-gate: review. Depends-on: _none_. Canonical at `wiki/briefs/cards/brief-153-requeue-gate-hold/index.md`.
