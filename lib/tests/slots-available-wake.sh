#!/usr/bin/env bash
# Regression test for the slots_available queen wake (issue #51).
#
# Historically the queen woke to dispatch only on `no_active` — a single active
# brief silenced every other lane's queued work, even with an open THROTTLE slot
# and the collision-check machinery to dispatch safely. This extends the wake
# condition to "board has capacity AND cross-lane work is dispatchable": the
# daemon runs the PURE why.py --slots-available check when assess emits no
# higher-priority trigger, and — if a candidate exists — wakes the queen (who
# remains the decider).
#
# Two things are verified here:
#   1. the real why.py --slots-available CLI, against fixtures modelling the
#      receipt scenario and its negatives (mutex, throttle, solo-drain);
#   2. the daemon's Phase-1.5 dedup decision (queue-fp + active-set key with TTL)
#      reproduced inline, in lockstep with lib/daemon.sh — no-spam on an
#      unchanged board, re-fire when the queue or the active set changes.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WHY_PY="$LIB_DIR/why.py"

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── fixture builder ──────────────────────────────────────────────────────────
# make_project <root> <throttle> [extra config lines...]
make_project() {
    local root="$1" throttle="$2"; shift 2
    mkdir -p "$root/.loop/state" "$root/wiki/briefs/cards"
    {
        echo "GIT_REMOTE=origin"
        echo "GIT_MAIN_BRANCH=main"
        echo "THROTTLE=$throttle"
        for line in "$@"; do echo "$line"; done
    } > "$root/.loop/config.sh"
    git -C "$root" init -q
    git -C "$root" config user.email t@t
    git -C "$root" config user.name t
    : > "$root/seed"; git -C "$root" add -A; git -C "$root" commit -qm seed >/dev/null 2>&1
    local remote="$root/../remote-$(basename "$root").git"
    git init --bare -q "$remote"
    git -C "$root" remote add origin "$remote"
    : > "$root/.loop/state/goals.md"
}

# add_card <root> <id> <status> <program> <parallel-safe> <edit-surface>
add_card() {
    local root="$1" id="$2" status="$3" program="$4" ps="$5" es="$6"
    local d="$root/wiki/briefs/cards/$id"; mkdir -p "$d"
    cat > "$d/index.md" <<EOF
---
ID: $id
Status: $status
Program: $program
Parallel-safe: $ps
Edit-surface: $es
---

# $id
EOF
    # goals.md ordering: the queue enumerator extracts brief ids by regex, so a
    # plain bullet per id fixes first-appearance order without line numbering.
    echo "- $id" >> "$root/.loop/state/goals.md"
}

commit_cards() {
    git -C "$1" add -A
    git -C "$1" commit -qm cards >/dev/null 2>&1
}

# set_running <root> <active-json-array>
set_running() {
    printf '{"active":%s,"awaiting_review":[],"pending_merges":[],"completed_pending_eval":[],"history":[]}\n' \
        "$2" > "$1/.loop/state/running.json"
}

# slots_out <root> — echo the three-line CLI output (empty if exit 1).
slots_out() {
    LOOP_LANE="" python3 "$WHY_PY" "$1" --slots-available 2>/dev/null || true
}

# ── Case 1: serve active + capture dispatchable + free slot → wake candidate ──
P="$TMP/c1"; make_project "$P" 3
add_card "$P" serve-009 active serving true serving/
add_card "$P" capture-005 queued capture true capture/
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","parallel_safe":true,"edit_surface":["serving/"]}]'
commit_cards "$P"
OUT="$(slots_out "$P")"; CAND="$(printf '%s\n' "$OUT" | sed -n 1p)"
if [ "$CAND" = "capture-005" ]; then
    pass "cross-lane dispatchable brief with a free slot → queen wake candidate (capture-005)"
else
    fail "cross-lane dispatchable → expected capture-005, got '$CAND'"
fi

# ── Case 2: same-lane-only queue → NO wake (lane mutex filters it) ────────────
P="$TMP/c2"; make_project "$P" 3
add_card "$P" serve-009 active serving true serving/a
add_card "$P" serve-010 queued serving true serving/b
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","parallel_safe":true,"edit_surface":["serving/a"]}]'
commit_cards "$P"
if [ -z "$(slots_out "$P" | sed -n 1p)" ]; then
    pass "same-lane queued brief while lane is held → no wake (mutex-filtered)"
else
    fail "same-lane queued brief → wake fired (mutex should have filtered it)"
fi

# ── Case 3: THROTTLE full → no wake ──────────────────────────────────────────
P="$TMP/c3"; make_project "$P" 1
add_card "$P" serve-009 active serving true serving/
add_card "$P" capture-005 queued capture true capture/
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","parallel_safe":true,"edit_surface":["serving/"]}]'
commit_cards "$P"
if [ -z "$(slots_out "$P" | sed -n 1p)" ]; then
    pass "board at THROTTLE cap → no wake"
