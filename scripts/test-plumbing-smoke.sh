#!/usr/bin/env bash
# scripts/test-plumbing-smoke.sh — Smoke test for lib/git_plumbing.py
#
# Reproduces the bug condition (working tree checked out on a feature branch)
# and asserts that commit_files_to_branch writes to main without touching the
# working tree or the feature branch.
#
# Assertions (5):
#   1. main ref advanced after commit
#   2. feature branch HEAD unchanged
#   3. working tree HEAD unchanged (still on feature branch)
#   4. working tree clean (no dirty state)
#   5. Idempotency: second call with same content returns did_commit=False,
#      ref does not advance
#
# Exits 0 iff all assertions pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"
PLUMBING="$LIB_DIR/git_plumbing.py"

if [ ! -f "$PLUMBING" ]; then
    echo "FAIL: $PLUMBING not found — run from simple-loop repo" >&2
    exit 1
fi

PASSED=0
FAILED=0

pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

assert_eq() {
    local label="$1" actual="$2" expected="$3"
    if [ "$actual" = "$expected" ]; then
        pass "$label"
    else
        fail "$label — expected '$expected', got '$actual'"
    fi
}

# ── Scratch repo setup ───────────────────────────────────────────────────────

SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

FIXTURE="$SCRATCH/fixture.txt"

git -C "$SCRATCH" init -q -b main
git -C "$SCRATCH" config user.email "test@test"
git -C "$SCRATCH" config user.name  "Test"

# Seed main with an initial commit so there is a parent
echo "initial" > "$SCRATCH/README.md"
git -C "$SCRATCH" add README.md
git -C "$SCRATCH" commit -q -m "init"

MAIN_SHA_BEFORE=$(git -C "$SCRATCH" rev-parse refs/heads/main)

# Create feature branch and check it out (working tree drift)
git -C "$SCRATCH" checkout -q -b feature
FEATURE_SHA_BEFORE=$(git -C "$SCRATCH" rev-parse refs/heads/feature)

# Write fixture file to disk (not staged — simulates daemon writing state files)
echo "smoke content v1" > "$FIXTURE"

echo ""
echo "=== Plumbing smoke: commit_files_to_branch with drifted working tree ==="

# ── Call commit_files_to_branch via Python ───────────────────────────────────

RESULT=$(python3 - <<PYEOF
import sys
sys.path.insert(0, "$LIB_DIR")
from git_plumbing import commit_files_to_branch

sha, did_commit = commit_files_to_branch(
    "$SCRATCH",
    [("$FIXTURE", "fixture.txt")],
    "main",
    "loop: smoke test write",
)
print(f"{sha} {did_commit}")
PYEOF
)

if [ $? -ne 0 ]; then
    fail "commit_files_to_branch raised an exception"
    echo "--- Python output ---"
    echo "$RESULT"
    echo ""
    echo "Results: 0 passed, 1 failed"
    exit 1
fi

COMMIT_SHA=$(echo "$RESULT" | awk '{print $1}')
DID_COMMIT=$(echo "$RESULT" | awk '{print $2}')

# ── Assertions ───────────────────────────────────────────────────────────────

MAIN_SHA_AFTER=$(git -C "$SCRATCH" rev-parse refs/heads/main)
FEATURE_SHA_AFTER=$(git -C "$SCRATCH" rev-parse refs/heads/feature)
WORKTREE_HEAD=$(git -C "$SCRATCH" rev-parse HEAD)
WORKTREE_BRANCH=$(git -C "$SCRATCH" rev-parse --abbrev-ref HEAD)
WORKTREE_STATUS=$(git -C "$SCRATCH" status --short --untracked-files=no)

assert_eq "main ref advanced"                  "$MAIN_SHA_AFTER"    "$COMMIT_SHA"
assert_eq "feature branch HEAD unchanged"      "$FEATURE_SHA_AFTER" "$FEATURE_SHA_BEFORE"
assert_eq "working tree HEAD unchanged (feature)" "$WORKTREE_BRANCH"   "feature"
assert_eq "working tree: no tracked file modifications" "$WORKTREE_STATUS"   ""
assert_eq "did_commit=True on first write"     "$DID_COMMIT"        "True"

# ── Idempotency check ────────────────────────────────────────────────────────

echo ""
echo "=== Plumbing smoke: idempotency (same content → no new commit) ==="

RESULT2=$(python3 - <<PYEOF
import sys
sys.path.insert(0, "$LIB_DIR")
from git_plumbing import commit_files_to_branch

sha, did_commit = commit_files_to_branch(
    "$SCRATCH",
    [("$FIXTURE", "fixture.txt")],
    "main",
    "loop: smoke test write (idempotent)",
)
print(f"{sha} {did_commit}")
PYEOF
)

if [ $? -ne 0 ]; then
    fail "idempotent call raised an exception"
    echo "--- Python output ---"
    echo "$RESULT2"
    echo ""
    echo "Results: $PASSED passed, $((FAILED + 1)) failed"
    exit 1
fi

COMMIT_SHA2=$(echo "$RESULT2" | awk '{print $1}')
DID_COMMIT2=$(echo "$RESULT2" | awk '{print $2}')
MAIN_SHA_IDEMPOTENT=$(git -C "$SCRATCH" rev-parse refs/heads/main)

assert_eq "idempotent: did_commit=False"       "$DID_COMMIT2"          "False"
assert_eq "idempotent: ref did not advance"    "$MAIN_SHA_IDEMPOTENT"  "$MAIN_SHA_AFTER"
assert_eq "idempotent: returned parent sha"    "$COMMIT_SHA2"          "$COMMIT_SHA"

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASSED passed, $FAILED failed"

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
