# Goals

simple-loop's own loop, bootstrapped 2026-05-05 per brief-143's ops execution. Briefs about simple-loop's source land here, not in portal. See portal's `wiki/operating-docs/cross-repo-loops.md` for the convention.

## Active program — hackathon hardening (2026-05-09)

Mattie hosts a hackathon Saturday 2026-05-09. One teammate also pushes to the same portal repo as a regular git user (no daemon, no briefs). Two threats this week's queue addresses:

1. **State-write correctness under worktree drift** — brief-150 fixes the class-of-bug that's been causing hand-merge churn (and indirectly caused today's `merge_sha`-as-int hive-blanking).
2. **Multi-tenant safety + observability** — briefs 146/147/148 surface external commits, escalate rebase conflicts, and stop swallowed-error dedup. brief-149 is polish; ship if time.

## Awaiting Mattie (not queued)

CLI bootstrap fixes from 2026-05-05 sit awaiting review/merge in simple-loop's `running.json#awaiting_review[]`. None block dispatch:

- **brief-142** — `loop set-status <brief-id> <new-status>` CLI primitive. Branch `brief-142-loop-set-status-command`.
- **brief-144** — `loop init` scaffolds `.loop/prompts/{queen,worker}.md`. Branch `brief-144-loop-init-scaffolds-prompts`.
- **brief-145** — `loop install-service` reads `config.json#git.main_branch`. Branch `brief-145-loop-install-service-reads-main-branch`.

## Credential-gated — NOT dispatchable

## Queued next

1. **brief-150 (daemon state writes always land on `main` — brief-140 redo)** — replaces `git commit` in `lib/actions.py:dispatch()` with git plumbing (`hash-object`/`update-index --cacheinfo`/`write-tree`/`commit-tree`/`update-ref`) so card-status flips + `running.json` projection commits land on `main` regardless of worktree state. New module `lib/git_plumbing.py` (~200 lines). brief-140 was authored on Lady-Titania, never pushed; this brief redoes the work in simple-loop's loop. Opus, parallel-safe, 2-3 cycles. Cycle ceiling 5. Auto-merge: false, Human-gate: review. Edit-surface: `lib/git_plumbing.py` (new), `lib/_set_card_status.py`, `lib/actions.py:dispatch()`, `install.sh`. Depends-on: _none_. Canonical at `wiki/briefs/cards/brief-150-daemon-state-writes-to-main-redo/index.md`.

2. **brief-146 (worker rebase-conflict → clean escalate signal)** — when worker's per-cycle rebase onto `${GIT_REMOTE}/${GIT_MAIN_BRANCH}` fails (teammate commit collision), emit `signals/escalate.json` with conflict context (`conflicted_paths`, `main_head` short SHA + author + subject), keep existing `awaiting_review[] kind=rebase-blocked` routing. Hive renders the row coral (`!`) instead of amber (`~`). Multi-tenant safety primitive. Sonnet, parallel-safe, 2 cycles. Cycle ceiling 4. Auto-merge: false, Human-gate: review. Edit-surface: `lib/daemon.sh` (rebase failure block), `lib/actions.py` (new escalate emitter helper). Depends-on: _none_. Canonical at `wiki/briefs/cards/brief-146-worker-rebase-conflict-escalate/index.md`.

3. **brief-147 (hive surfaces "external commits on main")** — new one-line status row: `External on main: N (last: <sha> <author> <subject>)`, count = commits on `${GIT_REMOTE}/${GIT_MAIN_BRANCH}` whose author is not in the daemon allowlist (filter by `committer.email`, env-overridable via `HIVE_DAEMON_AUTHORS`). Glance-surface for teammate activity. Sonnet, parallel-safe, 2 cycles. Cycle ceiling 3. Auto-merge: false, Human-gate: review. Edit-surface: `crates/hive/src/state.rs` (parser + struct), `crates/hive/src/main.rs` (render row). Depends-on: _none_. Canonical at `wiki/briefs/cards/brief-147-hive-external-commits-on-main/index.md`.

4. **brief-148 (daemon errors bypass dedup + `loop status` surfaces last error)** — two-part fix in one brief. (a) When `invoke_conductor` fails (queen prompt missing, claude CLI non-zero, JSON parse error), uniquify `LAST_CONDUCTOR_TRIGGER` so the next tick re-evaluates instead of catching the 1800s `no_active` dedup. (b) `loop status` prints a top-of-output `⚠ Daemon error (Ns ago): <reason>` block when `.loop/state/last-queen-error.json` is fresh. Closes both compounding factors named in `~/new-theory/portal/wiki/operating-docs/incidents/2026-05-03-harness-pain-points.md`. Sonnet, parallel-safe, 2 cycles. Cycle ceiling 4. Auto-merge: false, Human-gate: review. Edit-surface: `lib/daemon.sh` (`invoke_conductor` error paths + dedup), `bin/loop:cmd_status`. Depends-on: _none_. Canonical at `wiki/briefs/cards/brief-148-daemon-errors-bypass-dedup-and-loop-status-surfaces/index.md`.

5. **brief-149 (`loop info` command — provenance + drift)** — Tier-2 polish. New subcommand prints source-repo path + commit + dirty status, install dir + version + commit, brief counts (active/awaiting_review/queued/draft), drift line (first failing `loop lint` brief or `none`). Plumbs `install.sh` provenance write (`PROVENANCE.json`) + `loop init`/`loop update` config seeding. Sonnet, parallel-safe, 2 cycles. Cycle ceiling 3. Auto-merge: false, Human-gate: review. Edit-surface: `bin/loop` (new `cmd_info` + dispatch wiring), `install.sh` (PROVENANCE.json), `.loop/config.json` (new `simple_loop` block), possibly `lib/lint.py` (--summary mode). Depends-on: _none_. Ship if time; safe to land or shelve based on Friday EOD pressure. Canonical at `wiki/briefs/cards/brief-149-loop-info-command/index.md`.
