#!/usr/bin/env bash
# Regression test for repeat-failure escalation (issue #15).
#
# Tonight's receipt: a delivered-gate REFUSED re-idled identically 13x over
# 2h15m, silent — no escalation, no notification, no retry cap. The fix: any
# gate/dispatch/sync failure that repeats IDENTICALLY N times stops silently
# retrying and raises the EXISTING escalate.json with the receipt.
#
# Design rule (Mattie): the STATE CHANGE is the fix; notify() is one line at the
# end. So this test asserts the STATE (counter file, escalate.json, runtime
# event, once-per-fingerprint dedup) — notify is stubbed to a no-op.
#
# The daemon-side wrapper functions are extracted VERBATIM from lib/daemon.sh so
# the asserted logic cannot drift from the shipped code; they call the real
# lib/failure_tracker.py.
#
# Covers:
#   AC1 — identical failure xN triggers exactly ONE escalation + counter content.
#   AC2 — a DIFFERENT reason resets the counter (progress of a kind).
#   AC3 — success at the site resets the counter.
#   AC4 — already-escalated does NOT re-raise every tick (dedup).
#   AC5 — the delivered-gate site specifically (tonight's class): a simulated
#         refusal loop raises escalate.json with the receipt + parks the brief.
#   AC6 — resolving escalate.json re-arms the fingerprint (mirrors dedup reset).

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_SH="$SCRIPT_DIR/../daemon.sh"
DAEMON_LIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Extract the wrapper functions from lib/daemon.sh verbatim ────────────────
FUNC_RECORD=$(sed -n '/^record_failure_and_maybe_escalate() {/,/^}/p' "$DAEMON_SH")
FUNC_CLEAR=$(sed -n '/^clear_failure_fingerprint() {/,/^}/p' "$DAEMON_SH")
for fn in FUNC_RECORD FUNC_CLEAR; do
    if [ -z "${!fn}" ]; then
        fail "could not extract $fn from lib/daemon.sh"
        echo "FAILED: 1"; exit 1
    fi
done
eval "$FUNC_RECORD"
eval "$FUNC_CLEAR"

# Stubs the wrappers reference
_LOG=""
daemon_log() { _LOG="${_LOG}${1}"$'\n'; }
notify() { :; }
ESCALATE_AFTER_FAILURES=3

# ── Fixtures / helpers ───────────────────────────────────────────────────────
make_state() {
    STATE_DIR="$1"
    mkdir -p "$STATE_DIR/signals"
}
esc()      { echo "$STATE_DIR/signals/escalate.json"; }
counter()  { echo "$STATE_DIR/failure-fingerprints.json"; }
events()   { echo "$STATE_DIR/runtime-events.jsonl"; }
count_events() { [ -f "$(events)" ] && grep -c "repeat_failure_escalated" "$(events)" || echo 0; }
jget() { python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2" 2>/dev/null; }
# count for a given site::brief key in the counter file
kcount() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2],{}).get('count',0))" "$(counter)" "$1::$2" 2>/dev/null || echo 0; }
kesc()   { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2],{}).get('escalated',False))" "$(counter)" "$1::$2" 2>/dev/null || echo False; }

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC1: identical failure xN → exactly one escalation             ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC1: identical failure xN → one escalation ───────────────────"
make_state "$TMP/ac1"
ESCALATE_AFTER_FAILURES=3
R="delivered-gate: REFUSED — brief-x delivered['repo'] not verifiable"

record_failure_and_maybe_escalate "auto-merge-gate" "brief-x" "$R"; RC1=$?
record_failure_and_maybe_escalate "auto-merge-gate" "brief-x" "$R"; RC2=$?
[ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ] && pass "[AC1] first two failures return 0 (below threshold)" \
    || fail "[AC1] early returns wrong (rc1=$RC1 rc2=$RC2)"
[ ! -f "$(esc)" ] && pass "[AC1] no escalate.json before threshold" \
    || fail "[AC1] escalate.json written early"
[ "$(kcount auto-merge-gate brief-x)" = "2" ] && pass "[AC1] counter=2 after two failures" \
    || fail "[AC1] counter wrong (=$(kcount auto-merge-gate brief-x))"

record_failure_and_maybe_escalate "auto-merge-gate" "brief-x" "$R"; RC3=$?
[ "$RC3" -eq 10 ] && pass "[AC1] third identical failure returns 10 (escalate now)" \
    || fail "[AC1] third failure rc=$RC3 (expected 10)"
[ -f "$(esc)" ] && pass "[AC1] escalate.json written on Nth failure" \
    || fail "[AC1] escalate.json missing after Nth failure"
