# Goals

simple-loop's own loop, bootstrapped 2026-05-05 per brief-143's ops execution. Briefs about simple-loop's source land here, not in portal. See portal's `wiki/operating-docs/cross-repo-loops.md` for the convention.

## Awaiting Mattie (not queued)

## Credential-gated — NOT dispatchable

## Queued next

1. **brief-142 (`loop set-status <brief-id> <new-status>` CLI)** — adds a DRY primitive that wraps the card-status flip + running.json reproject + commit + push. Replaces the manual recipe at `docs/operating/hand-merge-brief.md` for arbitrary status transitions (everything that's not `loop approve`/`loop reject`). Ships regular-commit version now; swap to brief-140 plumbing as a follow-up. 2 cycles planned (implement + test); cycle ceiling 4. Auto-merge: false, Human-gate: review. Sonnet, parallel-safe. Edit-surface: `bin/loop`, possibly `lib/actions.py`. Depends-on: _none_. Originally filed in portal 2026-05-05; moved here after brief-143 bootstrap. Canonical at `wiki/briefs/cards/brief-142-loop-set-status-command/index.md`.

2. **brief-144 (`loop init` should scaffold `.loop/prompts/{queen,worker}.md`)** — hand-fix during bootstrap surfaced this gap; `loop init` doesn't currently copy `templates/prompts/` into the new project's `.loop/prompts/`, so daemon errors on missing queen prompt. Tiny one-cycle fix. Sonnet, cycle ceiling 3. Edit-surface: `bin/loop:cmd_init`. Canonical at `wiki/briefs/cards/brief-144-loop-init-scaffolds-prompts/index.md`.

3. **brief-145 (`loop install-service` should read `config.json#git.main_branch`)** — same bootstrap surface; install-service doesn't read the project's actual default branch, hardcodes `GIT_MAIN_BRANCH` default `main` in the plist. Bootstrap on simple-loop required manual plist patch + bootout/bootstrap. Tiny one-cycle fix. Sonnet, cycle ceiling 3. Edit-surface: `bin/loop:cmd_install_service`. Canonical at `wiki/briefs/cards/brief-145-loop-install-service-reads-main-branch/index.md`.
