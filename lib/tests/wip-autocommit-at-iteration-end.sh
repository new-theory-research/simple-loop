#!/usr/bin/env bash
# Regression test for the worker WIP auto-commit (issue #18):
#
# A worker iteration that ends with uncommitted changes (wall-time kill,
# error, or a worker that simply didn't commit) leaves the brief worktree
# dirty — and a dirty worktree fails the NEXT dispatch's cycle-start rebase
# unconditionally, routing the brief to awaiting_review for state the harness
# itself created. brief-250 (2026-06-11): one uncommitted file, zero real
# conflicts; the whole recovery round-trip was self-inflicted.
#
# Block under test is reproduced here so the asserted logic stays in lockstep
# with lib/daemon.sh's commit_worktree_wip().

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

daemon_log() { :; }  # stub — daemon.sh's logger, not under test

# Block under test — mirrors lib/daemon.sh's commit_worktree_wip().
commit_worktree_wip() {
    local worktree_dir="$1"
    local brief_id="$2"
    local label="${3:-at iteration end}"
    [ -d "$worktree_dir" ] || return 0
    if [ -n "$(git -C "$worktree_dir" status --porcelain 2>/dev/null)" ]; then
        git -C "$worktree_dir" add -A 2>/dev/null
        if git -C "$worktree_dir" commit -m "[loop] $brief_id WIP auto-commit $label" -q 2>/dev/null; then
            daemon_log "WORKER: WIP auto-commit for $brief_id ($label) — worktree was dirty"
        fi
    fi
    return 0
}

# Build a "main repo + brief worktree" fixture like the daemon's layout.
make_repo() {
    local root="$1"
    git init -q -b main "$root"
    git -C "$root" config user.email "test@loop"
    git -C "$root" config user.name "loop-test"
    echo "hello" > "$root/file.txt"
    git -C "$root" add -A
    git -C "$root" commit -q -m "initial"
}

# ── Case 1: dirty tracked file → WIP commit created, worktree clean ──────────
# The brief-250 shape: a worker modified a tracked file and never committed.
R="$TMP/c1"; make_repo "$R"
git -C "$R" worktree add -b brief-250-test "$R/.wt" main -q
echo "mid-flight change" >> "$R/.wt/file.txt"
commit_worktree_wip "$R/.wt" "brief-250-test"
if [ -z "$(git -C "$R/.wt" status --porcelain)" ]; then
    pass "dirty tracked file → worktree clean after auto-commit"
else
    fail "dirty tracked file → worktree still dirty (next rebase would block)"
fi
MSG=$(git -C "$R/.wt" log -1 --format=%s)
if [ "$MSG" = "[loop] brief-250-test WIP auto-commit at iteration end" ]; then
    pass "WIP commit carries the canonical label"
else
    fail "WIP commit label wrong: '$MSG'"
fi

# ── Case 2: untracked file → also committed ──────────────────────────────────
# `git diff` misses untracked files (the progress.json snapshot's blind spot);
# untracked dirt blocks `git rebase` just the same when paths collide.
R="$TMP/c2"; make_repo "$R"
git -C "$R" worktree add -b brief-251-test "$R/.wt" main -q
echo "new file" > "$R/.wt/new-component.tsx"
commit_worktree_wip "$R/.wt" "brief-251-test"
if [ -z "$(git -C "$R/.wt" status --porcelain)" ]; then
    pass "untracked file → committed too (git add -A)"
else
    fail "untracked file → left behind"
fi

# ── Case 3: clean worktree → no commit manufactured ──────────────────────────
R="$TMP/c3"; make_repo "$R"
git -C "$R" worktree add -b brief-252-test "$R/.wt" main -q
BEFORE=$(git -C "$R/.wt" rev-parse HEAD)
commit_worktree_wip "$R/.wt" "brief-252-test"
if [ "$(git -C "$R/.wt" rev-parse HEAD)" = "$BEFORE" ]; then
    pass "clean worktree → no spurious WIP commit"
else
    fail "clean worktree → manufactured an empty/spurious commit"
fi

# ── Case 4: missing worktree dir → no-op, returns 0 ──────────────────────────
# Exit paths can fire before the worktree exists; must never error the daemon.
if commit_worktree_wip "$TMP/does-not-exist" "brief-253-test"; then
    pass "missing worktree dir → silent no-op"
else
    fail "missing worktree dir → nonzero return (would trip set -e contexts)"
fi

# ── Case 5: the actual failure mode — dirty worktree blocks rebase; ──────────
# after auto-commit the same rebase succeeds. brief-250's shape: a MODIFIED
# tracked file (git refuses rebase on unstaged changes before any real
# conflict is evaluated), no content overlap with main's advance.
R="$TMP/c5"; make_repo "$R"
echo "other" > "$R/other.txt"
git -C "$R" add other.txt && git -C "$R" commit -q -m "add other.txt"
git -C "$R" worktree add -b brief-254-test "$TMP/c5-wt" main -q
# Advance main so there's something to rebase onto, touching file.txt only.
echo "main moved" >> "$R/file.txt"
git -C "$R" add file.txt && git -C "$R" commit -q -m "main advances"
# Dirty the worktree on a DIFFERENT tracked file (coherent WIP, no conflict).
echo "wip" >> "$TMP/c5-wt/other.txt"
if git -C "$TMP/c5-wt" rebase main -q 2>/dev/null; then
    fail "precondition: rebase succeeded on dirty worktree (expected refusal)"
    git -C "$TMP/c5-wt" rebase --abort 2>/dev/null || true
else
    git -C "$TMP/c5-wt" rebase --abort 2>/dev/null || true
    pass "precondition holds: dirty worktree refuses rebase"
fi
commit_worktree_wip "$TMP/c5-wt" "brief-254-test" "before cycle-start rebase"
if git -C "$TMP/c5-wt" rebase main -q 2>/dev/null; then
    pass "after WIP auto-commit → same rebase succeeds (no human round-trip)"
else
    fail "after WIP auto-commit → rebase still blocked"
fi

echo ""
echo "Passed: $PASSED   Failed: $FAILED"
[ "$FAILED" -eq 0 ]
