#!/bin/bash
# Brief-003 Thread 7: auto-merge dry-run harness.
#
# Spins up a throwaway git repo with a .loop/ skeleton, plants a brief + a
# validator review + a simulated escalate.json, and asserts that
# lib/auto_merge.py makes the expected decision for each scenario:
#
#   1. Happy path   : flag=true, validator=pass, kill-switch absent, reason=human_approval
#                     → escalate.json swapped for pending-merge.json with auto_merged=true
#   2. Flag off     : flag=absent → escalate.json stays; no pending-merge.json
#   3. Validator block: validator review verdict=block → escalate.json stays
#   4. Kill-switch  : pause-auto-merge present → escalate.json stays
#   5. Wrong reason : escalate reason is not human_approval_required_for_merge
#                     → escalate.json stays (auto-merge doesn't touch other classes)
#
# Exits 0 iff all scenarios produce their expected outcome. Emits per-case
# PASS/FAIL lines to stdout.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AUTO_MERGE_PY="$LIB_DIR/auto_merge.py"

if [ ! -f "$AUTO_MERGE_PY" ]; then
    echo "FAIL: auto_merge.py not found at $AUTO_MERGE_PY" >&2
    exit 1
fi

PASSED=0
FAILED=0

fresh_repo() {
    local dir="$1"
    local brief_id="$2"
    local auto_merge_flag="$3"       # "true" | "false"
    local validator_verdict="$4"     # "pass" | "issues" | "block" | "none"
    local kill_switch="$5"           # "yes" | "no"
    local escalate_reason="$6"       # "human_approval_required_for_merge" | "other"

    rm -rf "$dir"
    mkdir -p "$dir"
    git -C "$dir" init -q -b main
    git -C "$dir" config user.email "dry-run@test"
    git -C "$dir" config user.name  "Dry Run"

    mkdir -p "$dir/.loop/state/signals"
    mkdir -p "$dir/wiki/briefs/cards/$brief_id"
    mkdir -p "$dir/.loop/modules/validator/state/reviews"

    cat > "$dir/.loop/config.sh" <<EOF
PROJECT_NAME="dry-run"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
EOF

    local brief_file="wiki/briefs/cards/${brief_id}/index.md"
    {
        echo "# Brief: dry-run"
        echo ""
        echo "**ID:** $brief_id"
        echo "**Branch:** $brief_id"
        echo "**Status:** queued"
        echo "**Model:** opus"
        if [ "$auto_merge_flag" = "true" ]; then
            echo "**Auto-merge:** true"
        fi
        echo ""
        echo "Dry-run brief body."
    } > "$dir/$brief_file"

    # progress.json with status=complete, iteration=1 (so validator-caught-up check passes)
    cat > "$dir/.loop/state/progress.json" <<EOF
{"brief": "$brief_id", "brief_file": "$brief_file", "iteration": 1, "status": "complete", "tasks_completed": [], "tasks_remaining": [], "learnings": []}
EOF

    # Validator review (if requested)
    if [ "$validator_verdict" != "none" ]; then
        local review_path=".loop/modules/validator/state/reviews/${brief_id}-cycle-1.md"
        cat > "$dir/$review_path" <<EOF
---
cycle: 1
commit: deadbeef
brief: $brief_id
branch: $brief_id
verdict: $validator_verdict
summary: dry-run
validator: loop-reviewer
reviewed_at: 2026-04-20T00:00:00Z
---

## Bugs found
- _none_

## Execution concerns
- _none_

## Spec-fit notes
- _none_

## Deferred items
- _none_
EOF
    fi

    # Kill switch
    if [ "$kill_switch" = "yes" ]; then
        touch "$dir/.loop/state/pause-auto-merge"
    fi

    # Commit everything on main first so the branch has the files
    git -C "$dir" add -A
    git -C "$dir" commit -q -m "dry-run seed"
    # Create a branch matching brief_id (auto_merge looks up branch ref)
    git -C "$dir" branch "$brief_id"

    # Simulated escalate.json (written by queen in real life)
    cat > "$dir/.loop/state/signals/escalate.json" <<EOF
{"type": "human_approval",
 "reason": "$escalate_reason",
 "brief": "$brief_id",
 "brief_id": "$brief_id",
 "branch": "$brief_id",
 "brief_file": "$brief_file",
 "title": "Dry run brief"}
EOF
}

