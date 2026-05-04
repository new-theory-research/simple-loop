# Brief cards

Each brief lives in its own directory: `wiki/briefs/cards/brief-NNN-slug/`. The card collects all artifacts for one brief — it's both an organizational pattern and an observability surface.

## Card anatomy

| File | Purpose | Required |
|------|---------|---------|
| `index.md` | The brief itself (the assignment) | Yes |
| `plan.md` | Implementation plan written by worker | Optional |
| `closeout.md` | Summary written at completion | Expected |
| `evaluation.md` | Validator review | Written by reviewer agent |

## Naming

`brief-NNN-slug` — three-digit zero-padded number, descriptive kebab-case slug. Numbers are sequential across the project; slugs must be unique.

Examples: `brief-014-simple-loop-hardening`, `brief-026-simple-loop-bundle-portability`

## Card-as-enumerator

The card directory *is* the queue entry. The daemon enumerates dispatchable briefs by globbing `wiki/briefs/cards/*/index.md` and filtering on the card's frontmatter `Status:` field — `queued` cards are candidates, ordered by goals.md priority. See `lib/queue.py` for the canonical enumerator.

There's no symlink layer; the card is the only writable surface for brief lifecycle state.

## The observability benefit

When a brief is done, the card directory contains the full record: the assignment, the plan, what actually happened in each cycle (via `git log` on that branch), and the closeout. Pulling the card is the fastest path to understanding a past decision.

## Adding a brief

1. `mkdir wiki/briefs/cards/brief-NNN-slug/`
2. Write `wiki/briefs/cards/brief-NNN-slug/index.md` with frontmatter `Status: queued`
3. Add a line for the brief to `goals.md` under `## Queued next`
