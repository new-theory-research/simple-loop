---
ID: brief-147-hive-external-commits-on-main
Branch: brief-147-hive-external-commits-on-main
Status: merged
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - crates/hive/src/state.rs
  - crates/hive/src/main.rs
Depends-on: _none_
Tags: [hive, multi-tenant, observability]
---

# Brief: hive surfaces "external commits on main since last daemon write"

!!! abstract "Intent"
    Hive gains a one-line status row: `External on main: N (last: <short-sha> <author> <subject>)` — counting commits on `${GIT_REMOTE}/${GIT_MAIN_BRANCH}` whose author is not in a known-daemon allowlist. During the hackathon, when Mattie's teammate pushes directly to portal `main`, hive shows it within one render tick instead of staying silent until something downstream breaks.

## Motivation

Hackathon Saturday 2026-05-09. Mattie runs simple-loop; one teammate pushes to the same portal repo as a regular git user (no daemon, no briefs). Today hive renders daemon-side state — what queen + workers + validator did. It has no view of "stuff on main that didn't come from us." The first signal that a teammate has been working would be a rebase conflict (brief-146) or a stale `running.json#last_merge_sha`. Both are downstream tells.

A 1-line "external commits on main: N" pulled at refresh-time closes that gap. Mattie glances at hive, sees a non-zero count, and knows to coordinate before dispatching the next brief.

## Starting context

!!! info "Pointers — read in this order"
    1. `crates/hive/src/state.rs:370-441` — `BuzzState::load` shape: how panels assemble from log + state files. Same pattern for the new computed field.
    2. `crates/hive/src/state.rs:65-85` — `read_daemon_pid`, `read_daemon_started_at`, `pid_alive`. Reference for "small read helper at module top."
    3. `crates/hive/src/main.rs:307-433` — `render_hive` panel renderer. Where to slot the new row.
    4. `lib/daemon.sh:1160-1175` — `GIT SYNC` block; the daemon already calls `git fetch` periodically, so `${GIT_REMOTE}/${GIT_MAIN_BRANCH}` is fresh enough at hive-refresh cadence.
    5. `wiki/briefs/cards/brief-145-loop-install-service-reads-main-branch/index.md` — recent small brief shape.

## Scope

### In

- **New helper `read_external_commits_on_main(project_dir, daemon_authors) -> ExternalMain`** in `crates/hive/src/state.rs`. Runs `git -C <project_dir> log --format='%H|%an|%ae|%s' ${GIT_REMOTE}/${GIT_MAIN_BRANCH} -n 50` (cap at 50; we don't need the full history) and returns:
  - `count_external: usize` — commits whose author email/name is not in the allowlist
  - `last_external: Option<ExternalCommit { sha_short, author, subject }>`
- **Daemon-author allowlist.** Approach: filter by `committer.email` against a small static allowlist + an env override.
  - Static defaults: anything containing `scaviefae`, `claude-bot`, `noreply@anthropic`, plus the project's `git config user.email` (read via `git config --get user.email` at hive startup; this is "what Mattie's machine commits as").
  - Env override: `HIVE_DAEMON_AUTHORS` (comma-separated patterns) supplements the allowlist for projects with non-default daemon-author setups.
  - Match is `contains` (case-insensitive). Imperfect; we accept false negatives (treating a daemon commit as external) over false positives (silently hiding a teammate). When uncertain, count it as external.
- **Surface in hive Hive panel** as a new row beneath the Status block: `External on main: 0` (muted) when zero, `External on main: 3 (last: a3f1c0d alice@x.com — fix typo)` (coral when >0). Add to `BuzzState` and reference from `render_hive` in `main.rs`.
- **Refresh cadence:** read at the same tick as the rest of `BuzzState::load`. No new timer.

### Out

- **No `last-daemon-commit-sha` tracking in `.loop/state/heartbeat.json`.** Considered + rejected — adds a write-path for a fact we can derive from author filtering. If filtering proves insufficient (lots of false negatives at scale), revisit as a follow-up. Recommendation noted in the original brief framing; sticking with author-filter for simplicity.
- **No clickthrough / detail panel.** A non-zero count is the signal; Mattie runs `git log` herself if she wants the full diff.
- **No rebase or merge action from hive.** Read-only surface.
- **No alerting / desktop notification.** Hive is a glance surface; brief-148 owns "errors get a notification."

### Residue

- If false-negative rate (daemon commits flagged external) is high in practice, swap to "track last-daemon-commit-sha at write-time, count distance from there." That's a ~30-line follow-up.
- A follow-up could split external commits by author when the count is high — out of scope.

## Cycle plan

- Cycle 1 (`loop-coder`, sonnet) — implement `read_external_commits_on_main` + `ExternalCommit` struct + author allowlist resolver in `state.rs`; wire into `BuzzState::load`. Validator runs after.
- Cycle 2 (`loop-coder`, sonnet) — render row in `main.rs` (`render_hive`); cargo build clean; smoke against a fixture project with a known-external commit on main.

## Verification

```bash
# In a sandbox project:
# 1. Set git config user.email to "scaviefae@example.com" (will be in the daemon allowlist via "scaviefae" match)
# 2. Make a commit (counts as daemon)
# 3. Push, then make a commit as "teammate@example.com" and push
# 4. Run hive

# Expected hive row:
#   External on main: 1 (last: <sha> teammate@example.com — <subject>)
# Coloring: coral when >0, muted when 0.

# Override test:
HIVE_DAEMON_AUTHORS="teammate@example.com" hive
# Expected hive row:
#   External on main: 0 (muted)
```

## Escalation triggers

- **`git log` against `${GIT_REMOTE}/${GIT_MAIN_BRANCH}` errors** (remote not configured, branch not fetched yet) — render `External on main: ?` (muted, neutral framing); don't crash hive. Log to stderr once per session.
- **Allowlist resolution returns empty** (no `git config user.email`, no env override) — fall back to the static defaults; surface `External on main: N (allowlist=defaults-only)` so the operator knows their local config didn't apply. Don't escalate the brief.
- **Cycle 1 ships a working state read but `BuzzState::load` becomes >100ms slower** — escalate; cap `git log` at 20 instead of 50, or move to a cached-with-mtime read.

## Anti-patterns

- Don't write to `.loop/state/` for this brief — read-only.
- Don't shell out to `loop` from hive — `git` directly.
- Don't crash hive on a missing remote.
- Don't auto-execute long-running terminal commands inside cycles.

## Artifact

- New helper + struct in `crates/hive/src/state.rs`.
- New row in `crates/hive/src/main.rs` `render_hive`.
- `closeout.md` + `review.md` per contracts.

## What this unlocks

Multi-tenant portal stops being silent. Mattie sees teammate activity at hive-refresh latency, not at next-rebase-conflict latency. Pairs with brief-146 (rebase-conflict escalate) so both the "approaching" signal (external commits accumulating) and the "collision" signal (rebase failed) are visible.
