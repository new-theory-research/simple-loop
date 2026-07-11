# Harness updates — decision tree + restart protocols

The simple-loop harness (daemon + queen + worker + validator) is live infrastructure. Updating it while it's running is tricky because the thing running the update may be the thing being updated. This doc captures the decision tree we've worked out the hard way.

**Status: living document.** Add entries from every harness-update session. This reflects what we've tripped on, not what we've imagined.

## The one command: `loop update`

From inside any loop-enabled project, `loop update` is the propagation edge (issues #20, #57). It:

1. Locates the harness source from `PROVENANCE.json`'s `source_repo` (fails loud with guidance if it's missing — re-run `./install.sh` from your clone).
2. `git pull --ff-only`s that source, then runs `install.sh` (which refuses a dirty tree unless `--force`). Reports the installed old→new SHA.
3. Prints a since-baseline changelog (`git log --oneline base..HEAD` over `templates/ core/ lib/`) so you can see what migration work applies.
4. Diff-aware refresh of this project's `.loop/prompts/`: identical → left alone; template-newer with an unmodified project copy → updated; a copy you've customized (differs from both the old and new template) → **preserved**, with a three-way-sync instruction naming the file. A missing daemon-required prompt is recreated, never silently skipped.
5. If a daemon is running for this project, prints `loop stop && loop start` and why — it does **not** auto-restart.

Safe to run while the daemon is live: prompts and `lib/*.py` are re-read per tick; only `lib/daemon.sh` needs the restart, which `loop update` surfaces as an instruction. The manual five-command incantation below is what `loop update` now automates — reach for it only when you need to diverge from the happy path.

## The decision tree

### 1. Where does the code live?

Three locations, different ownership semantics:

| Location | What's there | Who edits | Effect |
|---|---|---|---|
| `ScavieFae/simple-loop` master (GitHub) | Source of truth for harness code | Maintainer, agents via briefs | Authoritative; changes propagate via `loop update` |
| `~/.local/share/simple-loop/` | Installed copy on this machine | `loop update` pulls from master; can hand-edit for urgent patches | What the daemon actually runs |
| `.loop/prompts/` + `.loop/modules/*/state/` (project-local) | Per-project customization | Project maintainer | Read by daemon each tick; overrides install for this project only |

**The propagation direction:** master → installed (via `loop update`) → project-local (copied on `loop init`, edited per project after).

**Project-local takes precedence** for the daemon on THIS project. If you edit `.loop/prompts/queen.md` here, the daemon reads your edit, not the installed template.

### 2. Brief vs manual?

Use this rubric:

| Situation | Fix mode |
|---|---|
| Harness isn't processing briefs | **Manual** — briefs can't unblock a queue that doesn't dispatch |
| Single-file prompt / config edit, text-only | **Manual** — 5-minute change, no test harness needed |
| New feature on a working harness | **Brief** — let the loop exercise itself, validator catches drift |
| State-migration / schema change | **Brief with test cases** — fabricate bad state, assert repair |
| Fix that would run through the daemon that's being fixed | **Manual** — circular dependency kills the brief flow |
| Refactor across multiple files with test coverage | **Brief, ideally with research cycle** |

**When unsure, ask:** "if this fix went wrong, would it make the harness *worse* than right now?" If yes → more test coverage, usually means a brief. If no → manual is fine.

### 3. Propagation for manual fixes

When you hand-patch the harness:

- **Project-local only** (`.loop/prompts/*`, `.loop/state/*`, `.loop/knowledge/*`): edit, commit to project, done. Daemon picks up on next tick.
- **Installed copy** (`~/.local/share/simple-loop/`): edit, but **also sync to simple-loop master** via clone-edit-push — otherwise next `loop update` overwrites your fix.
- **Template** (`~/.local/share/simple-loop/templates/`): same as installed — sync to master, or lose the change on next update.

The **"three-way sync"** pattern:

