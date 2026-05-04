# Simple Loop

A workstation kit of skills, subagents, and opt-in orchestration loops for Claude Code. Install once; every project gets the core skills (`/loop-push`, `/loop-pull`) and subagents (`loop-coder`, `loop-reviewer`, `loop-research`). Opt individual projects into a daemon-driven build, research, or experiment loop when the work warrants it.

## What you get

- **Workstation skills** — slash commands available in every Claude Code session
- **Workstation subagents** — addressable via `subagent_type: loop-coder` etc.
- **Modules** — opt-in per project via `loop add <name>`: research, build, autoresearch, docs
- **Orchestration daemon** — opt-in per project via `loop init` for multi-iteration autonomous work

## Install

```bash
git clone https://github.com/ScavieFae/simple-loop.git ~/simple-loop
bash ~/simple-loop/install.sh           # default: copy (snapshot)
# or
bash ~/simple-loop/install.sh --link    # maintainer mode: symlink, edits go live
```

This installs the `loop` binary at `~/.local/bin/loop` and populates `~/.claude/skills/loop-*/` and `~/.claude/agents/loop-*.md` with the core surface. Coworkers should use the default copy mode and re-run install to refresh:

```bash
cd ~/simple-loop && git pull && bash install.sh
```

If you used `--link`, your installed copy *is* your repo checkout — `git pull` is enough.

### Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI, authenticated
- Python 3.8+, Git, Bash
- [GitHub CLI (`gh`)](https://cli.github.com/), authenticated, configured as git credential helper:
  ```bash
  gh auth login
  gh auth setup-git
  ```
  Why: the daemon pushes commits from outside your terminal session. macOS
  keychain unlock doesn't propagate to background processes. `gh`'s stored
  token survives machine sleep and keychain lock. `loop init` refuses to
  proceed until `gh auth status` reports authenticated. Without this,
  daemon merge commits silently fail to push — observed on 2026-04-22 when
  a merge sat unpushed for ~14h until a manual push.

  Minimal token scopes for daemon operation: `repo` (required) + `read:org`
  (required for private-org repos). `workflow` and `admin:*` scopes are
  unnecessary — shrink with:
  ```bash
  gh auth refresh --hostname github.com --remove-scopes workflow
  ```
  (browser OAuth prompt; one-time). `loop init` emits a soft warning when
  extra scopes are detected.

### Daemon health: heartbeat

Every daemon tick writes `.loop/state/heartbeat.json` with `{ts, pid,
last_event}`. `loop status` reports **HUNG** when the heartbeat is older
than 2× `HEARTBEAT_INTERVAL` (default ~10 min stale-threshold at the 300s
tick interval, configurable in `.loop/config.sh`). Process-alive ≠
loop-healthy: a frozen main loop inside a live process is exactly the
failure mode heartbeat detects. External watchers can `jq .ts
.loop/state/heartbeat.json` for the same signal.

If `loop status` reports HUNG:
```bash
loop stop && loop start
```
Restart is cheap; the daemon picks up from disk state (`running.json`,
`pending-*.json`). Brief-014 2026-04-21→22 postmortem: an 11h frozen
daemon looked alive to `ps` but silent in the log; heartbeat closes
that gap.

## Install into a new project

After running `install.sh`, use `loop init` to scaffold the bundle into any project:

```bash
cd your-project
loop init --wiki-full    # full scaffold: .loop/ + wiki/ skeleton
# or
loop init --minimal      # .loop/ only, no wiki/
```

Then author a brief at `wiki/briefs/cards/brief-NNN-slug/index.md` with `Status: queued` in its frontmatter, and `loop start`. Full step-by-step walkthrough in [docs/bundle-install.md](docs/bundle-install.md).

## Workstation skills

| Skill | Triggers | What it does |
|-------|----------|-------------|
| `/loop-push` | "push", "ship it", "send it" | Scope audit → `HANDOFF.md` → `RUNNING.md` → commit + push |
| `/loop-pull` | "pull", "catch me up", "what changed" | Git pull → parallel HANDOFF/state scanners → briefing |
| `/loop-update-handoff` | manual | Append one entry to `HANDOFF.md` |
| `/loop-update-running` | manual | Append one entry to `RUNNING.md` |
| `/loop-update-trouble` | manual | Document a bug investigation in `TROUBLESHOOTING.md` |
| `/loop-file-issue` | manual | File a GitHub issue with `gh` |

`/loop-push` and `/loop-pull` are session-boundary workflows — they orchestrate the atomic update skills into a coherent ritual. Push captures context on the way out; pull absorbs it on the way in.

**Project overrides:** drop a skill at `<project>/.claude/skills/loop-push/SKILL.md` to wrap or replace the workstation version with project-specific steps (deploy gates, contract checks, etc.) while keeping the same name.

## Workstation subagents

| Subagent | `subagent_type` | What it does |
|----------|-----------------|-------------|
| Coder | `loop-coder` | Implements one scoped task per iteration, verifies, commits |
| Reviewer | `loop-reviewer` | Evaluates work against a brief's completion criteria |
| Research | `loop-research` | Searches, reads, synthesizes findings with sources cited |

**Project overrides:** `<project>/.claude/agents/loop-coder.md` shadows the workstation file.

## Orchestration loop (opt-in per project)

For projects you want to run autonomously across many iterations, `loop init` scaffolds a `.loop/` directory and the daemon takes over the busywork: pick a brief → spawn a coder for one task → verify → commit → repeat → evaluate when done. Most projects don't need this. Use it when the work is well-scoped enough to run while you sleep.

```bash
cd your-project
loop init                                  # one-time, interactive
loop add build                             # opt into the build module
loop brief "add user authentication"       # write a brief interactively
loop start                                 # start the daemon
loop status                                # check progress
loop logs -f                               # watch it work
```

### Modules

Each module is installed per-project with `loop add <name>`. It symlinks the module's skills as `/loop-<module>-<skill>`, appends a CLAUDE.md section between markers, and provisions state under `.loop/modules/<module>/`.

| Module | Loop style | What it does |
|--------|-----------|-------------|
| `build` | brief-driven | Autonomous implementation of structured briefs (the v1 simple-loop behavior) |
| `research` | brief-driven | Autonomous search/synthesis on a set of research questions |
| `autoresearch` | heartbeat | Hypothesize → review → execute → evaluate experiment cycles with budget gating (AWM-style) |
| `docs` | on-demand | Living docs site via Zensical with a prebuild step |

### CLI reference

| Command | Description |
|---------|-------------|
| `loop init` | Interactive project setup, creates `.loop/` |
| `loop add <module>` | Install a module into the current project |
| `loop brief "title"` | Write a brief interactively |
| `loop brief --no-interactive "title"` | Create brief from template, opens in `$EDITOR` |
| `loop start [interval]` | Start the daemon (default 300s heartbeat) |
| `loop stop` | Stop the daemon |
| `loop status` | Daemon state, active brief, recent logs |
| `loop logs [-f]` | Show / follow daemon logs |
| `loop pause [reason]` | Pause daemon (commits + pushes signal) |
| `loop resume [instruction]` | Resume daemon |
| `loop metrics [--since DATE]` | Cost report from `metrics.jsonl` |
| `loop lint <path>` | Lint a brief file or directory for format drift |
| `loop help` | Usage |

### Project layout

After `loop init`:

```
.loop/
├── config.json          # project settings (heartbeat, ntfy, git, modules list)
├── config.sh            # legacy shell-style config (kept for compatibility)
├── prompts/
│   ├── worker.md        # per-iteration daemon worker prompt
│   └── queen.md         # heartbeat queen prompt
├── briefs/              # brief markdown files
├── state/
│   ├── running.json     # active / completed / history
│   ├── goals.md         # what to build (you write this)
│   ├── metrics.jsonl    # cost and tokens per session
│   ├── log.jsonl        # decision log
│   └── signals/         # pause / escalate
├── evaluations/         # post-brief eval cards
├── knowledge/           # agent-writable knowledge base
├── modules/             # per-module config + state (after `loop add`)
└── logs/                # session logs (gitignored)
```

There's no `.loop/agents/` — generic agents live globally in `~/.claude/agents/`. Project-specific agent overrides go in `<project>/.claude/agents/`.

### Brief format

```markdown
# Brief: Add user login

**Branch:** brief-001-add-login
**Model:** sonnet

## Goal
Add JWT-based login to the API. Users POST credentials, get a token back.

## Tasks
1. Create auth middleware that validates JWT tokens
2. Add `POST /login` endpoint that issues tokens
3. Add token refresh endpoint

## Completion Criteria
- [ ] `POST /login` returns a JWT with 1h expiry
- [ ] Protected routes reject invalid tokens
- [ ] Token refresh extends session

## Verification
- `npm test` passes
- No new lint warnings
```

### Linting briefs

`loop lint` checks brief files for format drift before dispatch. Deterministic — no LLM calls, subsecond per file, read-only.

```bash
loop lint wiki/briefs/cards/brief-049-scene-reset-on-run/index.md   # single file
loop lint wiki/briefs/cards/              # all queued briefs
loop lint --all wiki/briefs/cards/        # full corpus scan
```

Checks: frontmatter style, required fields, Budget section, Depends-on validity, dep ID format, ADR link resolution, MANDATORY reading link resolution, Status consistency.

Exit `0` — clean. Exit `1` — drift detected with a human-readable report.

### Push notifications

Set `ntfy_topic` in `.loop/config.json` and install the [ntfy](https://ntfy.sh) app. Events that notify: daemon start/stop, worker iteration completed, worker failure, brief completed (awaiting eval), brief merged, rate limit hit, escalation.

### Multi-machine

The daemon commits state to git and pushes. You can run the daemon on a remote machine, pause/resume from any machine (`loop pause` / `loop resume` write git-committed signals), and check status from anywhere.

### Cost tracking

Every Claude Code session logs cost, tokens, and duration to `.loop/state/metrics.jsonl`. `loop metrics` summarizes:

```
# Loop Metrics Report

## Cost Summary
- Total: $4.23
- Worker (productive): $3.81 (90%)
- Queen (overhead): $0.42 (10%)
- Worker iterations: 12
```

## Philosophy

Simple Loop separates two things that often get conflated:

**The workstation surface** — skills and subagents you reach for constantly. `/loop-push` at session boundaries. `subagent_type: loop-research` when you need a parallel investigation. `/loop-pull` to orient at the start of a session. These should be in your hand, in every project, at all times. One install, no per-project setup.

**The orchestration loop** — opt-in heavy machinery for projects that warrant autonomous, multi-iteration work. The daemon, the briefs, the heartbeat queen. Not every project needs this. Use it when work is well-scoped enough that the human can stay at the director level and let the agents grind.

The kit ships with both. Install once, get the workstation surface forever. Run `loop init` only when a project actually needs the orchestration loop.

## License

MIT
