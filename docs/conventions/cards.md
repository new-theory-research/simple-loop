# Brief cards

Each brief lives in its own directory: `wiki/briefs/cards/brief-NNN-slug/`. The card collects all artifacts for one brief — it's both an organizational pattern and an observability surface.

## Card anatomy

| File | Purpose | Required |
|------|---------|---------|
| `index.md` | The brief itself (the assignment) | Yes |
| `plan.md` | Implementation plan written by worker | Optional |
| `closeout.md` | Summary written at completion | Expected |
| `comment-plan.md` | Triage runs only: issue → "tracked as brief-NNN" comment map, held for the gated posting step | Triage cards |
| `evaluation.md` | Validator review | Written by reviewer agent |

### Frontmatter fields

Card frontmatter is a YAML `---` block (what `lib/queue.py` parses). Beyond the
standard `ID` / `Status` / `Model` / `Auto-merge` / `Human-gate` / `Program`
fields, note:

- **`Issues:`** — a list of `"#NNN"` back-links to the open GitHub issues this card
  covers, e.g. `Issues: ["#2", "#25", "#54"]`. This is the join table
  `loop-triage-issues` reads to know which issues already have a card (so it never
  re-cards them). **Open issues only** — closed members of a mechanism belong in the
  card's prose as history, never in frontmatter, so the coverage diff (union of all
  cards' `Issues:` == `gh issue list --state open`) stays exact. When the card
  merges, its closeout closes those issues with the commit SHA.

## Naming

`brief-NNN-slug` — three-digit zero-padded number, descriptive kebab-case slug. Numbers are sequential across the project; slugs must be unique.

Examples: `brief-014-simple-loop-hardening`, `brief-026-simple-loop-bundle-portability`

## Card-as-enumerator

The card directory *is* the queue entry. The daemon enumerates dispatchable briefs by globbing `wiki/briefs/cards/*/index.md` and filtering on the card's frontmatter `Status:` field — `queued` cards are candidates, ordered by goals.md priority. See `lib/queue.py` for the canonical enumerator.

There's no symlink layer; the card is the only writable surface for brief lifecycle state.

## The observability benefit

When a brief is done, the card directory contains the full record: the assignment, the plan, what actually happened in each cycle (via `git log` on that branch), and the closeout. Pulling the card is the fastest path to understanding a past decision.

## Adding a brief

**Triage is the default intake.** Directors and humans file GitHub issues via
`loop-file-issue`; the standing path from issue → card is `loop-triage-issues`
(`core/skills/triage-issues/`). It reads the live tracker, groups open issues by
root-cause mechanism (holistic over symptom — one card per mechanism, not one per
issue), and emits **`Status: draft`** cards carrying an `Issues:` back-link, plus a
`goals.md` entry under `## Draft — awaiting human review` and a `comment-plan.md`.
A human then flips `draft → queued` and approves the "tracked as brief-NNN" comment
posting. Triage never dispatches and never writes to the tracker.

Hand-writing a card straight to `queued` is the **exception** — reach for it only
when there's no issue to triage (e.g. a bootstrap brief). The steps:

1. `mkdir wiki/briefs/cards/brief-NNN-slug/`
2. Write `wiki/briefs/cards/brief-NNN-slug/index.md` with frontmatter `Status: queued`
3. Add a line for the brief to `goals.md` under `## Queued next`

A triaged draft card, once a human flips it, follows the same lifecycle from step 3
on (moved from the `## Draft` section into `## Queued next`).
