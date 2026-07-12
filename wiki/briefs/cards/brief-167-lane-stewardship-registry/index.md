---
ID: brief-167-lane-stewardship-registry
Branch: brief-167-lane-stewardship-registry
Status: draft
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: new-theory-research/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: []
Depends-on: brief-160-blocked-brief-lifecycle
Tags: [harness, lanes, stewardship, multi-box, remote-queens]
---

# Brief: lane stewardship registry — lanes live on a selected box

!!! abstract "Intent"
    Ratified 2026-07-12 (Mattie: "having LANES live on a selected box is smart —
    that does indeed solve this"). Lane assignment moves from per-box
    config.local.sh into a git-tracked registry on main: one file mapping
    lane → box. The invariant: **every lane lives in exactly one box's roster
    at a time** — which preserves the one-thread-per-program ruling (#74)
    globally without any cross-box mutex, because disjoint rosters make the
    per-box mutex sufficient by construction. "Packing up the laptop" becomes
    one commit: flip the lanes to lady-titania, push, close the lid.

## Plain version

Today each daemon's LOOP_LANE is a machine-local setting, so moving work
between boxes means editing config on both and trusting nobody overlaps. The
registry makes stewardship a *commit*: daemons read their roster from the file
(env/config as fallback for compat), a validator refuses overlapping
assignments, and handoff gets history, blame, and rollback like everything
else on the coordination plane. Git decides; the apiary watches.

## The fix

1. `wiki/programs/lane-registry.md` (or .loop-adjacent — worker decides with
   the reviewer): `lane: box` map, parsed by the same config machinery family.
2. Daemon roster resolution: registry (filtered to own BOX identity) >
   LOOP_LANE env/config (compat fallback, logged as legacy when registry
   exists). Tick-time read — a pushed flip takes effect next tick, no restart.
3. Disjointness validator: registry parse refuses a lane on two boxes, loudly
   (validate-at-parse per the brief-163 doctrine — tiny vocabulary, no rule
   engine).
4. The handoff ceremony documented: one section in the ops docs — flip, push,
   verify via `loop why` + the dance floor that the receiving box picked up.
5. Unassigned lanes = no box runs them (fail-closed, matches lane filtering).

## Guards

- Depends hard on brief-160: a stale claim from the departing box must not
  wedge the receiving box (the claims invariant is what makes handoff safe).
- The apiary is never read for roster decisions — git only (the law).
- #85 (heartbeat visibility) should land first or alongside: an invisible
  steward is an untrusted steward.

## Out of scope

Cross-box load balancing, multi-box same-lane (explicitly forbidden by the
invariant), automatic failover (a dead box's lanes move by human commit, not
by inference — for now).

## Outputs

closeout.md; the handoff runbook section; registry file seeded with the
current live topology (morgan: product lanes; lady-titania: remote-queens).
