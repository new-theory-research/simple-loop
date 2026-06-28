---
ID: brief-149-loop-info-command
Branch: brief-149-loop-info-command
Status: merged
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - bin/loop
  - install.sh
  - .loop/config.json
Depends-on: _none_
Tags: [cli, onboarding, observability]
---

# Brief: `loop info` — provenance + drift surface for cold-start agents

!!! abstract "Intent"
    Add `loop info` subcommand printing the framework's provenance (source repo, install dir, version, commit SHA) and the project's `.loop/` state at a glance. Eliminates the cold-start archaeology pass documented in `docs/operating/agent-cold-start-friction.md`. Tier-2 polish: ship if time allows during the hackathon prep window; safe to land or shelve based on time pressure.

## Motivation

From `docs/operating/agent-cold-start-friction.md`:

> Every cold-start agent does the same archaeology pass on the same questions. The cost compounds across projects … and across sessions (every fresh context window starts from zero). Fixing it once at `loop init` time saves the cost forever after.

Today there's no command that answers "what version of simple-loop is this project on, where did it come from, and is anything drifting?" The doc proposes the exact output shape (`docs/operating/agent-cold-start-friction.md:89-104`).

For the hackathon the value is concrete: when teammate or scav asks "what's running?" Mattie types `loop info` and gets a one-screen answer.

## Starting context

!!! info "Pointers — read in this order"
    1. `docs/operating/agent-cold-start-friction.md:89-104` — proposed `loop info` output shape (the spec).
    2. `bin/loop:994-1110` — `cmd_status`; closest reference for a multi-section CLI command. Mirror its python-heredoc + section-block style.
    3. `bin/loop:715-722` — current `.loop/config.json` write at `loop init`; this is where the new `simple_loop.commit` field gets seeded.
    4. `install.sh:28-39` — install entrypoint where `SCRIPT_DIR` is known; this is where the source-repo commit SHA gets recorded into INSTALL_DIR for `loop info` to read later.
    5. `lib/lint.py` — drift-check primitive that `loop info` calls to compute the drift line.
    6. `wiki/briefs/cards/brief-142-loop-set-status-command/index.md` — recent small CLI brief shape.

## Scope

### In

- **`install.sh` records the source repo commit SHA** at install time. After line 39 (the `Mode:` echo), capture `SOURCE_COMMIT=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")` and `SOURCE_DIRTY=$(git -C "$SCRIPT_DIR" status --porcelain 2>/dev/null | head -1)` (presence indicates dirty). Write both to a tiny `$INSTALL_DIR/PROVENANCE.json`: `{"source_repo": "<SCRIPT_DIR>", "source_commit": "<short-sha>", "source_dirty": <bool>, "version": "0.2.0", "installed_at": "<iso>"}`.
- **`loop init` (and `loop update`) seeds `.loop/config.json#simple_loop`** as `{"commit": "<short-sha>", "installed_at": "<iso>", "source_repo": "<install-time-source-repo-path>"}`. Read from `$INSTALL_DIR/PROVENANCE.json` if present; fallback to `"unknown"`.
- **New `cmd_info()` in `bin/loop`** producing the output shape from the cold-start doc:
  ```
  simple-loop:
    source:    <path>  (commit <sha>, <clean|dirty>)
    install:   <path>  (version <X.Y.Z>, commit <sha>)
    binary:    <path>
    agents:    ~/.claude/agents/loop-*.md  (<N>)
    skills:    ~/.claude/skills/loop-*/    (<N>)

  project (<name>):
    .loop root:  <abs-path>
    config:      version <X>, last-installed <sha>
    briefs:      <total> (<N> active, <N> awaiting_review, <N> queued, <N> draft)
    drift:       <line> | none
  ```
