---
ID: brief-157-unbounded-llm-subprocesses
Branch: brief-157-unbounded-llm-subprocesses
Status: draft
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#44", "#47", "#49", "#51", "#77", "#81", "#86", "#89"]
Depends-on: none
Tags: [harness, llm, subprocess, budget, circuit-breaker, throttle]
---

# Brief: unbounded LLM subprocesses — no budget wiring, no backoff, no fill

!!! abstract "Intent"
    LLM invocations (queens, workers, validators) run without an enforced ceiling:
    the `Budget:` field is decorative, idle queens balloon, crash loops retry with
    no backoff, and the throttle never fills its slots. One mechanism — the harness
    launches LLM subprocesses without wiring them to a budget/backoff/concurrency
    controller.

## The mechanism

- **#44 — card `Budget:` field is decorative — daemon's max-iterations isn't wired
  to it, no over-budget event fires.**
- **#47 — idle queens occasionally balloon 15–50 min on an empty queue — long
  invocations turn transient API drops into big token burns.**
- **#49 — Worker/validator crash loop on non-retryable API refusals (safety-flag
  false positives) — no backoff, no circuit break.**
- **#51 — Queen only dispatches on `no_active` — THROTTLE>1 slots never fill
  (single-brief serialization).**

Root cause: subprocess lifecycle has no shared controller enforcing a budget
ceiling (#44), a wall-time/idle cap (#47), a retry circuit-breaker (#49), or slot
fill semantics (#51). (Note: the daemon already grew a circuit breaker for the
*queen* on 2026-07-11, commit ab8e91f — this card generalizes that discipline to
the worker/validator/budget surface rather than re-doing it.)

## Mechanism history (closed member — prose only)

**#32** (closed) was an earlier unbounded-subprocess report in this family; named
here as history, not carried in `Issues:` frontmatter.

## Holistic over symptom

One budget/backoff/concurrency controller wired through the invocation path, not
four separate timeouts. Verify #44 #47 #49 #51 each stop reproducing under it.

## Outputs

- `closeout.md` — the controller and per-issue confirmation. Close #44 #47 #49 #51
  with the merge SHA.
- `review.md` — gate runbook.

## Delivered early (2026-07-12): the #44 budget slice
Card `Budget:` wired to real iteration caps (parser mirrors hive's — one
vocabulary), burn logged per cycle, over-budget parks via the fix-15 site with
receipt + notify-last. Global-cap crossing deliberately kept on historical
mark-blocked behavior — unifying it under park+escalate is this card's
remaining controller scope (#47 #49 #51 + the queen cheap-idle-tick).
