# Agent instructions

## Session-start load order

Always, in order:

1. **Memories auto-load** — feedback_*.md + project_*.md + reference_*.md. Review the index.
2. **`CLAUDE.md`** — auto-loaded. Conventions, tech stack, who-operates-here.
3. **`wiki/soul.md`** — what this project refuses to be. 2 minutes.
4. **`.loop/state/goals.md`** — current priority + queue + done.
5. **`git log --oneline -30`** — recent decisions + who made them.
6. **`cat .loop/state/running.json`** — what's in flight.
7. **`ls .loop/state/signals/`** — any open escalates.
8. **`wiki/briefs/runway.md`** — upcoming pre-filed briefs. Skim the list.
9. **`wiki/operating-docs/director-context.md`** — pull-on-demand index + operating mode.

**Then** wait for direction. Do not preload briefs or research docs ahead of need.

## Three durable conventions

**Cite specifics.** `brief-NNN`, commit SHAs, `ADR-NNN` — not "recent work" or "the API thing." Specificity surfaces disagreement fast.

**Use the tools that exist.** Cards, stewardship-log, audit/capture patterns, harness-updates runbook, the memory system, runway doc. When something new comes up, first ask: does an existing tool handle this?

**Match the phase.** Clay-wetting (exploration, questions) → add to the pile, don't polish. Ship mode (commits, merges, demos) → cut and ship. Demo-prep → honest framing over perfect. Reading the phase is half the game.

## Dispatch floor

Orchestrate; don't implement. If a task will touch more than one file or produce more than 40 lines of prose/code, write a brief and dispatch. Main-thread time goes to drafting the ask, reviewing the diff, deciding — not to writing the artifact.

Context compaction is the receipt that the dispatch floor was ignored. When a session compacts, the first post-mortem question is: which of those tasks should have been subagents?

## Conventions

- **Commit prefixes:** `[scav]`, `[scaviefae]`, or `[titania]`.
- **Preserve en-dashes (–)** in writing. Don't silently convert to em-dashes.
- **Riffs** use `<!-- riff id="..." status="draft|developing|tested" could_become="..." -->` blocks. Don't clean them up.
- **Source citations** use `[source: url]` inline.
- **No LLM-speak.** No "Great question." No "Absolutely." No "I'd be happy to help." Just answer.

## Project-specific

<!-- Fill in below: who operates here, tech stack, what not to do. -->
