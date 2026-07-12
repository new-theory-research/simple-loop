---
ID: brief-159-runtime-observability
Branch: brief-159-runtime-observability
Status: draft
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#31", "#38", "#53"]
Depends-on: none
Tags: [harness, observability, sweep, status, false-positive]
---

# Brief: runtime observability — status/sweep signals that cry wolf

!!! abstract "Intent"
    The signals directors trust to see what the daemon is doing are wrong: `loop
    status` reports the clone you're standing in rather than the running daemon,
    and the sweep's freeze/subprocess predicates fire false positives that block
    auto-route. One mechanism: runtime observables are derived from the wrong
    source (local clone, command-line substring, dispatch age) instead of the
    running process's actual state.

## The mechanism

- **#31 — `loop status` reports whatever clone you're standing in, not the running
  daemon — stale PAUSED signal from a sibling clone misleads directors.**
- **#38 — sweep: iteration-advance predicate cries wolf — anchors 'frozen' to
  dispatch age, not last-advance time (+ latent subprocess-exists false-orphan).**
- **#53 — `loop sweep` subprocess-exists predicate matches PIDs by command-line
  substring — false positives block auto-route.**

Root cause: each observable reads a proxy (local clone state, dispatch timestamp,
argv substring) instead of the authoritative running-daemon state. Fix the source
of truth for runtime status once.

## Mechanism history (closed member — prose only)

**#41** (closed) was an earlier observability false-signal in this family; named as
history, not carried in `Issues:` frontmatter.

## Holistic over symptom

Anchor status and sweep predicates to the running daemon's real state (PID
identity, last-advance time), not the three separate proxies. #31 #38 #53 resolve
together.

## Outputs

- `closeout.md` — the status-source change and per-issue confirmation. Close #31
  #38 #53 with the merge SHA.
- `review.md` — gate runbook.

## Delivered early (2026-07-11, director session)

The hive "Drafts" catch-all — one of this card's cry-wolf signals — was fixed
ahead of the card's flip after it misled the operator's ATC view live (merge
`fix-hive-drafts-taxonomy`; floor now buckets by real Status:, hides terminal
cards, and renders an honest Anomalies section). Remaining scope (#31 #38 #53:
status reads the running daemon, sweep predicates, PID substring matching)
unchanged. Also add when working this card: startup banner should print the
active lane set (noted at lane activation).

## Scope addition (2026-07-11 night, from the queue-stall postmortem)

**The work-liveness instrument.** Every existing surface (log tail, heartbeat,
loop status) reports process-liveness, so a wedged-but-alive daemon reads
healthy on all of them — the director "papering over" was instruments lying,
not judgment failing. Add the one metric that can't lie: **queued > 0 (or
active briefs ready to advance) AND no state-advancing event for N minutes →
RED**, surfaced in `loop status`, the hive floor, and (once #66 lands) a push.
Time-since-last-advance vs queue depth is the whole signal. Related new
issues: #65 (wall-clock ticks — in flight), #66 (wire notify), and Scav's
retry→escalation filing (identical failure N times must raise escalate.json,
not log-and-idle — receipt: delivered-gate refused the same SHA every 15 min
for an hour, silently).
