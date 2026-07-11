---
ID: brief-154-issue-intake-triage
Branch: brief-154-issue-intake-triage
Status: queued
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: []
Edit-surface:
  - core/skills/triage-issues/SKILL.md
  - docs/conventions/cards.md
  - wiki/briefs/cards/ (first-run draft cluster cards)
  - .loop/state/goals.md (first-run cluster entries)
Depends-on: none
Tags: [harness, intake, triage, issues, gh, brick-2-fix]
---

# Brief: close the intake gap — a `loop-triage-issues` skill that turns issues into cards

!!! abstract "Intent"
    Portal directors file GitHub issues here (`loop-file-issue`), but nothing
    converts an issue into a brief card. The queen's queue is `lib/queue.py`
    globbing `wiki/briefs/cards/*/index.md` for `Status: queued`; `gh` is
    outbound-only. Result: the autonomous loop has closed **0 of 11** fixed
    issues — every fix was hand-carried. Dozens of issues sit open. Build the
    missing ingest edge: a `loop-triage-issues` skill (mirror of
    `loop-file-issue`) that a lane director or human runs to group open issues by
    root-cause mechanism and emit draft cards. No daemon change — the daemon is
    the least trustworthy component per the 2026-07-11 audit; keep the blast
    radius off it. (This card is itself hand-authored straight to `queued`; the
    skill it builds emits `draft` — the bootstrap has to be hand-carried to
    close the very gap that makes hand-carrying necessary.)

## Plain version

The loop has an outbound edge and no inbound one. `core/skills/file-issue/`
(installed as `~/.claude/skills/loop-file-issue/`) writes issues out via
`gh issue create`. Nothing reads them back: `enumerate_dispatchable` only sees
cards on disk, so an issue is a dead end until a human writes a card by hand.
That hand-carry is why fixes stayed symptom-scoped — five separate dirty-tree
patches in six weeks (the "daemon state in the git working tree" mechanism, open
members #2 #25 #33 #46 #54 plus closed members #5 #28 #29, patched piecemeal),
and the drift where #32/#34 sat open for days after their fixes merged (closed
2026-07-11 during the audit).

The fix is a **skill, not a daemon feature.** A director or human invokes it; it
reads the live tracker, clusters, and emits draft cards + goals entries + a
comment-plan (no tracker writes). A human flips `draft → queued` and approves the
comment posting. Triage never dispatches, never touches `lib/daemon.sh`.

## The fix (five pieces)

1. **`core/skills/triage-issues/SKILL.md` — the mirror skill.** New skill dir
   under `core/skills/`; install.sh's `core/skills/*/` loop (`install.sh:134-140`)
   copies it to `~/.claude/skills/loop-triage-issues/` with **zero install.sh
   edits** — verify, don't add. The skill's process: (a) `gh issue list --state open`
   and read each card's `Issues:` frontmatter to find open issues lacking a card
   back-link; (b) group the un-carded issues by **root-cause mechanism**, NOT
   1:1 — a card may cover many issues, and the holistic fix is preferred over the
   symptom patch; (c) emit one draft card per cluster (`Status: draft`,
   `Program: harness-improvements`, `Auto-merge: false`, `Human-gate: review`,
   `Issues:` listing every covered **open** issue) plus a matching `goals.md`
   entry; (d) write a **comment-plan** artifact into the run's card dir —
   `comment-plan.md` listing exactly which "tracked as brief-NNN" comment posts
   to which issue. Triage performs **no `gh` writes** — it only reads the tracker
   and writes local files.

2. **Bidirectional links — the `Issues:` frontmatter field.** Card frontmatter
   gains `Issues: ["#47", "#49"]` listing **open issues only** — closed issues
   never enter frontmatter (they belong in prose as mechanism history, so the
   coverage diff stays clean). This is the join table triage reads in step (a) to
   avoid re-carding. Merge closeout must name which issues the merge closes and
   close them with the commit SHA — fixing the drift where #32/#34 sat open for
   days after their fixes merged (closed 2026-07-11 during the audit).

3. **`docs/conventions/cards.md` — document the intake path.** The "Adding a
   brief" section (lines 30-34) makes triage the **default** intake: issue →
   `loop-triage-issues` → draft card → human flips to queued. Hand-writing a card
   stays the exception. Document the `Issues:` field in the card anatomy.

