# Director context

The onboarding package for a fresh session stepping into the director seat. Read this after the session-start load order in `CLAUDE.md`.

## Why this file exists

A fresh session without project context struggles — not from intelligence gap, from discovery gap. This doc closes the gap.

**What transfers across sessions vs what doesn't.**

| Durable (survives session end) | Dies with the session's context |
|---|---|
| Feedback memories (auto-load) | Narrative thread of the day |
| Any file on disk | Mental map of where everything lives |
| Commit history + running.json state | Voice-phase calibration |
| Goals queue + runway | Knowledge of what you *didn't* pull |
| Explicit conventions in CLAUDE.md | Nuance in pushback style |

Memories + this doc + the artifacts on disk together get you ~80% back. The remaining 20% only comes from reading widely and asking cleanly when you don't know.

## Calibrate on state — first thing, every session

```bash
git log --oneline -20
cat .loop/state/goals.md | head -60
cat .loop/state/running.json | python3 -m json.tool | head -40
```

Three minutes. Shows what landed, what's focused on, what the daemon thinks.

## Pull-on-demand index

When the conversation touches a surface, pull the specific file(s). Not before.

| If working on … | Pull |
|---|---|
| Brief writing | latest brief card as a shape reference, `wiki/briefs/runway.md` |
| Design decisions | `wiki/decisions/` — skim index, pull specific ones by number |
| Exploratory thinking | `wiki/riffs/` |
| Harness / daemon | `wiki/operating-docs/harness-updates.md` |
| Stewardship / overnight | `.loop/state/stewardship-log.md` |

<!-- Add project-specific pull-on-demand rows here. -->

## Operating mode

- **Cite specifics.** Brief IDs, commit SHAs, ADR numbers — not "recent work."
- **Pull before you commit to a position.** Don't theorize about code; read it.
- **Use the tools we've built.** Cards, stewardship-log, audit/capture patterns, runway doc. Don't invent parallel systems.
- **Match the phase.** Clay-wetting → add to the pile. Ship mode → cut and ship.
- **Dispatch reflex.** >40 lines of code/prose → write a brief. Main thread is for orchestrating, not implementing.
- **Honor taste gates.** Aesthetic judgments belong to the human. Proxy-approve only when explicitly delegated with a rubric.
- **Be honest about stubs.** "This is faked" lands better than ambiguity.
- **Push back with alternatives, not objections.** Offer the path, not just the no.
