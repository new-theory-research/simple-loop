---
ID: brief-144-loop-init-scaffolds-prompts
Branch: brief-144-loop-init-scaffolds-prompts
Status: active
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - bin/loop
Depends-on: _none_
Tags: [harness, cli, init, bootstrap]
---

# Brief: `loop init` should scaffold `.loop/prompts/{queen,worker}.md`

!!! abstract "Intent"
    `loop init --wiki-full` (and `loop init` in any mode) should copy `templates/prompts/{queen,worker}.md` into the new project's `.loop/prompts/` so the daemon can find the conductor + worker prompts at the path `daemon.sh:39` expects (`$LOOP_DIR/prompts/queen.md`).

## Motivation

Surfaced 2026-05-05 during simple-loop's bootstrap (brief-143 ops execution). `loop init --wiki-full` ran cleanly but the daemon errored on first tick: `ERROR: queen prompt not found at .../.loop/prompts/queen.md`. Hand-fix was `cp -r templates/prompts ~/claude-projects/simple-loop/.loop/prompts`. Every new project hits this wall — should be part of init.

## Scope

### In

- `cmd_init()` (`bin/loop:92`) copies `templates/prompts/queen.md` and `templates/prompts/worker.md` into the project's `.loop/prompts/` directory.
- Idempotent — if the prompts already exist (re-running init), don't overwrite.
- Works for `--minimal`, `--wiki-full`, and interactive modes.

### Out

- Don't change daemon prompt-loading behavior — that's a separate concern (and the user can override prompts post-init).
- Don't bundle prompts into a different location.

## Tasks

1. **Implement** — extend `cmd_init` in `bin/loop` to copy prompts; verify path resolution works regardless of where the simple-loop checkout lives.
2. **Test** — extend or add to `scripts/test-flow-v2.sh` to assert `.loop/prompts/queen.md` exists after `loop init`.

## Cycle shape

1 cycle, `loop-coder`. Cycle ceiling 3.

## Completion criteria

- [ ] `loop init --wiki-full` in a fresh dir produces `.loop/prompts/queen.md` + `worker.md`
- [ ] Re-running init doesn't clobber existing prompts
- [ ] Test asserts prompt files exist post-init
- [ ] `closeout.md` per contract

## Anti-patterns

- Don't change daemon's prompt-path expectation
- Don't auto-execute long-running terminal commands

## Artifact

- Patch to `bin/loop:cmd_init`
- Test in `scripts/test-flow-v2.sh`
- `closeout.md` + `review.md`
