#!/bin/bash
# Simple Loop Daemon — heartbeat loop for autonomous agent work
#
# Usage: bash lib/daemon.sh <project_dir> [heartbeat_seconds]
#
# Architecture:
#   Each tick: assess state → run queen or worker → push → sleep.
#   Queen: reads state, decides what to do (evaluate, dispatch, idle).
#   Worker: does ONE task from the active brief, commits, exits.
#   Both run as fresh Claude Code sessions. No long-lived processes.
#
# Model tier policy (2026-04-21, revised from brief-003 baseline):
#   heartbeat = haiku  (reserved — no heartbeat Claude calls today; assess.py is pure Python)
#   queen     = opus   (substantive state-transition reasoning, taste calls)
#   validator = sonnet (spec-fit review — Sonnet is sufficient; opt up per-brief if needed)
#   worker    = per-brief from **Model:** frontmatter, default sonnet
#                (set **Model:** opus in brief frontmatter for hard work)
# Any new `claude` invocation in this file MUST name a --model flag explicitly.

set -uo pipefail

PROJECT_DIR="${1:?Usage: daemon.sh <project_dir> [heartbeat_seconds]}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
LOOP_DIR="$PROJECT_DIR/.loop"

# Source project config. config.sh is tracked (project defaults);
# config.local.sh is gitignored and holds secrets (CLOUDFLARE_API_TOKEN, etc).
# Local overlay sourced AFTER config.sh so local values win. Projects create
# config.local.sh on demand; simple-loop does not ship it. Never commit secrets.
[ -f "$LOOP_DIR/config.sh" ] && source "$LOOP_DIR/config.sh"
[ -f "$LOOP_DIR/config.local.sh" ] && source "$LOOP_DIR/config.local.sh"

# brief-151 / multi-lane-daemon: optional program-lane partition. `--lane <spec>`
# (or the LOOP_LANE env / config var) restricts this daemon to dispatching briefs
# whose card Program: is in <spec>. <spec> is a comma-separated lane list —
# `--lane finetune,capture,fleets` (portal laptop owns several lanes) or a single
# `--lane remote-queens`. Unset/empty → no lane filter → single-daemon behavior
# byte-for-byte unchanged. Scan the args after project_dir so the optional
# heartbeat positional still works alongside --lane in any order; CLI --lane wins
# over the env/config LOOP_LANE.
shift || true
_CLI_LANE=""
_HEARTBEAT_ARG=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --lane)   shift; _CLI_LANE="${1:-}" ;;
        --lane=*) _CLI_LANE="${1#--lane=}" ;;
        *)        [ -z "$_HEARTBEAT_ARG" ] && _HEARTBEAT_ARG="$1" ;;
    esac
    shift
done
LOOP_LANE="${_CLI_LANE:-${LOOP_LANE:-}}"
LOOP_LANE="${LOOP_LANE// /}"   # strip spaces: unquoted $_LANE_OPT must stay one word (bash 3.2, no arrays) — a spaced list ("a, b") otherwise leaks the tail as a silently-ignored positional and lane-splits daemon vs projection
export LOOP_LANE
# Reusable CLI fragment for lane-aware queue.py invocations. Empty (expands to
# nothing) when no lane is set; word-splits into `--lane <spec>` otherwise.
# Lane specs are program slugs joined by commas (`finetune,capture,fleets`) with
# no spaces, so unquoted expansion splits into exactly two words — commas are not
# in IFS, so the whole comma-list rides through as one arg. This keeps the fragment
# comma-safe and sidesteps the bash-3.2 empty-array `set -u` trap.
_LANE_OPT=""
[ -n "$LOOP_LANE" ] && _LANE_OPT="--lane $LOOP_LANE"

STATE_DIR="$LOOP_DIR/state"
SIGNALS_DIR="$STATE_DIR/signals"
LOG_DIR="$LOOP_DIR/logs"
PID_FILE="$STATE_DIR/daemon.pid"
METRICS_FILE="$STATE_DIR/metrics.jsonl"
RUNNING_FILE="$STATE_DIR/running.json"
CONDUCTOR_PROMPT="$LOOP_DIR/prompts/queen.md"
WORKER_PROMPT="$LOOP_DIR/prompts/worker.md"
# Validator default agent spec (per-brief **Validator:** override lands in task 5).
VALIDATOR_AGENT_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/core/agents/reviewer.md"

HEARTBEAT_INTERVAL="${_HEARTBEAT_ARG:-${HEARTBEAT_INTERVAL:-300}}"
WORKER_COOLDOWN="${WORKER_COOLDOWN:-30}"
MAX_ITERATIONS="${MAX_ITERATIONS:-20}"
NTFY_TOPIC="${SIMPLE_LOOP_NTFY_TOPIC:-${NTFY_TOPIC:-}}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_MAIN_BRANCH="${GIT_MAIN_BRANCH:-main}"
MAX_COMMITS_BEHIND="${MAX_COMMITS_BEHIND:-30}"
MAX_CYCLE_WALL_TIME_SECS="${MAX_CYCLE_WALL_TIME_SECS:-5400}"
WORKER_KILL_GRACE_SECS="${WORKER_KILL_GRACE_SECS:-10}"
# Validator wall-time watchdog (issue #32): the validator's claude call runs
# synchronously in the main tick loop with no bound of its own. Default is
# generous — a validator review is a single Claude call, not an iterated
# worker cycle — but a hung network call (receipt: 2026-07-02, "API Error:
# Unable to connect" for 1467s) must not be allowed to stall the whole daemon.
VALIDATOR_WALL_TIME_SECS="${VALIDATOR_WALL_TIME_SECS:-1800}"
# Queen circuit breaker (receipts: 2026-07-02→09, Anthropic API degradation).
# A flaky upstream causes queen (claude -p opus) to hang 30+ min then exit-1;
# the daemon keeps firing them every tick and burns Opus continuously.
#   QUEEN_WALL_TIME_SECS   — kill a queen that runs longer than this (default 600s).
#                            A healthy queen takes 25–60s; 600s is a generous ceiling.
#   QUEEN_FAIL_THRESHOLD   — consecutive queen failures before the breaker trips
#                            and the daemon writes pause.json (default 3).
# Both are config-overridable via .loop/config.sh; in-code defaults apply when
# config.sh is absent or does not set the var, so an un-updated config still works.
QUEEN_WALL_TIME_SECS="${QUEEN_WALL_TIME_SECS:-600}"
QUEEN_FAIL_THRESHOLD="${QUEEN_FAIL_THRESHOLD:-3}"
# Health-probe timeout for auto-resume (lightweight ping, not a full queen).
# Short enough to not block the tick; long enough to survive a slow connect.
QUEEN_PROBE_TIMEOUT_SECS="${QUEEN_PROBE_TIMEOUT_SECS:-30}"
# TTL for conductor dedup cache entries. After this many seconds a cached
# trigger is treated as fresh, allowing re-evaluation of persistent conditions
# (e.g. stale_brief after a stuck worker exit). 30 min keeps dedup spam-free
# within a normal scout/conductor cadence while bounding worst-case stuck time.
CONDUCTOR_DEDUP_TTL_SECS="${CONDUCTOR_DEDUP_TTL_SECS:-1800}"

# Parallel worker execution (off by default → byte-identical legacy behavior).
# When "true", the tick spawns workers in the background and a reaper harvests
# finished ones at the start of each tick, so THROTTLE briefs iterate
# concurrently instead of alternating. THROTTLE caps concurrent workers.
WORKER_PARALLEL="${WORKER_PARALLEL:-false}"
THROTTLE="${THROTTLE:-1}"
# Strip any inline comment / non-digits a quoted config value may carry, then
# floor at 1 (matches actions.py dispatch throttle parsing).
THROTTLE="${THROTTLE%%#*}"
THROTTLE="$(printf '%s' "$THROTTLE" | tr -cd '0-9')"
[ -z "$THROTTLE" ] && THROTTLE=1
[ "$THROTTLE" -lt 1 ] && THROTTLE=1

# Tracking
CONSECUTIVE_SKIPS=0
CONSECUTIVE_WORKER_FAILURES=0
CONSECUTIVE_QUEEN_FAILURES=0

# Parallel-worker registry (daemon-process lifetime; reaped at each tick
# start). Parallel indexed arrays — bash 3.2 has no associative arrays and no
# `wait -n`, so the reaper polls PID liveness with `kill -0`. WP_PIDS[i] is the
# backgrounded `run_worker_iteration` subshell PID; WP_BRIEFS[i] its brief id.
WP_PIDS=()
WP_BRIEFS=()