[ "$(kcount auto-merge-gate brief-x)" = "3" ] && pass "[AC1] counter=3 at escalation" \
    || fail "[AC1] counter wrong at escalation (=$(kcount auto-merge-gate brief-x))"
[ "$(kesc auto-merge-gate brief-x)" = "True" ] && pass "[AC1] fingerprint marked escalated=true" \
    || fail "[AC1] fingerprint not marked escalated"
[ "$(count_events)" = "1" ] && pass "[AC1] exactly one runtime escalation event" \
    || fail "[AC1] wrong runtime event count (=$(count_events))"

# escalate.json content carries the receipt
[ "$(jget "$(esc)" type)" = "repeat_failure" ] && pass "[AC1] escalate.json type=repeat_failure" \
    || fail "[AC1] escalate.json type wrong (=$(jget "$(esc)" type))"
[ "$(jget "$(esc)" brief)" = "brief-x" ] && pass "[AC1] escalate.json brief=brief-x" \
    || fail "[AC1] escalate.json brief wrong"
[ "$(jget "$(esc)" count)" = "3" ] && pass "[AC1] escalate.json count=3" \
    || fail "[AC1] escalate.json count wrong"
echo "$(jget "$(esc)" failure_line)" | grep -q "REFUSED" && pass "[AC1] escalate.json failure_line carries the receipt" \
    || fail "[AC1] escalate.json failure_line missing receipt"
echo "$(jget "$(esc)" reason)" | grep -q "REFUSED" && pass "[AC1] escalate.json reason (notify text) carries receipt" \
    || fail "[AC1] escalate.json reason missing receipt"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC4: already-escalated does NOT re-raise                        ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC4: already-escalated → no re-raise ─────────────────────────"
# Continue from AC1's escalated state.
ESC_BEFORE=$(cat "$(esc)")
record_failure_and_maybe_escalate "auto-merge-gate" "brief-x" "$R"; RC4=$?
record_failure_and_maybe_escalate "auto-merge-gate" "brief-x" "$R"; RC5=$?
[ "$RC4" -eq 11 ] && [ "$RC5" -eq 11 ] && pass "[AC4] subsequent identical failures return 11 (suppress)" \
    || fail "[AC4] suppress rc wrong (rc4=$RC4 rc5=$RC5)"
[ "$(count_events)" = "1" ] && pass "[AC4] still exactly one escalation event (no re-raise)" \
    || fail "[AC4] escalation re-raised (event count=$(count_events))"
[ "$(cat "$(esc)")" = "$ESC_BEFORE" ] && pass "[AC4] escalate.json unchanged across suppressed ticks" \
    || fail "[AC4] escalate.json rewritten on suppressed tick"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC2: a DIFFERENT reason resets the counter                     ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC2: different reason resets the counter ─────────────────────"
make_state "$TMP/ac2"
ESCALATE_AFTER_FAILURES=3
record_failure_and_maybe_escalate "dispatch" "brief-y" "reason-A"; :
record_failure_and_maybe_escalate "dispatch" "brief-y" "reason-A"; :
[ "$(kcount dispatch brief-y)" = "2" ] && pass "[AC2] counter=2 after two identical" \
    || fail "[AC2] counter wrong (=$(kcount dispatch brief-y))"
record_failure_and_maybe_escalate "dispatch" "brief-y" "reason-B-DIFFERENT"; RCB=$?
[ "$(kcount dispatch brief-y)" = "1" ] && pass "[AC2] different reason resets counter to 1" \
    || fail "[AC2] counter not reset (=$(kcount dispatch brief-y))"
[ "$RCB" -eq 0 ] && pass "[AC2] reset failure returns 0 (no escalation)" \
    || fail "[AC2] reset failure escalated unexpectedly (rc=$RCB)"
[ ! -f "$(esc)" ] && pass "[AC2] no escalate.json after a reset" \
    || fail "[AC2] escalate.json written despite reset"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC3: success at the site resets the counter                    ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC3: success resets the counter ──────────────────────────────"
make_state "$TMP/ac3"
ESCALATE_AFTER_FAILURES=3
record_failure_and_maybe_escalate "auto-merge-gate" "brief-z" "R"; :
record_failure_and_maybe_escalate "auto-merge-gate" "brief-z" "R"; :
[ "$(kcount auto-merge-gate brief-z)" = "2" ] && pass "[AC3] counter=2 before success" \
    || fail "[AC3] counter wrong before success"
clear_failure_fingerprint "auto-merge-gate" "brief-z"
[ "$(kcount auto-merge-gate brief-z)" = "0" ] && pass "[AC3] success clears the fingerprint" \
    || fail "[AC3] fingerprint not cleared (=$(kcount auto-merge-gate brief-z))"
