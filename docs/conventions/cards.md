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

- **`Program:`** — the card's program lane (e.g. `serving`, `finetune`). This is
  the **unit of parallelism**, and it is a **mutex**: at most one brief per
  `Program:` value is active at a time — the lane is a single thread.

  > "programs are single-threaded, and we can have a max # of threads going at a
  > time. ft has one going at any time, serve has one going at any time, and if
  > both are active, they can happen at once." — Mattie, 2026-07-11, harness-director session ruling; #74 carries the companion formulation 'two programs can parallelize a single thread'

  Two same-lane briefs are sequenced by `goals.md` order and `Depends-on:`, never
  co-dispatched — **even when both are `Parallel-safe:` and their edit surfaces are
  disjoint.** A lane is single-threaded regardless of `Parallel-safe:`; that is the
  point. Concurrency lives *across* programs: two different `Program:` lanes run at
  once (`Parallel-safe:`/edit-surface disjointness still governs *cross*-lane
  eligibility, unchanged). Unlabeled cards (no `Program:`) keep the older
  surface-based concurrency behavior — the ruling governs programs.

  The dispatcher enforces this as a first-class gate (`lane_mutex_hold`, evaluated
  before the `THROTTLE` and edit-surface gates); `loop why` reports it as the
  `lane_mutex` check. Scoping w.r.t. issue #51 (fill unused `THROTTLE` slots): the
  extra slots the slot-filler fills are **cross-program** slots only — the mutex and
  the slot-filler are complementary, never a second thread in the same lane. The
  slot-filler ships as the daemon's `slots_available` wake (issue #51, Phase 1.5 in
  `lib/daemon.sh`): when a brief is active but a `THROTTLE` slot is open, the daemon
  runs the pure `why.py --slots-available` check and, if a cross-lane brief is
  DISPATCHABLE, wakes the queen — which is exactly a same-lane candidate failing the
  `lane_mutex` check, so the wake is cross-program by construction. The daemon only
  wakes; the queen still decides whether to dispatch.

  *Retired interim encoding:* before the first-class mutex (issue #74), the lane was
  serialized by a shared fiction — every serving card declared `wiki/programs/serving/`
  as a fake shared `Edit-surface:` so the overlap check serialized the lane as a side
  effect (portal commit `0a2a0909`). That workaround is superseded; declare `Program:`
  and let the mutex do it.

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
