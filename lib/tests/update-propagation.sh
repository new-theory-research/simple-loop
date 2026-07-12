#!/usr/bin/env bash
# Regression test for `loop update` harness propagation (issues #20, #57).
#
# Covers the bin/loop-level guards and the prompt-refresh CLI through real git:
#   AC1 — missing PROVENANCE.json => loud fail, project prompts untouched.
#   AC2 — PROVENANCE present but source_repo unusable => loud fail.
#   AC3 — three-way refresh via git baseline: identical (no-op), template-newer
#         (updated), locally-customized (preserved + instruction printed).
#
# The three-way classification itself is unit-tested in test_update_prompts.py;
# this exercises the shell entrypoint + git-show baseline resolution end-to-end.

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOOP_BIN="$REPO_ROOT/bin/loop"
UPDATE_PROMPTS="$REPO_ROOT/lib/update_prompts.py"

# ── Fixture: a minimal loop-enabled project ──────────────────────────────────
make_project() {
    local root="$1"
    mkdir -p "$root/.loop/state" "$root/.loop/prompts"
    cat > "$root/.loop/config.json" <<'JSON'
{"project_name": "fixture", "modules": [], "simple_loop": {"commit": "abc1234"}}
JSON
    : > "$root/.loop/config.sh"
    printf 'PROJECT QUEEN\n' > "$root/.loop/prompts/queen.md"
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC1: missing PROVENANCE.json => loud fail, prompts untouched     ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC1: missing PROVENANCE => loud fail ────────────────────────"

P1="$TMP/proj1"
make_project "$P1"
EMPTY_HOME="$TMP/empty-home"   # no PROVENANCE.json inside
mkdir -p "$EMPTY_HOME"

OUT1="$( cd "$P1" && SIMPLE_LOOP_HOME="$EMPTY_HOME" bash "$LOOP_BIN" update 2>&1 )"
RC1=$?

if [ "$RC1" -ne 0 ]; then
    pass "[AC1] update exits non-zero when PROVENANCE.json is absent"
else
    fail "[AC1] update exited 0 despite missing PROVENANCE.json"
fi
if echo "$OUT1" | grep -qi "PROVENANCE"; then
    pass "[AC1] error message names PROVENANCE.json"
else
    fail "[AC1] error message did not mention PROVENANCE (got: $OUT1)"
fi
if echo "$OUT1" | grep -qi "install.sh"; then
    pass "[AC1] error message gives install.sh guidance"
else
    fail "[AC1] error message lacked install.sh guidance"
fi
if [ "$(cat "$P1/.loop/prompts/queen.md")" = "PROJECT QUEEN" ]; then
    pass "[AC1] project prompt untouched on loud fail"
else
    fail "[AC1] project prompt was modified despite loud fail"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC2: PROVENANCE present, source_repo unusable => loud fail       ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC2: unusable source_repo => loud fail ──────────────────────"

P2="$TMP/proj2"
make_project "$P2"
HOME2="$TMP/home2"
mkdir -p "$HOME2"
cat > "$HOME2/PROVENANCE.json" <<JSON
{"source_repo": "$TMP/does-not-exist", "source_commit": "deadbee", "source_dirty": false}
JSON

OUT2="$( cd "$P2" && SIMPLE_LOOP_HOME="$HOME2" bash "$LOOP_BIN" update 2>&1 )"
RC2=$?

if [ "$RC2" -ne 0 ]; then
    pass "[AC2] update exits non-zero when source_repo does not exist"
else
    fail "[AC2] update exited 0 despite unusable source_repo"
fi
if echo "$OUT2" | grep -qi "source_repo"; then
    pass "[AC2] error message names source_repo"
else
    fail "[AC2] error message did not mention source_repo (got: $OUT2)"
fi
if [ "$(cat "$P2/.loop/prompts/queen.md")" = "PROJECT QUEEN" ]; then
    pass "[AC2] project prompt untouched on loud fail"
else
    fail "[AC2] project prompt was modified despite loud fail"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC3: three-way refresh via git baseline                          ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC3: three-way refresh via git baseline ─────────────────────"

SRC="$TMP/src"
mkdir -p "$SRC/templates/prompts"
( cd "$SRC" && git init -q && git config user.email t@t && git config user.name t
  printf 'QUEEN BASE\n'  > templates/prompts/queen.md
  printf 'WORKER BASE\n' > templates/prompts/worker.md
  printf 'GUIDE BASE\n'  > templates/prompts/guide.md
  git add -A && git commit -q -m base )
BASE="$( cd "$SRC" && git rev-parse --short HEAD )"

# "Installed" (new) templates: queen changed, worker unchanged, guide unchanged.
NEWT="$TMP/installed/prompts"
mkdir -p "$NEWT"
printf 'QUEEN NEW\n'   > "$NEWT/queen.md"
printf 'WORKER BASE\n' > "$NEWT/worker.md"
printf 'GUIDE BASE\n'  > "$NEWT/guide.md"

# Project copies: queen unmodified-from-base (=> updated),
#                 worker customized (=> preserved),
#                 guide identical to new (=> in sync, untouched).
PP="$TMP/proj3/prompts"
mkdir -p "$PP"
printf 'QUEEN BASE\n'          > "$PP/queen.md"
printf 'WORKER MY EDIT\n'      > "$PP/worker.md"
printf 'GUIDE BASE\n'          > "$PP/guide.md"

OUT3="$( python3 "$UPDATE_PROMPTS" \
    --templates-dir "$NEWT" --prompts-dir "$PP" \
    --source-repo "$SRC" --base-sha "$BASE" 2>&1 )"

# queen: unmodified from baseline, template moved => overwritten with new
if [ "$(cat "$PP/queen.md")" = "QUEEN NEW" ]; then
    pass "[AC3] template-newer + unmodified project => updated to new template"
else
    fail "[AC3] queen.md was not updated (got: $(cat "$PP/queen.md"))"
fi
if echo "$OUT3" | grep -q "queen.md" && echo "$OUT3" | grep -qi "updated"; then
    pass "[AC3] report marks queen.md updated"
else
    fail "[AC3] report did not mark queen.md updated"
fi

# worker: customized => preserved + three-way instruction printed
if [ "$(cat "$PP/worker.md")" = "WORKER MY EDIT" ]; then
    pass "[AC3] locally-customized worker.md preserved (not clobbered)"
else
    fail "[AC3] worker.md was clobbered (got: $(cat "$PP/worker.md"))"
fi
if echo "$OUT3" | grep -qi "CUSTOMIZED" && echo "$OUT3" | grep -q "worker.md"; then
    pass "[AC3] report flags worker.md CUSTOMIZED"
else
    fail "[AC3] report did not flag worker.md customized"
fi
if echo "$OUT3" | grep -q "diff .*worker.md"; then
    pass "[AC3] three-way-sync instruction printed for worker.md"
else
    fail "[AC3] no three-way-sync instruction for worker.md"
fi

# guide: identical to new template => in sync, unchanged, still reported
if [ "$(cat "$PP/guide.md")" = "GUIDE BASE" ]; then
    pass "[AC3] identical guide.md left untouched"
else
    fail "[AC3] identical guide.md was modified"
fi
if echo "$OUT3" | grep -q "guide.md"; then
    pass "[AC3] in-sync guide.md still reported by name (never silently skipped)"
else
    fail "[AC3] guide.md was silently skipped"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC4: propagation bookkeeping commits tracked re-seed (issue #78) ║
# ╚══════════════════════════════════════════════════════════════════╝
# `loop update` re-seeds simple_loop.commit into the TRACKED .loop/config.json
# and refreshes the TRACKED .loop/prompts/*.md — leaving the clone dirty and
# feeding stranded-commit push churn. _commit_propagation_bookkeeping folds both
# into ONE in-place commit (no push). Exercised directly by sourcing bin/loop.
echo ""
echo "── AC4: propagation bookkeeping (issue #78) ────────────────────"

# Source the CLI (help path is a no-op) so the function is callable in isolation.
run_bookkeeping() {
    local proj="$1"
    (
        source "$LOOP_BIN" help >/dev/null 2>&1 || true
        PROJECT_DIR="$proj"
        LOOP_DIR="$proj/.loop"
        _commit_propagation_bookkeeping
    )
}

# Fixture: a git project whose .loop/config.json + one prompt are TRACKED.
make_git_project() {
    local root="$1"
    mkdir -p "$root/.loop/state" "$root/.loop/prompts"
    cat > "$root/.loop/config.json" <<'JSON'
{"project_name": "fixture", "modules": [], "simple_loop": {"commit": "abc1234"}}
JSON
    printf 'QUEEN COMMITTED\n' > "$root/.loop/prompts/queen.md"
    ( cd "$root" && git init -q && git config user.email t@t && git config user.name t \
      && git add -A && git commit -q -m init )
}
commit_count() { git -C "$1" rev-list --count HEAD 2>/dev/null; }

# ── AC4a: tracked + modified => exactly one commit, both files staged ──
P4="$TMP/proj4"
make_git_project "$P4"
BEFORE4="$(commit_count "$P4")"
# Simulate an update: re-seed rewrote config.json, refresh rewrote a prompt.
cat > "$P4/.loop/config.json" <<'JSON'
{"project_name": "fixture", "modules": [], "simple_loop": {"commit": "def5678"}}
JSON
printf 'QUEEN REFRESHED\n' > "$P4/.loop/prompts/queen.md"

OUT4="$(run_bookkeeping "$P4")"
AFTER4="$(commit_count "$P4")"

if [ "$((AFTER4 - BEFORE4))" -eq 1 ]; then
    pass "[AC4a] exactly one commit created"
else
    fail "[AC4a] expected +1 commit, got before=$BEFORE4 after=$AFTER4"
fi
COMMITTED4="$(git -C "$P4" show --name-only --format= HEAD 2>/dev/null)"
if echo "$COMMITTED4" | grep -q ".loop/config.json" && echo "$COMMITTED4" | grep -q ".loop/prompts/queen.md"; then
    pass "[AC4a] commit stages BOTH baseline (config.json) and refreshed prompt"
else
    fail "[AC4a] commit missing baseline or prompt (got: $COMMITTED4)"
fi
if git -C "$P4" show -s --format=%s HEAD | grep -q "update propagation (baseline + prompts)"; then
    pass "[AC4a] commit message names baseline + prompts propagation"
else
    fail "[AC4a] unexpected commit subject: $(git -C "$P4" show -s --format=%s HEAD)"
fi
if echo "$OUT4" | grep -qi "Committed propagation bookkeeping"; then
    pass "[AC4a] update output documents the bookkeeping commit"
else
    fail "[AC4a] output did not document bookkeeping (got: $OUT4)"
fi
if git -C "$P4" diff --quiet HEAD -- .loop; then
    pass "[AC4a] working tree clean after bookkeeping (no stranded dirt)"
else
    fail "[AC4a] .loop still dirty after bookkeeping commit"
fi

# ── AC4b: untracked project => no commit attempted, no error ──
P5="$TMP/proj5"
mkdir -p "$P5/.loop/prompts"
( cd "$P5" && git init -q && git config user.email t@t && git config user.name t \
  && printf 'readme\n' > README.md && git add README.md && git commit -q -m init )
# .loop/* written but NEVER tracked
cat > "$P5/.loop/config.json" <<'JSON'
{"project_name": "fixture", "simple_loop": {"commit": "def5678"}}
JSON
printf 'QUEEN UNTRACKED\n' > "$P5/.loop/prompts/queen.md"
BEFORE5="$(commit_count "$P5")"

OUT5="$(run_bookkeeping "$P5")"; RC5=$?
AFTER5="$(commit_count "$P5")"

if [ "$RC5" -eq 0 ]; then
    pass "[AC4b] untracked project => function returns 0 (no error)"
else
    fail "[AC4b] function errored on untracked project (rc=$RC5)"
fi
if [ "$AFTER5" = "$BEFORE5" ]; then
    pass "[AC4b] untracked project => no commit attempted"
else
    fail "[AC4b] a commit was created despite untracked .loop (before=$BEFORE5 after=$AFTER5)"
fi
if [ -z "$OUT5" ]; then
    pass "[AC4b] untracked project => no bookkeeping output"
else
    fail "[AC4b] unexpected output on untracked project: $OUT5"
fi

# ── AC4c: non-git project => no error, no commit ──
P6="$TMP/proj6"
mkdir -p "$P6/.loop/prompts"
printf '{}\n' > "$P6/.loop/config.json"
OUT6="$(run_bookkeeping "$P6")"; RC6=$?
if [ "$RC6" -eq 0 ] && [ -z "$OUT6" ]; then
    pass "[AC4c] non-git project => silent no-op, no error"
else
    fail "[AC4c] non-git project misbehaved (rc=$RC6 out=$OUT6)"
fi

# ── AC4d: commit failure => loud warning, update continues ──
P7="$TMP/proj7"
make_git_project "$P7"
BEFORE7="$(commit_count "$P7")"
cat > "$P7/.loop/config.json" <<'JSON'
{"project_name": "fixture", "simple_loop": {"commit": "fail999"}}
JSON
printf 'QUEEN REFRESHED\n' > "$P7/.loop/prompts/queen.md"
# Force the commit to fail deterministically (stands in for mid-merge/detached).
mkdir -p "$P7/.git/hooks"
printf '#!/bin/sh\nexit 1\n' > "$P7/.git/hooks/pre-commit"
chmod +x "$P7/.git/hooks/pre-commit"

OUT7="$(run_bookkeeping "$P7")"; RC7=$?
AFTER7="$(commit_count "$P7")"

if [ "$RC7" -eq 0 ]; then
    pass "[AC4d] commit failure => function still returns 0 (update not aborted)"
else
    fail "[AC4d] function aborted on commit failure (rc=$RC7)"
fi
if echo "$OUT7" | grep -qi "Could not commit propagation bookkeeping"; then
    pass "[AC4d] commit failure => loud one-line warning naming the files"
else
    fail "[AC4d] no warning on commit failure (got: $OUT7)"
fi
if [ "$AFTER7" = "$BEFORE7" ]; then
    pass "[AC4d] commit failure => no bookkeeping commit landed"
else
    fail "[AC4d] a commit landed despite forced failure (before=$BEFORE7 after=$AFTER7)"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Summary                                                        ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "update-propagation: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