```bash
# 1. Edit project-local (takes effect next tick)
vi .loop/prompts/queen.md
git commit -m "..." && git push

# 2. Sync to installed template (so `loop update` won't regress)
cp .loop/prompts/queen.md ~/.local/share/simple-loop/templates/prompts/queen.md

# 3. Push to simple-loop master (durable across any fresh install)
git clone https://github.com/ScavieFae/simple-loop /tmp/sl-$(date +%s)
cp ~/.local/share/simple-loop/templates/prompts/queen.md /tmp/sl-*/templates/prompts/queen.md
cd /tmp/sl-* && git add . && git commit -m "..." && git push
```

### 4. Restart protocols

When does a restart take effect?

| Edit target | Hot-reloaded | Needs daemon restart | Needs `loop update` |
|---|---|---|---|
| `.loop/prompts/*` (queen, worker, validator) | ✅ next tick (~120s) | — | — |
| `.loop/knowledge/*`, `.loop/state/goals.md` | ✅ next tick | — | — |
| `~/.local/share/simple-loop/lib/daemon.sh` | — | ✅ `loop stop && loop start` | — |
| `~/.local/share/simple-loop/lib/actions.py` | — | ✅ actions invoked fresh per command | — |
| `~/.local/share/simple-loop/templates/*` | — | — | Template's only read by `loop init` / `loop update` |
| `~/.local/bin/loop` | N/A | Shell picks up immediately for new invocations | — |
| `ScavieFae/simple-loop` master | — | — | ✅ `loop update` pulls + installs |

**The trap:** editing `~/.local/share/simple-loop/templates/...` doesn't affect the running daemon until you also copy to `.loop/prompts/...` or the daemon restarts and re-reads. Always edit *both* — or the change doesn't land.

### 5. Verification after a harness update

- Daemon alive: `cat .loop/state/daemon.pid | xargs -I{} ps -p {} -o pid,etime,command`
- Daemon ticking: heartbeat fresh — `cat .loop/state/heartbeat.json` shows `ts` within 2× tick interval
- Queen running: `tail -5 .loop/state/log.jsonl` — expect `heartbeat_noop` or `daemon:*` events recently
- Queue moving: if something's queued, queen writes pending-dispatch.json within a tick or two
- No stale signals: `ls .loop/state/signals/` returns clean or expected-live files only

If any of these are wrong post-update, **roll back first, diagnose second.** Harness regressions compound.

### 6. Recovery if a harness update breaks something

- `loop stop` — always first, stops the compounding
- Restore the previous version: `git checkout HEAD~1 -- <file>` for project-local, or pull from `~/.local/share/simple-loop/templates/` for local overrides
- For install-level damage: `loop update` pulls fresh from master — only works if master isn't what broke
- Hard reset of `~/.local/share/simple-loop/`: `rm -rf ~/.local/share/simple-loop && loop install` — last resort

### 7. Known escape hatches (ordered by "least invasive")

If the daemon's misbehaving but you need work to move:

1. **Write `pending-dispatch.json` manually** to unblock a specific brief (queen failed to dispatch it)
2. **Write `pending-merge.json` manually** to approve a brief without daemon ceremony (or use `loop approve <brief-id>` which does the same thing — always prefer the CLI)
3. **Hand-merge a brief on main** via `git merge --no-ff <branch>` when the daemon's stuck in a state-mismatch or other internal error (see [hand-merge-brief.md](hand-merge-brief.md))
4. **Stop the daemon entirely** and run cycles by dispatching loop-coder agents directly from the main-thread session
5. **Clean running.json by hand** (pure-JSON edits) to prune stale active entries or backfill missed merges — last-resort because it's easy to corrupt state further
6. **Unblock a parked (`status:"blocked"`) brief** — commit `progress.json` status `blocked`→`running` + a learnings note to the brief's **branch** (see below; issue #39)

