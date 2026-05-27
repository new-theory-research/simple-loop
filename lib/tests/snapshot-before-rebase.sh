#!/usr/bin/env bash
# Regression test for issue #5: worker rebase fails on dirty progress.json.
#
# Reproduces the bug class in isolation (no daemon, no Python — just the
# snapshot-then-rebase sequence that daemon.sh now performs at cycle start),
# then asserts:
#
#   1. Dirty progress.json + clean rebase target → rebase succeeds after snapshot
#   2. Clean working tree → snapshot block is a no-op (no extra commit created)
#   3. Snapshot preserves worker progress across the rebase
#
# Exits 0 iff all assertions hold.

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Snapshot logic mirrored from lib/daemon.sh::run_worker_iteration.
snapshot_progress_before_rebase() {
    local WORKTREE_DIR="$1"
    if [ -f "$WORKTREE_DIR/.loop/state/progress.json" ] && \
       ! git -C "$WORKTREE_DIR" diff --quiet HEAD -- .loop/state/progress.json 2>/dev/null; then
        git -C "$WORKTREE_DIR" add .loop/state/progress.json 2>/dev/null
        git -C "$WORKTREE_DIR" -c user.email=t@t -c user.name=t \
            commit -m "loop: snapshot progress.json before cycle-start rebase" -q 2>/dev/null
    fi
}

setup_repo() {
    local repo="$1" brief="$2"
    git init -q -b main "$repo/upstream"
    mkdir -p "$repo/upstream/.loop/state"
    echo "{\"brief\":\"$brief\",\"iteration\":0,\"status\":\"running\"}" \
        > "$repo/upstream/.loop/state/progress.json"
    git -C "$repo/upstream" add . >/dev/null
    git -C "$repo/upstream" -c user.email=t@t -c user.name=t commit -q -m "init"
    git clone -q "$repo/upstream" "$repo/wt"
}

# ── Case 1: dirty progress.json — rebase succeeds after snapshot ─────────────
C1="$TMP/c1"; mkdir -p "$C1"
setup_repo "$C1" "brief-100-foo"
git -C "$C1/wt" -c user.email=t@t -c user.name=t commit --allow-empty -q -m "cycle 1 work"
echo '{"brief":"brief-100-foo","iteration":1,"tasks_completed":["t1"]}' \
    > "$C1/wt/.loop/state/progress.json"
snapshot_progress_before_rebase "$C1/wt"
if git -C "$C1/wt" rebase origin/main -q 2>/dev/null; then
    pass "dirty progress.json → snapshot unblocks rebase"
else
    git -C "$C1/wt" rebase --abort 2>/dev/null || true
    fail "dirty progress.json → rebase still failed after snapshot"
fi

# Worker progress preserved across the rebase.
ITERATION=$(python3 -c "import json; print(json.load(open('$C1/wt/.loop/state/progress.json'))['iteration'])")
if [ "$ITERATION" = "1" ]; then
    pass "snapshot preserved worker iteration across rebase"
else
    fail "expected iteration=1, got '$ITERATION'"
fi

# ── Case 2: clean working tree — snapshot is a no-op ─────────────────────────
C2="$TMP/c2"; mkdir -p "$C2"
setup_repo "$C2" "brief-101-bar"
BEFORE=$(git -C "$C2/wt" rev-parse HEAD)
snapshot_progress_before_rebase "$C2/wt"
AFTER=$(git -C "$C2/wt" rev-parse HEAD)
if [ "$BEFORE" = "$AFTER" ]; then
    pass "clean working tree → snapshot block created no commit"
else
    fail "clean working tree → snapshot block unexpectedly created a commit"
fi

# ── Case 3: rebase against an advanced main also clean (no real conflict) ────
C3="$TMP/c3"; mkdir -p "$C3"
setup_repo "$C3" "brief-102-baz"
# Advance upstream main with an unrelated file
echo "hello" > "$C3/upstream/README.md"
git -C "$C3/upstream" add README.md >/dev/null
git -C "$C3/upstream" -c user.email=t@t -c user.name=t commit -q -m "advance main"
git -C "$C3/wt" fetch origin -q
echo '{"brief":"brief-102-baz","iteration":2,"tasks_completed":["a","b"]}' \
    > "$C3/wt/.loop/state/progress.json"
snapshot_progress_before_rebase "$C3/wt"
if git -C "$C3/wt" rebase origin/main -q 2>/dev/null; then
    pass "dirty progress.json + advanced main → snapshot+rebase succeeds"
else
    git -C "$C3/wt" rebase --abort 2>/dev/null || true
    fail "dirty progress.json + advanced main → rebase failed"
fi

echo ""
echo "Passed: $PASSED   Failed: $FAILED"
[ "$FAILED" -eq 0 ]
