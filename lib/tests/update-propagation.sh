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
# ║  Summary                                                        ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "update-propagation: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
