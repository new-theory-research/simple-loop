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

# ── Case 4: draining UNLABELED solo head → no wake ───────────────────────────
# An unlabeled solo head keeps legacy single-slot semantics — it genuinely needs
# the board to drain, so it suppresses the labeled brief behind it. (A LABELED
# cross-lane solo head would itself be dispatchable under the #74 cross-lane
# rule — the correct new behavior — so the drain test uses an unlabeled head.)
P="$TMP/c4"; make_project "$P" 3 "SOLO_DRAIN_AFTER_SECS=1"
add_card "$P" solo-001 queued "" false misc/x
add_card "$P" fleets-002 queued fleets true fleets/y
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","parallel_safe":true,"edit_surface":["serving/z"]}]'
# Backdate the commit so the solo head reads as past the drain threshold.
git -C "$P" add -A
GIT_AUTHOR_DATE="2020-01-01T00:00:00" GIT_COMMITTER_DATE="2020-01-01T00:00:00" \
    git -C "$P" commit -qm cards >/dev/null 2>&1
if [ -z "$(slots_out "$P" | sed -n 1p)" ]; then
    pass "draining unlabeled solo head at queue head → no wake (solo_drain suppresses slot-filling)"
else
    fail "draining solo head → wake fired (solo_drain should suppress it)"
fi

# ── Case 4b: serving stacks the queue head, cross-lane no-flag brief wakes ────
# Fix-51b: four serving briefs stack the queue head with the serving lane HELD
# by an active brief; behind them a finetune brief with Parallel-safe:false
# (the single-slot default). Per-lane head scan skips the serving heads (mutex),
# and the #74 cross-lane rule admits ft-013 despite no Parallel-safe → it wakes.
P="$TMP/c4b"; make_project "$P" 3
add_card "$P" serve-009 active serving true serving/
add_card "$P" serve-005 queued serving true serving/5
add_card "$P" serve-006 queued serving true serving/6
add_card "$P" serve-007 queued serving true serving/7
add_card "$P" serve-008 queued serving true serving/8
add_card "$P" ft-013 queued finetune false finetune/
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","program":"serving","parallel_safe":true,"edit_surface":["serving/"]}]'
commit_cards "$P"
CAND="$(slots_out "$P" | sed -n 1p)"
if [ "$CAND" = "ft-013" ]; then
    pass "serving stacks the head, lane held → cross-lane no-flag ft-013 wakes (starvation + #74)"
else
    fail "cross-lane no-flag brief behind a stacked lane → expected ft-013, got '$CAND'"
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

# ── fix-51c: Phase-1.5 gate runs on BOTH no-queen branches ───────────────────
# The original build ran Phase 1.5 only when assess emitted NONE. Receipt
# (2026-07-12, portal): ft-013 sat active-but-blocked on a human desk decision,
# emitting `brief_blocked` every tick; that trigger DEDUPED after its first
# queen, so the live trigger was `brief_blocked` (≠ NONE) forever — Phase 1.5
# never RAN — while serve-009 sat cross-lane-dispatchable with a free slot for
# 25+ min. The fix: Phase 1.5 evaluates whenever NO queen would otherwise fire
# this tick — assess emitted NONE, OR the live trigger was dedup-suppressed.
# Reproduced here to keep the gate placement in lockstep with lib/daemon.sh.
#   phase15_gate <root> <trigger> <suppressed> → echoes the tick's queen action:
#     "queen:<reason>"          a live trigger fired the queen (not suppressed)
#     "queen:slots_available"   no queen would fire → Phase 1.5 woke slot-fill
#     "idle"                    no queen would fire and no slot candidate
phase15_gate() {
    local root="$1" trigger="$2" suppressed="$3"
    if [ "$trigger" = "NONE" ]; then
        [ "$(slots_decide "$root")" = "WAKE" ] && { echo "queen:slots_available"; return; }
        echo "idle"; return
    fi
    # A live trigger fires the queen UNLESS it was dedup-suppressed this tick.
    if [ "$suppressed" != "yes" ]; then
        echo "queen:$trigger"; return
    fi
    # Suppressed → no queen would otherwise fire → Phase 1.5 runs on this branch.
    # (A slots wake here leaves the suppressed trigger's dedup untouched — the
    #  daemon never writes LAST_CONDUCTOR_TRIGGER/TS on this path.)
    [ "$(slots_decide "$root")" = "WAKE" ] && { echo "queen:slots_available"; return; }
    echo "idle"
}

