#!/usr/bin/env bash
# Regression test for the ntfy notification policy (ntfy-notification-policy).
#
# Mattie's ruling (2026-07-12): ntfy should push only on brief lifecycle
# (started / escalated / completed) + a stuck-queue alarm. Everything else is
# class `ops`, silenced by default. notify() gained a CLASS first arg gated by
# the NTFY_EVENTS allowlist.
#
# House pattern (mirrors repeat-failure-escalation.sh): the daemon-side helpers
# are extracted VERBATIM from lib/daemon.sh and eval'd so the asserted logic
# cannot drift from the shipped code. `curl` is stubbed to record pushes.
#
# Covers:
#   AC1 — class filtering: allowlisted class sends; `ops` silenced by default;
#         empty NTFY_TOPIC is a no-op regardless of class; `ops` sends once
#         explicitly allowlisted.
#   AC2 — brief_started fires exactly ONCE per dispatch (on_dispatch_success),
#         raises the per-tick dispatch flag, and carries lane + slot.
#   AC3 — the shipped completion + dispatch sites carry the right classes
#         (structural: no bare notify, brief_completed x2, brief_started x1).
#   AC4 — queue_stuck fires exactly once at the threshold, latches (no re-fire),
#         and re-arms after a successful dispatch (_queue_stuck_rearm).
#   AC5 — the dedup counter file is gitignored.

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
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Extract the helpers from lib/daemon.sh verbatim ──────────────────────────
FUNC_NOTIFY=$(sed -n '/^notify() {/,/^}/p' "$DAEMON_SH")
FUNC_QS_TICK=$(sed -n '/^_queue_stuck_tick() {/,/^}/p' "$DAEMON_SH")
FUNC_QS_REARM=$(sed -n '/^_queue_stuck_rearm() {/,/^}/p' "$DAEMON_SH")
FUNC_ON_DISPATCH=$(sed -n '/^on_dispatch_success() {/,/^}/p' "$DAEMON_SH")
for fn in FUNC_NOTIFY FUNC_QS_TICK FUNC_QS_REARM FUNC_ON_DISPATCH; do
    if [ -z "${!fn}" ]; then
        fail "could not extract $fn from lib/daemon.sh"
        echo "FAILED: 1"; exit 1
    fi
done
eval "$FUNC_NOTIFY"
eval "$FUNC_QS_TICK"
eval "$FUNC_QS_REARM"
eval "$FUNC_ON_DISPATCH"

# ── Stubs ────────────────────────────────────────────────────────────────────
# Shadow curl with a shell function so notify's push is captured, not sent.
NTFY_OUT=""
curl() {
    local msg=""
    while [ "$#" -gt 0 ]; do
        case "$1" in -d) shift; msg="$1" ;; esac
        shift
    done
    NTFY_OUT="${NTFY_OUT}${msg}"$'\n'
    return 0
}
daemon_log() { :; }
PROJECT_NAME="test-project"
NTFY_DEFAULT="brief_started,brief_escalated,brief_completed,queue_stuck"

# ── AC1: class filtering ─────────────────────────────────────────────────────
NTFY_TOPIC="testtopic"
NTFY_EVENTS="$NTFY_DEFAULT"

NTFY_OUT=""; notify brief_started "started-msg"
grep -q "started-msg" <<<"$NTFY_OUT" && pass "AC1 allowlisted class (brief_started) sends" \
    || fail "AC1 allowlisted class (brief_started) sends"

NTFY_OUT=""; notify brief_completed "completed-msg"
grep -q "completed-msg" <<<"$NTFY_OUT" && pass "AC1 allowlisted class (brief_completed) sends" \
    || fail "AC1 allowlisted class (brief_completed) sends"

NTFY_OUT=""; notify ops "ops-msg"
[ -z "$(tr -d '[:space:]' <<<"$NTFY_OUT")" ] && pass "AC1 ops silenced by default" \
    || fail "AC1 ops silenced by default (got: $NTFY_OUT)"

NTFY_OUT=""; NTFY_TOPIC=""; notify brief_started "no-topic-msg"; NTFY_TOPIC="testtopic"
[ -z "$(tr -d '[:space:]' <<<"$NTFY_OUT")" ] && pass "AC1 empty NTFY_TOPIC is a no-op" \
    || fail "AC1 empty NTFY_TOPIC is a no-op (got: $NTFY_OUT)"

NTFY_OUT=""; NTFY_EVENTS="ops"; notify ops "ops-explicit"; NTFY_EVENTS="$NTFY_DEFAULT"
grep -q "ops-explicit" <<<"$NTFY_OUT" && pass "AC1 ops sends once explicitly allowlisted" \
    || fail "AC1 ops sends once explicitly allowlisted"

# Substring-safety: a class must match a whole token, not a substring.
NTFY_OUT=""; NTFY_EVENTS="brief_started"; notify start "substr-msg"; NTFY_EVENTS="$NTFY_DEFAULT"
[ -z "$(tr -d '[:space:]' <<<"$NTFY_OUT")" ] && pass "AC1 class match is whole-token, not substring" \
    || fail "AC1 class match is whole-token, not substring (got: $NTFY_OUT)"

