#!/usr/bin/env bash
# Regression test for wall-clock tick sleeps (issue #65).
#
# The daemon's scheduling naps used process-clock `sleep N`. When the machine
# slept mid-nap (lid close, pmset, VM pause) the child suspended with it and, on
# wake, bash resumed counting the remaining seconds — the daemon froze for the
# whole nap with no tick and no log line distinguishing "frozen" from "idle".
# wall_sleep() sleeps in short wall-clock slices instead, so the first slice
# after a suspend returns to find `now` already past the target and the nap ends
# immediately; a jump past ~2x the intended nap is logged loudly and appended to
# runtime-events.jsonl.
#
# Covers:
#   AC1 — no machine sleep: slices, no WAKE log, no wake event.
#   AC2 — machine sleep (clock jumps past target): immediate return (no long
#         sleep), loud WAKE log, wake event appended to runtime-events.jsonl.
#   AC3 — WALL_SLICE_SECS caps the per-slice sleep length.
#   AC4 — real-time end-to-end: a 1s nap returns in ~1s via real date/sleep.
#
# wall_sleep is extracted VERBATIM from lib/daemon.sh via sed so the asserted
# logic cannot drift from the shipped code.

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_SH="$SCRIPT_DIR/../daemon.sh"

# ── Extract wall_sleep from lib/daemon.sh verbatim ───────────────────────────
FUNC_WALL_SLEEP=$(sed -n '/^wall_sleep() {/,/^}/p' "$DAEMON_SH")
if [ -z "$FUNC_WALL_SLEEP" ]; then
    fail "could not extract wall_sleep from lib/daemon.sh"
    echo "FAILED: 1"
    exit 1
fi
eval "$FUNC_WALL_SLEEP"

# state.py append-event is invoked by wall_sleep on a detected jump. Point the
# real lib dir so the real writer runs.
DAEMON_LIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Stubs: capture daemon_log lines; fake date+%s from a queue; record sleeps ─
_DAEMON_LOG_LINES=""
daemon_log() { _DAEMON_LOG_LINES="${_DAEMON_LOG_LINES}${1}"$'\n'; }

# Queue-driven `date +%s`. Each `date +%s` consumes the next value; any other
# date format falls through to the real binary (daemon_log timestamps).
# wall_sleep reads the clock inside `$(date +%s)` command substitutions — those
# run in a subshell, so the consume-index MUST live in a file to advance in the
# parent (a plain shell var would reset every call and spin the loop forever).
_NOW_QUEUE=()
_NOW_I_FILE="$TMP/now_index"
date() {
    if [ "${1:-}" = "+%s" ]; then
        local i; i=$(cat "$_NOW_I_FILE")
        echo "${_NOW_QUEUE[$i]}"
        echo $((i + 1)) > "$_NOW_I_FILE"
    else
        command date "$@"
    fi
}

# Record every sleep arg instead of actually sleeping.
_SLEEP_ARGS=()
sleep() { _SLEEP_ARGS+=("$1"); }

reset_stubs() {
    _DAEMON_LOG_LINES=""
    _NOW_QUEUE=("$@")
    echo 0 > "$_NOW_I_FILE"
    _SLEEP_ARGS=()
}

