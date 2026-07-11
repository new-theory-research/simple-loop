---
name: triage-issues
description: Turn open GitHub issues into draft brief cards — cluster the tracker by root-cause mechanism, emit Status:draft cards + goals entries + a comment-plan. Read-only against the tracker; no gh writes, no dispatch.
---

# Triage Issues Into Draft Cards

The inbound mirror of `loop-file-issue`. `file-issue` writes issues *out* via
`gh issue create`; nothing reads them back — the daemon only enumerates cards on
disk, so an open issue is a dead end until someone writes a card by hand. This
skill closes that gap: a lane director or human runs it to group open issues by
root-cause mechanism and emit **draft** brief cards a human can then flip to
`queued`.

Run this when the tracker has accumulated open issues with no cards behind them —
the periodic intake sweep, or after a burst of `loop-file-issue` reports.

**This skill never dispatches, never writes to the tracker, and never touches
`lib/daemon.sh` or `lib/queue.py`.** It reads `gh` and writes local files only.
The "tracked as brief-NNN" comments post through a *separate*, human-approved
gated step (see Rules) — not here.

## Process

1. **Enumerate the live tracker.** `gh issue list --repo <owner/repo> --state open
   --limit 200 --json number,title`. This is the authoritative set — every issue
   open at run time must land in exactly one card.

2. **Read the join table — which issues already have a card.** For every
   `wiki/briefs/cards/*/index.md`, read the `Issues:` frontmatter field (a list of
   `"#NNN"` back-links). The union of all those lists is the set of already-carded
   issues. Subtract it from step 1 to get the **un-carded** open issues — the ones
   this run must cluster. (Closed issues never appear in frontmatter, so they never
   confuse the diff.)

3. **Cluster by root-cause mechanism — holistic over symptom.** Group the un-carded
   issues by the *mechanism* that produces them, NOT one card per issue. A single
   dirty-working-tree mechanism that spawned eight issues is **one** card, not
   eight. Prefer the holistic fix (the day-one redesign) over the symptom patch, and
   name it as such. Where a mechanism has one member, a singleton card is fine; where
   the remainder shares no mechanism, a `misc` card is fine. Every un-carded open
   issue must land in exactly one cluster — no issue in two cards, no issue uncovered.

4. **Emit one draft card per cluster.** Create
   `wiki/briefs/cards/brief-NNN-slug/index.md` (next sequential number, unique
   kebab slug). YAML frontmatter, matching the existing cards
   (`lib/queue.py` parses YAML):
     - `Status: draft` — never `queued`, never dispatched. A human flips it.
     - `Program: harness-improvements`
     - `Auto-merge: false`
     - `Human-gate: review`
     - `Validator: core/agents/reviewer.md`
     - `Issues:` — a list of every **open** issue this card covers, e.g.
       `Issues: ["#2", "#25", "#54"]`. **Open issues only.** Closed members of the
       same mechanism belong in the card's prose as history, never in frontmatter,
       so the coverage diff stays clean.
     - `Depends-on: none`, plus the usual `ID`, `Branch`, `Model`, `Target repo`,
       `Parallel-safe`, `Tags`.
   Body: state the mechanism, cite each covered issue by number, name the holistic
   fix, and list closed members in prose as mechanism history.

5. **Add a matching `goals.md` entry.** Under a `## Draft — awaiting human review`
   section (NOT under `## Queued next` — draft cards are not dispatchable). One
   priority-intent line per card. Keep it state-prose-free so `loop lint
   .loop/state/goals.md` stays clean (no "merged", "shipped", "completed",
   strikethrough — see `lib/lint.py` check_goals_md_state_prose).

6. **Write the comment-plan artifact.** `comment-plan.md` in *this run's* card dir,
   mapping each covered open issue → the "tracked as brief-NNN" comment to post.
   This is a plan held for the gated step — writing it performs no `gh` write.

7. **Prove coverage mechanically.** Script the diff: union of all emitted cards'
   `Issues:` lists vs `gh issue list --state open`. Assert the two sets are equal —
   no open issue uncovered, no issue in two cards. Paste the output into the run's
   `closeout.md`.

## Rules

- **No `gh` writes during triage.** The run reads the tracker and writes local
  files. It creates no issues, posts no comments, closes nothing.
- **Cards land as `draft`, never `queued`.** The human-gate is a person (or lane
  director) flipping `draft → queued`. Do not auto-flip; do not dispatch.
- **Holistic over symptom.** One card per mechanism. A cluster of dirty-tree issues
  is one card citing the day-one fix, not N symptom patches.
- **Open issues only in `Issues:` frontmatter.** Closed issues are mechanism history
  — prose, not frontmatter. This keeps the coverage diff exact.
- **Zero edits to `lib/daemon.sh` or `lib/queue.py`.** The ingest happens off the
  daemon. If a step seems to need a daemon change, stop and escalate — it doesn't.
- **Gated comment posting is a separate step.** After a human reviews the draft cards
  and `comment-plan.md` and approves, a follow-up action posts the "tracked as
  brief-NNN" comments per the plan. That step — the *only* one that writes to the
  tracker — needs `gh` write auth and never runs unattended.
- **Merge closeout closes the issues.** When a triaged card merges, its closeout
  names which issues the merge closes and closes them with the commit SHA — no issue
  drifts open after its fix lands.
- No `conductor` naming.