# A single subsequent failure must be count=1, not 3.
record_failure_and_maybe_escalate "auto-merge-gate" "brief-z" "R"; RCZ=$?
[ "$(kcount auto-merge-gate brief-z)" = "1" ] && [ "$RCZ" -eq 0 ] \
    && pass "[AC3] post-success failure starts fresh at 1 (no escalation)" \
    || fail "[AC3] streak not reset after success (=$(kcount auto-merge-gate brief-z), rc=$RCZ)"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC5: delivered-gate site (tonight's class) — simulated loop     ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC5: delivered-gate refusal loop (issue #15 receipt) ─────────"
make_state "$TMP/ac5"
ESCALATE_AFTER_FAILURES=3
BRIEF="serve-004-dead-registry-entries"
REFUSAL="delivered-gate: REFUSED — ${BRIEF} delivered['nt-runway'] = 'https://github.com/new-theory-research/nt-runway/commit/90a02501' is not verifiable on the remote: gh api ... exit 1"
ESCALATED_AT=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13; do
    record_failure_and_maybe_escalate "auto-merge-gate" "$BRIEF" "$REFUSAL"
    rc=$?
    if [ "$rc" -eq 10 ] && [ "$ESCALATED_AT" -eq 0 ]; then ESCALATED_AT=$i; fi
done
[ "$ESCALATED_AT" -eq 3 ] && pass "[AC5] escalation fires on the 3rd refusal (not the 13th, not never)" \
    || fail "[AC5] escalation fired at $ESCALATED_AT (expected 3)"
[ "$(count_events)" = "1" ] && pass "[AC5] the 13-refusal loop raises exactly ONE escalation" \
    || fail "[AC5] wrong escalation count over the loop (=$(count_events))"
[ "$(jget "$(esc)" brief)" = "$BRIEF" ] && pass "[AC5] escalate.json names the delivered-gate brief" \
    || fail "[AC5] escalate.json brief wrong"
echo "$(jget "$(esc)" failure_line)" | grep -q "delivered-gate: REFUSED" \
    && pass "[AC5] receipt is the actual delivered-gate REFUSED line" \
    || fail "[AC5] receipt missing the delivered-gate line"
[ "$(jget "$(esc)" human_action_required)" = "True" ] && pass "[AC5] escalation flags human_action_required" \
    || fail "[AC5] human_action_required not set"

# Mutation-discriminate: with a higher threshold the loop would NOT escalate at 3.
make_state "$TMP/ac5-mut"
ESCALATE_AFTER_FAILURES=10
for i in 1 2 3; do record_failure_and_maybe_escalate "auto-merge-gate" "$BRIEF" "$REFUSAL"; done
[ ! -f "$(esc)" ] && pass "[AC5-mut] threshold=10: 3 refusals do NOT escalate" \
    || fail "[AC5-mut] escalated at 3 despite threshold=10"
ESCALATE_AFTER_FAILURES=3

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC6: resolving escalate.json re-arms the fingerprint           ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC6: resolved escalation re-arms the fingerprint ─────────────"
make_state "$TMP/ac6"
ESCALATE_AFTER_FAILURES=3
for i in 1 2 3; do record_failure_and_maybe_escalate "sync" "-" "diverged 1/1"; done
[ -f "$(esc)" ] && pass "[AC6] escalated after 3 failures" || fail "[AC6] no escalation"
# Human resolves: move escalate.json aside (mirrors escalate.json.resolved-*).
mv "$(esc)" "$STATE_DIR/signals/escalate.json.resolved-test"
record_failure_and_maybe_escalate "sync" "-" "diverged 1/1"; RCR=$?
[ "$(kcount sync -)" = "1" ] && pass "[AC6] resolved escalation re-arms (counter back to 1)" \
    || fail "[AC6] did not re-arm after resolution (=$(kcount sync -))"
[ "$RCR" -eq 0 ] && pass "[AC6] re-armed failure returns 0 (fresh streak, no re-raise)" \
    || fail "[AC6] re-armed failure rc=$RCR (expected 0)"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Config knob presence                                           ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── Config knob ──────────────────────────────────────────────────"
grep -q 'ESCALATE_AFTER_FAILURES.*:-' "$DAEMON_SH" \
    && pass "[config] ESCALATE_AFTER_FAILURES has in-code default" \
    || fail "[config] ESCALATE_AFTER_FAILURES missing in-code default"

echo ""
echo "────────────────────────────────────────────────────────────────"
echo "repeat-failure-escalation: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