make_project() {
    local root="$1"
    mkdir -p "$root/.loop/state"
    echo "$root"
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC2: machine sleep — clock jumps past target                    ║
# ╚══════════════════════════════════════════════════════════════════╝

echo ""
echo "── AC2: machine sleep detected ─────────────────────────────────"

PROJECT_DIR="$(make_project "$TMP/ac2")"
export WALL_SLICE_SECS=60
# start=1000, target=1300 (secs=300). First in-loop `now` already jumped to
# 99999 (machine slept during the interval) → break with zero sleeps.
reset_stubs 1000 99999 99999
wall_sleep 300

if [ "${#_SLEEP_ARGS[@]}" -eq 0 ]; then
    pass "[AC2] clock already past target → returns without sleeping"
else
    fail "[AC2] slept ${#_SLEEP_ARGS[@]} time(s) despite target in the past"
fi

if echo "$_DAEMON_LOG_LINES" | grep -q "WAKE: wall clock jumped 98699s past tick target"; then
    pass "[AC2] loud WAKE log with correct seconds-past (98699)"
else
    fail "[AC2] missing/incorrect WAKE log line: $_DAEMON_LOG_LINES"
fi

EVENTS="$PROJECT_DIR/.loop/state/runtime-events.jsonl"
if [ -f "$EVENTS" ] && grep -q '"event": "wake"' "$EVENTS"; then
    pass "[AC2] wake event appended to runtime-events.jsonl"
else
    fail "[AC2] no wake event in runtime-events.jsonl"
fi

# Field integrity: intended_nap_s and seconds_past_target land as written.
NAP=$(python3 -c "import json,sys; print([json.loads(l)['intended_nap_s'] for l in open('$EVENTS') if json.loads(l).get('event')=='wake'][-1])" 2>/dev/null)
PAST=$(python3 -c "import json,sys; print([json.loads(l)['seconds_past_target'] for l in open('$EVENTS') if json.loads(l).get('event')=='wake'][-1])" 2>/dev/null)
if [ "$NAP" = "300" ] && [ "$PAST" = "98699" ]; then
    pass "[AC2] wake event fields: intended_nap_s=300, seconds_past_target=98699"
else
    fail "[AC2] wake event fields wrong (nap=$NAP past=$PAST)"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC1: no machine sleep — slices, no WAKE, no event               ║
# ╚══════════════════════════════════════════════════════════════════╝

echo ""
echo "── AC1: normal nap, no clock jump ──────────────────────────────"

PROJECT_DIR="$(make_project "$TMP/ac1")"
export WALL_SLICE_SECS=60
# start=1000, target=1120 (secs=120). Clock advances 60s per slice, no jump.
reset_stubs 1000 1000 1060 1120 1120
wall_sleep 120

if [ "${#_SLEEP_ARGS[@]}" -eq 2 ]; then
    pass "[AC1] 120s nap taken as 2 slices"
else
    fail "[AC1] expected 2 slices, got ${#_SLEEP_ARGS[@]} (${_SLEEP_ARGS[*]:-none})"
fi

if echo "$_DAEMON_LOG_LINES" | grep -q "WAKE:"; then
    fail "[AC1] spurious WAKE log on a normal nap"
else
    pass "[AC1] no WAKE log on a normal nap"
fi

if [ -f "$PROJECT_DIR/.loop/state/runtime-events.jsonl" ]; then
    fail "[AC1] wake event written on a normal nap"
else
    pass "[AC1] no wake event on a normal nap"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC3: WALL_SLICE_SECS caps per-slice length                      ║
# ╚══════════════════════════════════════════════════════════════════╝

echo ""
echo "── AC3: slice length honors WALL_SLICE_SECS ────────────────────"

PROJECT_DIR="$(make_project "$TMP/ac3")"
export WALL_SLICE_SECS=60
reset_stubs 1000 1000 1060 1120 1120
wall_sleep 120
if [ "${_SLEEP_ARGS[0]:-}" = "60" ]; then
    pass "[AC3] WALL_SLICE_SECS=60 → first slice is 60s"
else
    fail "[AC3] expected first slice 60, got '${_SLEEP_ARGS[0]:-none}'"
fi

# Mutation-discriminate: a smaller slice knob yields a smaller first slice.
PROJECT_DIR="$(make_project "$TMP/ac3b")"
export WALL_SLICE_SECS=30
reset_stubs 1000 1000 1030 1060 1090 1120 1120
wall_sleep 120
if [ "${_SLEEP_ARGS[0]:-}" = "30" ]; then
    pass "[AC3-mutation] WALL_SLICE_SECS=30 → first slice is 30s (knob honored)"
else
    fail "[AC3-mutation] expected first slice 30, got '${_SLEEP_ARGS[0]:-none}'"
fi

# Final slice is clamped to the remaining time, never overshooting the target.
PROJECT_DIR="$(make_project "$TMP/ac3c")"
export WALL_SLICE_SECS=60
# secs=90: start=1000 target=1090. iter1 now=1000 rem=90 slice=60; iter2 now=1060
# rem=30 slice=30; iter3 now=1090 break.
reset_stubs 1000 1000 1060 1090 1090
wall_sleep 90
if [ "${_SLEEP_ARGS[1]:-}" = "30" ]; then
    pass "[AC3] final slice clamped to remaining 30s (no overshoot)"
else
    fail "[AC3] final slice not clamped (got '${_SLEEP_ARGS[1]:-none}')"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC4: real-time end-to-end — 1s nap returns in ~1s               ║
# ╚══════════════════════════════════════════════════════════════════╝

echo ""
echo "── AC4: real date/sleep end-to-end ─────────────────────────────"

# Drop the stubs and run against the real binaries.
unset -f date
unset -f sleep
PROJECT_DIR="$(make_project "$TMP/ac4")"
export WALL_SLICE_SECS=60
_DAEMON_LOG_LINES=""
RT_START=$(command date +%s)
wall_sleep 1
RT_END=$(command date +%s)
RT_DUR=$((RT_END - RT_START))
if [ "$RT_DUR" -ge 1 ] && [ "$RT_DUR" -le 3 ]; then
    pass "[AC4] real 1s nap returned in ${RT_DUR}s"
else
    fail "[AC4] real 1s nap took ${RT_DUR}s (expected ~1s)"
fi
if [ ! -f "$PROJECT_DIR/.loop/state/runtime-events.jsonl" ]; then
    pass "[AC4] no spurious wake event on a real short nap"
else
    fail "[AC4] wake event written on a real short nap"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Wiring: scheduling naps call wall_sleep, not plain sleep         ║
# ╚══════════════════════════════════════════════════════════════════╝

echo ""
echo "── Wiring: daemon scheduling paths use wall_sleep ──────────────"

# The rate-limit no-reset fallback naps via wall_sleep too. Pre-#81 this was a
# flat `wall_sleep 3600`; #81 replaced it with a capped exponential backoff
# (`wall_sleep "$RATE_LIMIT_BACKOFF_SECS"`) — still a wall_sleep, never a hot loop.
for site in 'wall_sleep "\$SKIP_SLEEP"' 'wall_sleep "\$SLEEP_SECS"' 'wall_sleep "\$RATE_LIMIT_BACKOFF_SECS"'; do
    if grep -q "$site" "$DAEMON_SH"; then
        pass "[wiring] daemon.sh uses: $site"
    else
        fail "[wiring] daemon.sh missing: $site"
    fi
done
if grep -q 'WALL_SLICE_SECS.*:-60' "$DAEMON_SH"; then
    pass "[wiring] WALL_SLICE_SECS has in-code default of 60"
else
    fail "[wiring] WALL_SLICE_SECS missing in-code default"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Summary                                                         ║
# ╚══════════════════════════════════════════════════════════════════╝

echo ""
echo "────────────────────────────────────────────────────────────────"
echo "wall-clock-tick-sleep: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