# Find lib directory (co-located with this script)
DAEMON_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Version visibility (issue #41): read the installed SHA + date back from
# PROVENANCE.json (written by install.sh at install time) so the running
# daemon announces which commit it's actually on. Loud fallback — never a
# blank line — when the install predates PROVENANCE.json.
INSTALL_ROOT="$(dirname "$DAEMON_LIB_DIR")"
INSTALLED_VERSION_LINE=$(python3 -c "
import sys
sys.path.insert(0, '$DAEMON_LIB_DIR')
from actions import installed_version_line
print(installed_version_line('$INSTALL_ROOT'))
" 2>/dev/null)
[ -z "$INSTALLED_VERSION_LINE" ] && INSTALLED_VERSION_LINE="unknown (pre-VERSION install)"

# Ensure directories exist
mkdir -p "$STATE_DIR/signals" "$LOG_DIR"

# Ensure state files exist
[ -f "$RUNNING_FILE" ] || echo '{"active":[],"completed_pending_eval":[],"pending_merges":[],"awaiting_review":[]}' > "$RUNNING_FILE"

# brief-108-d: project running.json from cards + runtime-events.jsonl on every
# daemon start. This is idempotent — running it twice produces the same output
# — and replaces the legacy "running.json is hand-spliced by 4+ writers" model
# with single-writer ownership (lib/state.py:write_running_json). The
# startup_repair pass that follows seeds runtime-events.jsonl from running.json
# the first time after this brief lands.
# harness-001/003: pass --lane so a lane-scoped daemon's local running.json
# contains only its lane's active state. Empty LOOP_LANE → no filter (global).
python3 "$DAEMON_LIB_DIR/state.py" write-running-json "$PROJECT_DIR" ${LOOP_LANE:+--lane "$LOOP_LANE"} 2>/dev/null || true

echo ""
echo "╔══════════════════════════════════════╗"
echo "║       Simple Loop Daemon             ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Project:   ${PROJECT_NAME:-$(basename "$PROJECT_DIR")}"
echo "  Directory: $PROJECT_DIR"
echo "  Version:   $INSTALLED_VERSION_LINE"
echo "  Idle interval: ${HEARTBEAT_INTERVAL}s"
echo "  Worker cooldown: ${WORKER_COOLDOWN}s (max $MAX_ITERATIONS/brief)"
echo "  PID:       $$"
echo ""

# Kill existing daemon
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  Killing existing daemon (PID $OLD_PID)"
        kill -9 "$OLD_PID" 2>/dev/null
        sleep 1
    fi
fi
echo $$ > "$PID_FILE"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Helpers                                                        ║
# ╚══════════════════════════════════════════════════════════════════╝

daemon_log() {
    # bin-loop's `>> "$LOG_FILE" 2>&1` outer redirect already appends stdout
    # (and stderr) to daemon.log. `tee -a` here would double-write each line,
    # which made `loop logs -f` print every entry twice.
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Worker-scoped log line. Flag-off: emits the message verbatim (byte-identical
# to the legacy `daemon_log "WORKER: ..."` calls). Flag-on: rewrites a leading
# `WORKER:` token to `WORKER[<brief>]:` so interleaved background-worker output
# stays attributable. `_WLOG_BRIEF` is set at run_worker_iteration entry.
_WLOG_BRIEF=""
wlog() {
    if [ "$WORKER_PARALLEL" = "true" ] && [ -n "$_WLOG_BRIEF" ]; then
        case "$1" in
            WORKER:*) daemon_log "WORKER[$_WLOG_BRIEF]:${1#WORKER:}" ;;
            *)        daemon_log "$1" ;;
        esac
    else
        daemon_log "$1"
    fi
}

notify() {
    [ -z "$NTFY_TOPIC" ] && return
    local title="${PROJECT_NAME:-Simple Loop}"
    curl -s \
        -H "Title: $title" \
        -H "Priority: high" \
        -d "$1" \
        "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
}

# Parse metrics from Claude JSON output.
# Args: $1=json_file, $2=log_file, $3=source, $4=extra_fields (python dict literal)
parse_metrics() {
    local json_file="$1" log_file="$2" source="$3" extra="$4"
    python3 -c "
import json, sys, datetime
try:
    with open('$json_file') as f:
        data = json.load(f)
    with open('$log_file', 'a') as f:
        f.write(data.get('result', ''))

    def get_tokens(data, *keys):
        for k in keys:
            v = data.get(k, 0)
            if v: return v
        usage = data.get('usage', {})
        if isinstance(usage, dict):
            for k in keys:
                v = usage.get(k, 0)
                if v: return v
        return 0

    entry = {
        'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': '$source',
        'heartbeat': $TURN,
        'session_id': data.get('session_id', ''),
        'duration_ms': data.get('duration_ms', 0),
        'duration_api_ms': data.get('duration_api_ms', 0),
        'num_turns': data.get('num_turns', 0),
        'cost_usd': data.get('total_cost_usd', 0),
        'input_tokens': get_tokens(data, 'input_tokens', 'inputTokens'),
        'output_tokens': get_tokens(data, 'output_tokens', 'outputTokens'),
        'cache_read_tokens': get_tokens(data, 'cache_read_input_tokens', 'cache_read_tokens', 'cacheReadTokens'),
        'cache_write_tokens': get_tokens(data, 'cache_creation_input_tokens', 'cache_creation_tokens', 'cacheWriteTokens'),
        'is_error': data.get('is_error', False),
    }
    entry.update($extra)
    with open('$METRICS_FILE', 'a') as f:
        f.write(json.dumps(entry) + '\n')
except Exception as e:
    print(f'Metrics parse error: {e}', file=sys.stderr)
    with open('$json_file') as src, open('$log_file', 'a') as dst:
        dst.write(src.read())
" 2>>"$log_file"
}

# Handle rate limit: parse reset time, sleep until then.
handle_rate_limit() {
    local log_file="$1"
    RESET_INFO=$(grep -o "resets [0-9]*[ap]m" "$log_file" 2>/dev/null | head -1)
    if [ -n "$RESET_INFO" ]; then
        RESET_HOUR=$(echo "$RESET_INFO" | grep -o '[0-9]*')
        RESET_AMPM=$(echo "$RESET_INFO" | grep -o '[ap]m')
        if [ "$RESET_AMPM" = "pm" ] && [ "$RESET_HOUR" -ne 12 ]; then
            RESET_HOUR=$((RESET_HOUR + 12))
        elif [ "$RESET_AMPM" = "am" ] && [ "$RESET_HOUR" -eq 12 ]; then
            RESET_HOUR=0
        fi
        NOW_EPOCH=$(date +%s)
        RESET_TODAY=$(date -v${RESET_HOUR}H -v0M -v0S +%s 2>/dev/null || date -d "today ${RESET_HOUR}:00:00" +%s 2>/dev/null)
        if [ -n "$RESET_TODAY" ]; then
            if [ "$RESET_TODAY" -le "$NOW_EPOCH" ]; then
                RESET_TODAY=$((RESET_TODAY + 86400))
            fi
            SLEEP_SECS=$(( RESET_TODAY - NOW_EPOCH + 300 ))
            daemon_log "RATE LIMITED: sleeping $((SLEEP_SECS / 3600))h $(((SLEEP_SECS % 3600) / 60))m until ${RESET_HOUR}:00"
            notify "Rate limited — sleeping until ${RESET_HOUR}:00"
            sleep "$SLEEP_SECS"
            return
        fi
    fi
    daemon_log "RATE LIMITED: couldn't parse reset time. Sleeping 1h."
    sleep 3600
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  State Assessment                                               ║
# ╚══════════════════════════════════════════════════════════════════╝

assess_state() {
    python3 "$DAEMON_LIB_DIR/assess.py" "$PROJECT_DIR" 2>/dev/null || {
        echo "CONDUCTOR:error"
        echo "NONE"
    }
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Queen Invocation                                               ║
# ╚══════════════════════════════════════════════════════════════════╝

invoke_conductor() {
    local reason="$1"
    daemon_log "QUEEN #$TURN: invoking ($reason)"

    if [ ! -f "$CONDUCTOR_PROMPT" ]; then
        daemon_log "ERROR: queen prompt not found at $CONDUCTOR_PROMPT"
        local _qe_tmp _qe_ts
        _qe_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        _qe_tmp=$(mktemp)
        python3 -c "import json,sys; json.dump({'ts':sys.argv[1],'reason':sys.argv[2],'exit_code':1,'log_tail':''},sys.stdout)" \
            "$_qe_ts" "queen prompt not found at $CONDUCTOR_PROMPT" \
            > "$_qe_tmp" && mv "$_qe_tmp" "$STATE_DIR/last-queen-error.json" || rm -f "$_qe_tmp"
        return 1
    fi

    local TURN_LOG="$LOG_DIR/queen_${TURN}_$(date +%Y%m%d_%H%M%S).log"
    local TURN_START=$(date +%s)
    local JSON_TMP=$(mktemp)
    local QUEEN_TIMEOUT_FLAG
    QUEEN_TIMEOUT_FLAG=$(mktemp)
    rm -f "$QUEEN_TIMEOUT_FLAG"  # written by watchdog only if timeout fires

    cd "$PROJECT_DIR"

    # Tier: queen = opus (see top-of-file model tier policy).
    # Circuit breaker wall-time cap (receipts: 2026-07-02→09): background the
    # claude invocation so a watchdog can kill it if it hangs past
    # QUEEN_WALL_TIME_SECS (default 600s; healthy queen = 25–60s). Mirrors the
    # worker watchdog pattern at run_worker_iteration (~line 640).
    python3 -c "import os,sys; os.setpgrp(); os.execvp(sys.argv[1],sys.argv[1:])" \
        claude --model opus --dangerously-skip-permissions \
        --output-format json \
        -p "$(cat "$CONDUCTOR_PROMPT")

Trigger reason: $reason" \
        > "$JSON_TMP" 2>>"$TURN_LOG" &
    local QUEEN_PID
    QUEEN_PID=$!

    # Watchdog: fires after QUEEN_WALL_TIME_SECS, SIGTERM-grace-SIGKILL the
    # process group. Leaves QUEEN_TIMEOUT_FLAG so the return path below records
    # the kill as a failure rather than treating it as a normal exit-code.
    (
        sleep "$QUEEN_WALL_TIME_SECS"
        if kill -0 "$QUEEN_PID" 2>/dev/null; then
            touch "$QUEEN_TIMEOUT_FLAG"
            kill -TERM -"$QUEEN_PID" 2>/dev/null || kill -TERM "$QUEEN_PID" 2>/dev/null
            sleep "$WORKER_KILL_GRACE_SECS"
            kill -KILL -"$QUEEN_PID" 2>/dev/null || true
        fi
    ) &
    local QUEEN_WATCHDOG_PID
    QUEEN_WATCHDOG_PID=$!

    wait "$QUEEN_PID"
    local EXIT_CODE=$?

    # Cancel watchdog (no-op if already fired; avoids a dangling sleep).
    kill "$QUEEN_WATCHDOG_PID" 2>/dev/null
    wait "$QUEEN_WATCHDOG_PID" 2>/dev/null || true

    parse_metrics "$JSON_TMP" "$TURN_LOG" "queen" "{'reason': '$reason', 'exit_code': $EXIT_CODE}"
    rm -f "$JSON_TMP"

    local TURN_END=$(date +%s)
    local TURN_DURATION=$((TURN_END - TURN_START))

    # Wall-time kill: watchdog touched QUEEN_TIMEOUT_FLAG. Treat as failure.
    if [ -f "$QUEEN_TIMEOUT_FLAG" ]; then
        rm -f "$QUEEN_TIMEOUT_FLAG"
        daemon_log "QUEEN #$TURN: wall-time cap exceeded ${QUEEN_WALL_TIME_SECS}s — killed (${TURN_DURATION}s)"
        notify "Queen killed: wall-time cap (${QUEEN_WALL_TIME_SECS}s)"
        local _qe_tmp _qe_ts
        _qe_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        _qe_tmp=$(mktemp)
        python3 -c "import json,sys; json.dump({'ts':sys.argv[1],'reason':'wall-time cap exceeded (${QUEEN_WALL_TIME_SECS}s)','exit_code':1,'log_tail':''},sys.stdout)" \
            "$_qe_ts" \
            > "$_qe_tmp" && mv "$_qe_tmp" "$STATE_DIR/last-queen-error.json" || rm -f "$_qe_tmp"
        return 1
    fi
    rm -f "$QUEEN_TIMEOUT_FLAG"

    if [ "$EXIT_CODE" -ne 0 ]; then
        daemon_log "QUEEN #$TURN: FAILED (exit $EXIT_CODE, ${TURN_DURATION}s)"
        notify "Queen FAILED (exit $EXIT_CODE)"

        local _qe_tmp _qe_ts
        _qe_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        _qe_tmp=$(mktemp)
        python3 -c '
import json, sys, os
ts, reason, exit_code, log_file = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
log_tail = ""
if log_file and os.path.isfile(log_file):
    with open(log_file) as f:
        log_tail = "".join(f.readlines()[-10:])[:1900]
json.dump({"ts": ts, "reason": reason, "exit_code": exit_code, "log_tail": log_tail}, sys.stdout)
' "$_qe_ts" "claude exit $EXIT_CODE" "$EXIT_CODE" "$TURN_LOG" \
            > "$_qe_tmp" && mv "$_qe_tmp" "$STATE_DIR/last-queen-error.json" || rm -f "$_qe_tmp"

        if [ "$TURN_DURATION" -le 10 ] && grep -q "out of extra usage" "$TURN_LOG" 2>/dev/null; then
            handle_rate_limit "$TURN_LOG"
            return 1
        fi
        return 1
    else
        daemon_log "QUEEN #$TURN: complete (${TURN_DURATION}s)"
        rm -f "$STATE_DIR/last-queen-error.json"
        local _qs_tmp
        _qs_tmp=$(mktemp)
        printf '{"ts":"%s"}' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            > "$_qs_tmp" && mv "$_qs_tmp" "$STATE_DIR/last-queen-success.json" || rm -f "$_qs_tmp"

        # Brief-003 Thread 7: Auto-merge layer.
        # If the queen wrote an `escalate.json` with reason
        # `human_approval_required_for_merge`, check whether the brief opted in
        # via `**Auto-merge:** true` + validator verdict == `pass` + no
        # `.loop/state/pause-auto-merge` kill-switch. When all three hold, swap
        # escalate.json → pending-merge.json and log `auto_merge_approved`.
        # Any other escalation class (infra failure, validator_block, etc.)
        # still pages a human — auto-merge is strictly opt-in per brief.
        if [ -f "$SIGNALS_DIR/escalate.json" ]; then
            AM_OUT=$(python3 "$DAEMON_LIB_DIR/auto_merge.py" check-escalate "$PROJECT_DIR" 2>&1)
            AM_RC=$?
            if [ "$AM_RC" -eq 0 ] && [ -n "$AM_OUT" ]; then
                daemon_log "AUTO-MERGE: $AM_OUT"
            fi
        fi

        # Push after queen (it may have committed state changes). Non-fatal
        # but never silent (issue #19): a swallowed failure here strands a
        # loop: commit and the divergence only surfaces as next-tick SYNC
        # FAILED lines with no root cause attached.
        git -C "$PROJECT_DIR" push "$GIT_REMOTE" "$GIT_MAIN_BRANCH" -q 2>/dev/null \
            || daemon_log "QUEEN: state push failed — loop: commit stranded; next-tick sync will log/auto-heal"
    fi

    return 0
}

# ── Queen circuit-breaker helpers ─────────────────────────────────────────────
# Called by the Phase 2 main loop after invoke_conductor returns. Manages
# the consecutive-failure counter and the pause.json circuit-trip.

# Write the circuit-breaker pause signal. Shape matches the existing pause.json
# that directors write by hand — same file, same consumer (the pause check
# block in the main loop). The `"by": "circuit-breaker"` field lets the
# pause-check block distinguish circuit-breaker pauses from human-written ones
# so it can run the auto-resume probe instead of waiting for a human signal.
_cb_write_pause() {
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    mkdir -p "$SIGNALS_DIR"
    # Build JSON without embedded single quotes in the python -c string so
    # `eval` in tests can parse this function without quoting confusion.
    local threshold="$QUEEN_FAIL_THRESHOLD"
    local reason="circuit-breaker: ${threshold} consecutive queen failures"
    printf '{"reason":"%s","paused_at":"%s","by":"circuit-breaker","fail_threshold":%s}\n' \
        "$reason" "$ts" "$threshold" > "$SIGNALS_DIR/pause.json" 2>/dev/null \
    || printf '{"reason":"circuit-breaker","paused_at":"%s","by":"circuit-breaker"}\n' "$ts" \
        > "$SIGNALS_DIR/pause.json"
}

# Lightweight API health probe for auto-resume. Returns 0 = API healthy,
# non-zero = still degraded or result ambiguous. Rule 10 fail-safe: anything
# other than a clean success is treated as still-down so we never resume into
# a cascade. Uses haiku (cheapest model) with a short wall-time cap.
_cb_probe_api() {
    local probe_tmp probe_exit
    probe_tmp=$(mktemp)
    # Spawn with a PGID of its own so the kill-on-timeout reaches it cleanly.
    python3 -c "import os,sys; os.setpgrp(); os.execvp(sys.argv[1],sys.argv[1:])" \
        claude --model haiku --dangerously-skip-permissions \
        --output-format json \
        -p "Reply with the single word: pong" \
        > "$probe_tmp" 2>/dev/null &
    local probe_pid=$!

    # Watchdog: hard-kill after QUEEN_PROBE_TIMEOUT_SECS.
    (
        sleep "$QUEEN_PROBE_TIMEOUT_SECS"
        if kill -0 "$probe_pid" 2>/dev/null; then
            kill -KILL -"$probe_pid" 2>/dev/null || kill -KILL "$probe_pid" 2>/dev/null || true
        fi
    ) &
    local probe_watchdog_pid=$!

    wait "$probe_pid"
    probe_exit=$?
    kill "$probe_watchdog_pid" 2>/dev/null
    wait "$probe_watchdog_pid" 2>/dev/null || true
    rm -f "$probe_tmp"
    return "$probe_exit"
}

# Update the consecutive-failure counter and trip the circuit breaker if the
# threshold is reached. Call with "fail" or "success".
#   fail    — increment counter; if >= QUEEN_FAIL_THRESHOLD, write pause.json
#   success — reset counter to 0
_cb_record_queen_result() {
    local result="$1"
    if [ "$result" = "success" ]; then
        CONSECUTIVE_QUEEN_FAILURES=0
        return 0
    fi
    # fail path
    CONSECUTIVE_QUEEN_FAILURES=$((CONSECUTIVE_QUEEN_FAILURES + 1))
    daemon_log "CIRCUIT BREAKER: queen failure $CONSECUTIVE_QUEEN_FAILURES/$QUEEN_FAIL_THRESHOLD"
    if [ "$CONSECUTIVE_QUEEN_FAILURES" -ge "$QUEEN_FAIL_THRESHOLD" ]; then
        daemon_log "CIRCUIT BREAKER: threshold reached — writing pause.json, backing off"
        notify "Circuit breaker tripped: $CONSECUTIVE_QUEEN_FAILURES consecutive queen failures"
        _cb_write_pause
    fi
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Worker Iteration                                               ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── WIP auto-commit (issue #18) ──────────────────────────────────────────────
# A worker iteration that ends with uncommitted changes (wall-time kill, error,
# or a worker that simply didn't commit) leaves the worktree dirty — and a
# dirty worktree fails the NEXT dispatch's cycle-start rebase unconditionally,
# routing the brief to awaiting_review for state the harness itself created
# (brief-250, 2026-06-11: one uncommitted file, zero real conflicts). Commit
# the dirt to the brief branch as a labeled WIP commit; the next cycle's
# worker sees its own WIP in history and continues.
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

run_worker_iteration() {
    local brief_id="$1"
    local branch="$2"
    # Tag this function's WORKER: log lines with the brief id under
    # WORKER_PARALLEL (see wlog). No-op for the flag-off serial path.
    _WLOG_BRIEF="$brief_id"

    # Resolve worktree — create if needed
    local WORKTREE_DIR="$PROJECT_DIR/.loop/worktrees/$brief_id"

    if [ ! -d "$WORKTREE_DIR" ]; then
        wlog "WORKER: creating worktree for $brief_id"
        mkdir -p "$PROJECT_DIR/.loop/worktrees"

        if git -C "$PROJECT_DIR" show-ref --verify --quiet "refs/heads/$branch" 2>/dev/null; then
            # Brief-100: stale-branch guard. If the local branch is ≥ MAX_COMMITS_BEHIND
            # commits behind origin/main, it's the brief-067 phantom root cause — delete
            # and recreate from main so the worker starts from current state.
            git -C "$PROJECT_DIR" fetch "$GIT_REMOTE" "$GIT_MAIN_BRANCH" -q 2>/dev/null || true
            STALE_COUNT=$(git -C "$PROJECT_DIR" rev-list --count "$branch".."${GIT_REMOTE}/${GIT_MAIN_BRANCH}" 2>/dev/null || echo "0")
            if [ "$STALE_COUNT" -ge "$MAX_COMMITS_BEHIND" ]; then
                wlog "WORKER: stale-branch refused — $branch is $STALE_COUNT commits behind ${GIT_REMOTE}/${GIT_MAIN_BRANCH} (threshold $MAX_COMMITS_BEHIND) — deleting and recreating from main"
                git -C "$PROJECT_DIR" branch -D "$branch" -q 2>/dev/null || true
                git -C "$PROJECT_DIR" worktree add -b "$branch" "$WORKTREE_DIR" "${GIT_REMOTE}/${GIT_MAIN_BRANCH}" -q 2>/dev/null
            else
                git -C "$PROJECT_DIR" worktree add "$WORKTREE_DIR" "$branch" -q 2>/dev/null
            fi
        elif git -C "$PROJECT_DIR" show-ref --verify --quiet "refs/remotes/${GIT_REMOTE}/$branch" 2>/dev/null; then
            git -C "$PROJECT_DIR" worktree add "$WORKTREE_DIR" "$branch" -q 2>/dev/null
        else
            wlog "WORKER: creating branch $branch from $GIT_MAIN_BRANCH"
            git -C "$PROJECT_DIR" worktree add -b "$branch" "$WORKTREE_DIR" "$GIT_MAIN_BRANCH" -q 2>/dev/null
        fi

        if [ ! -d "$WORKTREE_DIR" ]; then
            wlog "WORKER ERROR: failed to create worktree for $branch"
            return 1
        fi
    fi

    wlog "WORKER: starting iteration for $brief_id in worktree"

    # Pull latest into worktree (doesn't touch main tree)
    if ! git -C "$WORKTREE_DIR" pull --ff-only "$GIT_REMOTE" "$branch" -q 2>/dev/null; then
        # Issue #19: don't swallow a diverged worktree. No auto-heal here —
        # the cycle-start rebase below handles staleness; this is visibility.
        # A missing remote ref (fresh branch, never pushed) stays quiet.
        local _wt_counts
        _wt_counts=$(git -C "$WORKTREE_DIR" rev-list --left-right --count "HEAD...${GIT_REMOTE}/${branch}" 2>/dev/null) || _wt_counts=""
        if [ -n "$_wt_counts" ]; then
            wlog "WORKER: SYNC FAILED for $branch worktree — diverged ($(printf '%s' "$_wt_counts" | awk '{print $1}') ahead / $(printf '%s' "$_wt_counts" | awk '{print $2}') behind)"
        fi
    fi

    # ── Snapshot dirty progress.json before rebase (issue #5) ────────────────
    # Workers sometimes leave .loop/state/progress.json uncommitted at cycle
    # end. A dirty working tree makes the cycle-start rebase below fail with a
    # false "conflict", routing the brief to awaiting_review unnecessarily.
    # Commit the snapshot so rebase has a clean tree; the post-rebase reset
    # logic below overrides progress.json if it belongs to a different brief.
    if [ -f "$WORKTREE_DIR/.loop/state/progress.json" ] && \
       ! git -C "$WORKTREE_DIR" diff --quiet HEAD -- .loop/state/progress.json 2>/dev/null; then
        git -C "$WORKTREE_DIR" add .loop/state/progress.json 2>/dev/null
        if git -C "$WORKTREE_DIR" commit -m "loop: snapshot progress.json before cycle-start rebase" -q 2>/dev/null; then
            wlog "WORKER: snapshotted dirty progress.json before rebase for $brief_id"
        fi
    fi

    # Belt-and-suspenders for issue #18: any remaining dirt (not just
    # progress.json) also fails the rebase below before a real conflict is
    # even evaluated. Commit it rather than manufacturing a rebase-block.
    commit_worktree_wip "$WORKTREE_DIR" "$brief_id" "before cycle-start rebase"
    # ─────────────────────────────────────────────────────────────────────────

    # ── Rebase onto main (cycle-start, Phase 1 of brief-061) ─────────────────
    # Each cycle starts on current main so merge-time staleness is bounded to
    # single-cycle wall-clock (~15 min). Conflict → abort + route to
    # awaiting_review; don't continue the cycle with a broken state.
    git -C "$WORKTREE_DIR" fetch "$GIT_REMOTE" "$GIT_MAIN_BRANCH" -q 2>/dev/null || true
    COMMITS_BEHIND_BEFORE=$(git -C "$WORKTREE_DIR" rev-list --count HEAD.."${GIT_REMOTE}/${GIT_MAIN_BRANCH}" 2>/dev/null || echo "0")
    if git -C "$WORKTREE_DIR" rebase "${GIT_REMOTE}/${GIT_MAIN_BRANCH}" -q 2>/dev/null; then
        wlog "WORKER: rebased $branch onto ${GIT_REMOTE}/${GIT_MAIN_BRANCH} ($COMMITS_BEHIND_BEFORE commits)"
    else
        # Issue #55: a conflict set of exactly .loop/state/progress.json is
        # transient loop bookkeeping (rewritten on every dispatch), never
        # load-bearing branch content. Auto-resolve in main's favor and
        # continue the rebase instead of false-parking the brief
        # (ft-011 2026-07-10, capture-005-cont 2026-07-11). Any other
        # conflicting file still parks below.
        local CONFLICT_FILES
        local REBASE_RECOVERED=0
        CONFLICT_FILES=$(git -C "$WORKTREE_DIR" diff --name-only --diff-filter=U 2>/dev/null)
        if [ "$CONFLICT_FILES" = ".loop/state/progress.json" ]; then
            git -C "$WORKTREE_DIR" checkout --ours -- .loop/state/progress.json 2>/dev/null
            git -C "$WORKTREE_DIR" add .loop/state/progress.json 2>/dev/null
            # Note: `-q` is not accepted alongside `--continue` (git errors
            # with usage text) — redirect instead of relying on -q here.
            if git -C "$WORKTREE_DIR" rebase --continue >/dev/null 2>&1; then
                REBASE_RECOVERED=1
                wlog "WORKER[$brief_id]: auto-resolved progress.json-only rebase conflict in main's favor"
                wlog "WORKER: rebased $branch onto ${GIT_REMOTE}/${GIT_MAIN_BRANCH} ($COMMITS_BEHIND_BEFORE commits)"
            fi
        fi
        if [ "$REBASE_RECOVERED" != "1" ]; then
            git -C "$WORKTREE_DIR" rebase --abort 2>/dev/null || true
            wlog "WORKER: rebase failed for $branch (conflicts) → routed to awaiting_review"
            python3 "$DAEMON_LIB_DIR/actions.py" move-to-awaiting-review "$brief_id" "$PROJECT_DIR" \
                rebase-blocked "rebase conflict against main — human resolution required" \
                2>>"$LOG_DIR/daemon.log" || true
            notify "$brief_id: rebase conflict → routed to awaiting_review"
            return 0
        fi
    fi
    # ─────────────────────────────────────────────────────────────────────────

    # Reset progress.json if missing OR if it belongs to a different brief (brief-124 Bug 1).
    # Rebase can pull in the last-merged brief's progress.json from main — always reset
    # when the brief field doesn't match the dispatched brief.
    local PROGRESS_FILE="$WORKTREE_DIR/.loop/state/progress.json"
    local existing_brief=""
    if [ -f "$PROGRESS_FILE" ]; then
        existing_brief=$(python3 -c "import json; print(json.load(open('$PROGRESS_FILE')).get('brief',''))" 2>/dev/null || echo "")
    fi
    if [ ! -f "$PROGRESS_FILE" ] || [ "$existing_brief" != "$brief_id" ]; then
        local brief_file
        brief_file=$(python3 -c "
import json
with open('$RUNNING_FILE') as f:
    rc = json.load(f)
for b in rc.get('active', []):
    if b.get('brief') == '$brief_id':
        print(b.get('brief_file', ''))
        break
" 2>/dev/null)

        if [ -z "$brief_file" ] || [ ! -f "$WORKTREE_DIR/$brief_file" ]; then
            wlog "WORKER: no brief file found for $brief_id — skipping"
            return 0
        fi

        RESET_RESULT=$(python3 "$DAEMON_LIB_DIR/actions.py" ensure-progress-for-brief \
            "$brief_id" "$PROJECT_DIR" "$brief_file" "$PROGRESS_FILE" 2>/dev/null || echo "initialized")
        case "$RESET_RESULT" in
            initialized)
                wlog "WORKER: initialized progress.json for $brief_id"
                ;;
            reset:*)
                wlog "WORKER: reset progress.json for $brief_id (was: ${RESET_RESULT#reset:} — rebase inheritance)"
                ;;
        esac
        git -C "$WORKTREE_DIR" add ".loop/state/progress.json"
        git -C "$WORKTREE_DIR" commit -m "loop: reset progress.json for $brief_id (was: ${existing_brief:-missing})" -q 2>/dev/null
    fi

    # Safety: check iteration count
    local iteration
    iteration=$(python3 -c "import json; print(json.load(open('$PROGRESS_FILE')).get('iteration', 0))" 2>/dev/null || echo "0")
    if [ "$iteration" -ge "$MAX_ITERATIONS" ]; then
        wlog "WORKER: max iterations ($MAX_ITERATIONS) reached — marking blocked"
        python3 -c "
import json
with open('$PROGRESS_FILE') as f:
    p = json.load(f)
p['status'] = 'blocked'
p['learnings'] = p.get('learnings', []) + ['Daemon: max iterations ($MAX_ITERATIONS) reached.']
with open('$PROGRESS_FILE', 'w') as f:
    json.dump(p, f, indent=2)
"
        git -C "$WORKTREE_DIR" add ".loop/state/progress.json"
        git -C "$WORKTREE_DIR" commit -m "Max iterations reached — marking blocked" -q 2>/dev/null
        git -C "$WORKTREE_DIR" push -u --force-with-lease "$GIT_REMOTE" "$branch" 2>&1 || true
        return 0
    fi

    # Tier: worker = per-brief (see top-of-file model tier policy).
    # Default sonnet; override from brief frontmatter Model: line (YAML) or
    # **Model:** line (legacy bold-markdown).  The Python parser handles both;
    # bash grep below is a compat fallback for the bold form only.
    # Receipt 2026-06-11: bash grep matched only bold form, silently ignoring
    # every YAML-frontmatter card (brief-249 `Model: opus` ran sonnet worker).
    local WORKER_MODEL="sonnet"
    local brief_file_path
    brief_file_path=$(python3 -c "import json; print(json.load(open('$PROGRESS_FILE')).get('brief_file', ''))" 2>/dev/null)
    if [ -n "$brief_file_path" ] && [ -f "$WORKTREE_DIR/$brief_file_path" ]; then
        local brief_model
        # Primary: Python parser — handles both `Model: opus` (YAML frontmatter)
        # and `**Model:** opus` (bold-markdown).  Validates against allowed set;
        # emits a warning and falls back to sonnet on unrecognized values.
        brief_model=$(python3 "$DAEMON_LIB_DIR/actions.py" parse-worker-model "$WORKTREE_DIR/$brief_file_path" 2>/dev/null)
        # Fallback: legacy bash grep for bold-markdown form (defensive; Python
        # already covers this, but keeps the old path alive in case of import error).
        if [ -z "$brief_model" ]; then
            brief_model=$(grep -m1 '^\*\*Model:\*\*' "$WORKTREE_DIR/$brief_file_path" 2>/dev/null | sed 's/.*\*\*Model:\*\*[[:space:]]*//' | awk '{print $1}' | cut -d'(' -f1 | cut -d',' -f1 | tr '[:upper:]' '[:lower:]')
        fi
        if [ -n "$brief_model" ]; then
            WORKER_MODEL="$brief_model"
            if [ "$brief_model" != "sonnet" ]; then
                wlog "WORKER: using model '$brief_model' (from brief)"
            fi
        fi
    fi

    # Per-brief wall-time override (Cycle-wall-time-secs: frontmatter).
    # Same parser shape as Model: line. Default = MAX_CYCLE_WALL_TIME_SECS.
    local CYCLE_WALL_TIME_SECS="$MAX_CYCLE_WALL_TIME_SECS"
    if [ -n "$brief_file_path" ] && [ -f "$WORKTREE_DIR/$brief_file_path" ]; then
        local brief_cycle_secs
        brief_cycle_secs=$(grep -m1 '^\*\*Cycle-wall-time-secs:\*\*' "$WORKTREE_DIR/$brief_file_path" 2>/dev/null \
            | sed 's/.*\*\*Cycle-wall-time-secs:\*\*[[:space:]]*//' \
            | grep -oE '^[0-9]+')
        if [ -n "$brief_cycle_secs" ]; then
            CYCLE_WALL_TIME_SECS="$brief_cycle_secs"
            wlog "WORKER: Cycle-wall-time-secs=$brief_cycle_secs (from brief)"
        fi
    fi

    # Run one iteration IN THE WORKTREE (main tree untouched)
    local WORKER_LOG="$LOG_DIR/worker_${brief_id}_$(date +%Y%m%d_%H%M%S).log"
    local WORKER_JSON
    WORKER_JSON=$(mktemp)
    local WORKER_TIMEOUT_FLAG
    WORKER_TIMEOUT_FLAG=$(mktemp)
    rm -f "$WORKER_TIMEOUT_FLAG"  # written by watchdog only if timeout fires
    local WORKER_START
    WORKER_START=$(date +%s)

    # Read prompt from main tree (canonical), execute in worktree
    local PROMPT_CONTENT
    PROMPT_CONTENT=$(cat "$WORKER_PROMPT")

    cd "$WORKTREE_DIR"

    # Spawn worker in a new process group (os.setpgrp + execvp) so SIGTERM/SIGKILL
    # on the PGID reaches hung subprocesses (brief-047d: lerobot loader deadlock).
    # os.setpgrp() makes the python process a new group leader (PGID = its own PID);
    # os.execvp() replaces python with claude, preserving the PID and PGID.
    # After exec: WORKER_PID == PGID of the new group containing claude + descendants.
    python3 -c "import os,sys; os.setpgrp(); os.execvp(sys.argv[1],sys.argv[1:])" \
        claude --model "$WORKER_MODEL" --dangerously-skip-permissions \
        --output-format json \
        -p "$PROMPT_CONTENT" \
        > "$WORKER_JSON" 2>>"$WORKER_LOG" &
    local WORKER_PID
    WORKER_PID=$!

    # Timeout watchdog: fires after CYCLE_WALL_TIME_SECS, then SIGTERM-grace-SIGKILL
    # the process group. 10s grace gives in-flight git ops time to settle (KC3).
    (
        sleep "$CYCLE_WALL_TIME_SECS"
        if kill -0 "$WORKER_PID" 2>/dev/null; then
            touch "$WORKER_TIMEOUT_FLAG"
            kill -TERM -"$WORKER_PID" 2>/dev/null || kill -TERM "$WORKER_PID" 2>/dev/null
            sleep "$WORKER_KILL_GRACE_SECS"
            kill -KILL -"$WORKER_PID" 2>/dev/null || true
        fi
    ) &
    local WATCHDOG_PID
    WATCHDOG_PID=$!

    wait "$WORKER_PID"
    local WORKER_EXIT=$?
    local WORKER_END
    WORKER_END=$(date +%s)
    local WORKER_DURATION=$((WORKER_END - WORKER_START))

    # Cancel watchdog (no-op if already fired; avoids a dangling sleep).
    kill "$WATCHDOG_PID" 2>/dev/null
    wait "$WATCHDOG_PID" 2>/dev/null || true

    cd "$PROJECT_DIR"

    # Issue #18: every exit path (timeout, failure, success) leaves the
    # worktree clean. Uncommitted worker WIP becomes a labeled commit on the
    # brief branch — never a landmine for the next dispatch's rebase. On the
    # success path below this lands in the normal push; on the timeout path
    # it preserves mid-flight work that was previously lost.
    commit_worktree_wip "$WORKTREE_DIR" "$brief_id" "at iteration end"

    # ── Timeout path ──────────────────────────────────────────────────────────
    # Watchdog touched WORKER_TIMEOUT_FLAG when the budget expired. Route to
    # awaiting_review so a human investigates before redispatching. Work on
    # the brief branch is preserved — including mid-flight dirt, which the
    # WIP auto-commit above just committed (issue #18).
    if [ -f "$WORKER_TIMEOUT_FLAG" ]; then
        rm -f "$WORKER_TIMEOUT_FLAG"
        wlog "WORKER: cycle wall-time exceeded ${CYCLE_WALL_TIME_SECS}s — killed worker for $brief_id cycle $iteration"
        notify "$brief_id: cycle wall-time exceeded (${CYCLE_WALL_TIME_SECS}s) — routed to awaiting_review"
        parse_metrics "$WORKER_JSON" "$WORKER_LOG" "worker" "{'brief': '$brief_id', 'model': '$WORKER_MODEL', 'exit_code': $WORKER_EXIT, 'timed_out': True}"
        rm -f "$WORKER_JSON"
        python3 "$DAEMON_LIB_DIR/actions.py" move-to-awaiting-review "$brief_id" "$PROJECT_DIR" \
            watchdog-timed-out "cycle wall-time exceeded — human investigation required" \
            2>>"$LOG_DIR/daemon.log" || true
        return 0
    fi
    rm -f "$WORKER_TIMEOUT_FLAG"
    # ─────────────────────────────────────────────────────────────────────────

    parse_metrics "$WORKER_JSON" "$WORKER_LOG" "worker" "{'brief': '$brief_id', 'model': '$WORKER_MODEL', 'exit_code': $WORKER_EXIT}"
    rm -f "$WORKER_JSON"

    # Push results from worktree
    if [ "$WORKER_EXIT" -eq 0 ]; then
        git -C "$WORKTREE_DIR" push -u --force-with-lease "$GIT_REMOTE" "$branch" 2>&1 || wlog "WORKER: push failed (non-fatal)"
        wlog "WORKER: iteration complete (${WORKER_DURATION}s), pushed to $branch"
        notify "$brief_id: iteration done (${WORKER_DURATION}s)"
        CONSECUTIVE_WORKER_FAILURES=0
    else
        wlog "WORKER: iteration FAILED (exit $WORKER_EXIT, ${WORKER_DURATION}s)"
        notify "$brief_id: worker FAILED (exit $WORKER_EXIT)"
        CONSECUTIVE_WORKER_FAILURES=$((CONSECUTIVE_WORKER_FAILURES + 1))

        if [ "$WORKER_DURATION" -le 10 ] && grep -q "out of extra usage" "$WORKER_LOG" 2>/dev/null; then
            handle_rate_limit "$WORKER_LOG"
            return 1
        fi
    fi

    # No checkout needed — main tree was never touched

    return $WORKER_EXIT
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Parallel worker execution (WORKER_PARALLEL=true)               ║
# ╚══════════════════════════════════════════════════════════════════╝
#
# Legacy (flag off): Phase 3 calls run_worker_iteration synchronously and the
# tick BLOCKS on its `wait "$WORKER_PID"`. Even with THROTTLE=2 and two active
# briefs, iterations alternate instead of overlapping.
#
# Flag on: spawn_parallel_worker backgrounds the WHOLE run_worker_iteration in
# a subshell (its wall-time watchdog, WIP-auto-commit, metrics, completion
# routing, and push all preserved) and records (PID, brief) in the WP_* tick-
# spanning registry. reap_parallel_workers (start of each tick) harvests any
# finished subshell, mapping its exit code to the parent's consecutive-failure
# counter — the one piece a subshell can't mutate in-place.
#
# Single-flight invariants preserved (see the gating in Phase 3): one worker
# per brief at a time; a parallel-safe:false brief runs ALONE; concurrency
# never exceeds THROTTLE.

# True if a backgrounded worker for this brief is still live in the registry.
worker_already_running() {
    local brief_id="$1"
    local i
    for i in "${!WP_PIDS[@]}"; do
        if [ "${WP_BRIEFS[$i]}" = "$brief_id" ] && kill -0 "${WP_PIDS[$i]}" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

# True if a brief is solo — i.e. parallel_safe:false in running.json.active[].
# A brief absent from active[] (shouldn't happen for a worker target) is
# treated as solo (fail-safe: when in doubt, run it alone).
brief_is_solo() {
    local brief_id="$1"
    python3 -c "
import json, sys
try:
    with open('$RUNNING_FILE') as f:
        rc = json.load(f)
except Exception:
    sys.exit(0)  # solo (fail-safe)
for b in rc.get('active', []):
    if b.get('brief') == '$brief_id':
        sys.exit(1 if b.get('parallel_safe', False) else 0)
sys.exit(0)  # not found → solo (fail-safe)
" 2>/dev/null
    # exit 0 → solo (true); exit 1 → parallel-safe (false)
    [ $? -eq 0 ]
}

# True if any currently-live background worker belongs to a solo brief.
solo_worker_live() {
    local i brief
    for i in "${!WP_PIDS[@]}"; do
        if kill -0 "${WP_PIDS[$i]}" 2>/dev/null; then
            brief="${WP_BRIEFS[$i]}"
            if brief_is_solo "$brief"; then
                return 0
            fi
        fi
    done
    return 1
}

# Count of live backgrounded workers.
live_worker_count() {
    local n=0 i
    for i in "${!WP_PIDS[@]}"; do
        if kill -0 "${WP_PIDS[$i]}" 2>/dev/null; then
            n=$((n + 1))
        fi
    done
    printf '%s' "$n"
}

# Reaper: harvest finished backgrounded workers. For each registry entry whose
# PID is no longer alive, `wait` it (returns its exit code immediately since it
# already exited), route the exit code through the SAME consecutive-failure
# accounting the synchronous path uses, then drop it from the registry. Live
# entries are retained for the next tick. Runs at the START of every tick.
reap_parallel_workers() {
    [ "${#WP_PIDS[@]}" -eq 0 ] && return 0
    local new_pids=() new_briefs=()
    local i pid brief rc
    for i in "${!WP_PIDS[@]}"; do
        pid="${WP_PIDS[$i]}"
        brief="${WP_BRIEFS[$i]}"
        if kill -0 "$pid" 2>/dev/null; then
            # Still running — keep in registry for a later tick.
            new_pids+=("$pid")
            new_briefs+=("$brief")
            continue
        fi
        # Finished — harvest exit code. `wait` on an already-exited child
        # returns its status; on an unknown pid returns 127 (treated as failure).
        wait "$pid" 2>/dev/null
        rc=$?
        if [ "$rc" -eq 0 ]; then
            CONSECUTIVE_WORKER_FAILURES=0
            daemon_log "WORKER[$brief]: reaped — iteration ok (exit 0)"
        else
            CONSECUTIVE_WORKER_FAILURES=$((CONSECUTIVE_WORKER_FAILURES + 1))
            daemon_log "WORKER[$brief]: reaped — iteration FAILED (exit $rc, consecutive=$CONSECUTIVE_WORKER_FAILURES)"
        fi
    done
    # Reassign registry to only the still-live entries. This daemon runs under
    # `set -uo pipefail` (top of file), and in bash 3.2 `arr=("${empty[@]}")`
    # on an empty array trips "unbound variable". The explicit empty-case reset
    # avoids that expansion. (The `${!arr[@]}` index loops above are already
    # set-u-safe on empty arrays — only the `${arr[@]}` value form is not.)
    if [ "${#new_pids[@]}" -eq 0 ]; then
        WP_PIDS=()
        WP_BRIEFS=()
    else
        WP_PIDS=("${new_pids[@]}")
        WP_BRIEFS=("${new_briefs[@]}")
    fi
    return 0
}

# Spawn one backgrounded worker for (brief, branch) and register it. The whole
# run_worker_iteration runs in a subshell; its stdout/stderr already land in
# the per-worker log via the function's internal redirects, and the daemon.log
# lines are brief-tagged via wlog so interleaved parallel output stays readable.
# Caller (Phase 3) owns the single-flight + solo + THROTTLE gating.
spawn_parallel_worker() {
    local brief_id="$1"
    local branch="$2"
    run_worker_iteration "$brief_id" "$branch" &
    local pid=$!
    WP_PIDS+=("$pid")
    WP_BRIEFS+=("$brief_id")
    daemon_log "WORKER[$brief_id]: spawned background iteration (pid $pid, live=$(live_worker_count)/$THROTTLE)"
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Validator Iteration                                            ║
# ╚══════════════════════════════════════════════════════════════════╝
#
# Fires between Phase 2.5 and Phase 3 when assess.py emits a VALIDATOR target.
# Reads the builder commit fresh-context, writes a review artifact to
# .loop/modules/validator/state/reviews/<brief>-cycle-<N>.md on the brief
# branch. The validator is read-only on source (anti-pattern: "Don't put the
# validator in the commit path"); this function commits+pushes the review
# artifact on the validator's behalf after its Claude subprocess exits.

run_validator_iteration() {
    local brief_id="$1"
    local branch="$2"
    local commit_sha="$3"

    local WORKTREE_DIR="$PROJECT_DIR/.loop/worktrees/$brief_id"
    if [ ! -d "$WORKTREE_DIR" ]; then
        daemon_log "VALIDATOR: no worktree for $brief_id — skipping"
        return 0
    fi

    # Pull latest into worktree
    if ! git -C "$WORKTREE_DIR" pull --ff-only "$GIT_REMOTE" "$branch" -q 2>/dev/null; then
        # Issue #19: same loud-log as the worker pull site; no auto-heal here.
        local _wt_counts
        _wt_counts=$(git -C "$WORKTREE_DIR" rev-list --left-right --count "HEAD...${GIT_REMOTE}/${branch}" 2>/dev/null) || _wt_counts=""
        if [ -n "$_wt_counts" ]; then
            daemon_log "VALIDATOR: SYNC FAILED for $branch worktree — diverged ($(printf '%s' "$_wt_counts" | awk '{print $1}') ahead / $(printf '%s' "$_wt_counts" | awk '{print $2}') behind)"
        fi
    fi

    local PROGRESS_FILE="$WORKTREE_DIR/.loop/state/progress.json"
    if [ ! -f "$PROGRESS_FILE" ]; then
        daemon_log "VALIDATOR: no progress.json in worktree for $brief_id — skipping"
        return 0
    fi

    local cycle
    cycle=$(python3 -c "import json; print(json.load(open('$PROGRESS_FILE')).get('iteration', 0))" 2>/dev/null || echo "0")
    if [ -z "$cycle" ] || [ "$cycle" = "0" ]; then
        daemon_log "VALIDATOR: $brief_id cycle=0 — nothing to review yet"
        return 0
    fi

    local brief_file
    brief_file=$(python3 -c "import json; print(json.load(open('$PROGRESS_FILE')).get('brief_file', ''))" 2>/dev/null)

    # Per-brief validator override (Thread 1 scope item). Brief frontmatter
    # `**Validator:**` names an agent spec. Resolution:
    #   - bare name (e.g. `loop-reviewer`, `reviewer`, `security-reviewer`)
    #     → `core/agents/<name>.md` (with optional `loop-` prefix stripped)
    #   - path containing `/`
    #     → absolute path used as-is; relative path resolved under worktree
    # Unresolvable overrides log and fall back to default. VALIDATOR_NAME
    # carries through to the review artifact's `validator:` frontmatter.
    local VALIDATOR_NAME="loop-reviewer"
    local VALIDATOR_AGENT_FILE="$VALIDATOR_AGENT_DEFAULT"
    if [ -n "$brief_file" ] && [ -f "$WORKTREE_DIR/$brief_file" ]; then
        local brief_validator
        brief_validator=$(grep -m1 '^\*\*Validator:\*\*' "$WORKTREE_DIR/$brief_file" 2>/dev/null \
            | sed 's/.*\*\*Validator:\*\*[[:space:]]*//' | awk '{print $1}')
        if [ -n "$brief_validator" ]; then
            local resolved=""
            if [[ "$brief_validator" == /* ]]; then
                resolved="$brief_validator"
            elif [[ "$brief_validator" == */* ]]; then
                resolved="$WORKTREE_DIR/$brief_validator"
            else
                local base="${brief_validator#loop-}"
                resolved="$(dirname "$VALIDATOR_AGENT_DEFAULT")/${base}.md"
            fi
            if [ -f "$resolved" ]; then
                VALIDATOR_NAME="$brief_validator"
                VALIDATOR_AGENT_FILE="$resolved"
                daemon_log "VALIDATOR: using override '$brief_validator' (from brief)"
            else
                daemon_log "VALIDATOR: override '$brief_validator' unresolved at '$resolved' — using default loop-reviewer"
            fi
        fi
    fi

    daemon_log "VALIDATOR: reviewing $brief_id cycle $cycle (commit ${commit_sha:0:8})"

    mkdir -p "$WORKTREE_DIR/.loop/modules/validator/state/reviews"

    local REVIEW_REL=".loop/modules/validator/state/reviews/${brief_id}-cycle-${cycle}.md"

    # Brief-014 fix 5: presence check for process artifacts named in brief
    # completion criteria. If any declared artifact (plan.md, closeout.md,
    # etc.) is missing from the worktree, the daemon writes a synthetic
    # `block` review and skips the Claude invocation entirely. Deterministic
    # floor under the LLM rubric — brief-012 shipped 14 passing cycles without
    # plan.md or closeout.md because the rubric didn't enforce presence.
    #
    # 2026-04-22 follow-up: gate on worker_status == complete. Multi-cycle
    # briefs declare end-of-brief artifacts; firing presence-check on every
    # cycle blocks cycles 1..N-1 from ever producing those artifacts. Only
    # enforce on the final cycle (status=complete).
    local WORKER_STATUS
    WORKER_STATUS=$(python3 -c "import json; print(json.load(open('$PROGRESS_FILE')).get('status', ''))" 2>/dev/null)

    local MISSING_ARTIFACTS=""
    if [ "$WORKER_STATUS" = "complete" ]; then
        MISSING_ARTIFACTS=$(python3 -c "
import sys
sys.path.insert(0, '$DAEMON_LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$WORKTREE_DIR/$brief_file', '$WORKTREE_DIR')
print(','.join(missing))
" 2>/dev/null)
    else
        daemon_log "VALIDATOR: presence check skipped for $brief_id cycle $cycle (worker_status=$WORKER_STATUS, only runs on complete)"
    fi

    if [ -n "$MISSING_ARTIFACTS" ]; then
        daemon_log "VALIDATOR: presence check FAILED for $brief_id cycle $cycle — missing: $MISSING_ARTIFACTS"
        notify "$brief_id cycle $cycle: validator BLOCK — missing artifacts ($MISSING_ARTIFACTS)"

        local NOW_ISO_PRE
        NOW_ISO_PRE=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

        # Synthetic review file — same shape as a Claude-written review.
        cat > "$WORKTREE_DIR/$REVIEW_REL" <<EOF
---
cycle: $cycle
commit: $commit_sha
brief: $brief_id
branch: $branch
verdict: block
summary: presence check failed — missing declared artifacts: $MISSING_ARTIFACTS
validator: presence-check (pre-LLM, brief-014)
reviewed_at: $NOW_ISO_PRE
---

## Bugs found
- Missing declared artifact(s): $MISSING_ARTIFACTS. Completion criteria named these paths; neither the card dir nor the worktree root contains them. Worker must write these files before the validator can pass.

## Execution concerns
- _none_

## Spec-fit notes
- _none_

## Deferred items
- _none_
EOF

        git -C "$WORKTREE_DIR" add "$REVIEW_REL"
        if ! git -C "$WORKTREE_DIR" diff --cached --quiet; then
            git -C "$WORKTREE_DIR" commit -m "[scav] validator: $brief_id cycle $cycle block (missing artifacts)" -q 2>/dev/null
            git -C "$WORKTREE_DIR" push -u --force-with-lease "$GIT_REMOTE" "$branch" 2>&1 || daemon_log "VALIDATOR: push failed on synthetic review (non-fatal)"
        fi
        return 0
    fi

    # Build prompt: agent spec + per-run context + required schema.
    # Strip any leading YAML frontmatter — the claude CLI parses a prompt
    # starting with `---` as a flag and aborts with "unknown option '---".
    local AGENT_SPEC=""
    if [ -f "$VALIDATOR_AGENT_FILE" ]; then
        AGENT_SPEC=$(awk 'NR==1 && $0=="---"{in_fm=1; next} in_fm && $0=="---"{in_fm=0; next} !in_fm' "$VALIDATOR_AGENT_FILE")
    fi

    local NOW_ISO
    NOW_ISO=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    local VALIDATOR_PROMPT_BODY="$AGENT_SPEC

---

# This validation run

You are reviewing ONE builder cycle, fresh-context. No previous conversation.

- Brief ID: \`$brief_id\`
- Branch: \`$branch\`
- Cycle (iteration): $cycle
- Commit under review: \`$commit_sha\`
- Brief file: \`$brief_file\`

## What to read

1. Brief: \`cat $brief_file\`
2. Progress so far: \`cat .loop/state/progress.json\`
3. Diff under review: \`git show $commit_sha\`
4. Recent history: \`git log --oneline -10\`

## What to write

ONE file, at path:

\`\`\`
$REVIEW_REL
\`\`\`

Use this exact shape (YAML frontmatter + four fixed body buckets):

\`\`\`markdown
---
cycle: $cycle
commit: $commit_sha
brief: $brief_id
branch: $branch
verdict: pass   # one of: pass | issues | block
summary: <one-line verdict, <=120 chars>
validator: $VALIDATOR_NAME
reviewed_at: $NOW_ISO
---

## Bugs found
- _none_ OR bullet list

## Execution concerns
- _none_ OR bullet list

## Spec-fit notes
- _none_ OR bullet list

## Deferred items
- _none_ OR bullet list
\`\`\`

Verdict guide:
- \`pass\` — no issues, clean spec-fit. Queen proceeds as today.
- \`issues\` — non-blocking concerns surfaced. Do NOT block merge; queen reads at merge-time.
- \`block\` — show-stopper bug or spec violation. The daemon preempts the queen on the next tick with \`validator_blocked\`.

## Rules

- Read-only on source code. You may ONLY create/modify the review file above.
- Do NOT run git commit, git push, or any git write operation. The daemon commits the review on your behalf after you exit.
- Do NOT modify any file outside \`$REVIEW_REL\`.
- All four body buckets must appear. Empty buckets use the literal \`_none_\`.
- Keep \`summary\` tight — it's the at-a-glance line Mattie reads in the wiki."

    local VALIDATOR_LOG="$LOG_DIR/validator_${brief_id}_$(date +%Y%m%d_%H%M%S).log"
    local VALIDATOR_JSON=$(mktemp)
    local V_START=$(date +%s)
    local VALIDATOR_TIMEOUT_FLAG
    VALIDATOR_TIMEOUT_FLAG=$(mktemp)
    rm -f "$VALIDATOR_TIMEOUT_FLAG"  # written by watchdog only if timeout fires

    cd "$WORKTREE_DIR"

    # Tier: validator = sonnet (see top-of-file model tier policy).
    # Wall-time watchdog (issue #32): same shape as the worker watchdog above
    # (~line 626) — background the claude call, sleep-then-SIGTERM-then-SIGKILL
    # if it's still alive past VALIDATOR_WALL_TIME_SECS. Without this a hung
    # network call ran synchronously in the main tick loop with no bound.
    claude --model sonnet --dangerously-skip-permissions \
        --output-format json \
        -p "$VALIDATOR_PROMPT_BODY" \
        > "$VALIDATOR_JSON" 2>>"$VALIDATOR_LOG" &
    local VALIDATOR_PID
    VALIDATOR_PID=$!

    (
        sleep "$VALIDATOR_WALL_TIME_SECS"
        if kill -0 "$VALIDATOR_PID" 2>/dev/null; then
            touch "$VALIDATOR_TIMEOUT_FLAG"
            kill -TERM "$VALIDATOR_PID" 2>/dev/null
            sleep "$WORKER_KILL_GRACE_SECS"
            kill -KILL "$VALIDATOR_PID" 2>/dev/null || true
        fi
    ) &
    local VALIDATOR_WATCHDOG_PID
    VALIDATOR_WATCHDOG_PID=$!

    wait "$VALIDATOR_PID"
    local V_EXIT=$?

    # Cancel watchdog (no-op if already fired; avoids a dangling sleep).
    kill "$VALIDATOR_WATCHDOG_PID" 2>/dev/null
    wait "$VALIDATOR_WATCHDOG_PID" 2>/dev/null || true

    local V_END=$(date +%s)
    local V_DURATION=$((V_END - V_START))

    cd "$PROJECT_DIR"

    if [ -f "$VALIDATOR_TIMEOUT_FLAG" ]; then
        rm -f "$VALIDATOR_TIMEOUT_FLAG"
        daemon_log "VALIDATOR: wall-time exceeded ${VALIDATOR_WALL_TIME_SECS}s — killed validator for $brief_id cycle $cycle"
        notify "$brief_id: validator wall-time exceeded (${VALIDATOR_WALL_TIME_SECS}s)"
    fi

    parse_metrics "$VALIDATOR_JSON" "$VALIDATOR_LOG" "validator" "{'brief': '$brief_id', 'cycle': $cycle, 'commit': '${commit_sha:0:12}', 'exit_code': $V_EXIT}"
    rm -f "$VALIDATOR_JSON"

    if [ "$V_EXIT" -ne 0 ]; then
        daemon_log "VALIDATOR: FAILED (exit $V_EXIT, ${V_DURATION}s)"
        notify "$brief_id: validator FAILED (exit $V_EXIT)"
        if [ "$V_DURATION" -le 10 ] && grep -q "out of extra usage" "$VALIDATOR_LOG" 2>/dev/null; then
            handle_rate_limit "$VALIDATOR_LOG"
            return 1
        fi
        return $V_EXIT
    fi

    if [ ! -f "$WORKTREE_DIR/$REVIEW_REL" ]; then
        if [ -f "$PROJECT_DIR/$REVIEW_REL" ]; then
            # Validator agent wrote to project root instead of worktree — rescue it.
            # Happens when claude's path resolution picks main's .git as root (leaky
            # worktree abstraction). Moving it here keeps main's tree clean for merge.
            mkdir -p "$(dirname "$WORKTREE_DIR/$REVIEW_REL")"
            mv "$PROJECT_DIR/$REVIEW_REL" "$WORKTREE_DIR/$REVIEW_REL"
            daemon_log "VALIDATOR: rescued stray review from project root to worktree: $REVIEW_REL"
        else
            # Validator agent exited without writing a review — synthesize wrapper-pass
            # at the worktree path. Never write to project root (that's the bug we fixed).
            local NOW_ISO_WRAP
            NOW_ISO_WRAP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
            mkdir -p "$(dirname "$WORKTREE_DIR/$REVIEW_REL")"
            cat > "$WORKTREE_DIR/$REVIEW_REL" <<SYNTHEOF
---
cycle: $cycle
commit: $commit_sha
brief: $brief_id
branch: $branch
verdict: pass
summary: validator agent returned without writing — wrapper-synthesized pass review
validator: wrapper-synthesized (brief-025)
reviewed_at: $NOW_ISO_WRAP
---

## Bugs found
- _none_

## Execution concerns
- validator agent exited without producing a review file; wrapper wrote this synthetic pass. Investigate validator logs if this recurs.

## Spec-fit notes
- _none_

## Deferred items
- _none_
SYNTHEOF
            daemon_log "VALIDATOR: wrapper-synthesized pass review for $brief_id cycle $cycle"
        fi
    fi

    # Daemon commits + pushes the review artifact (validator is read-only).
    git -C "$WORKTREE_DIR" add "$REVIEW_REL"
    if git -C "$WORKTREE_DIR" diff --cached --quiet; then
        daemon_log "VALIDATOR: review file unchanged — skipping commit"
    else
        git -C "$WORKTREE_DIR" commit -m "[scav] validator: $brief_id cycle $cycle review" -q 2>/dev/null
        git -C "$WORKTREE_DIR" push -u --force-with-lease "$GIT_REMOTE" "$branch" 2>&1 || daemon_log "VALIDATOR: push failed (non-fatal)"
        daemon_log "VALIDATOR: review committed for $brief_id cycle $cycle (${V_DURATION}s)"
        notify "$brief_id: validator review cycle $cycle"
    fi

    return 0
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Scouts (specialists) — brief-034 cycle 4                       ║
# ╚══════════════════════════════════════════════════════════════════╝
#
# Scouts are declarative single-file agents at .loop/specialists/<name>.md.
# Cadence, daily cap, runtime cap, and output contract are enforced in the
# daemon tick (scripts/scouts.py does the parsing + state checks). A scout
# never modifies code/state beyond its declared `outputs` contract — a post-
# filter on the claude JSON result is the enforcement layer.
#
# Feature-flag: SCOUTS_ENABLED="" in config.sh keeps this loop dormant. Each
# enabled scout runs at most once per tick, backgrounded so THROTTLE workers
# and scouts cycle concurrently.

fire_scout() {
    local scout_file="$1"
    local name
    name="$(basename "$scout_file" .md)"

    local mode
    mode="$(python3 "$DAEMON_LIB_DIR/scouts.py" get-mode "$scout_file" 2>/dev/null)"
    [ -z "$mode" ] && mode="inference"

    local max_runtime
    max_runtime="$(python3 "$DAEMON_LIB_DIR/scouts.py" get-field "$scout_file" max_runtime_seconds 2>/dev/null)"
    [ -z "$max_runtime" ] && max_runtime="60"

    local scout_log="$LOG_DIR/scout_${name}_$(date +%Y%m%d_%H%M%S).log"
    local start
    start="$(date +%s)"

    # Timeout wrapper — shared by deterministic and inference dispatch paths.
    local TIMEOUT_BIN=""
    if command -v timeout >/dev/null 2>&1; then
        TIMEOUT_BIN="timeout"
    elif command -v gtimeout >/dev/null 2>&1; then
        TIMEOUT_BIN="gtimeout"
    fi

    # ── Deterministic dispatch ──────────────────────────────────────────
    # mode: deterministic → run python3 <binary> --project-dir <path> directly.
    # No claude invocation; the binary manages its own writes. No output-contract
    # enforcement (the binary is trusted to write what the spec says it writes).
    if [ "$mode" = "deterministic" ]; then
        local binary
        binary="$(python3 "$DAEMON_LIB_DIR/scouts.py" get-field "$scout_file" binary 2>/dev/null)"
        if [ -z "$binary" ]; then
            daemon_log "SCOUT: $name mode=deterministic but missing binary field — skipping"
            return 0
        fi
        local binary_path="$DAEMON_LIB_DIR/$binary"
        if [ ! -f "$binary_path" ]; then
            daemon_log "SCOUT: $name binary not found at $binary_path — skipping"
            return 0
        fi

        if [ -n "$TIMEOUT_BIN" ]; then
            "$TIMEOUT_BIN" "${max_runtime}s" python3 "$binary_path" --project-dir "$PROJECT_DIR" \
                >> "$scout_log" 2>&1
        else
            daemon_log "SCOUT: $name running without timeout (no timeout/gtimeout on PATH)"
            python3 "$binary_path" --project-dir "$PROJECT_DIR" >> "$scout_log" 2>&1
        fi
        local exit_code=$?
        local duration_ms=$(( ( $(date +%s) - start ) * 1000 ))

        # Pass "wrote" as output_status — record_fire maps exit_code!=0 to
        # scout_failed first, so this only affects the success path (→ scout_fire).
        python3 "$DAEMON_LIB_DIR/scouts.py" record-fire \
            "$scout_file" "$PROJECT_DIR" "$exit_code" "$duration_ms" \
            "wrote" "" >/dev/null 2>&1

        if [ "$exit_code" -eq 0 ]; then
            daemon_log "SCOUT: $name (deterministic) fired (${duration_ms}ms)"
        else
            daemon_log "SCOUT: $name (deterministic) exit=$exit_code (${duration_ms}ms)"
        fi
        return 0
    fi

    # ── Inference dispatch (default) ────────────────────────────────────
    local model
    model="$(python3 "$DAEMON_LIB_DIR/scouts.py" get-field "$scout_file" model 2>/dev/null)"
    [ -z "$model" ] && model="sonnet"

    local scout_json
    scout_json="$(mktemp)"

    # Body is the role prompt (everything after frontmatter).
    local body
    body="$(python3 "$DAEMON_LIB_DIR/scouts.py" get-body "$scout_file" 2>/dev/null)"
    if [ -z "$body" ]; then
        daemon_log "SCOUT: $name has empty body — skipping"
        rm -f "$scout_json"
        return 0
    fi

    if [ -n "$TIMEOUT_BIN" ]; then
        "$TIMEOUT_BIN" "${max_runtime}s" claude --model "$model" --dangerously-skip-permissions \
            --output-format json \
            -p "$body" \
            > "$scout_json" 2>>"$scout_log"
    else
        daemon_log "SCOUT: $name running without timeout (no timeout/gtimeout on PATH)"
        claude --model "$model" --dangerously-skip-permissions \
            --output-format json \
            -p "$body" \
            > "$scout_json" 2>>"$scout_log"
    fi
    local exit_code=$?
    local duration_ms=$(( ( $(date +%s) - start ) * 1000 ))

    # Parse metrics via the same helper workers/queens use. Extra fields
    # flag the scout lineage so loop-report can segregate.
    parse_metrics "$scout_json" "$scout_log" "scout" \
        "{'specialist': '$name', 'model': '$model', 'exit_code': $exit_code}"

    # Apply output-contract (post-filter). The Python returns a status string
    # + optional destination path; record-fire maps that to the right event.
    local contract_out status dest
    contract_out="$(python3 "$DAEMON_LIB_DIR/scouts.py" apply-output-contract \
        "$scout_file" "$scout_json" "$PROJECT_DIR" 2>/dev/null)"
    status="$(echo "$contract_out" | cut -f1)"
    dest="$(echo "$contract_out" | cut -f2)"

    python3 "$DAEMON_LIB_DIR/scouts.py" record-fire \
        "$scout_file" "$PROJECT_DIR" "$exit_code" "$duration_ms" \
        "$status" "$dest" >/dev/null 2>&1

    case "$status" in
        wrote)
            daemon_log "SCOUT: $name fired (${duration_ms}ms) → $(basename "$dest")"
            ;;
        noop)
            daemon_log "SCOUT: $name noop (${duration_ms}ms)"
            ;;
        rejected)
            daemon_log "SCOUT: $name rejected (${duration_ms}ms) — $dest"
            ;;
        *)
            daemon_log "SCOUT: $name exit=$exit_code (${duration_ms}ms)"
            ;;
    esac

    rm -f "$scout_json"
    return 0
}

invoke_scouts() {
    [ -z "${SCOUTS_ENABLED:-}" ] && return 0
    local specialists_dir="$LOOP_DIR/specialists"
    [ -d "$specialists_dir" ] || return 0

    for scout_name in $SCOUTS_ENABLED; do
        local scout_file="$specialists_dir/${scout_name}.md"
        if [ ! -f "$scout_file" ]; then
            daemon_log "SCOUT: $scout_name enabled but $scout_file missing — skipping"
            continue
        fi

        local state
        state="$(python3 "$DAEMON_LIB_DIR/scouts.py" check "$scout_file" "$PROJECT_DIR" 2>/dev/null)"
        if [ "$state" = "kill" ]; then
            daemon_log "SCOUT: $scout_name killed (kill_on condition tripped)"
            continue
        fi
        [ "$state" = "skip" ] && continue

        local due
        due="$(python3 "$DAEMON_LIB_DIR/scouts.py" is-due "$scout_file" "$PROJECT_DIR" 2>/dev/null)"
        [ "$due" = "yes" ] || continue

        local over_cap
        over_cap="$(python3 "$DAEMON_LIB_DIR/scouts.py" over-daily-cap "$scout_file" "$PROJECT_DIR" 2>/dev/null)"
        if [ "$over_cap" = "yes" ]; then
            daemon_log "SCOUT: $scout_name at daily cap — skipping"
            continue
        fi

        daemon_log "SCOUT: firing $scout_name"
        SCOUTS_FIRED_THIS_TICK=$((SCOUTS_FIRED_THIS_TICK + 1))
        fire_scout "$scout_file" &
    done
    return 0
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Signal Handling                                                ║
# ╚══════════════════════════════════════════════════════════════════╝

SHUTTING_DOWN=0
cleanup() {
    [ "$SHUTTING_DOWN" -eq 1 ] && return
    SHUTTING_DOWN=1
    echo ""
    daemon_log "SHUTDOWN: caught signal, exiting cleanly"
    notify "Daemon stopped"
    pkill -P $$ 2>/dev/null
    rm -f "$PID_FILE"
    exit 0
}
trap 'cleanup' SIGINT SIGTERM SIGHUP EXIT

# ── Startup repair ──────────────────────────────────────────────────────────
# Reconcile running.json against ground truth (git log + filesystem) before
# the tick loop begins. Catches state drift across restarts and hand-merges.
# Disable with NT_DAEMON_STARTUP_REPAIR=false.
if [ "${NT_DAEMON_STARTUP_REPAIR:-true}" = "false" ]; then
    daemon_log "STARTUP REPAIR: disabled via NT_DAEMON_STARTUP_REPAIR=false"
else
    REPAIR_COUNT=$(python3 -c "
import sys
sys.path.insert(0, '$DAEMON_LIB_DIR')
from startup_repair import run_startup_repair
from actions import init_paths
paths = init_paths('$PROJECT_DIR')
actions = run_startup_repair(paths, '$PROJECT_DIR')
print(len(actions))
" 2>/dev/null || echo "0")
    daemon_log "STARTUP REPAIR: complete (${REPAIR_COUNT:-0} action(s))"
fi

notify "Daemon started (PID $$)"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Main Loop                                                      ║
# ╚══════════════════════════════════════════════════════════════════╝

TURN=0
LAST_CONDUCTOR_TRIGGER=""
LAST_CONDUCTOR_TRIGGER_TS=0
LAST_QUEUE_FP=""
LAST_ESCALATE_PRESENT=false
HEARTBEAT_FILE="$STATE_DIR/heartbeat.json"

# Brief-014 fix 4: heartbeat helper. Fires at top of each tick, plus after each
# phase to narrate "what the loop was last doing." Mattie or any external
# watcher can `jq .last_event` to distinguish a healthy idle from a hang.
write_heartbeat() {
    local event="${1:-tick}"
    python3 -c "
import sys
sys.path.insert(0, '$DAEMON_LIB_DIR')
from actions import write_heartbeat
write_heartbeat('$HEARTBEAT_FILE', pid=$$, last_event='$event')
" 2>/dev/null || true
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Project-dir sync (issue #19)                                    ║
# ╚══════════════════════════════════════════════════════════════════╝
#
# The per-tick sync of the daemon's project checkout used to be
# `git pull --ff-only ... || true`. One silently-failed bookkeeping push
# left a local commit stranded; from then on the ff-only pull failed
# silently every tick and the daemon projected the whole queue from a
# frozen checkout (2026-06-11 portal: ghost-active brief ~40 min after
# its card was parked on origin). Cards are the daemon's source of truth
# — sync failures must be loud.
#
#   1. ff-only pull succeeds → reset failure counter, done.
#   2. Diverged and ALL local-ahead commits carry the `loop:` prefix
#      (pure daemon bookkeeping) → auto-heal: stash tracked changes,
#      pull --rebase, push, pop. Abort-safe: any rebase failure aborts
#      and falls through to (3).
#   3. Otherwise: log `SYNC FAILED: diverged (N ahead / M behind)` every
#      tick it persists; after 3 consecutive failures write escalate.json
#      (same shape as actions.py push_with_escalate) instead of ticking
#      silently on stale cards.
#
# Runs with cwd = PROJECT_DIR, on the main branch (caller checks), after
# fetch. Tested by lib/tests/sync-diverged-checkout.sh, which extracts
# this function verbatim — keep its closing brace as the only column-0 `}`.

SYNC_FAIL_COUNT=0

sync_project_checkout() {
    if git pull --ff-only "$GIT_REMOTE" "$GIT_MAIN_BRANCH" -q 2>/dev/null; then
        SYNC_FAIL_COUNT=0
        return 0
    fi

    local upstream="${GIT_REMOTE}/${GIT_MAIN_BRANCH}"
    local counts ahead behind
    counts=$(git rev-list --left-right --count "HEAD...$upstream" 2>/dev/null) || counts=""
    ahead=$(printf '%s' "$counts" | awk '{print $1}')
    behind=$(printf '%s' "$counts" | awk '{print $2}')
    case "$ahead" in ''|*[!0-9]*) ahead=0 ;; esac
    case "$behind" in ''|*[!0-9]*) behind=0 ;; esac

    # Issue #28: when ahead==0 AND behind==0 the refs are identical — HEAD IS
    # origin/main. The pull failed because the working tree is dirty (untracked
    # or modified bookkeeping files), NOT because the checkout diverged. This is
    # a false positive; log "SYNC FAILED: diverged (0 ahead / 0 behind)" 89×
    # was the observed symptom. Fix: autostash everything, retry ff-only pull
    # (which only needs a fast-forward — usually a no-op when already in sync),
    # then pop. Even if the retry is a no-op it leaves the working tree clean
    # relative to HEAD, which is what matters for the projector reading cards.
    if [ "$ahead" -eq 0 ] && [ "$behind" -eq 0 ]; then
        local stashed=false
        if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
            git stash push -q --include-untracked -m "loop: sync dirty-tree stash (issue #28)" 2>/dev/null && stashed=true
        fi
        git pull --ff-only "$GIT_REMOTE" "$GIT_MAIN_BRANCH" -q 2>/dev/null || true
        if [ "$stashed" = true ]; then git stash pop -q 2>/dev/null || true; fi
        SYNC_FAIL_COUNT=0
        daemon_log "GIT SYNC: dirty working tree with refs in sync — stashed, re-pulled, restored (issue #28)"
        return 0
    fi

    # Auto-heal the common case: every local-ahead commit is daemon
    # bookkeeping (`loop:` prefix) — replaying those onto origin is safe.
    local non_loop=1
    if [ "$ahead" -gt 0 ]; then
        non_loop=$(git log --format=%s "$upstream..HEAD" 2>/dev/null | grep -v '^loop:' | grep -c . || true)
    fi
    if [ "$ahead" -gt 0 ] && [ "$non_loop" -eq 0 ]; then
        local stashed=false
        if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
            git stash push -q -m "loop: sync auto-heal stash (issue #19)" 2>/dev/null && stashed=true
        fi
        if git pull --rebase "$GIT_REMOTE" "$GIT_MAIN_BRANCH" -q 2>/dev/null; then
            if [ "$stashed" = true ]; then git stash pop -q 2>/dev/null || true; fi
            if git push "$GIT_REMOTE" "$GIT_MAIN_BRANCH" -q 2>/dev/null; then
                daemon_log "GIT SYNC: auto-healed — rebased $ahead loop: commit(s) onto $upstream and pushed"
            else
                daemon_log "GIT SYNC: rebased $ahead loop: commit(s) but push failed — checkout fresh, will retry push next tick"
            fi
            SYNC_FAIL_COUNT=0
            return 0
        fi
        git rebase --abort 2>/dev/null || true
        if [ "$stashed" = true ]; then git stash pop -q 2>/dev/null || true; fi
        daemon_log "GIT SYNC: rebase auto-heal failed — aborted, falling through to loud failure"
    fi

    SYNC_FAIL_COUNT=$((SYNC_FAIL_COUNT + 1))
    daemon_log "SYNC FAILED: diverged ($ahead ahead / $behind behind) — daemon checkout is stale (consecutive: $SYNC_FAIL_COUNT)"
    if [ "$SYNC_FAIL_COUNT" -ge 3 ] && [ ! -f "$SIGNALS_DIR/escalate.json" ]; then
        mkdir -p "$SIGNALS_DIR"
        printf '%s\n' \
            '{' \
            '  "type": "sync_failed",' \
            '  "reason": "project_dir_sync_diverged",' \
            "  \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"," \
            "  \"remote\": \"$GIT_REMOTE\"," \
            "  \"branch\": \"$GIT_MAIN_BRANCH\"," \
            "  \"ahead\": $ahead," \
            "  \"behind\": $behind," \
            "  \"consecutive_failures\": $SYNC_FAIL_COUNT" \
            '}' > "$SIGNALS_DIR/escalate.json"
        daemon_log "GIT SYNC: escalate.json written after $SYNC_FAIL_COUNT consecutive failed syncs"
    fi
    return 1
}

while true; do
    TURN=$((TURN + 1))
    write_heartbeat "tick_start"

    # Per-tick aggregate counters (brief-034 cycle 6). Reset every tick; read
    # back in the Phase 5 tick-metric emitter. active_scouts + api_calls are
    # tracked in-process — don't hoist into shared state.
    SCOUTS_FIRED_THIS_TICK=0
    CONDUCTOR_FIRED_THIS_TICK=0
    WORKER_FIRED_THIS_TICK=0
    VALIDATOR_FIRED_THIS_TICK=0

    # Reap finished background workers from prior ticks (WORKER_PARALLEL only;
    # no-op when the registry is empty, so flag-off ticks are unchanged).
    if [ "$WORKER_PARALLEL" = "true" ]; then
        reap_parallel_workers
    fi

    # --- Escalate-resolved detection (breaks dedup on stale triggers) ---
    # When the queen writes escalate.json, subsequent ticks with the same
    # trigger de-dup and the daemon goes silent. If a human (or scav) clears
    # the escalate, the daemon must re-run the queen — otherwise it sits
    # deduped on a decision that no longer applies.  Track escalate presence
    # across ticks; on the tick where it disappears, reset the dedup marker.
    if [ -f "$SIGNALS_DIR/escalate.json" ]; then
        CURRENT_ESCALATE_PRESENT=true
    else
        CURRENT_ESCALATE_PRESENT=false
    fi
    if [ "$LAST_ESCALATE_PRESENT" = "true" ] && [ "$CURRENT_ESCALATE_PRESENT" = "false" ]; then
        daemon_log "QUEEN: escalate.json resolved — resetting dedup so next queen re-evaluates"
        LAST_CONDUCTOR_TRIGGER=""
        LAST_CONDUCTOR_TRIGGER_TS=0
    fi
    LAST_ESCALATE_PRESENT="$CURRENT_ESCALATE_PRESENT"

    # --- State-change dedup clear (brief-076; portal-obs 2026-06-01 Pattern 1) ---
    # actions.py writes dedup-clear-<brief_id>.json after moving a brief out
    # of active[] (merge, approve, reject, move-to-awaiting-review). The
    # signal means "world changed, please re-evaluate." Always clear the
    # cached trigger when we see one.
    #
    # Earlier this code gated the clear on `LAST_CONDUCTOR_TRIGGER`
    # containing the brief id, which only matched `stale_brief:brief-N`-shape
    # triggers and missed the common `no_active` case. After a merge the
    # cached trigger is typically `CONDUCTOR:no_active` (no brief id), so the
    # signal was consumed without clearing — leaving the next queued brief
    # stuck for the full 1800s TTL even though its depends_on just resolved.
    _CLEAR_COUNT=0
    for _CLEAR_FILE in "$SIGNALS_DIR"/dedup-clear-*.json; do
        [ -f "$_CLEAR_FILE" ] || continue
        _CLEAR_FNAME=$(basename "$_CLEAR_FILE")
        _CLEAR_BRIEF="${_CLEAR_FNAME#dedup-clear-}"
        _CLEAR_BRIEF="${_CLEAR_BRIEF%.json}"
        daemon_log "QUEEN: dedup cleared by state-change signal (${_CLEAR_BRIEF})"
        rm -f "$_CLEAR_FILE"
        _CLEAR_COUNT=$((_CLEAR_COUNT + 1))
    done
    if [ "$_CLEAR_COUNT" -gt 0 ]; then
        LAST_CONDUCTOR_TRIGGER=""
        LAST_CONDUCTOR_TRIGGER_TS=0
    fi

    # --- Pause check ---
    if [ -f "$SIGNALS_DIR/pause.json" ]; then
        daemon_log "PAUSED: $(cat "$SIGNALS_DIR/pause.json")"
        notify "Paused"

        # Detect whether this pause was written by the circuit breaker.
        # If so, run a lightweight API probe each 60s; on success, remove
        # pause.json and let the tick resume. A human-written pause waits for
        # a human resume.json as before.
        _CB_PAUSE=$(python3 -c "
import json, sys
try:
    with open('$SIGNALS_DIR/pause.json') as f:
        d = json.load(f)
    print('yes' if d.get('by') == 'circuit-breaker' else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

        while [ -f "$SIGNALS_DIR/pause.json" ] && [ ! -f "$SIGNALS_DIR/resume.json" ]; do
            sleep 60
            if [ "$_CB_PAUSE" = "yes" ] && [ -f "$SIGNALS_DIR/pause.json" ] && [ ! -f "$SIGNALS_DIR/resume.json" ]; then
                # Auto-resume probe: lightweight haiku ping with short timeout.
                # Fail-safe (Rule 10): only resume on unambiguous success (exit 0).
                # Any non-zero exit — including timeouts and ambiguous errors —
                # keeps the daemon paused rather than risking a cascade.
                daemon_log "CIRCUIT BREAKER: probing API health..."
                if _cb_probe_api; then
                    daemon_log "CIRCUIT BREAKER: API probe succeeded — resuming and resetting failure counter"
                    notify "Circuit breaker: API recovered — resuming"
                    CONSECUTIVE_QUEEN_FAILURES=0
                    rm -f "$SIGNALS_DIR/pause.json"
                    break
                else
                    daemon_log "CIRCUIT BREAKER: API probe failed — staying paused (fail-safe)"
                fi
            fi
        done

        if [ -f "$SIGNALS_DIR/resume.json" ]; then
            daemon_log "RESUMED: $(cat "$SIGNALS_DIR/resume.json")"
            notify "Resumed"
            CONSECUTIVE_QUEEN_FAILURES=0
            rm -f "$SIGNALS_DIR/pause.json" "$SIGNALS_DIR/resume.json"
        fi
    fi

    # --- Git sync (worktree-safe: never stash, never force-checkout) ---
    cd "$PROJECT_DIR"
    git fetch "$GIT_REMOTE" --quiet 2>/dev/null

    CURRENT_BRANCH=$(git branch --show-current 2>/dev/null)
    if [ "$CURRENT_BRANCH" = "$GIT_MAIN_BRANCH" ]; then
        # Only pull if we're on main — don't disturb user's branch.
        # Issue #19: loud + self-healing; never `|| true` the truth surface.
        sync_project_checkout || true
    else
        daemon_log "GIT SYNC: main tree on '$CURRENT_BRANCH' (not $GIT_MAIN_BRANCH) — fetch only"
    fi

    # ┌──────────────────────────────────────────────────────────┐
    # │  Pre-tick sweep check (brief-077)                        │
    # │  Runs sweep.py --quick before Phase 1 assess.            │
    # │  O(N briefs), cheap operations, target <1s.              │
    # │  Observational — no auto-route at daemon pre-tick.       │
    # └──────────────────────────────────────────────────────────┘
    _SWEEP_SCRIPT="$DAEMON_LIB_DIR/sweep.py"
    if [ -f "$_SWEEP_SCRIPT" ]; then
        _SWEEP_OUTPUT=$(python3 "$_SWEEP_SCRIPT" "$PROJECT_DIR" --quick 2>&1)
        _SWEEP_RC=$?
        if [ "$_SWEEP_RC" -ne 0 ]; then
            daemon_log "SWEEP: pre-tick check found issues (exit=$_SWEEP_RC) — see stewardship-log for details"
        fi
    fi

    DID_WORK=false

    # ┌──────────────────────────────────────────────────────────────────┐
    # │  brief-108-d: project running.json (single-writer safety net)    │
    # │                                                                  │
    # │  Cards + runtime-events.jsonl are the truth. running.json is     │
    # │  derived. Re-projecting on every tick is idempotent and catches  │
    # │  drift introduced by hand-edits or stale state (the 4-write tail │
    # │  hand-merge-brief.md used to enshrine).                          │
    # │                                                                  │
    # │  harness-001/003: --lane scopes the projection to this daemon's  │
    # │  lane so it never inherits another lane's active state.          │
    # └──────────────────────────────────────────────────────────────────┘
    python3 "$DAEMON_LIB_DIR/state.py" write-running-json "$PROJECT_DIR" ${LOOP_LANE:+--lane "$LOOP_LANE"} 2>/dev/null || true

    # ┌─────────────────────────────────────┐
    # │  Phase 1: Assess state              │
    # └─────────────────────────────────────┘
    # assess.py prints three lines: queen trigger, worker target, validator target.
    write_heartbeat "phase1_assess"
    ASSESS_OUTPUT=$(assess_state)
    CONDUCTOR_TRIGGER=$(echo "$ASSESS_OUTPUT" | sed -n 1p)
    WORKER_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 2p)
    VALIDATOR_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 3p)

    # ┌─────────────────────────────────────┐
    # │  Phase 2: Queen (if triggered)      │
    # └─────────────────────────────────────┘
    case "$CONDUCTOR_TRIGGER" in
        CONDUCTOR:*)
            REASON="${CONDUCTOR_TRIGGER#CONDUCTOR:}"

            # Dedup: skip if same trigger as last tick AND the cache entry is
            # within TTL. After CONDUCTOR_DEDUP_TTL_SECS the entry is stale and
            # the trigger is re-evaluated from scratch (brief-076).
            _NOW=$(date +%s)
            _TRIGGER_AGE=$(( _NOW - LAST_CONDUCTOR_TRIGGER_TS ))
            _SHOULD_DEDUP=false
            if [ "$CONDUCTOR_TRIGGER" = "$LAST_CONDUCTOR_TRIGGER" ] && [ "$_TRIGGER_AGE" -lt "$CONDUCTOR_DEDUP_TTL_SECS" ]; then
                _SHOULD_DEDUP=true
            fi

            # Queue fingerprint in the dedup key (issue #17). The trigger name
            # alone can't see queue mutations — three briefs flipped to queued
            # on 2026-06-11 sat undispatched for ~25 min while dedup skipped on
            # an unchanged `no_active`. Fold a cheap queue-state fingerprint
            # (goals.md stat + ordered dispatchable ids, see queue.py) into
            # the comparison: any queue change invalidates the dedup and the
            # next tick invokes the queen.
            _QUEUE_FP=$(python3 "$DAEMON_LIB_DIR/queue.py" "$PROJECT_DIR" --fingerprint $_LANE_OPT 2>/dev/null || echo "fp-unavailable")
            if [ "$_SHOULD_DEDUP" = "true" ] && [ "$_QUEUE_FP" != "$LAST_QUEUE_FP" ]; then
                daemon_log "QUEEN: dedup invalidated — queue fingerprint changed (${LAST_QUEUE_FP:-none} → ${_QUEUE_FP})"
                _SHOULD_DEDUP=false
            fi

            # Queue-aware dedup bypass (portal-obs P1 companion). The
            # `no_active` trigger reason ignores the dispatchable queue —
            # filing a new brief while the daemon is idle doesn't change the
            # reason, so dedup would silently swallow it for up to 1800s.
            # Whenever we're about to skip on `no_active`, do a cheap
            # filesystem scan via queue.py; if there's anything dispatchable,
            # bypass the dedup so the queen sees the new work this tick.
            if [ "$_SHOULD_DEDUP" = "true" ] && [ "$REASON" = "no_active" ]; then
                _DISPATCH_COUNT=$(python3 "$DAEMON_LIB_DIR/queue.py" "$PROJECT_DIR" $_LANE_OPT 2>/dev/null \
                    | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null \
                    || echo "0")
                if [ "${_DISPATCH_COUNT:-0}" -gt 0 ]; then
                    daemon_log "QUEEN: dedup bypassed — ${_DISPATCH_COUNT} dispatchable brief(s) queued while trigger is no_active"
                    _SHOULD_DEDUP=false
                fi
            fi

            if [ "$_SHOULD_DEDUP" = "true" ]; then
                daemon_log "QUEEN: dedup — same trigger ($REASON), skipping (age ${_TRIGGER_AGE}s / ttl ${CONDUCTOR_DEDUP_TTL_SECS}s)"
            else
                if [ "$CONDUCTOR_TRIGGER" = "$LAST_CONDUCTOR_TRIGGER" ] && [ "$_TRIGGER_AGE" -ge "$CONDUCTOR_DEDUP_TTL_SECS" ]; then
                    daemon_log "QUEEN: dedup TTL expired (age ${_TRIGGER_AGE}s) — re-evaluating trigger ($REASON)"
                fi
                write_heartbeat "phase2_queen:$REASON"
                invoke_conductor "$REASON"
                _CONDUCTOR_INVOKE_RC=$?
                CONDUCTOR_FIRED_THIS_TICK=$((CONDUCTOR_FIRED_THIS_TICK + 1))
                if [ "$_CONDUCTOR_INVOKE_RC" -ne 0 ]; then
                    # Error tick (brief-148): unique key so next tick's dedup
                    # doesn't match — a swallowed queen error gets re-evaluated
                    # next tick instead of cached for up to CONDUCTOR_DEDUP_TTL_SECS.
                    LAST_CONDUCTOR_TRIGGER="error_${_CONDUCTOR_INVOKE_RC}_${_NOW}"
                    # Circuit breaker: track consecutive queen failures.
                    _cb_record_queen_result "fail"
                else
                    LAST_CONDUCTOR_TRIGGER="$CONDUCTOR_TRIGGER"
                    # Circuit breaker: queen succeeded — reset failure counter.
                    _cb_record_queen_result "success"
                fi
                LAST_CONDUCTOR_TRIGGER_TS="$_NOW"
                # Re-stat the queue AFTER the queen ran: she may have dispatched
                # (mutating the queue herself); stamping the post-queen state
                # keeps her own dispatch from busting the dedup next tick.
                LAST_QUEUE_FP=$(python3 "$DAEMON_LIB_DIR/queue.py" "$PROJECT_DIR" --fingerprint $_LANE_OPT 2>/dev/null || echo "fp-unavailable")
                DID_WORK=true

                # Re-assess after queen
                ASSESS_OUTPUT=$(assess_state)
                WORKER_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 2p)
                VALIDATOR_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 3p)
            fi
            ;;
    esac

    # ┌──────────────────────────────────────────────┐
    # │  Phase 2.5: Daemon-side state transitions   │
    # └──────────────────────────────────────────────┘
    DAEMON_ACTIONS="$DAEMON_LIB_DIR/actions.py"

    # Process completed briefs: free active slot immediately (v2 flow).
    # Reads auto-merge flag from brief frontmatter; routes to pending_merges
    # (auto) or awaiting_review (human). Active slot is freed on the same tick
    # completion is detected, so dispatch can fire without waiting for merge.
    for active_entry in $(python3 -c "
import json, subprocess
try:
    with open('$RUNNING_FILE') as f:
        rc = json.load(f)
    for b in rc.get('active', []):
        branch = b.get('branch', '')
        if not branch: continue
        for ref in [branch, f'$GIT_REMOTE/{branch}']:
            try:
                r = subprocess.run(['git', '-C', '$PROJECT_DIR', 'show', f'{ref}:.loop/state/progress.json'],
                    capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    p = json.loads(r.stdout)
                    if p.get('status') == 'complete':
                        print(b.get('brief', ''))
                    break
            except: continue
except: pass
" 2>/dev/null); do
        if [ -n "$active_entry" ]; then
            # Read auto-merge flag from brief frontmatter on the brief branch
            AM_FLAG=$(python3 -c "
import json, sys
sys.path.insert(0, '$DAEMON_LIB_DIR')
from assess import git_show, AUTO_MERGE_LINE_RE
try:
    with open('$RUNNING_FILE') as f:
        rc = json.load(f)
    for b in rc.get('active', []):
        if b.get('brief') != '$active_entry':
            continue
        branch = b.get('branch', '')
        brief_file = b.get('brief_file', '')
        if not branch or not brief_file:
            break
        for ref in [branch, '$GIT_REMOTE/' + branch]:
            content = git_show('$PROJECT_DIR', ref, brief_file)
            if content is not None:
                for line in content.splitlines():
                    m = AUTO_MERGE_LINE_RE.match(line)
                    if m:
                        val = m.group(1).strip().lower().strip('\"').strip(\"'\")
                        print('true' if val == 'true' else 'false')
                        sys.exit(0)
                print('false')
                sys.exit(0)
        break
except Exception: pass
print('false')
" 2>/dev/null)

            # Staleness gate: refuse merge if branch is too far behind main,
            # regardless of Auto-merge flag. Bounded by MAX_COMMITS_BEHIND (default 30).
            GATE_BRANCH=$(python3 -c "
import json, sys
try:
    with open('$RUNNING_FILE') as f:
        rc = json.load(f)
    for b in rc.get('active', []):
        if b.get('brief') == '$active_entry':
            print(b.get('branch', ''))
            sys.exit(0)
except Exception: pass
print('')
" 2>/dev/null)
            STALENESS_GATED=false
            if [ -n "$GATE_BRANCH" ]; then
                CB=$(git -C "$PROJECT_DIR" rev-list --count "${GATE_BRANCH}..${GIT_REMOTE}/${GIT_MAIN_BRANCH}" 2>/dev/null || echo "0")
                if [ "$CB" -gt "$MAX_COMMITS_BEHIND" ]; then
                    STALENESS_GATED=true
                    daemon_log "DAEMON ACTION: merge refused — $active_entry is $CB commits behind main (>$MAX_COMMITS_BEHIND threshold)"
                    python3 "$DAEMON_ACTIONS" move-to-awaiting-review "$active_entry" "$PROJECT_DIR" \
                        staleness-gated "branch is $CB commits behind main — staleness gate triggered, hand-merge required (see wiki/operating-docs/incidents/2026-04-24-brief-049-050-merge-watchlist.md)" \
                        2>>"$LOG_DIR/daemon.log" && DID_WORK=true
                    notify "$active_entry merge refused: $CB commits behind main (staleness gate)"
                fi
            fi

            if [ "$STALENESS_GATED" = "false" ]; then
                if [ "$AM_FLAG" = "true" ]; then
                    daemon_log "DAEMON ACTION: move-to-pending-merges $active_entry (auto-merge)"
                    python3 "$DAEMON_ACTIONS" move-to-pending-merges "$active_entry" "$PROJECT_DIR" 2>>"$LOG_DIR/daemon.log" && DID_WORK=true
                    notify "$active_entry complete → queued for auto-merge"
                else
                    daemon_log "DAEMON ACTION: move-to-awaiting-review $active_entry (human approval required)"
                    python3 "$DAEMON_ACTIONS" move-to-awaiting-review "$active_entry" "$PROJECT_DIR" \
                        complete 2>>"$LOG_DIR/daemon.log" && DID_WORK=true
                    notify "$active_entry complete → awaiting human review (run: loop approve $active_entry)"
                fi
            fi
        fi
    done

    # Process pending dispatch queue
    if [ -f "$STATE_DIR/pending-dispatch.json" ]; then
        # Check **Depends-on:** frontmatter before dispatching.
        # Brief-014 fix: parser now handles comma-separated lists. Captures full
        # line value, splits on commas, strips whitespace + trailing punctuation
        # from each id. All deps must appear in history[] for dispatch to proceed.
        # Emits a structured diagnostic line on every check (allowed or blocked)
        # so future debugging has cheap receipts.
        DEPS_OUTPUT=$(python3 "$DAEMON_LIB_DIR/actions.py" check-depends-on "$PROJECT_DIR" 2>/dev/null)
        # Output protocol: first line is VERDICT ("allowed" or "blocked:<dep>"),
        # second line is the diagnostic (brief=... depends_on=... history_ids=... match=...).
        DEPS_VERDICT=$(echo "$DEPS_OUTPUT" | sed -n 1p)
        DEPS_DIAG=$(echo "$DEPS_OUTPUT" | sed -n 2p)
        [ -n "$DEPS_DIAG" ] && daemon_log "DEPS CHECK: $DEPS_DIAG"
        if [[ "${DEPS_VERDICT:-allowed}" == blocked:* ]]; then
            DEP_ID="${DEPS_VERDICT#blocked:}"
            BLOCKED_BRIEF=$(python3 -c "import json; print(json.load(open('$STATE_DIR/pending-dispatch.json')).get('brief',''))" 2>/dev/null || echo "unknown")
            daemon_log "DAEMON ACTION: dispatch blocked — $BLOCKED_BRIEF depends-on $DEP_ID (not yet merged)"
            notify "$BLOCKED_BRIEF dispatch blocked: depends on $DEP_ID (not merged yet)"
            rm -f "$STATE_DIR/pending-dispatch.json"
        else
            daemon_log "DAEMON ACTION: processing pending dispatch"
            if ! python3 "$DAEMON_ACTIONS" dispatch "$PROJECT_DIR" 2>>"$LOG_DIR/daemon.log"; then
                daemon_log "DAEMON ACTION: dispatch failed, retrying once"
                sleep 5
                python3 "$DAEMON_ACTIONS" dispatch "$PROJECT_DIR" 2>>"$LOG_DIR/daemon.log" || \
                    daemon_log "DAEMON ACTION: dispatch retry failed"
            fi
            DID_WORK=true
            ASSESS_OUTPUT=$(assess_state)
            WORKER_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 2p)
            VALIDATOR_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 3p)
        fi
    fi

    # Process pending_merges queue (peer to dispatch — runs same tick, does not block).
    # Pops one entry from running.json pending_merges[], writes pending-merge.json,
    # executes merge. Guard against double-processing if pending-merge.json already exists.
    PENDING_MERGE_COUNT=$(python3 -c "
import json
try:
    with open('$RUNNING_FILE') as f:
        rc = json.load(f)
    print(len(rc.get('pending_merges', [])))
except: print(0)
" 2>/dev/null || echo "0")
    if [ "${PENDING_MERGE_COUNT:-0}" -gt 0 ] && [ ! -f "$STATE_DIR/pending-merge.json" ]; then
        daemon_log "DAEMON ACTION: processing pending_merges queue ($PENDING_MERGE_COUNT entries)"
        python3 "$DAEMON_ACTIONS" process-pending-merges "$PROJECT_DIR" 2>>"$LOG_DIR/daemon.log"
        if [ $? -eq 0 ]; then
            notify "Brief merged to $GIT_MAIN_BRANCH"
        fi
        DID_WORK=true
        ASSESS_OUTPUT=$(assess_state)
        WORKER_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 2p)
        VALIDATOR_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 3p)
    fi

    # Legacy/manual merge path: pending-merge.json written directly (e.g. by loop approve).
    # process-pending-merges above creates+deletes this atomically, so if it persists
    # across ticks it means a manual stamp or a crash recovery case.
    if [ -f "$STATE_DIR/pending-merge.json" ]; then
        daemon_log "DAEMON ACTION: processing pending merge (legacy/manual path)"
        python3 "$DAEMON_ACTIONS" merge "$PROJECT_DIR" 2>>"$LOG_DIR/daemon.log"
        if [ $? -eq 0 ]; then
            notify "Brief merged to $GIT_MAIN_BRANCH"
        fi
        DID_WORK=true
        ASSESS_OUTPUT=$(assess_state)
        WORKER_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 2p)
        VALIDATOR_TARGET=$(echo "$ASSESS_OUTPUT" | sed -n 3p)
    fi

    # ┌─────────────────────────────────────┐
    # │  Phase 2.6: Scouts (specialists)    │
    # └─────────────────────────────────────┘
    # Brief-034 cycle 4. Dormant when SCOUTS_ENABLED is empty. Backgrounded
    # scouts run in parallel with worker/validator on the same tick.
    write_heartbeat "phase2_6_scouts"
    invoke_scouts

    # ┌─────────────────────────────────────┐
    # │  Phase 2.7: Validator (if pending)  │
    # └─────────────────────────────────────┘
    # Sits between 2.5 and 3: a builder commit lands on tick N (Phase 3),
    # assess.py sees it on tick N+1 with no matching review → emits
    # VALIDATOR:brief,branch,commit. Validator runs fresh-context; daemon
    # commits the review artifact on its behalf after the subprocess exits.
    case "$VALIDATOR_TARGET" in
        VALIDATOR:*)
            IFS=',' read -r V_BRIEF V_BRANCH V_COMMIT <<< "${VALIDATOR_TARGET#VALIDATOR:}"
            if [ -n "$V_BRIEF" ] && [ -n "$V_BRANCH" ] && [ -n "$V_COMMIT" ]; then
                write_heartbeat "phase2_7_validator:$V_BRIEF"
                run_validator_iteration "$V_BRIEF" "$V_BRANCH" "$V_COMMIT"
                VALIDATOR_FIRED_THIS_TICK=$((VALIDATOR_FIRED_THIS_TICK + 1))
                DID_WORK=true
                LAST_CONDUCTOR_TRIGGER=""
                LAST_CONDUCTOR_TRIGGER_TS=0
            else
                daemon_log "VALIDATOR: malformed target '$VALIDATOR_TARGET' — skipping"
            fi
            ;;
    esac

    # ┌─────────────────────────────────────┐
    # │  Phase 3: Worker (if active brief)  │
    # └─────────────────────────────────────┘
    if [ "$WORKER_PARALLEL" = "true" ]; then
        # ── Parallel path ────────────────────────────────────────────────────
        # assess.py (flag-on) emits the primary worker on line 2 and any extras
        # on lines 4+. Collect ALL WORKER: lines, then spawn each in the
        # background subject to the single-flight + solo + THROTTLE gates.
        WORKER_LINES=$(printf '%s\n' "$ASSESS_OUTPUT" | grep '^WORKER:' || true)

        if [ "$CONSECUTIVE_WORKER_FAILURES" -ge 3 ] && [ -n "$WORKER_LINES" ]; then
            # Same escalation gate as the serial path, evaluated once per tick.
            _ESC_BRIEF=$(printf '%s\n' "$WORKER_LINES" | head -1 | sed 's/^WORKER://' | cut -d',' -f1)
            daemon_log "WORKER: 3 consecutive failures — escalating to queen"
            notify "3 worker failures on $_ESC_BRIEF — escalating"
            write_heartbeat "phase3_queen_escalate:$_ESC_BRIEF"
            invoke_conductor "worker_failures_${_ESC_BRIEF}"
            CONDUCTOR_FIRED_THIS_TICK=$((CONDUCTOR_FIRED_THIS_TICK + 1))
            CONSECUTIVE_WORKER_FAILURES=0
        elif [ -n "$WORKER_LINES" ]; then
            while IFS= read -r _wline; do
                [ -z "$_wline" ] && continue
                IFS=',' read -r BRIEF_ID BRIEF_BRANCH <<< "${_wline#WORKER:}"
                [ -z "$BRIEF_ID" ] && continue

                # Invariant 1: one worker per brief at a time. Never spawn a
                # second iteration of a brief that already has a live worker.
                if worker_already_running "$BRIEF_ID"; then
                    daemon_log "WORKER[$BRIEF_ID]: already running in background — skip respawn"
                    continue
                fi

                _LIVE=$(live_worker_count)

                # Invariant 2: THROTTLE cap on concurrent workers.
                if [ "$_LIVE" -ge "$THROTTLE" ]; then
                    daemon_log "WORKER[$BRIEF_ID]: throttle reached (live=$_LIVE/$THROTTLE) — deferring to a later tick"
                    break
                fi

                # Invariant 3: solo (parallel-safe:false) briefs run ALONE.
                # If a solo worker is already live, spawn nothing else; if this
                # brief is solo and anything is live, hold it until they drain.
                if solo_worker_live; then
                    daemon_log "WORKER[$BRIEF_ID]: a solo worker is live — spawning nothing else this tick"
                    break
                fi
                if brief_is_solo "$BRIEF_ID" && [ "$_LIVE" -gt 0 ]; then
                    daemon_log "WORKER[$BRIEF_ID]: solo (parallel-safe:false) — holding until $_LIVE live worker(s) drain"
                    continue
                fi

                write_heartbeat "phase3_worker:$BRIEF_ID"
                spawn_parallel_worker "$BRIEF_ID" "$BRIEF_BRANCH"
                WORKER_FIRED_THIS_TICK=$((WORKER_FIRED_THIS_TICK + 1))
                DID_WORK=true
                LAST_CONDUCTOR_TRIGGER=""
                LAST_CONDUCTOR_TRIGGER_TS=0
            done <<< "$WORKER_LINES"
        fi
    else
        # ── Serial path (legacy; byte-identical to pre-WORKER_PARALLEL) ───────
        case "$WORKER_TARGET" in
            WORKER:*)
                IFS=',' read -r BRIEF_ID BRIEF_BRANCH <<< "${WORKER_TARGET#WORKER:}"

                if [ "$CONSECUTIVE_WORKER_FAILURES" -ge 3 ]; then
                    daemon_log "WORKER: 3 consecutive failures — escalating to queen"
                    notify "3 worker failures on $BRIEF_ID — escalating"
                    write_heartbeat "phase3_queen_escalate:$BRIEF_ID"
                    invoke_conductor "worker_failures_${BRIEF_ID}"
                    CONDUCTOR_FIRED_THIS_TICK=$((CONDUCTOR_FIRED_THIS_TICK + 1))
                    CONSECUTIVE_WORKER_FAILURES=0
                else
                    write_heartbeat "phase3_worker:$BRIEF_ID"
                    run_worker_iteration "$BRIEF_ID" "$BRIEF_BRANCH"
                    WORKER_FIRED_THIS_TICK=$((WORKER_FIRED_THIS_TICK + 1))
                    DID_WORK=true
                    LAST_CONDUCTOR_TRIGGER=""
                    LAST_CONDUCTOR_TRIGGER_TS=0
                fi
                ;;
        esac
    fi

    # ┌─────────────────────────────────────┐
    # │  Phase 4: Notifications             │
    # └─────────────────────────────────────┘
    if [ -f "$SIGNALS_DIR/escalate.json" ]; then
        ESCALATE_MSG=$(python3 -c "import json; print(json.load(open('$SIGNALS_DIR/escalate.json')).get('reason','Review needed'))" 2>/dev/null || echo "Review needed")
        if [ ! -f "$SIGNALS_DIR/.escalate_notified" ]; then
            notify "$ESCALATE_MSG"
            daemon_log "NOTIFY: escalation sent"
            touch "$SIGNALS_DIR/.escalate_notified"
        fi
    else
        rm -f "$SIGNALS_DIR/.escalate_notified"
    fi

    # ┌─────────────────────────────────────┐
    # │  Phase 4.5: Per-tick metric emit    │
    # └─────────────────────────────────────┘
    # Brief-034 cycle 6. One aggregate record per tick with concurrency + scout
    # + api-call counts. Downstream: loop-report.py reads source=="tick" to
    # compute concurrency utilization + scout signal/noise + api-burst sizes.
    # Intentionally separate from the existing source=="daemon"/source=="idle"
    # records so consumers can filter cleanly without schema overload.
    API_CALLS_THIS_TICK=$((CONDUCTOR_FIRED_THIS_TICK + WORKER_FIRED_THIS_TICK + VALIDATOR_FIRED_THIS_TICK + SCOUTS_FIRED_THIS_TICK))
    if [ "$DID_WORK" = true ]; then DID_WORK_PY=True; else DID_WORK_PY=False; fi
    python3 -c "
import json, datetime, os, sys
try:
    ifb = 0
    if os.path.exists('$RUNNING_FILE'):
        with open('$RUNNING_FILE') as f:
            ifb = len(json.load(f).get('active', []))
    entry = {
        'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'tick',
        'tick_number': $TURN,
        'in_flight_briefs': ifb,
        'active_scouts': $SCOUTS_FIRED_THIS_TICK,
        'api_calls_total_tick': $API_CALLS_THIS_TICK,
        'did_work': $DID_WORK_PY,
    }
    with open('$METRICS_FILE', 'a') as f:
        f.write(json.dumps(entry) + '\n')
except Exception as e:
    print(f'tick metric error: {e}', file=sys.stderr)
" 2>/dev/null

    # ┌─────────────────────────────────────┐
    # │  Phase 5: Sleep (adaptive)          │
    # └─────────────────────────────────────┘
    if [ "$DID_WORK" = true ]; then
        CONSECUTIVE_SKIPS=0
        daemon_log "Sleeping ${WORKER_COOLDOWN}s before next tick"
        write_heartbeat "phase5_sleep_worked"
        sleep "$WORKER_COOLDOWN"
    else
        CONSECUTIVE_SKIPS=$((CONSECUTIVE_SKIPS + 1))
        write_heartbeat "phase5_sleep_idle"
        if [ "$CONSECUTIVE_SKIPS" -ge 6 ]; then
            SKIP_SLEEP=900   # 15 min
        elif [ "$CONSECUTIVE_SKIPS" -ge 3 ]; then
            SKIP_SLEEP=600   # 10 min
        else
            SKIP_SLEEP="$HEARTBEAT_INTERVAL"
        fi
        daemon_log "IDLE #$TURN: nothing to do — sleeping $((SKIP_SLEEP / 60))m (skip $CONSECUTIVE_SKIPS)"

        python3 -c "
import json, datetime
entry = {
    'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    'source': 'daemon',
    'category': 'idle',
    'heartbeat': $TURN,
    'cost_usd': 0,
    'consecutive_skips': $CONSECUTIVE_SKIPS,
    'sleep_interval_s': $SKIP_SLEEP
}
with open('$METRICS_FILE', 'a') as f:
    f.write(json.dumps(entry) + '\n')
" 2>/dev/null

        sleep "$SKIP_SLEEP"
    fi
done
