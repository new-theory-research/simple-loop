#!/usr/bin/env bash
# Regression test for rate/usage/session-limit classification + backoff (issue #81).
#
# Receipt (2026-07-12 00:44): the Max-plan session limit killed every claude
# invocation for 26 min; the daemon hot-looped through the generic retry path
# instead of sleeping to the stated reset, because the session-limit error text
# ("You've hit your session limit · resets 1:10am") did not match the narrow
# `out of extra usage` gate that routes into handle_rate_limit.
#
# Covers:
#   AC1 — every captured limit shape routes to the limit path (is_rate_limit_log).
#   AC2 — reset-time parse handles minutes ("1:10am") AND hour-only ("1am"),
#         am/pm conversion, and sleeps to reset+5min.
#   AC3 — no-reset backoff grows exponentially and CAPS (never hot-loops).
#   AC4 — a parseable reset clears the no-reset backoff streak.
#   AC5 — benign (non-limit) output does NOT route to the limit path.
#
# Functions under test are extracted VERBATIM from lib/daemon.sh via sed so the
# asserted logic cannot drift from the shipped code.

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_SH="$SCRIPT_DIR/../daemon.sh"

# ── Extract functions under test verbatim ────────────────────────────────────
FUNC_IS_LIMIT=$(sed -n '/^is_rate_limit_log() {/,/^}/p' "$DAEMON_SH")
FUNC_HANDLE=$(sed -n '/^handle_rate_limit() {/,/^}/p' "$DAEMON_SH")

for fn in FUNC_IS_LIMIT FUNC_HANDLE; do
    if [ -z "${!fn}" ]; then
        fail "could not extract $fn from lib/daemon.sh"
        echo "FAILED: 1"
        exit 1
    fi
done
eval "$FUNC_IS_LIMIT"
eval "$FUNC_HANDLE"

# ── Stubs ────────────────────────────────────────────────────────────────────
# wall_sleep records the requested nap instead of actually sleeping.
RECORDED_SLEEP=-1
wall_sleep() { RECORDED_SLEEP="$1"; }
daemon_log() { _LOG="${_LOG:-}${1}"$'\n'; }
notify() { :; }

# Config knobs the function reads.
RATE_LIMIT_BACKOFF_CAP_SECS=3600
RATE_LIMIT_BACKOFF_SECS=0

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC1: every captured limit shape routes to the limit path        ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC1: limit-shape classification ──────────────────────────────"

# The exact stderr captured from the 2026-07-12 incident plus the Max-plan
# variants the issue enumerates.
declare -a SHAPES=(
  "You've hit your session limit · resets 1:10am (America/Los_Angeles)"
  "Max usage limit reached"
  "Claude AI usage limit reached · resets 4pm"
  "You are out of extra usage. Upgrade your plan."
)
i=0
for shape in "${SHAPES[@]}"; do
    i=$((i + 1))
    f="$TMP/shape_$i.log"
    printf '%s\n' "$shape" > "$f"
    if is_rate_limit_log "$f"; then
        pass "[AC1] routed to limit path: '${shape:0:42}...'"
    else
        fail "[AC1] MISSED limit shape (would hot-loop): '$shape'"
    fi
done

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC2: reset-time parse (minutes + hour-only + am/pm)             ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC2: reset-time parse ────────────────────────────────────────"

# Independent oracle mirroring the function's date math, so we validate that
# minutes are actually parsed (not silently dropped as pre-#81).
expected_sleep() {
    local hour="$1" min="$2"
    local now reset
    now=$(date +%s)
    reset=$(date -v${hour}H -v${min}M -v0S +%s 2>/dev/null || date -d "today ${hour}:${min}:00" +%s 2>/dev/null)
    [ "$reset" -le "$now" ] && reset=$((reset + 86400))
    echo $(( reset - now + 300 ))
}

# abs-diff tolerance for the ~1s clock skew between the call and the oracle.
close() {
    local a="$1" b="$2" tol="$3" d
    d=$((a - b)); [ "$d" -lt 0 ] && d=$((-d))
    [ "$d" -le "$tol" ]
}

# minutes form: "resets 1:10am" → hour 1, minute 10.
f="$TMP/reset_110am.log"
printf 'You have hit your session limit · resets 1:10am (America/Los_Angeles)\n' > "$f"
RATE_LIMIT_BACKOFF_SECS=5   # will be cleared by a parseable reset
RECORDED_SLEEP=-1
handle_rate_limit "$f"
EXP=$(expected_sleep 1 10)
if close "$RECORDED_SLEEP" "$EXP" 3; then
    pass "[AC2] 'resets 1:10am' → slept ${RECORDED_SLEEP}s (oracle ${EXP}s)"
else
    fail "[AC2] 'resets 1:10am' → slept ${RECORDED_SLEEP}s, expected ~${EXP}s"
fi

