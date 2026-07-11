# Closeout — brief-162: harness-update propagation

Merge SHA: (filled at merge)

## TL;DR

`loop update` was a partial no-op — it re-synced installed skills/modules but
never pulled the source, never ran install.sh (so `lib/` and `daemon.sh`
stayed stale), never refreshed project `.loop/prompts/`, and never flagged a
running daemon. The receipts: hand-propagating queen.md to two clones twice in
one evening (2026-07-11 Wave-1b), and the 2026-04-29 queen.md rename silently
breaking a project daemon. This brief makes `loop update` the one invokable
propagation edge: pull → reinstall → changelog → diff-aware prompt refresh →
restart notice. All existing behaviors preserved. 240 pytest + 14 shell
assertions green.

## What shipped, step by step

`loop update` now runs (bin/loop `cmd_update`):

1. **Locate the harness source** from `PROVENANCE.json`'s `source_repo`
   (written by install.sh). Fails loud, non-zero, with re-run-install.sh
   guidance if PROVENANCE is missing or `source_repo` is empty/`unknown`/not a
   directory. Captures the project's baseline harness commit from
   `config.json`'s `simple_loop.commit` *before* anything mutates it.
2. **`git pull --ff-only`** the source. A non-fast-forward prints a loud ⚠
   warning (installing from source as-is; upstream commits may be missing) and
   continues.
3. **Run install.sh** — inheriting its dirty-tree refusal and PROVENANCE
   rewrite. On failure: loud abort, nothing propagated, commit/stash/`--force`
   hint. On success: prints `Installed: <old> → <new>` SHA.
4. **Re-seed** the project's recorded harness commit to the new install.
5. **Since-baseline changelog** — `git log --oneline <baseline>..HEAD` over
   `templates/ core/ lib/`, so the operator sees what migration work applies.
   Skips gracefully (with a note) when the baseline is unknown.
6. **Diff-aware prompt refresh** (`lib/update_prompts.py`, new) — three-way
   classification per `.loop/prompts/*.md`, baseline resolved via
   `git show <baseline>:templates/prompts/<file>`:
   - identical to new template → **in sync**, untouched
   - matches the baseline, template moved → **updated** (safe overwrite)
   - differs from both baselines → **CUSTOMIZED — preserved**, with a
     three-way-sync `diff`/`cp` instruction naming the file
   - no baseline available → **DRIFT — preserved**, with a compare warning
   - project copy missing → **created** (daemon-required, never silently
     skipped)
   Every template file is reported by name; nothing is silent.
7. **Skills/modules re-sync** — the pre-existing behavior, unchanged.
8. **Daemon restart notice** — if a daemon is running for THIS project
   (pid-file alive), prints `loop stop && loop start` and *why* (`daemon.sh`
   is read only at startup; prompts and `lib/*.py` hot-reload per tick). Does
   **not** auto-restart — operator action.

Also shipped:

- `lib/tests/test_update_prompts.py` — 13 unit tests over
  `classify()`/`refresh()`.
- `lib/tests/update-propagation.sh` — 14 assertions over the bin/loop guards
  and the git-baseline refresh end-to-end.
- `docs/operating/harness-updates.md` — now leads with `loop update` as the
  propagation edge; the manual five-command path demoted to
  diverge-from-happy-path fallback.
- `loop help` text updated to describe what update actually does.

Guards honored: `lib/daemon.sh` untouched; no auto-restart; safe to run while
a daemon is live.

## Per-issue confirmation

### #20 — `loop update` never refreshes project prompt copies

Closed by step 6. The issue's fix direction asked for exactly the three-way
shape: byte-identical → in sync; unmodified-since-sync → safe overwrite;
differs-from-both → drift warning, never overwrite. Implemented with the
baseline resolved from the project's recorded harness commit (via `git show`)
rather than a separate `.template-shas` manifest — same information, one
fewer state file, and it works retroactively for any project whose
`config.json` carries `simple_loop.commit`. Legacy projects with no usable
baseline get the warn-only DRIFT behavior the issue specified.

### #57 — no invokable, discoverable way to absorb harness updates

The six-point ask, point by point:

| # | Ask | Status |
|---|-----|--------|
| 1 | Locate harness source + installed copy | ✅ step 1 (PROVENANCE.json `source_repo`; loud fail with guidance) |
| 2 | Pull source, run install.sh, report old→new SHA | ✅ steps 2–3 |
| 3 | Diff-aware refresh of `.loop/prompts/`, preserve customizations, never silently skip | ✅ step 6 |
| 4 | Since-last-update changelog (templates/ core/ lib/) | ✅ step 5 |
| 5 | Restart the daemon if running; surface running-vs-installed | ⚠ deliberately inverted per the brief: **no auto-restart** — loud detect-and-instruct instead (step 8). The running-vs-installed gap is exactly what the printed instruction closes. |
| 6 | Run `loop lint` on project briefs, flag spec drift | ❌ **deliberately scoped out** — see below |

Discoverability: `loop update` is the existing, already-documented command
(honest now rather than a new surface); `loop help` and
docs/operating/harness-updates.md both point at it. The skill-form variant
(`loop-absorb-harness`) was not built — the brief chose "make `loop update`
honest."

## Deliberate scope cut — #57 item 6 (lint integration)

`loop lint` integration on update was **not implemented**. It is orthogonal to
the propagation mechanism (it validates project briefs, not harness files),
`loop lint` already exists as its own invokable command, and bolting it on
would couple update's exit status to brief-format drift unrelated to the
update itself. Recorded here as an explicit follow-up candidate: a
`loop update --lint` flag or a printed suggestion line would be a small,
separable brief.

## Verification

- `python3 -m pytest lib/tests/ -q` — **240 passed** (baseline 227 + 13 new).
- `bash lib/tests/update-propagation.sh` — **14/14 PASS**.
- Live end-to-end drive of `lib/update_prompts.py` against a real git
  baseline during development: updated/preserved/created behaviors observed
  on disk, not just asserted.
- Reviewer (gate run, 2026-07-11) re-ran all tests and drove
  `classify()`/`refresh()` through adversarial cases — technical checks
  passed.

## Commits (branch `brief-162-harness-update-propagation`)

- `6960f81` — three-way prompt-refresh helper + 13 unit tests
- `31f51a4` — `cmd_update` full propagation path + shell test
- `e119f7f` — harness-updates.md leads with `loop update`
- (this commit) — review.md + closeout.md gate artifacts; louder non-ff warning
