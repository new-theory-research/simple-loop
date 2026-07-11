# Closeout — brief-154: close the intake gap with a `loop-triage-issues` skill

## TL;DR

The loop had an outbound edge (`loop-file-issue`) and no inbound one, so it had
closed 0 of 11 fixed issues while dozens sat open. This brief built the missing
ingest edge — a `loop-triage-issues` skill that a human/director runs to turn open
issues into draft cards — and ran it once against the live tracker. 32 open issues
now map to 10 draft cluster cards, each issue covered exactly once, proven by a
mechanical coverage diff. Nothing was dispatched; nothing was written to the
tracker.

## What shipped — the five pieces

1. **`core/skills/triage-issues/SKILL.md`** — the inbound mirror of `file-issue`.
   `name`/`description` frontmatter, `## Process`, `## Rules`, same shape. Installs
   via `install.sh`'s existing `core/skills/*/` loop as
   `~/.claude/skills/loop-triage-issues/` — **zero `install.sh` edits** (verified:
   the loop runs `install_skill` = `basename` → `Skill: /loop-triage-issues`).
2. **The `Issues:` frontmatter field** — the join table. Cards carry
   `Issues: ["#NNN", ...]` of **open issues only**; closed members live in prose as
   mechanism history so the coverage diff stays exact. Documented in
   `docs/conventions/cards.md`.
3. **`docs/conventions/cards.md`** — "Adding a brief" now makes triage the default
   intake (issue → `loop-triage-issues` → draft card → human flips to queued);
   hand-writing a card is the exception. The `Issues:` field is documented in the
   anatomy section.
4. **First run (this deliverable)** — 10 draft cluster cards under
   `wiki/briefs/cards/`, a `## Draft — awaiting human review` section in
   `.loop/state/goals.md`, and `comment-plan.md`. Cluster A (brief-155) cites #2 as
   the day-one holistic fix.
5. **`comment-plan.md`** — the issue → "tracked as brief-NNN" map, held for the
   gated posting step. No `gh` write occurred during triage.

## First-run cluster map (issue → card)

Run against the tracker on 2026-07-11. Between the first enumeration and the final
diff the tracker moved: **#55 closed** (its single-commit fix merged) and **#59
opened** (its multi-commit follow-up) — #55 dropped to prose history in brief-160,
#59 took its place. The map below is the final, re-enumerated state.

| Card | Mechanism | Open issues (in `Issues:`) | Closed members (prose only) |
|------|-----------|----------------------------|-----------------------------|
| brief-155-dirty-tree-daemon-state | daemon state in the git working tree; #2 is the day-one holistic fix | #2 #25 #33 #46 #54 | #5 #28 #29 |
| brief-156-gate-audit-model | approvals with no actor / gates that don't hold | #16 #26 #48 #52 | — |
| brief-157-unbounded-llm-subprocesses | no budget/backoff/fill controller | #44 #47 #49 #51 | #32 |
| brief-158-lane-id-parsing | lane identity not honored across dispatch + ID parse | #30 #50 | — |
| brief-159-runtime-observability | status/sweep signals read proxies, not running-daemon state | #31 #38 #53 | #41 |
| brief-160-blocked-brief-lifecycle | no first-class parked/blocked-brief state | #15 #27 #39 #58 #59 | #55 |
| brief-161-cross-repo-delivery | target-repo artifacts/merge unbuilt | #35 #36 | — |
| brief-162-harness-update-propagation | `loop update` doesn't propagate | #20 #57 | — |
| brief-163-input-validation-robustness | parse-without-validate on free-form fields | #21 #23 | — |
| brief-164-roadmap-misc | no shared bug mechanism — roadmap/field-report holding pen | #1 #3 #4 | — |

**Totals:** 5 + 4 + 4 + 2 + 3 + 5 + 2 + 2 + 2 + 3 = **32 open issues, each in
exactly one card.**

The five clusters the brief named are all present (A/dirty-tree, gate/audit,
subprocesses, lane IDs, observability). The remainder the brief seeded was formed
into four additional cards: blocked-brief lifecycle (#15 #27 #39 #58 #59, absorbing
the "#27 busy-loop" and "#15 escalation-surfacing" seeds), cross-repo delivery
(#35 #36), harness-update propagation (#20 #57), input validation (#21 #23), and a
misc holding pen (#1 #3 #4). #59 (open after the run started) was picked up live.

## Coverage-diff proof (mechanical)

Script: `coverage_diff.py` in this card dir. It parses `Issues:` from every
`wiki/briefs/cards/*/index.md`, unions them, and compares to
`gh issue list --repo ScavieFae/simple-loop --state open`. Re-run it any time —
`python3 wiki/briefs/cards/brief-154-issue-intake-triage/coverage_diff.py` (exit 0
== exact). Output at close:

```
open issues        : 32
issues carded      : 32
uncovered (open, no card) : NONE
in >1 card                : NONE
carded but not open       : NONE

COVERAGE EXACT: YES — union of Issues: == gh open set, each once
```

No open issue uncovered; no issue in two cards; nothing carded that isn't open.

## Guards honored

- **Zero edits to `lib/daemon.sh` or `lib/queue.py`** — the ingest is a skill; the
  daemon is untouched. (`git diff --stat` on the branch shows neither file.)
- **Triage never dispatched and never wrote to the tracker** — all 10 cards are
  `Status: draft`; the "tracked as brief-NNN" comments sit in `comment-plan.md`,
  unposted, for the gated step. The tracker carries no new triage comments.
- **Holistic over symptom** — 32 issues → 10 mechanism cards, not 32 symptom cards.
  The eight-issue dirty-tree family is one card (brief-155) citing #2, not eight
  patches.
- **Emitted cards** are `Status: draft`, `Program: harness-improvements`,
  `Auto-merge: false`, `Human-gate: review`, with an open-issues-only `Issues:`
  list.
- No `conductor` naming.

## Verification

- `bin/loop lint .loop/state/goals.md` → clean (the Draft section carries no
  state-prose).
- `python3 -m pytest lib/tests/ -q` → green (unchanged; no `lib` edits).
- Coverage diff → exact (above).
- Draft cards use YAML frontmatter matching every existing card (what `lib/queue.py`
  parses); they are `Status: draft` so the default `bin/loop lint` scan (queued
  only) does not touch them.

## Next (not this brief)

A human reviews the 10 draft cards + `comment-plan.md`, flips the cards they accept
`draft → queued` (moving the goals entry into `## Queued next`), and runs the gated
posting step. brief-155 (dirty-tree, cites #2) is the recommended first flip.