4. **First run is a deliverable.** Run the skill against the current tracker and
   commit the resulting draft cluster cards + goals entries + `comment-plan.md`.
   Every issue **open at run time** maps to exactly one card. The five named
   clusters below are a **minimum, not the whole map** — the worker forms
   additional clusters (or a misc/singleton card where a mechanism has one
   member) so coverage is complete and each open issue lands in exactly one card:
     - **Cluster A** — daemon state in the git working tree; open members
       #2 #25 #33 #46 #54 (closed members of this mechanism: #5 #28 #29). Cites
       **#2 as the day-one holistic fix**, superseding the piecemeal patches.
     - **gate/audit model** — #16 #26 #48 #52.
     - **unbounded LLM subprocesses** — open members #44 #47 #49 #51 (#32 closed).
     - **lane IDs** — #30 #50.
     - **observability** — open members #31 #38 #53 (#41 closed).

   Starting point for the un-clustered remainder (unassigned at review time):
   #1 #3 #4 #15 #20 #21 #23 #27 #35 #36 #39 #55 — #27 belongs with the
   blocked-brief busy-loop mechanism, #35/#36 are cross-repo delivery, and
   #15/the #16-family are escalation surfacing. Re-enumerate live at run time;
   this list is a seed, not the authority.

5. **Gated comment posting — a separate, explicit outward-facing step.** After a
   human reviews the triage output (draft cards + `comment-plan.md`) and approves,
   a follow-up action posts the "tracked as brief-NNN" comments per the plan.
   This is the **only** step that writes to the tracker — flag it as the gated,
   outward-facing action requiring `gh` write auth, distinct from the read-only
   triage run. It never runs unattended.

## Success criteria

- `core/skills/triage-issues/SKILL.md` exists and follows the `file-issue`
  SKILL shape (name + description frontmatter, Process, Rules). A fresh
  `./install.sh` produces `~/.claude/skills/loop-triage-issues/` — verified from
  the install output line `Skill: /loop-triage-issues`, with no install.sh diff.
- The triage run enumerates every issue open at run time, partitions the
  un-carded ones into the named clusters plus whatever additional clusters or
  singleton cards coverage requires, and covers **every** open issue with exactly
  one draft card. Verified by the **coverage diff**: the union of all card
  `Issues:` frontmatter equals `gh issue list --state open` (no open issue
  uncovered, no issue in two cards).
- Emitted cards are `Status: draft` (never `queued`, never dispatched),
  `Program: harness-improvements`, and carry an `Issues:` list of **open issues
  only** (closed issues appear in prose as mechanism history, never in
  frontmatter). No `gh` write occurs during the triage run.
- The run emits `comment-plan.md` in its card dir, mapping each covered open
  issue to its "tracked as brief-NNN" comment. The comments post only via the
  separate, human-approved gated step (piece 5) — proven by the tracker having no
  new triage comments until that step runs.
- Cluster-A day-one card exists, lists open members #2 #25 #33 #46 #54 (closed
  members #5 #28 #29 named in prose, not frontmatter), and frames #2 as the
  holistic fix.
- `docs/conventions/cards.md` "Adding a brief" documents the triage path as the
  default intake and documents the `Issues:` frontmatter field.

## Guards

- **Zero edits to `lib/daemon.sh` or `lib/queue.py`.** Triage is a skill; the
  ingest happens off the daemon. If a criterion seems to need a daemon change,
  stop and escalate — it doesn't.
- **Triage never dispatches and never writes to the tracker.** Cards land as
  `draft`; the human-gate (a person or lane director flipping to `queued`) is
  preserved — do not auto-flip. The triage run only reads `gh` and writes local
  files; the "tracked as brief-NNN" comments post solely through the separate
  human-approved gated step (piece 5).
- **Holistic over symptom.** Prefer one card per mechanism to N cards per issue.
  A cluster of dirty-tree issues is one card, not eight.
- No `conductor` naming.

## Outputs

- `closeout.md` — what shipped, the five pieces, the first-run cluster map (which
  open issues → which cards), and the coverage-diff proof that every open issue
  is covered exactly once.
- `comment-plan.md` — the first run's issue → "tracked as brief-NNN" map, held
  for the gated posting step.
- `review.md` — gate runbook (Human-gate: review); link closeout for "what
  shipped" and the cluster coverage table.
