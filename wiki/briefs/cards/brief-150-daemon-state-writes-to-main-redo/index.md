---
ID: brief-150-daemon-state-writes-to-main-redo
Branch: brief-150-daemon-state-writes-to-main-redo
Status: active
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - lib/daemon.sh
  - lib/actions.py
  - lib/_set_card_status.py
  - lib/git_plumbing.py (new)
  - install.sh
Depends-on: _none_
Tags: [harness, daemon, state, working-tree-drift, multi-tenant, urgent, redo]
---

# Brief: daemon state writes always land on `main` (brief-140 redo)

!!! abstract "Intent"
    Eliminate the bug class where the daemon's dispatch-time bookkeeping commits (card-status flips + `running.json` projection) land on whichever branch the main worktree happens to be checked out on, instead of `main`. Replace `git -C <project_dir> commit` in the dispatch path with git plumbing (`hash-object`, `update-index --cacheinfo`, `write-tree`, `commit-tree`, `update-ref`). Working tree state becomes irrelevant.

## Why this is brief-150 and not "approve brief-140"

brief-140 (in portal at `~/new-theory/portal/wiki/briefs/cards/brief-140-daemon-state-writes-to-main/`) ships closeout, review, and smoke artifacts dated 2026-05-05 claiming the work is on branch `brief-140-daemon-state-writes-to-main` of `ScavieFae/simple-loop`. That branch does not exist on the remote, no PR was opened, and `lib/git_plumbing.py` is absent from current `master`. Most likely the work was authored on Lady-Titania and never pushed before ScavieFae's session there ended. This brief redoes the work in simple-loop's own loop on morgan-lefay so it actually lands. Existing portal artifacts will be invalidated post-merge (`closeout.md` rewritten, `review.md` rewritten — taste call belongs to Mattie's review pass).

## Motivation / receipts

The bug bites whenever the main worktree drifts onto a feature branch. Originally surfaced 2026-05-04 (scav's report — 4 hand-merges in one day burning ~5 min each because hive reads `main` but daemon was committing to `brief-122-...`). It re-surfaced indirectly today (2026-05-07): the hand-merge recipe used to work around the bug emitted `merge_sha=92329478` as a JSON int, which poisoned hive's parser and blanked the Active cell.

The hackathon constraint (Saturday 2026-05-09): a teammate also pushes to portal `main`. Multi-tenant turbulence makes worktree drift more likely (kickstarts, debug detours), so the workaround tax compounds. Fix the class instead of patching the workaround.

## Starting context

!!! info "Pointers — read in this order"
    1. `~/new-theory/portal/wiki/research/2026-05-04-daemon-write-branch-drift.md` — full root-cause analysis. The recommended fix ("Option C — plumbing-only writes") is what this brief implements.
    2. `~/new-theory/portal/wiki/briefs/cards/brief-140-daemon-state-writes-to-main/closeout.md` — orphaned closeout from the unrecovered branch. Lists exact module shape and patch sites; treat as design spec, not as a record of merged work.
    3. `~/new-theory/portal/wiki/briefs/cards/brief-140-daemon-state-writes-to-main/smoke.md` — orphaned smoke; reuse the assertion list as this brief's smoke target.
    4. `lib/actions.py:dispatch()` — current state-write call sites. Search for `git_commit` / `git -C "$project_dir" commit` near the dispatch flow. The two relevant sites: card-status flip (`Status: queued → active`) and `running.json` projection commit. Both currently `git commit` against whatever branch HEAD points at.
    5. `lib/_set_card_status.py` — current writer; the redo extracts a pure `transform_card_status_content(content, status) -> (new_content, changed)` so the plumbing path can transform without touching disk.
    6. `lib/state.py:write_running_json` — the on-disk projection writer (brief-108-d). Untouched here; the bug is where the *commit* lands, not where the projection is *written*.
    7. `lib/daemon.sh:1160-1175` — `GIT SYNC: fetch only` mode. Anti-pattern: do not disable. Once plumbing lands, this mode becomes purely advisory for `git pull`; commits succeed regardless of worktree state.
    8. `wiki/briefs/cards/brief-145-loop-install-service-reads-main-branch/index.md` — recent simple-loop CLI brief shape.

## Scope

### In

**1. New module `lib/git_plumbing.py` (~200 lines):**

- `commit_files_to_branch(project_dir, files, branch, message, remote=None, push=False) -> (commit_sha, did_commit)` — primary entrypoint. Writes one or more files to a named branch via:
  - `git hash-object -w <file>` for each file (writes blobs into the object store)
  - `update-index --cacheinfo 100644,<sha>,<path>` against a temporary index seeded from the parent tree (`GIT_INDEX_FILE=.git/index.plumbing.<pid>`, cleaned up in `finally`)
  - `git write-tree` against the temp index
  - `git commit-tree <tree-sha> -p <parent-sha> -m <message>` to produce the commit
  - `git update-ref refs/heads/<branch> <new-sha> <expected-parent-sha>` for atomicity
  - Optional `git push <remote> <branch>` if `push=True`
- `commit_file_to_branch(...)` — single-file convenience wrapper.
- `read_file_at_branch(project_dir, file_path, branch)` — `git show <branch>:<path>` wrapper.
- `GitPlumbingError` — surfaces ref races, missing branches, malformed trees.
- **Idempotency:** when the resulting tree equals the parent tree, return `(parent_sha, False)` without creating a commit or moving the ref.
- Working tree is never touched.

**2. `lib/_set_card_status.py` refactor:**

Extract `transform_card_status_content(content: str, status: str) -> (new_content: str, changed: bool)` — pure string-transform. `set_card_status` keeps its on-disk semantics; the new plumbing path uses the transform directly on content from `git show main:<path>`.

**3. `lib/actions.py:dispatch()` patch:**

Replace the two `git commit` call sites in the dispatch flow:

- **Card status flip** (`Status: queued → active`): read card content from `main` via `read_file_at_branch`, transform via `transform_card_status_content`, commit to `main` via `commit_file_to_branch`. Failure non-fatal (mirrors prior behavior — log + continue).
- **`running.json` projection commit**: still call `project_running()` to write `running.json` on disk (single-writer contract preserved); then read both `running.json` and `runtime-events.jsonl` from disk and commit them to `main` via `commit_files_to_branch`.

The trailing `git push origin main` is unchanged. Post-fix, the local `main` ref actually has the new commits when push runs.

**4. `install.sh` patch:**

One-line addition — copy `lib/git_plumbing.py` to `$INSTALL_DIR/lib/` next to the other `lib/` copies. Position next to the existing `lib/state.py` copy line.

**5. Smoke fixture:**

Standalone test under `scripts/test-plumbing-smoke.sh` (or extension to `scripts/test-flow-v2.sh`):
- Build a fresh git repo with `main` branch + a feature branch
- Check out the feature branch (working tree drifts)
- Call `commit_files_to_branch` to write a fixture file to `main`
- Assert: `main` ref advanced, feature branch HEAD unchanged, working tree HEAD unchanged, working tree clean
- Idempotency: call again with same content → returns `(sha, False)`, ref does not advance

### Out

- **Merge-time writes** (`actions.py:cleanup_card_status_set` + post-merge projection commit). Research §"Recommended fix" tagged this as lower priority — the merge path explicitly does `git checkout main` before its writes, so the bug class doesn't reach it. Migrating to plumbing here is a consistency win, not a bug fix; track as a follow-up if drift surfaces.
- **`daemon.sh:78` (startup) and `daemon.sh:1197` (per-tick projector).** These shell out to `state.py write-running-json` which writes the file on disk only — no commit. The next `dispatch()` tick commits via plumbing, picking up the on-disk projection correctly.
- **`GIT SYNC: fetch only` mode.** Untouched. Per anti-pattern: do not disable.
- **Brief-107 cleanup contract / brief-108-d projector.** Untouched. The bug was upstream; both produce/project state correctly, the writes just landed in the wrong place.
- **Lint guard for `git -C <project_dir> commit` in `lib/`.** Cheap insurance against regression, but not a bug fix; out of scope here per `feedback_fix_it_three_places` follow-up plan.

### Residue

- Merge-path migration to plumbing — track as follow-up if drift appears post-merge. Trigger: any "card-status `merged` flip landed on a feature branch" log line.
- Lint guard for `git commit` in `lib/`. Add if pattern reappears.
- Hand-merge recipe (`docs/operating/hand-merge-brief.md`) updates: with plumbing in place, the hand-merge recipe's `state.py append-event` step still works the same way; no recipe edit needed. Confirm.

## Cycle plan

- **Cycle 1** (`loop-coder`, opus) — implement: write `lib/git_plumbing.py`, refactor `_set_card_status.py`, patch `actions.py:dispatch()` at both call sites, update `install.sh`. Validator runs after.
- **Cycle 2** (`loop-coder`, opus, cushion) — smoke fixture: `scripts/test-plumbing-smoke.sh` reproducing the bug condition + asserting all five smoke checks. Run `scripts/test-flow-v2.sh`; expect baseline pass count unchanged.
- **Cycle 3** (cushion) — only if needed.

Cycle ceiling: 5. Opus model because the plumbing module touches a class of git semantics that's easy to get wrong (index races, ref atomicity, dirty-tree edge cases) and the cost of a bug here is "daemon state corrupted across all running projects."

## Verification

```bash
# Baseline (master before patch):
cd ~/claude-projects/simple-loop
bash scripts/test-flow-v2.sh 2>&1 | tail -3
# expect: a known pass count (record it; brief-140 closeout reported 157/49)

# After patch lands on the worker branch:
bash scripts/test-flow-v2.sh 2>&1 | tail -3
# expect: same pass count, no regression

# Smoke fixture:
bash scripts/test-plumbing-smoke.sh
# expect: all assertions green — main ref advances, feature branch and working tree HEAD unchanged

# End-to-end on a real project after install + restart:
cd ~/new-theory/portal && git checkout brief-XXX-some-feature
launchctl kickstart -k gui/$(id -u)/com.scaviefae.simpleloop.portal
# wait one tick; daemon dispatches; observe:
git -C ~/new-theory/portal log -3 --oneline main
# expect: "loop: card status → active for ..." + "loop: project running.json (...)" on main
git -C ~/new-theory/portal log -3 --oneline brief-XXX-some-feature
# expect: NO daemon commits on the feature branch (worktree HEAD)
```

## Escalation triggers

- **Index race in test fixture.** If `GIT_INDEX_FILE` collision causes flaky fixtures within 2 cycles of attempts, escalate; the production daemon serializes ticks so production is fine, but the test pattern needs work.
- **`update-ref` expected-parent races.** If concurrent dispatches (worker_slot=0 and worker_slot=1) both race on `refs/heads/main`, the second `update-ref` fails. Behavior must be: detect non-zero exit, log "loop: dispatch state-write race — retrying", retry once with fresh parent. If retry also fails, escalate the brief and surface as `signals/escalate.json`.
- **`scripts/test-flow-v2.sh` regression.** If the patch causes any previously-passing test to fail, escalate. Do not paper over.
- **`install.sh` codesign behavior.** Today's install signs hive after `cp` (commit `4719aa5`). If `lib/git_plumbing.py` deployment hits a similar permissions/signing surface (it shouldn't — Python files don't sign), escalate.
- **Card content drift between `main` and worktree.** If a card has uncommitted edits on the current branch that aren't on `main`, the plumbing flip writes only the status change against `main`'s content (drops the uncommitted edits). In practice dispatch always operates against committed cards (the brief was queued, which means the card was committed). If a real failure mode emerges, escalate; document in TROUBLESHOOTING.

## Anti-patterns

- Don't disable `GIT SYNC: fetch only` mode — that's a state-corruption guard.
- Don't migrate the merge-path writes in this brief — separate concern.
- Don't `git stash` the working tree to "make commits clean" — defeats the entire fix.
- Don't add ANSI / formatting to the new commit messages; mirror the existing `loop: card status → ...` and `loop: project running.json (...)` format.
- Don't auto-execute long-running terminal commands inside cycles.

## Artifact

- New `lib/git_plumbing.py`.
- Refactor `lib/_set_card_status.py` (extracted transform).
- Patch `lib/actions.py:dispatch()` (two call sites).
- Patch `install.sh` (deploy plumbing module).
- New `scripts/test-plumbing-smoke.sh` (or test-flow-v2 extension).
- `closeout.md` + `review.md` per contracts.

## What this unlocks

Daemon bookkeeping commits become correct under any worktree state. The hand-merge recipe's role narrows (no longer the only path that lands state on `main`), reducing the surface area where today's `merge_sha`-as-int class bug can re-emerge. Hackathon two-human portal becomes safe in the state-write dimension; pairs with brief-146 (rebase-conflict escalate) for the read-side multi-tenant story. Closes the bug class scav's report flagged 2026-05-04 + the orphaned brief-140 was supposed to ship.