else
    fail "THROTTLE full → wake fired (throttle gate should have closed)"
fi

# ── Case 4: draining solo head → no wake ─────────────────────────────────────
P="$TMP/c4"; make_project "$P" 3 "SOLO_DRAIN_AFTER_SECS=1"
add_card "$P" capture-001 queued capture false capture/x
add_card "$P" fleets-002 queued fleets true fleets/y
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","parallel_safe":true,"edit_surface":["serving/z"]}]'
# Backdate the commit so the solo head reads as past the drain threshold.
git -C "$P" add -A
GIT_AUTHOR_DATE="2020-01-01T00:00:00" GIT_COMMITTER_DATE="2020-01-01T00:00:00" \
    git -C "$P" commit -qm cards >/dev/null 2>&1
if [ -z "$(slots_out "$P" | sed -n 1p)" ]; then
    pass "draining solo head at queue head → no wake (solo_drain suppresses slot-filling)"
else
    fail "draining solo head → wake fired (solo_drain should suppress it)"
fi

# ── Dedup decision (mirrors lib/daemon.sh Phase 1.5) ─────────────────────────
# The daemon keys the slots_available dedup on queue-fp|active-set-fp with a TTL.
# Reproduced here so the asserted logic stays in lockstep with the daemon.
CONDUCTOR_DEDUP_TTL_SECS=1800
# State persists in files (slots_decide is called via command substitution, so
# shell-variable updates in the subshell would be lost).
SLOTS_KEY_FILE="$TMP/last-slots-key"; : > "$SLOTS_KEY_FILE"
SLOTS_TS_FILE="$TMP/last-slots-ts"; echo 0 > "$SLOTS_TS_FILE"
# slots_decide <root> — echoes "WAKE"/"DEDUP"/"NONE"; persists key+ts on wake.
slots_decide() {
    local out brief qfp afp key now age last_key last_ts
    out="$(slots_out "$1")"
    brief="$(printf '%s\n' "$out" | sed -n 1p)"
    [ -z "$brief" ] && { echo "NONE"; return; }
    qfp="$(printf '%s\n' "$out" | sed -n 2p)"
    afp="$(printf '%s\n' "$out" | sed -n 3p)"
    key="${qfp}|${afp}"
    now=$(date +%s)
    last_key="$(cat "$SLOTS_KEY_FILE")"; last_ts="$(cat "$SLOTS_TS_FILE")"
    age=$(( now - last_ts ))
    if [ "$key" = "$last_key" ] && [ "$age" -lt "$CONDUCTOR_DEDUP_TTL_SECS" ]; then
        echo "DEDUP"
    else
        printf '%s' "$key" > "$SLOTS_KEY_FILE"; echo "$now" > "$SLOTS_TS_FILE"
        echo "WAKE"
    fi
}

# ── Case 5: first tick wakes, unchanged board on the next tick dedups ─────────
P="$TMP/c5"; make_project "$P" 3
add_card "$P" serve-009 active serving true serving/
add_card "$P" capture-005 queued capture true capture/
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","parallel_safe":true,"edit_surface":["serving/"]}]'
commit_cards "$P"
D1="$(slots_decide "$P")"
D2="$(slots_decide "$P")"
if [ "$D1" = "WAKE" ] && [ "$D2" = "DEDUP" ]; then
    pass "first tick WAKE, unchanged board next tick DEDUP (no queen spam)"
else
    fail "expected WAKE then DEDUP, got '$D1' then '$D2'"
fi

# ── Case 6: an active-set change re-fires the wake ───────────────────────────
# serve-009 finishes, serve-011 takes the slot: the active-set fingerprint flips
# even though capture-005 is still the queued candidate → re-fire.
set_running "$P" '[{"brief":"serve-011","branch":"serve-011","parallel_safe":true,"edit_surface":["serving/"]}]'
D3="$(slots_decide "$P")"
if [ "$D3" = "WAKE" ]; then
    pass "active-set change (serve-009 → serve-011) re-fires the wake"
else
    fail "active-set change → expected WAKE, got '$D3'"
fi

# ── Case 7: a queue change (dispatch success) re-fires the wake ──────────────
# capture-005 dispatched (leaves the queue), capture-006 filed → queue fp flips.
git -C "$P" rm -q -r "wiki/briefs/cards/capture-005" >/dev/null 2>&1
: > "$P/.loop/state/goals.md"
add_card "$P" capture-006 queued capture true capture/
commit_cards "$P"
D4="$(slots_decide "$P")"
if [ "$D4" = "WAKE" ]; then
    pass "queue change (capture-005 dispatched, capture-006 filed) re-fires the wake"
else
    fail "queue change → expected WAKE, got '$D4'"
fi

echo ""
echo "Passed: $PASSED   Failed: $FAILED"
[ "$FAILED" -eq 0 ]