# ── Case 8: the live receipt — suppressed brief_blocked + free slot wakes ─────
# ft-013 active-but-blocked (its brief_blocked trigger already deduped),
# serve-009 queued cross-lane, one free THROTTLE slot. Old gate: idle (trigger
# ≠ NONE, so Phase 1.5 never ran). New gate: slots_available wakes.
P="$TMP/c8"; make_project "$P" 3
add_card "$P" ft-013 active finetune false finetune/
add_card "$P" serve-009 queued serving true serving/
set_running "$P" '[{"brief":"ft-013","branch":"ft-013","program":"finetune","parallel_safe":false,"edit_surface":["finetune/"]}]'
commit_cards "$P"
: > "$SLOTS_KEY_FILE"; echo 0 > "$SLOTS_TS_FILE"   # fresh slots-dedup state
G8="$(phase15_gate "$P" brief_blocked yes)"
if [ "$G8" = "queen:slots_available" ]; then
    pass "suppressed brief_blocked + free slot + cross-lane serve-009 → Phase 1.5 wakes slots_available (fix-51c)"
else
    fail "suppressed brief_blocked → expected queen:slots_available, got '$G8'"
fi

# ── Case 9: declining queen holds the dedup — no re-wake next tick ────────────
# The woken queen DECLINES to dispatch (board+queue unchanged). Next tick the
# same suppressed-trigger gate runs Phase 1.5 again; the slots key is unchanged
# so it DEDUPS — the declining queen is not re-woken every tick (point 3).
G9="$(phase15_gate "$P" brief_blocked yes)"
if [ "$G9" = "idle" ]; then
    pass "unchanged board next tick (queen declined) → slots dedup holds, no re-wake (fix-51c point 3)"
else
    fail "declining-queen re-wake guard → expected idle, got '$G9'"
fi

# ── Case 10: a NON-suppressed live trigger fires its own queen (no slot path) ─
# When the live trigger is NOT deduped the queen fires on it directly; Phase 1.5
# does not run (a queen already fires this tick).
: > "$SLOTS_KEY_FILE"; echo 0 > "$SLOTS_TS_FILE"
G10="$(phase15_gate "$P" brief_blocked no)"
if [ "$G10" = "queen:brief_blocked" ]; then
    pass "non-suppressed live trigger → its own queen fires, Phase 1.5 skipped"
else
    fail "non-suppressed live trigger → expected queen:brief_blocked, got '$G10'"
fi

# ── Case 11: trigger NONE path still wakes slot-fill (regression) ─────────────
P="$TMP/c11"; make_project "$P" 3
add_card "$P" serve-009 active serving true serving/
add_card "$P" capture-005 queued capture true capture/
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","parallel_safe":true,"edit_surface":["serving/"]}]'
commit_cards "$P"
: > "$SLOTS_KEY_FILE"; echo 0 > "$SLOTS_TS_FILE"
G11="$(phase15_gate "$P" NONE no)"
if [ "$G11" = "queen:slots_available" ]; then
    pass "trigger NONE + free slot + cross-lane brief → slots_available wakes (regression)"
else
    fail "NONE path regression → expected queen:slots_available, got '$G11'"
fi

# ── Case 12: NONE with a full board → idle (regression) ──────────────────────
P="$TMP/c12"; make_project "$P" 1
add_card "$P" serve-009 active serving true serving/
add_card "$P" capture-005 queued capture true capture/
set_running "$P" '[{"brief":"serve-009","branch":"serve-009","parallel_safe":true,"edit_surface":["serving/"]}]'
commit_cards "$P"
: > "$SLOTS_KEY_FILE"; echo 0 > "$SLOTS_TS_FILE"
G12="$(phase15_gate "$P" NONE no)"
if [ "$G12" = "idle" ]; then
    pass "trigger NONE + full board → idle, no wake (regression)"
else
    fail "NONE full-board regression → expected idle, got '$G12'"
fi

echo ""
echo "Passed: $PASSED   Failed: $FAILED"
[ "$FAILED" -eq 0 ]
