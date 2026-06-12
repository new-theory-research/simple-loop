#!/usr/bin/env bash
# Regression test for the WORKER_PARALLEL registry + reaper + single-flight
# gating (daemon.sh: worker_already_running / brief_is_solo / solo_worker_live
# / live_worker_count / reap_parallel_workers).
#
# The functions under test are EXTRACTED from lib/daemon.sh at run time (not
# copy-pasted) so the asserted logic can never drift from the daemon. daemon.sh
# itself isn't sourceable (its tail runs the tick loop), so we slice out just
# the parallel-worker helper block between its banner and the next banner.
#
# Invariants asserted (the WHY, not just the WHAT):
#   - one worker per brief: worker_already_running blocks a respawn while a
#     PID is live, and stops blocking once it dies.
#   - THROTTLE cap: live_worker_count counts only live PIDs.
#   - solo isolation: brief_is_solo reads parallel_safe from running.json;
#     solo_worker_live detects a live solo worker so Phase 3 can hold the rest.
#   - reaper accounting: a reaped exit 0 resets CONSECUTIVE_WORKER_FAILURES;
#     a nonzero increments it (the one piece the backgrounded subshell can't
#     mutate in the parent).

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$HERE/../daemon.sh"
[ -f "$DAEMON" ] || { echo "daemon.sh not found at $DAEMON"; exit 1; }

# Slice the parallel-worker helper block out of daemon.sh and source it.
HELPERS="$(awk '
  /Parallel worker execution \(WORKER_PARALLEL=true\)/ { grab=1 }
  grab { print }
  grab && /^spawn_parallel_worker\(\)/ { inspawn=1 }
  inspawn && /^}/ { exit }
' "$DAEMON")"

# Stubs for daemon globals the helpers reference.
daemon_log() { :; }
WORKER_PARALLEL="true"
THROTTLE=2
CONSECUTIVE_WORKER_FAILURES=0
WP_PIDS=()
WP_BRIEFS=()

eval "$HELPERS"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
RUNNING_FILE="$TMP/running.json"

# ── live_worker_count + worker_already_running ────────────────────────────────
sleep 5 & LIVE1=$!
sleep 5 & LIVE2=$!
WP_PIDS=("$LIVE1" "$LIVE2")
WP_BRIEFS=("brief-a" "brief-b")

[ "$(live_worker_count)" = "2" ] && pass "live_worker_count counts two live workers" \
    || fail "live_worker_count expected 2, got $(live_worker_count)"

worker_already_running "brief-a" && pass "worker_already_running true for live brief-a" \
    || fail "worker_already_running should be true for live brief-a"

worker_already_running "brief-z" && fail "worker_already_running should be false for absent brief-z" \
    || pass "worker_already_running false for absent brief-z"

# Kill one; its slot should stop counting as live.
kill "$LIVE1" 2>/dev/null; wait "$LIVE1" 2>/dev/null
[ "$(live_worker_count)" = "1" ] && pass "live_worker_count drops to 1 after a worker dies" \
    || fail "live_worker_count expected 1 after kill, got $(live_worker_count)"

worker_already_running "brief-a" && fail "worker_already_running should be false after brief-a died" \
    || pass "worker_already_running false after brief-a died"

# ── brief_is_solo (reads running.json) ────────────────────────────────────────
cat > "$RUNNING_FILE" <<'JSON'
{"active": [
  {"brief": "brief-solo", "parallel_safe": false},
  {"brief": "brief-par", "parallel_safe": true}
]}
JSON

brief_is_solo "brief-solo" && pass "brief_is_solo true for parallel_safe:false" \
    || fail "brief_is_solo should be true for parallel_safe:false"

brief_is_solo "brief-par" && fail "brief_is_solo should be false for parallel_safe:true" \
    || pass "brief_is_solo false for parallel_safe:true"

brief_is_solo "brief-missing" && pass "brief_is_solo true (fail-safe) for absent brief" \
    || fail "brief_is_solo should fail-safe to solo for absent brief"

# ── solo_worker_live ──────────────────────────────────────────────────────────
sleep 5 & SOLO=$!
WP_PIDS=("$SOLO")
WP_BRIEFS=("brief-solo")
solo_worker_live && pass "solo_worker_live true when a live worker is solo" \
    || fail "solo_worker_live should be true for a live solo worker"

WP_BRIEFS=("brief-par")
solo_worker_live && fail "solo_worker_live should be false for a live parallel worker" \
    || pass "solo_worker_live false for a live parallel worker"
kill "$SOLO" 2>/dev/null; wait "$SOLO" 2>/dev/null

# ── reaper: exit-code → consecutive-failure accounting ────────────────────────
# A short-lived success and a short-lived failure, both already exited.
( exit 0 ) & OK=$!
( exit 7 ) & BAD=$!
wait "$OK" 2>/dev/null; wait "$BAD" 2>/dev/null

CONSECUTIVE_WORKER_FAILURES=3
WP_PIDS=("$OK")
WP_BRIEFS=("brief-ok")
reap_parallel_workers
[ "$CONSECUTIVE_WORKER_FAILURES" = "0" ] && pass "reaper resets failures on exit 0" \
    || fail "reaper should reset CONSECUTIVE_WORKER_FAILURES to 0, got $CONSECUTIVE_WORKER_FAILURES"
[ "${#WP_PIDS[@]}" = "0" ] && pass "reaper drops the finished worker from the registry" \
    || fail "reaper should empty the registry, got ${#WP_PIDS[@]} entries"

CONSECUTIVE_WORKER_FAILURES=1
WP_PIDS=("$BAD")
WP_BRIEFS=("brief-bad")
reap_parallel_workers
[ "$CONSECUTIVE_WORKER_FAILURES" = "2" ] && pass "reaper increments failures on nonzero exit" \
    || fail "reaper should increment to 2, got $CONSECUTIVE_WORKER_FAILURES"

# ── reaper retains a still-live worker across a tick ───────────────────────────
sleep 5 & STILL=$!
WP_PIDS=("$STILL")
WP_BRIEFS=("brief-still")
reap_parallel_workers
[ "${#WP_PIDS[@]}" = "1" ] && pass "reaper retains a still-live worker" \
    || fail "reaper should keep the live worker, got ${#WP_PIDS[@]} entries"
kill "$STILL" 2>/dev/null; wait "$STILL" 2>/dev/null

echo ""
echo "  $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
