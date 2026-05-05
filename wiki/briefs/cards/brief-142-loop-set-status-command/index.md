---
ID: brief-142-loop-set-status-command
Branch: brief-142-loop-set-status-command
Status: queued
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - bin/loop
  - lib/actions.py
Depends-on: _none_
Tags: [harness, cli, dry, set-status]
---

!!! info "Filed in simple-loop's loop after brief-143 bootstrap (2026-05-05)"
    This brief was originally filed in portal at `wiki/briefs/cards/brief-142-loop-set-status-command/`. After brief-143 stood up simple-loop's own loop, the card was moved here per the cross-repo-loops convention (see `~/new-theory/portal/wiki/operating-docs/cross-repo-loops.md`). Portal copy marked `not-doing` with a pointer back here.

# Brief: `loop set-status <brief-id> <new-status>` — DRY CLI for arbitrary card-status transitions

!!! abstract "Intent"
    Add a `loop set-status <brief-id> <new-status>` subcommand to `bin/loop` that wraps the card-status flip + running.json reproject + commit + push as a single DRY primitive. At completion, agents and humans no longer re-derive the manual recipe from `docs/operating/hand-merge-brief.md` for arbitrary status transitions; one verb does it.

## Motivation

Today, only `loop approve` and `loop reject` wrap the chain. Every other status transition (queued → active, active → awaiting_review when daemon misses, awaiting_review → not-doing, etc.) requires the manual recipe — three invocations, easy to forget the chain, easy to drift. Card-as-truth requires the chain to run reliably; making it a CLI primitive eliminates the muscle-memory tax.

Mattie 2026-05-05: *"sounds like we need: loop set-status command, as CLI/code and not a skill (though heavily used in skills)."*

## Starting context

!!! info "Pointers — read in this order"
    1. `bin/loop` — has `cmd_approve()`, `cmd_reject()` as the closest reference shape
    2. `lib/_set_card_status.py` — primitive that flips frontmatter
    3. `lib/state.py` — has `write-running-json` subcommand (the projector)
    4. `docs/operating/hand-merge-brief.md` — the manual recipe being replaced (canonical version of the doc; portal carries a working copy)
    5. Portal memory `feedback_use_daemon_for_filed_briefs` — load-bearing reason this matters (lost review rounds + observability)

## Scope

### In

- **New subcommand `loop set-status <brief-id> <new-status>`** in `bin/loop`.
- **Validates:** brief exists, new status is one of (`queued` / `active` / `awaiting_review` / `merged` / `rejected` / `not-doing` / `draft`), idempotent if already at target status.
- **Wraps:** `_set_card_status.py` + `state.py write-running-json` + `git add` + `git commit` + `git push`.
- **Plumbing posture:** uses brief-140 plumbing once that lands (today: regular `git commit` against working tree; once 140 lands, plumbing only). Don't block on 140 — ship the regular-commit version now, swap to plumbing as a follow-up.
- **`loop approve` and `loop reject`** could rewrap this primitive in a follow-up — out of scope for this brief, name as residue.
- **Test coverage:** idempotency, invalid status, missing brief, missing edit permission, status transition correctness.

### Out

- **Not extending the status enum** — use existing valid transitions only.
- **Not changing how `loop approve`/`loop reject` work today** — that's a follow-up.
- **Not skill-side changes** (per Mattie: "not a skill, though heavily used in skills") — skills consume the new CLI but skill changes are separate.
- **Not auto-execute long-running terminal commands** in cycles.

## Tasks

1. **Implement** — add `cmd_set_status()` in `bin/loop` mirroring `cmd_approve()` shape; wire to `_set_card_status.py` + `state.py write-running-json` + git steps; idempotency + validation.
2. **Test** — `scripts/test-flow-v2.sh` extension (or new `scripts/test-set-status.sh`) covering idempotency, invalid status, missing brief, status transition correctness; update `bin/loop help`; update `docs/operating/hand-merge-brief.md` to recommend `loop set-status` for in-between transitions.

## Cycle shape

Each cycle lands one task, commits, pushes. Validator runs after each cycle with `core/agents/reviewer.md`.

- Cycle 1: `loop-coder` — implementation.
- Cycle 2: `loop-coder` — tests + help string + operating-doc update.

**Validator should flag:**
- Status enum extended silently (anti-pattern: enum changes are out-of-scope).
- Behavior change in `_set_card_status.py` (anti-pattern: just call it, don't modify it).
- `cmd_set_status` re-implementing the chain logic instead of calling existing primitives.
- Idempotency check missing — repeated invocation produces a spurious commit.

## Completion criteria

- [ ] `loop set-status <brief-id> <new-status>` works for all valid statuses
- [ ] Idempotent: `loop set-status brief-X queued` when already queued is a no-op (no spurious commit)
- [ ] Validates input: invalid status returns non-zero with clear error
- [ ] Tests in `scripts/test-flow-v2.sh` (or new `scripts/test-set-status.sh`) cover the cases above
- [ ] `bin/loop help` lists the new command
- [ ] Updated `docs/operating/hand-merge-brief.md` to recommend `loop set-status` for in-between transitions where `loop approve`/`loop reject` don't apply
- [ ] `closeout.md` + `review.md` per contracts

## Escalation triggers

- **Plumbing-style commit (post-brief-140) requires major refactor to share with `loop approve`/`loop reject`** — escalate, file a follow-up to harmonize; ship the regular-commit version now.
- **Status-transition validation discovers card-as-truth contract gaps** — escalate, surface them; don't silently widen scope.

## Budget

**4 cycles.** Two for the planned shape (implement + test); two cushion for plumbing seam or test-flake.

## Anti-patterns

Inherits template defaults plus:

- **Don't extend the status enum** — out of scope.
- **Don't change `_set_card_status.py` behavior** — just call it.
- **Don't auto-execute long-running terminal commands** in cycles.

## Artifact

- New CLI subcommand at `bin/loop` (`cmd_set_status()`).
- Tests in `scripts/test-flow-v2.sh` or `scripts/test-set-status.sh`.
- Updated `docs/operating/hand-merge-brief.md` recommending `loop set-status` for in-between transitions.
- `closeout.md` + `review.md` in this card dir.

## Verification

```bash
loop set-status brief-001-foo active   # transitions card; reprojects; commits + pushes
loop set-status brief-001-foo active   # idempotent; no second commit
loop set-status brief-001-foo bogus    # exits 1 with clear error
loop help | grep set-status            # appears
```

## What this unlocks

After this lands, every status transition runs through one verb that enforces the chain — card flip + running.json reproject + commit + push, atomic from the operator's POV. Skills that today re-derive the recipe (stewardship, hand-merge flows, smoke-prep, mid-flight reroutes) consume the CLI instead. The "I forgot the running.json reproject" failure mode goes away. Mattie's `loop approve`/`loop reject` paths get a sibling primitive they can rewrap in a future cleanup pass.
