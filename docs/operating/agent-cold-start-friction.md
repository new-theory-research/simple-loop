# Agent cold-start friction — bringing simple-loop into a project

> Captured 2026-04-29 from a Scav session in `nt-rnd` that needed to "pull the
> latest from simple-loop and update our project to accommodate it." The agent
> had to re-derive a lot of state from filesystem search. This note captures
> what was missing and proposes fixes to `loop init` / `loop update` so the
> next cold-start agent picks the project up clean.

---

## Friction observed (this session)

1. **simple-loop source repo location is undiscoverable from inside the project.**
   The project's `.loop/config.json`, `.loop/config.sh`, and root `CLAUDE.md`
   contain zero pointers to where simple-loop lives. I ran
   `find /Users/mattie-newtheory -name simple-loop` to find it. Same for the
   install dir (`~/.local/share/simple-loop/`).

2. **No version-to-commit mapping.** `.loop/config.json` says
   `"version": "0.2.0"` but doesn't record the simple-loop commit SHA the
   project was last installed against. There's no way to answer "are we behind
   upstream?" without `cd`'ing to the (undocumented) source repo.

3. **`loop` CLI has no introspection command.** `loop status` reports daemon
   state, not framework state. There's no `loop info` / `loop where` /
   `loop version` that prints:
   - install dir
   - source repo path (if known)
   - installed version + commit
   - upstream commit if reachable
   - this project's `.loop/` root

4. **Project CLAUDE.md drifts.** `nt-rnd/CLAUDE.md` says
   *"Agents in `.loop/agents/`. Briefs in `.loop/briefs/`."* The first half is
   wrong post-install — agents now live at `~/.claude/agents/loop-*.md` (copied
   by `install.sh`). The brief location is also half-true: `.loop/briefs/` is
   real, but the files there are symlinks into `wiki/briefs/cards/brief-NNN/index.md`,
   which is what `loop lint` resolves to. A cold agent has to discover both
   facts by hand.

5. **Two brief locations, one quietly canonical.** The symlink convention
   (`.loop/briefs/brief-NNN.md` → `wiki/briefs/cards/brief-NNN/index.md`) is
   documented in `docs/bundle-install.md` step 6, but nothing in the project
   itself points to that doc. The cycle-4 reviewer for `nt-rnd`'s
   `brief-001-atlas-teardowns` *also* hit this exact ambiguity — flagged that
   the card dir landed at `wiki/briefs/cards/...` rather than
   `.loop/briefs/cards/...` because the brief said "card dir" without
   qualifying root.

6. **No upgrade runbook surfaces in-project.** `loop update` runs and prints
   `✓ Updated`, but the project doesn't know what changed. There's no
   per-project changelog ("since you last ran update: brief-097 added an
   Outputs section, brief-101 added code-change review shape, brief-104 added
   state-prose check") — so an agent has to git-log the simple-loop repo to
   figure out what migration work to do on existing project artifacts.

7. **Existing project artifacts silently fall out of spec on framework
   upgrade.** Both `nt-rnd` briefs lint-failed under the new
   `**N cycles MODEL.**` rule (introduced after the briefs were written). No
   warning fired during `loop update`; you only see the drift if you
   manually `loop lint` each brief.

## What `loop init` could do to prevent this

- **Write `.loop/README.md`** at init time, containing:
  - Path to simple-loop source repo (resolved from where `install.sh` ran from)
  - Install dir
  - Installed version + commit SHA
  - Where agents live (`~/.claude/agents/loop-*.md`)
  - Where skills live (`~/.claude/skills/loop-*/`)
  - Where briefs live (the symlink convention, named explicitly)
  - One-liner upgrade path: `cd <source> && git pull && bash install.sh && cd <project> && loop update`
- **Append a `## Loop framework` block to project CLAUDE.md** (or create one
  if absent) with the same pointers, so any cold-start agent picks them up
  in the standard load order.
- **Record simple-loop commit SHA in `.loop/config.json`** under a
  `simple_loop.commit` field. `loop update` rewrites it. Then `loop info` can
  diff against upstream.

## What `loop update` could do

- **Print a since-last-update changelog.** Read the recorded commit SHA, walk
  `git log` in the source repo, surface any commits that touch
  `templates/`, `core/`, or known migration paths.
- **Run `loop lint` against project briefs after update**, surfacing any
  briefs that fell out of spec. Don't auto-fix — just flag.
- **Update the `.loop/README.md` breadcrumbs** so they don't go stale.

## What `loop info` should print (proposed new command)

```
simple-loop:
  source:         /Users/.../claude-projects/simple-loop  (commit 914a4f5, 23 ahead of origin)
  install:        /Users/.../.local/share/simple-loop      (version 0.2.0, commit 914a4f5)
  binary:         /Users/.../.local/bin/loop
  agents:         ~/.claude/agents/loop-*.md               (3)
  skills:         ~/.claude/skills/loop-*/                 (6)

project (nt-rnd):
  .loop root:     /Users/.../new-theory/nt-rnd/.loop
  config:         version 0.2.0, last-installed 914a4f5
  briefs:         2 (1 active, 1 awaiting_review)
  drift:          brief-002-atlas-schema fails lint (Outputs missing) — run `loop lint` for detail
```

## Why this matters

Every cold-start agent does the same archaeology pass on the same questions.
The cost compounds across projects (this same friction will hit the next
project the next time it onboards) and across sessions (every fresh
context window starts from zero). Fixing it once at `loop init` time saves
the cost forever after.

---

## Concrete case: queen.md rename, 2026-04-29

After running `bash install.sh && loop update` from `nt-rnd`, the daemon failed silently on first dispatch:

```
[13:26:43] QUEEN #1: invoking (no_active)
[13:26:43] ERROR: queen prompt not found at .../.loop/prompts/queen.md
[13:26:43] Sleeping 30s before next tick
[13:27:15] QUEEN: dedup — same trigger (no_active), skipping (age 32s / ttl 1800s)
```

Two issues:

1. **`loop update` didn't sync the renamed prompt.** Framework had renamed the conductor prompt from `conductor.md` to `queen.md`, but `loop update` only refreshes `~/.local/share/simple-loop/` — project-level templates in `.loop/prompts/` got skipped. The daemon expected `queen.md`; the project still had `conductor.md`. Manual fix: `cp ~/.local/share/simple-loop/templates/prompts/queen.md .loop/prompts/queen.md`.

2. **Dedup hid the error.** After the first failed invocation, dedup kicked in (TTL 1800s) and the daemon kept logging "QUEEN: dedup — same trigger, skipping" rather than retrying. Without `loop status` flagging the underlying error, an operator would think "nothing to do" rather than "fatal config error, swallowed by dedup."

**Fixes proposed:**

- `loop update` should sync `.loop/prompts/` against `templates/prompts/` (with diff preview, since users may customize). At minimum, add missing files; never silently skip a file the daemon will require.
- Errors during QUEEN invocation should bypass dedup. A failure isn't the same trigger as a successful idle tick.
- `loop status` should surface the *last error*, not just the dedup state. "Awaiting you" should include "daemon errored 5m ago: queen prompt not found" before listing routine signals.

## Related artifacts

- `docs/bundle-install.md` — install runbook (target audience: a human setting
  up a new project). The runbook is fine; the gap is what gets *left in the
  project* after the runbook is followed.
- `nt-rnd/wiki/briefs/cards/brief-001-atlas-teardowns/` cycle-4 review —
  earlier flag of the same `.loop/briefs/cards/` vs `wiki/briefs/cards/`
  ambiguity, in a different agent's voice.