- **Brief counts** computed from `wiki/briefs/cards/*/index.md` frontmatter `Status:` (use `lib/queue.py`'s enumerator if it exposes a public function; otherwise mirror its grep pattern).
- **Drift line** runs `python3 lib/lint.py --summary` (or whatever lint.py's quiet-mode flag is; if not present, add a `--summary` flag in this brief's scope) and reports the first failing brief or `none`.
- **Wire `info` into the dispatch table** at `bin/loop:1918` and `loop help`.

### Out

- **No diffing against upstream `git log`.** The doc proposes "23 ahead of origin"; that requires a `git fetch` against the source repo, which we won't do at every `loop info`. Show only the recorded SHA. Diff-against-upstream is a `loop update` enhancement, out of scope here.
- **No auto-fix of drift.** `loop info` reports; `loop lint --fix` is a separate concern.
- **No remote-call telemetry / posture check.** Local-only.
- **No `.loop/README.md` write at init time.** Cold-start doc proposes that too, but it's a separate concern; this brief stays scoped to `loop info` + the provenance plumbing it needs.

### Residue

- `loop update` could print a since-last-update changelog by walking the source-repo `git log` from the recorded SHA forward. Out of scope; would be a follow-up brief.
- `.loop/README.md` write at init time. Follow-up.

## Cycle plan

- Cycle 1 (`loop-coder`, sonnet) — implement `install.sh` provenance write + `loop init`/`loop update` config seeding + `cmd_info` skeleton with the section blocks. Validator runs after.
- Cycle 2 (`loop-coder`, sonnet) — flesh out brief counts + drift line; add `lint.py --summary` if missing; update `loop help`; smoke against simple-loop's own `.loop/`.

## Verification

```bash
# After install:
cat ~/.local/share/simple-loop/PROVENANCE.json | jq '.source_commit, .version, .source_dirty'
# expect: "<short-sha>", "0.2.0", false (or true if uncommitted)

cat .loop/config.json | jq '.simple_loop'
# expect: { "commit": "<sha>", "installed_at": "<iso>", "source_repo": "<path>" }

loop info
# expect output matching the cold-start doc shape; non-empty for every line.
# Specifically:
#   - source: line shows the simple-loop checkout, with clean|dirty
#   - install: line shows ~/.local/share/simple-loop, version, commit
#   - briefs: line shows the actual counts (cross-check against `ls wiki/briefs/cards`)
#   - drift: line is `none` if all briefs lint clean, or names the first failing brief

loop help | grep info
# expect: info subcommand listed
```

## Escalation triggers

- **`install.sh` runs in a non-git directory** (someone unzipped a tarball) — `git rev-parse` returns non-zero; fall back to `source_commit=unknown`, `source_dirty=null` and continue. Don't fail install.
- **`lib/lint.py` doesn't have a `--summary` mode** and adding it pulls in scope creep beyond a 1-cycle change — escalate; ship `cmd_info` without the drift line in cycle 1, file a follow-up for `--summary`. Don't widen this brief.
- **Brief counts don't match `wiki/briefs/index.md`** (the rendered index) — the index is generated by `gen-briefs-index.py`; if there's a mismatch, both could be wrong. Render the count from frontmatter (source of truth) and surface the disagreement as a TROUBLESHOOTING.md entry, not as a brief blocker.

## Anti-patterns

- Don't add a `loop info --json` mode in this brief — the spec is text. JSON is a follow-up if needed.
- Don't make `loop info` write any state — read-only.
- Don't auto-execute long-running terminal commands in cycles.

## Artifact

- Patch to `install.sh` (PROVENANCE.json write).
- Patch to `bin/loop` (`cmd_info`, init/update seed, dispatch wiring).
- Possible patch to `lib/lint.py` (`--summary` flag).
- `closeout.md` + `review.md` per contracts.

## What this unlocks

Cold-start agents (and Mattie at the hackathon) get a one-screen answer to "what's the state of this loop?" Closes the same archaeology pass that the cold-start friction doc documented in detail. Sets up future polish (`loop update`'s changelog, `.loop/README.md` breadcrumbs) without requiring them.