Each escape hatch is a signal the harness wanted something it didn't have. File that observation somewhere durable (`.loop/knowledge/learnings.md`, or a runway entry for the permanent fix).

#### Unblocking a parked brief — read the trap first

`lib/assess.py` reads `.loop/state/progress.json` via `git_show(project_dir, ref, ...)` (`git_show(project_dir, branch, ".loop/state/progress.json")`, `assess.py:400`) — **from the brief's committed branch (or its remote), never from the worktree.** A brief a worker deliberately parked with `status:"blocked"` (e.g. pending director-supervised spend) produces `CONDUCTOR:brief_blocked` on every tick until that status flips on the branch.

**The trap:** editing the worktree's `.loop/state/progress.json` is the obvious move and a silent no-op — assess never reads it. There is no unblock signal today; nothing in the daemon, queen prompt, or these docs defines how a parked brief resumes on its own (tracked as a fix-shape decision on issue #39). Until then, this is the only working path:

```bash
# 1. Temp worktree of the brief's branch (not the daemon's live worktree)
git worktree add /tmp/unblock-<brief-id> <brief-id>
cd /tmp/unblock-<brief-id>

# 2. Flip status and record why, in progress.json
#    - "status": "blocked" -> "running"
#    - append a learnings entry pointing at the receipts that satisfy the block
#      (e.g. "unblocked 2026-07-05: supervised Modal run completed GREEN, receipts at <commit>")

# 3. Commit and push to the brief's branch
git add -f .loop/state/progress.json   # -f: gitignored on main, carried on branches (issue #64)
git commit -m "[scav] unblock <brief-id>: <why>"
git push origin <brief-id>
```

The next tick's `assess` sees the brief's status as `running` again and emits a `WORKER` trigger — a different conductor target than the cached `brief_blocked` dedup, so the stale dedup window doesn't suppress it. Clean up the temp worktree (`git worktree remove /tmp/unblock-<brief-id>`) once the daemon picks the brief back up.

## Script-over-inference

An emerging pattern: when an agent needs to produce a deterministic, repeatable output (a timestamp, a UUID, a hash, a structured log line in a fixed schema), **write a script the agent calls** instead of asking the LLM to produce the output directly. The agent passes fields; the script handles the mechanics.

### Why

LLMs hallucinate under token pressure, especially for values with statistical structure (round numbers, round minutes, plausible-looking IDs). Writing a script costs ~30 lines and ~0 inference tokens per call. Re-prompting the LLM to "please use real timestamps" costs tokens every invocation and works until it doesn't.

### When to reach for this

- The output is **deterministic** (wall-clock time, repo hash, env lookup, config read)
- The output has **statistical structure the LLM will drift toward** (round numbers, templated IDs, schema-shaped JSON)
- The call happens **often** (log event per tick, metrics emission per cycle) — per-call savings compound
- A **downstream consumer trusts the field** (TUI reading ts, validator reading verdict). Trust + hallucination is the combination that silently breaks things.

### When NOT to reach for this

- **Taste calls.** Deciding merge vs fix vs escalate *is* the LLM's job. Don't script a taste gate.
- **One-off operations.** Writing a script for a job that runs twice is over-engineering.
- **Inputs the LLM needs to reason about.** If the agent needs to synthesize the value from context, keep it in the prompt. Only script the *output plumbing* after the thinking is done.

### Companion discipline: pin model versions

Script-over-inference removes hallucination from one surface. Model-version drift removes reproducibility from another. When `claude --model opus` resolves to whatever ships as "latest opus," a silent upgrade can change agent behavior overnight. Pin specific model IDs in daemon invocations when the behavior matters.

Current scripts in simple-loop:
- `scripts/log-event.py` — injects wall-clock `ts`, appends to `.loop/state/log.jsonl`

## Contribution rule

**Add an entry here after any harness-update session.** Include: what you changed, where it lived, what propagation you did, what broke along the way, how you recovered. Running log of earned knowledge.

---

## Session log