# ── AC2: brief_started fires once per dispatch, with lane + slot ─────────────
RUNNING_FILE="$TMP/running.json"
QUEUE_STUCK_STATE="$TMP/qs.json"
LOOP_LANE=""
printf '{"active":[{"brief":"brief-a"},{"brief":"brief-x"}]}' > "$RUNNING_FILE"
_DISPATCHED_THIS_TICK=false
NTFY_OUT=""
on_dispatch_success "brief-x"
STARTED_N=$(grep -c "brief-x started" <<<"$NTFY_OUT")
[ "$STARTED_N" = "1" ] && pass "AC2 brief_started fires exactly once per dispatch" \
    || fail "AC2 brief_started fires exactly once per dispatch (n=$STARTED_N)"
grep -q "brief-x started (lane default, slot 1)" <<<"$NTFY_OUT" \
    && pass "AC2 brief_started carries lane + slot (slot 1, lane default)" \
    || fail "AC2 brief_started carries lane + slot (got: $NTFY_OUT)"
[ "$_DISPATCHED_THIS_TICK" = "true" ] && pass "AC2 on_dispatch_success raises the per-tick dispatch flag" \
    || fail "AC2 on_dispatch_success raises the per-tick dispatch flag"

# ── AC3: shipped sites carry honest classes (structural) ─────────────────────
BARE=$(grep -cE '\bnotify "' "$DAEMON_SH")
[ "$BARE" = "0" ] && pass "AC3 no bare (classless) notify call sites remain" \
    || fail "AC3 no bare notify call sites remain (found $BARE)"
COMPLETED_SITES=$(grep -c 'notify brief_completed ' "$DAEMON_SH")
[ "$COMPLETED_SITES" = "2" ] && pass "AC3 brief_completed at both completion sites (auto-merge + awaiting-review)" \
    || fail "AC3 brief_completed at both completion sites (found $COMPLETED_SITES)"
STARTED_SITES=$(grep -c 'notify brief_started ' "$DAEMON_SH")
[ "$STARTED_SITES" = "1" ] && pass "AC3 brief_started lives only in on_dispatch_success" \
    || fail "AC3 brief_started lives only in on_dispatch_success (found $STARTED_SITES)"
DISPATCH_CALLS=$(grep -c 'on_dispatch_success "' "$DAEMON_SH")
[ "$DISPATCH_CALLS" = "2" ] && pass "AC3 on_dispatch_success wired to both dispatch-success paths" \
    || fail "AC3 on_dispatch_success wired to both dispatch-success paths (found $DISPATCH_CALLS)"

# ── AC4: queue_stuck fires once, latches, and re-arms on dispatch ───────────
QUEUE_STUCK_TICKS=3
rm -f "$QUEUE_STUCK_STATE"

v1=$(_queue_stuck_tick true); v2=$(_queue_stuck_tick true); v3=$(_queue_stuck_tick true)
[ "$v1" = "silent" ] && [ "$v2" = "silent" ] && [ "$v3" = "notify" ] \
    && pass "AC4 queue_stuck fires at the QUEUE_STUCK_TICKS threshold" \
    || fail "AC4 queue_stuck fires at threshold (got $v1/$v2/$v3)"

v4=$(_queue_stuck_tick true); v5=$(_queue_stuck_tick true)
[ "$v4" = "silent" ] && [ "$v5" = "silent" ] \
    && pass "AC4 queue_stuck latches — no re-fire after the first alarm" \
    || fail "AC4 queue_stuck latches (got $v4/$v5)"

# Re-arm on dispatch, then it can alarm again.
_queue_stuck_rearm
r1=$(_queue_stuck_tick true); r2=$(_queue_stuck_tick true); r3=$(_queue_stuck_tick true)
[ "$r1" = "silent" ] && [ "$r2" = "silent" ] && [ "$r3" = "notify" ] \
    && pass "AC4 queue_stuck re-arms after a successful dispatch" \
    || fail "AC4 queue_stuck re-arms after dispatch (got $r1/$r2/$r3)"

# A non-stuck tick resets the running counter (fresh episode restarts count).
_queue_stuck_rearm
_queue_stuck_tick true >/dev/null            # count = 1
_queue_stuck_tick false >/dev/null           # reset → count = 0
n1=$(_queue_stuck_tick true); n2=$(_queue_stuck_tick true); n3=$(_queue_stuck_tick true)
[ "$n1" = "silent" ] && [ "$n2" = "silent" ] && [ "$n3" = "notify" ] \
    && pass "AC4 a non-stuck tick resets the consecutive counter" \
    || fail "AC4 non-stuck tick resets counter (got $n1/$n2/$n3)"

# on_dispatch_success also re-arms queue_stuck (dispatch clears a latched alarm).
_queue_stuck_tick true >/dev/null; _queue_stuck_tick true >/dev/null; _queue_stuck_tick true >/dev/null  # latch
_DISPATCHED_THIS_TICK=false
NTFY_OUT=""
on_dispatch_success "brief-x"
d1=$(_queue_stuck_tick true); d2=$(_queue_stuck_tick true); d3=$(_queue_stuck_tick true)
[ "$d3" = "notify" ] && pass "AC4 on_dispatch_success re-arms the queue_stuck latch" \
    || fail "AC4 on_dispatch_success re-arms the queue_stuck latch (got $d1/$d2/$d3)"

# ── AC5: dedup counter file is gitignored ────────────────────────────────────
if git -C "$REPO_ROOT" check-ignore -q .loop/state/queue-stuck-dedup.json; then
    pass "AC5 queue-stuck-dedup.json is gitignored"
else
    fail "AC5 queue-stuck-dedup.json is NOT gitignored"
fi

echo ""
echo "PASSED: $PASSED"
echo "FAILED: $FAILED"
[ "$FAILED" -eq 0 ]