# hour-only form still works: "resets 1am" → hour 1, minute 0.
f="$TMP/reset_1am.log"
printf 'usage limit reached · resets 1am\n' > "$f"
RECORDED_SLEEP=-1
handle_rate_limit "$f"
EXP=$(expected_sleep 1 0)
if close "$RECORDED_SLEEP" "$EXP" 3; then
    pass "[AC2] 'resets 1am' → slept ${RECORDED_SLEEP}s (oracle ${EXP}s)"
else
    fail "[AC2] 'resets 1am' → slept ${RECORDED_SLEEP}s, expected ~${EXP}s"
fi

# Mutation-discriminate: minutes must actually change the nap. "1:10am" and
# "1am" resolve to reset times 10 min apart, so their naps must differ by ~600s.
printf 'resets 1:10am\n' > "$TMP/m1.log"; printf 'resets 1am\n' > "$TMP/m2.log"
handle_rate_limit "$TMP/m1.log"; A="$RECORDED_SLEEP"
handle_rate_limit "$TMP/m2.log"; B="$RECORDED_SLEEP"
DIFF=$((A - B)); [ "$DIFF" -lt 0 ] && DIFF=$((-DIFF))
if close "$DIFF" 600 3; then
    pass "[AC2] minutes parsed: 1:10am nap − 1am nap = ${DIFF}s (~600s)"
else
    fail "[AC2] minutes NOT parsed: diff ${DIFF}s (expected ~600s)"
fi

# pm conversion: "resets 4:30pm" → hour 16, minute 30.
printf 'resets 4:30pm\n' > "$TMP/pm.log"
RECORDED_SLEEP=-1
handle_rate_limit "$TMP/pm.log"
EXP=$(expected_sleep 16 30)
if close "$RECORDED_SLEEP" "$EXP" 3; then
    pass "[AC2] 'resets 4:30pm' → slept ${RECORDED_SLEEP}s (oracle ${EXP}s)"
else
    fail "[AC2] 'resets 4:30pm' → slept ${RECORDED_SLEEP}s, expected ~${EXP}s"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC3: no-reset backoff grows and caps (never hot-loops)          ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC3: no-reset exponential backoff, capped ────────────────────"

printf 'You have hit your session limit but no time is given here\n' > "$TMP/noreset.log"
RATE_LIMIT_BACKOFF_CAP_SECS=300
RATE_LIMIT_BACKOFF_SECS=0

expect_backoff() {
    local want="$1"
    RECORDED_SLEEP=-1
    handle_rate_limit "$TMP/noreset.log"
    if [ "$RECORDED_SLEEP" -eq "$want" ]; then
        pass "[AC3] no-reset backoff = ${RECORDED_SLEEP}s"
    else
        fail "[AC3] no-reset backoff = ${RECORDED_SLEEP}s (expected ${want}s)"
    fi
}
expect_backoff 60     # first hit: floor
expect_backoff 120    # double
expect_backoff 240    # double
expect_backoff 300    # 480 would exceed cap → clamp to 300
expect_backoff 300    # stays capped, never hot-loops (0s)

# The key invariant: a no-reset limit NEVER sleeps 0 (the hot-loop bug).
if [ "$RECORDED_SLEEP" -gt 0 ]; then
    pass "[AC3] no-reset nap is always > 0 (no hot-loop)"
else
    fail "[AC3] no-reset nap was 0 — HOT LOOP"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC4: a parseable reset clears the no-reset backoff streak       ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC4: parseable reset clears the backoff streak ───────────────"

RATE_LIMIT_BACKOFF_SECS=240   # a streak is in progress
printf 'session limit · resets 3am\n' > "$TMP/clears.log"
handle_rate_limit "$TMP/clears.log"
if [ "$RATE_LIMIT_BACKOFF_SECS" -eq 0 ]; then
    pass "[AC4] reset-time parse reset RATE_LIMIT_BACKOFF_SECS to 0"
else
    fail "[AC4] backoff streak not cleared (got $RATE_LIMIT_BACKOFF_SECS)"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC5: benign output does NOT route to the limit path             ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC5: no false positives ──────────────────────────────────────"

printf 'Committed 2 files. All tests green. Iteration complete.\n' > "$TMP/benign.log"
if is_rate_limit_log "$TMP/benign.log"; then
    fail "[AC5] benign agent output misclassified as a limit"
else
    pass "[AC5] benign agent output NOT routed to limit path"
fi

# A missing log file must not match (and must not error under set -e-ish flags).
if is_rate_limit_log "$TMP/does-not-exist.log"; then
    fail "[AC5] missing log file matched the limit path"
else
    pass "[AC5] missing log file → no match"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Config: RATE_LIMIT_BACKOFF_CAP_SECS has an in-code default      ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── Config knob ──────────────────────────────────────────────────"
if grep -q 'RATE_LIMIT_BACKOFF_CAP_SECS.*:-' "$DAEMON_SH"; then
    pass "[config] RATE_LIMIT_BACKOFF_CAP_SECS has in-code default"
else
    fail "[config] RATE_LIMIT_BACKOFF_CAP_SECS missing in-code default"
fi

echo ""
echo "────────────────────────────────────────────────────────────────"
echo "rate-limit-classification: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
