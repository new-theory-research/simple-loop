#!/usr/bin/env bash
# Regression test for issue #55: rebase-blocked false park on
# progress.json-only conflicts.
#
# ft-011 (2026-07-10) and capture-005-cont (2026-07-11) both false-parked
# because the worker's cycle-start rebase conflicted ONLY on
# .loop/state/progress.json — transient loop bookkeeping, rewritten on every
# dispatch, never load-bearing branch content. Recovery each time was a
# human hand-running the same recipe.
#
# Block under test mirrors lib/daemon.sh's run_worker_iteration() rebase
# handling: if the conflict set is exactly {.loop/state/progress.json},
# auto-resolve in main's favor and continue; any other conflict still parks.
#
# Exits 0 iff all assertions hold.

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Rebase-conflict handling mirrored from lib/daemon.sh::run_worker_iteration.
# Returns 0 if the rebase landed (cleanly or via progress.json auto-resolve),
# 1 if it was parked (rebase aborted, would route to awaiting_review).
attempt_dispatch_rebase() {
    local WORKTREE_DIR="$1"
    local UPSTREAM_REF="$2"
    if git -C "$WORKTREE_DIR" rebase "$UPSTREAM_REF" -q 2>/dev/null; then
        return 0
    fi
    local CONFLICT_FILES
    local REBASE_RECOVERED=0
    CONFLICT_FILES=$(git -C "$WORKTREE_DIR" diff --name-only --diff-filter=U 2>/dev/null)
    if [ "$CONFLICT_FILES" = ".loop/state/progress.json" ]; then
        git -C "$WORKTREE_DIR" checkout --ours -- .loop/state/progress.json 2>/dev/null
        git -C "$WORKTREE_DIR" add .loop/state/progress.json 2>/dev/null
        if git -C "$WORKTREE_DIR" -c user.email=t@t -c user.name=t rebase --continue >/dev/null 2>&1; then
            REBASE_RECOVERED=1
        fi
    fi
    if [ "$REBASE_RECOVERED" != "1" ]; then
        git -C "$WORKTREE_DIR" rebase --abort 2>/dev/null || true
        return 1
    fi
    return 0
}

setup_repo() {
    local repo="$1" brief="$2"
    git init -q -b main "$repo/upstream"
    git -C "$repo/upstream" config user.email t@t
    git -C "$repo/upstream" config user.name t
    mkdir -p "$repo/upstream/.loop/state"
    echo "{\"brief\":\"$brief\",\"iteration\":0,\"status\":\"running\"}" \
        > "$repo/upstream/.loop/state/progress.json"
    git -C "$repo/upstream" add . >/dev/null
    git -C "$repo/upstream" commit -q -m "init"
    git clone -q "$repo/upstream" "$repo/wt"
    git -C "$repo/wt" config user.email t@t
    git -C "$repo/wt" config user.name t
}

# ── Case 1: progress.json-only conflict → auto-resolved, rebase proceeds ─────
C1="$TMP/c1"; mkdir -p "$C1"
setup_repo "$C1" "brief-100-foo"
# Worker branch advances progress.json (its own cycle bookkeeping)...
echo '{"brief":"brief-100-foo","iteration":1,"tasks_completed":["t1"]}' \
    > "$C1/wt/.loop/state/progress.json"
git -C "$C1/wt" commit -q -am "cycle 1 progress"
# ...while main independently advances the SAME file (last-merged brief's
# reset commit) — the exact ft-011 / capture-005-cont shape.
echo '{"brief":"brief-099-bar","iteration":3,"status":"complete"}' \
    > "$C1/upstream/.loop/state/progress.json"
git -C "$C1/upstream" commit -q -am "main advances progress.json"
git -C "$C1/wt" fetch origin -q
if attempt_dispatch_rebase "$C1/wt" "origin/main"; then
    pass "progress.json-only conflict → rebase proceeds instead of parking"
else
    fail "progress.json-only conflict → still parked"
fi
RESULT_BRIEF=$(python3 -c "import json; print(json.load(open('$C1/wt/.loop/state/progress.json')).get('brief',''))" 2>/dev/null)
if [ "$RESULT_BRIEF" = "brief-099-bar" ]; then
    pass "auto-resolve took main's version of progress.json (not the worker's)"
else
    fail "expected main's progress.json (brief-099-bar), got '$RESULT_BRIEF'"
fi

# ── Case 2: real content conflict on a different file → still parks ─────────
C2="$TMP/c2"; mkdir -p "$C2"
setup_repo "$C2" "brief-101-baz"
echo "worker's line" > "$C2/wt/shared.txt"
git -C "$C2/wt" add shared.txt
git -C "$C2/wt" commit -q -m "worker edits shared.txt"
echo "main's line" > "$C2/upstream/shared.txt"
git -C "$C2/upstream" add shared.txt
git -C "$C2/upstream" commit -q -m "main edits shared.txt"
git -C "$C2/wt" fetch origin -q
if attempt_dispatch_rebase "$C2/wt" "origin/main"; then
    fail "real content conflict → unexpectedly proceeded (should park)"
else
    pass "real content conflict on non-progress.json file → still parks"
fi
if git -C "$C2/wt" status --porcelain=1 2>/dev/null | grep -q '^UU\|rebase' ; then
    fail "rebase --abort left the worktree mid-rebase"
fi

# ── Case 3: progress.json conflict PLUS a real conflict → still parks ───────
# Guards against a loose match (e.g. substring check) treating a mixed
# conflict set as progress.json-only.
C3="$TMP/c3"; mkdir -p "$C3"
setup_repo "$C3" "brief-102-qux"
echo "worker's line" > "$C3/wt/shared.txt"
git -C "$C3/wt" add shared.txt
echo '{"brief":"brief-102-qux","iteration":1}' > "$C3/wt/.loop/state/progress.json"
git -C "$C3/wt" commit -q -am "worker edits shared.txt and progress.json"
echo "main's line" > "$C3/upstream/shared.txt"
git -C "$C3/upstream" add shared.txt
echo '{"brief":"brief-098-quux","iteration":5}' > "$C3/upstream/.loop/state/progress.json"
git -C "$C3/upstream" commit -q -am "main edits shared.txt and progress.json"
git -C "$C3/wt" fetch origin -q
if attempt_dispatch_rebase "$C3/wt" "origin/main"; then
    fail "mixed conflict set (progress.json + real file) → unexpectedly proceeded"
else
    pass "mixed conflict set (progress.json + real file) → still parks"
fi

echo ""
echo "Passed: $PASSED   Failed: $FAILED"
[ "$FAILED" -eq 0 ]
