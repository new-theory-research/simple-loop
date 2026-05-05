# Brief cards

Each brief lives in its own directory here: `cards/brief-NNN-slug/`. The card collects all artifacts for one brief:

- `index.md` — the brief itself (Status: frontmatter is the source of truth for brief state)
- `plan.md` — implementation plan (optional, written by worker)
- `closeout.md` — summary written at completion
- `evaluation.md` — validator review

## Adding a brief

1. Create a directory: `mkdir wiki/briefs/cards/brief-NNN-slug/`
2. Write the brief: `wiki/briefs/cards/brief-NNN-slug/index.md` with `Status: queued` in frontmatter
3. Add it to `goals.md` under `## Queued next` (for priority ordering).