### 2026-04-28 — brief-100: promotion-path classification (phantom-completion fix)

**Trigger:** Two phantom completions in two days — brief-067 and brief-099 routed to `awaiting_review[]` with zero cycles completed. `awaiting_review[]` was a lossy mailbox: legitimate completions and exception-routed failures landed in the same bucket with no label distinguishing them.

**What changed (simple-loop master, commits `c4bb9f5`→`18a343c`):**

| Cycle | Commit | What |
|---|---|---|
| 2 | `c4bb9f5` | `kind` field on `move_to_awaiting_review()` in `lib/actions.py` (required, `Literal[...]`). Cycle-completion gate on the `complete` path: refuses if `iteration==0`, `tasks_remaining` non-empty, or no commits beyond `Initialize brief`. All 4 `daemon.sh` call sites updated to pass explicit `kind`. `actions.py:857` merge-conflict bypass patched with `entry["kind"] = "merge-conflict"` directly. |
| 3 | `c94713b` | Stale-local-branch refuse-or-recreate in `lib/daemon.sh` worktree creation (~lines 288–294). Before reusing an existing local branch, fetch `origin/main` and count commits behind. If ≥ `MAX_COMMITS_BEHIND` (default 30), delete + recreate from main. Closes the brief-067 upstream cause (110 commits stale, rebase failed, phantom routed). |
| 4 | `18a343c` | `human_queue_summary()` in `lib/actions.py` surfaces `kind` per entry in `loop status` output. Queue-steward classification: `kind=complete` → "ready for review (Mattie's gate)"; any other kind → "needs daemon-side disposition." Backfill: `entry.get('kind', 'unknown')` on read — no schema migration, old entries show as `kind=unknown`. |

**Files edited (simple-loop master):**
- `lib/actions.py` — `move_to_awaiting_review` signature + cycle-completion gate + merge bypass + `human_queue_summary` kind surfacing
- `lib/daemon.sh` — 4 call sites updated; stale-branch refuse-or-recreate at worktree creation
- `scripts/test-flow-v2.sh` — tests 105–114 (cycle-completion gate, stale-branch recreate, kind backfill + queue-steward)

**Test count:** 197 pass, 3 fail (pre-existing presence-check canonical-root failures; not caused by brief-100).

**Propagation required (install-loop discipline):**
```bash
cd ~/claude-projects/simple-loop && git pull && ./install.sh && loop stop && loop start
```
Without this: daemon runs the old `lib/daemon.sh` and `lib/actions.py`. New `kind` field won't appear on promotions. Stale-branch check won't fire.

**What broke along the way:**
- `actions.py:857` — a 5th unlabeled promotion path (direct `awaiting_review[]` append in `merge()`) was not a call to `move_to_awaiting_review()` and thus didn't inherit the `kind` parameter. Required a separate direct `entry["kind"] = "merge-conflict"` patch. Documented in cycle-1-audit.md.
- Cycle-completion git check: used `subprocess.run` with `check=False`. On fetch failure, stdout is empty, which parses as n=0 — would incorrectly refuse legitimate promotions. Guarded with `returncode==0` before parsing.
- Stale-branch fetch must happen before counting — without `git fetch origin main` first, the count uses a stale `origin/main` ref and misses recently-committed commits.

**Pattern crystallized — promotion-path classification:**

Every queue destination (`awaiting_review[]`, `rejected[]`, `history[]`) should carry a `kind` label at write time. "Why is this entry here?" is a question that comes up every time scav reads queue state. The `kind` field answers it structurally — no string-parsing of `reason` fields needed. When a new exception path is added to the daemon, add a new `kind` literal first.

**Companion discipline (install-loop):** Every harness brief approval is paired with `git pull && ./install.sh && loop stop && loop start` before verifying receipt-claims. The install step is not optional — the daemon runs from `~/.local/share/simple-loop/`, not from the master clone. Skipping the install means you're verifying against the prior version.
