#!/usr/bin/env bash
# Regression test for portal-obs 2026-06-01 Pattern 1:
#
# `dedup-clear-<brief>.json` signals are written by actions.py whenever a
# brief moves out of active[] (merge, approve, reject, move-to-awaiting-
# review). The daemon must always clear LAST_CONDUCTOR_TRIGGER when it
# consumes one — otherwise the queen sits deduped on `no_active` for the
# full 1800s TTL even though the queue just advanced.
#
# Mirrors the state-change-dedup-clear block from lib/daemon.sh so the
# tested logic stays in lockstep with what the daemon actually runs.

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Block under test — kept in sync with lib/daemon.sh state-change block.
clear_dedup_on_signals() {
    local SIGNALS_DIR="$1"
    _CLEAR_COUNT=0
    for _CLEAR_FILE in "$SIGNALS_DIR"/dedup-clear-*.json; do
        [ -f "$_CLEAR_FILE" ] || continue
        _CLEAR_FNAME=$(basename "$_CLEAR_FILE")
        _CLEAR_BRIEF="${_CLEAR_FNAME#dedup-clear-}"
        _CLEAR_BRIEF="${_CLEAR_BRIEF%.json}"
        rm -f "$_CLEAR_FILE"
        _CLEAR_COUNT=$((_CLEAR_COUNT + 1))
    done
    if [ "$_CLEAR_COUNT" -gt 0 ]; then
        LAST_CONDUCTOR_TRIGGER=""
        LAST_CONDUCTOR_TRIGGER_TS=0
    fi
}

# ── Case 1: dedup-clear after merge clears `no_active` cache ─────────────────
# The wedge from the portal observations: trigger was `no_active`, a merge
# wrote dedup-clear-brief-N.json, the daemon must clear the cache.
SIGS="$TMP/c1"; mkdir -p "$SIGS"
echo '{"brief":"brief-218"}' > "$SIGS/dedup-clear-brief-218.json"
LAST_CONDUCTOR_TRIGGER="CONDUCTOR:no_active"
LAST_CONDUCTOR_TRIGGER_TS=$(date +%s)
clear_dedup_on_signals "$SIGS"
if [ -z "$LAST_CONDUCTOR_TRIGGER" ] && [ "$LAST_CONDUCTOR_TRIGGER_TS" = "0" ]; then
    pass "no_active trigger cleared after merge-induced dedup-clear"
else
    fail "no_active trigger NOT cleared (was the bug); trigger='$LAST_CONDUCTOR_TRIGGER' ts='$LAST_CONDUCTOR_TRIGGER_TS'"
fi
if [ ! -f "$SIGS/dedup-clear-brief-218.json" ]; then
    pass "signal file consumed"
else
    fail "signal file still on disk"
fi

# ── Case 2: dedup-clear when trigger DOES name the brief ─────────────────────
# Pre-existing behavior should still work — `stale_brief:brief-X` triggers
# also get cleared (they did under the gated logic too).
SIGS="$TMP/c2"; mkdir -p "$SIGS"
echo '{"brief":"brief-100"}' > "$SIGS/dedup-clear-brief-100.json"
LAST_CONDUCTOR_TRIGGER="CONDUCTOR:stale_brief:brief-100"
LAST_CONDUCTOR_TRIGGER_TS=$(date +%s)
clear_dedup_on_signals "$SIGS"
if [ -z "$LAST_CONDUCTOR_TRIGGER" ]; then
    pass "stale_brief trigger cleared (regression coverage for prior behavior)"
else
    fail "stale_brief trigger NOT cleared; trigger='$LAST_CONDUCTOR_TRIGGER'"
fi

# ── Case 3: no signals → cache untouched ─────────────────────────────────────
# Don't bust dedup gratuitously when there's nothing to consume.
SIGS="$TMP/c3"; mkdir -p "$SIGS"
LAST_CONDUCTOR_TRIGGER="CONDUCTOR:no_active"
SAVED_TS=$(date +%s)
LAST_CONDUCTOR_TRIGGER_TS=$SAVED_TS
clear_dedup_on_signals "$SIGS"
if [ "$LAST_CONDUCTOR_TRIGGER" = "CONDUCTOR:no_active" ] && [ "$LAST_CONDUCTOR_TRIGGER_TS" = "$SAVED_TS" ]; then
    pass "no signals → cache preserved"
else
    fail "cache mutated without a signal — would cause queen spam"
fi

# ── Case 4: multiple signals at once → still clears, all consumed ────────────
SIGS="$TMP/c4"; mkdir -p "$SIGS"
echo '{}' > "$SIGS/dedup-clear-brief-201.json"
echo '{}' > "$SIGS/dedup-clear-brief-202.json"
echo '{}' > "$SIGS/dedup-clear-brief-203.json"
LAST_CONDUCTOR_TRIGGER="CONDUCTOR:no_active"
LAST_CONDUCTOR_TRIGGER_TS=$(date +%s)
clear_dedup_on_signals "$SIGS"
REMAINING=$(ls "$SIGS"/dedup-clear-*.json 2>/dev/null | wc -l | tr -d ' ')
if [ -z "$LAST_CONDUCTOR_TRIGGER" ] && [ "$REMAINING" = "0" ]; then
    pass "batch of signals → all consumed, cache cleared"
else
    fail "batch handling broken; trigger='$LAST_CONDUCTOR_TRIGGER' remaining=$REMAINING"
fi

echo ""
echo "Passed: $PASSED   Failed: $FAILED"
[ "$FAILED" -eq 0 ]