assert_swap() {
    local dir="$1"
    local label="$2"
    if [ -f "$dir/.loop/state/pending-merge.json" ] && [ ! -f "$dir/.loop/state/signals/escalate.json" ]; then
        local auto_merged
        auto_merged=$(python3 -c "import json; print(json.load(open('$dir/.loop/state/pending-merge.json')).get('auto_merged', False))")
        if [ "$auto_merged" = "True" ]; then
            echo "PASS  [$label] escalate→pending-merge swap with auto_merged=true"
            PASSED=$((PASSED + 1))
            return
        fi
        echo "FAIL  [$label] pending-merge.json written but auto_merged != true"
    else
        echo "FAIL  [$label] expected swap: pending-merge.json present, escalate.json gone"
    fi
    FAILED=$((FAILED + 1))
}

assert_no_swap() {
    local dir="$1"
    local label="$2"
    if [ -f "$dir/.loop/state/signals/escalate.json" ] && [ ! -f "$dir/.loop/state/pending-merge.json" ]; then
        echo "PASS  [$label] escalate.json retained; no pending-merge.json"
        PASSED=$((PASSED + 1))
        return
    fi
    echo "FAIL  [$label] expected escalate retained + no pending-merge"
    FAILED=$((FAILED + 1))
}

run_check() {
    python3 "$AUTO_MERGE_PY" check-escalate "$1" >/dev/null 2>&1
}

# ─── Scenario 1: Happy path ─────────────────────────────────────────
DIR=$(mktemp -d)
fresh_repo "$DIR" "brief-999-happy" "true" "pass" "no" "human_approval_required_for_merge"
run_check "$DIR"
assert_swap "$DIR" "happy path"
rm -rf "$DIR"

# ─── Scenario 2: Flag off ───────────────────────────────────────────
DIR=$(mktemp -d)
fresh_repo "$DIR" "brief-999-flag-off" "false" "pass" "no" "human_approval_required_for_merge"
run_check "$DIR"
assert_no_swap "$DIR" "flag off"
rm -rf "$DIR"

# ─── Scenario 3: Validator block ────────────────────────────────────
DIR=$(mktemp -d)
fresh_repo "$DIR" "brief-999-block" "true" "block" "no" "human_approval_required_for_merge"
run_check "$DIR"
assert_no_swap "$DIR" "validator block"
rm -rf "$DIR"

# ─── Scenario 4: Kill switch ────────────────────────────────────────
DIR=$(mktemp -d)
fresh_repo "$DIR" "brief-999-killsw" "true" "pass" "yes" "human_approval_required_for_merge"
run_check "$DIR"
assert_no_swap "$DIR" "kill switch"
rm -rf "$DIR"

# ─── Scenario 5: Wrong escalation class ─────────────────────────────
DIR=$(mktemp -d)
fresh_repo "$DIR" "brief-999-other" "true" "pass" "no" "infra_failure"
run_check "$DIR"
assert_no_swap "$DIR" "non-merge escalation"
rm -rf "$DIR"

# ─── Scenario 6: Validator issues (non-blocking) ────────────────────
# 'issues' is non-blocking in the merge gate policy, BUT the auto-merge
# precondition demands a `pass` verdict — non-pass means human still decides.
DIR=$(mktemp -d)
fresh_repo "$DIR" "brief-999-issues" "true" "issues" "no" "human_approval_required_for_merge"
run_check "$DIR"
assert_no_swap "$DIR" "validator issues"
rm -rf "$DIR"

echo ""
echo "────────────────────────────────────────"
echo "auto-merge dry-run: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────"

exit $([ "$FAILED" -eq 0 ] && echo 0 || echo 1)
