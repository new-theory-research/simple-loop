#!/usr/bin/env bash
# Regression test for watchdog subprocess supervision (issue #77).
#
# Covers:
#   L1 — the shutdown trap reaps tracked watchdogs (spawn a fake watchdog,
#        register it, run _reap_watchdogs, assert it dies).
#   L1b— _reap_watchdogs stands down when a registry pid was recycled
#        (stored start-time no longer matches) — it never signals a stranger.
#   L2 — spawn_watchdog fires when the target's identity still matches.
#   L2b— spawn_watchdog stands down when the target pid was recycled
#        (start-time changed) — the exact bug that assassinated a successor
#        queen 39s in. This is the core fix.
#   IDENT — _proc_starttime is a usable identity token (non-empty live, empty
#           dead), and the timeout FLAG is touched on a real kill but NOT when
#           the target had already exited.
#
# Functions under test are extracted VERBATIM from lib/daemon.sh via sed so the
# asserted logic cannot drift from the shipped code (house pattern).

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_SH="$SCRIPT_DIR/../daemon.sh"

# ── Extract helpers from lib/daemon.sh verbatim ──────────────────────────────
FUNC_STARTTIME=$(sed -n '/^_proc_starttime() {/,/^}/p' "$DAEMON_SH")
FUNC_REGISTER=$(sed -n '/^_watchdog_register() {/,/^}/p' "$DAEMON_SH")
FUNC_REAP=$(sed -n '/^_reap_watchdogs() {/,/^}/p' "$DAEMON_SH")
FUNC_SPAWN=$(sed -n '/^spawn_watchdog() {/,/^}/p' "$DAEMON_SH")

for fn in FUNC_STARTTIME FUNC_REGISTER FUNC_REAP FUNC_SPAWN; do
    if [ -z "${!fn}" ]; then
        fail "could not extract $fn from lib/daemon.sh"
        echo "FAILED: 1"
        exit 1
    fi
done
eval "$FUNC_STARTTIME"
eval "$FUNC_REGISTER"
eval "$FUNC_REAP"
eval "$FUNC_SPAWN"

# Daemon-provided globals / stubs. daemon_log writes to a FILE, not a variable:
# a watchdog logs its stand-down from inside a subshell, which cannot mutate a
# parent-shell variable — but the real daemon_log echoes to the log file, so a
# file-backed stub faithfully captures subshell log lines.
DAEMON_LOG_FILE="$TMP/daemon.log"
: > "$DAEMON_LOG_FILE"
daemon_log() { echo "$1" >> "$DAEMON_LOG_FILE"; }
WORKER_KILL_GRACE_SECS=1
WATCHDOG_REGISTRY="$TMP/watchdog-pids"
WATCHDOG_SPAWNED_PID=""

# ── Test primitives ──────────────────────────────────────────────────────────
# A backgrounded target that is TERM/KILL'd shows up as a zombie until the
# shell reaps it; `kill -0` reports a zombie as alive. Treat zombie (ps state
# Z*) or absent as gone so death assertions don't false-fail.
is_gone() {
    local st
    st=$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ')
    [ -z "$st" ] && return 0
    case "$st" in Z*) return 0 ;; *) return 1 ;; esac
}
# Poll is_gone up to ~4s (bounded — never hangs the suite).
wait_gone() {
    local pid="$1" i=0
    while [ "$i" -lt 40 ]; do
        is_gone "$pid" && return 0
        sleep 0.1; i=$((i + 1))
    done
    return 1
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  IDENT: _proc_starttime as identity token                        ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── IDENT: start-time token ──────────────────────────────────────"

sleep 5 & LIVE=$!
if [ -n "$(_proc_starttime "$LIVE")" ]; then
    pass "[IDENT] live pid has a non-empty start-time"
else
    fail "[IDENT] live pid returned empty start-time"
fi
kill "$LIVE" 2>/dev/null; wait "$LIVE" 2>/dev/null || true
if [ -z "$(_proc_starttime "$LIVE")" ]; then
    pass "[IDENT] dead pid returns empty start-time"
else
    fail "[IDENT] dead pid still returned a start-time"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  L1: shutdown trap reaps tracked watchdogs                       ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── L1: _reap_watchdogs kills tracked watchdogs ──────────────────"

: > "$WATCHDOG_REGISTRY"
: > "$DAEMON_LOG_FILE"
sleep 30 & FAKE_WD=$!
_watchdog_register "$FAKE_WD"
_reap_watchdogs
if wait_gone "$FAKE_WD"; then
    pass "[L1] registered watchdog TERM'd by _reap_watchdogs"
else
    fail "[L1] registered watchdog survived the reap"
fi
kill "$FAKE_WD" 2>/dev/null; wait "$FAKE_WD" 2>/dev/null || true
if grep -q "killing watchdog pid $FAKE_WD" "$DAEMON_LOG_FILE"; then
    pass "[L1] reap logs each kill"
else
    fail "[L1] reap did not log the kill"
fi

# Mutation-discriminate: without registration, the reap leaves it alone.
: > "$WATCHDOG_REGISTRY"
sleep 3 & UNTRACKED=$!
_reap_watchdogs
if kill -0 "$UNTRACKED" 2>/dev/null; then
    pass "[L1-mutation] unregistered process is NOT killed by reap"
else
    fail "[L1-mutation] reap killed a process it never tracked"
fi
kill "$UNTRACKED" 2>/dev/null; wait "$UNTRACKED" 2>/dev/null || true

# ╔══════════════════════════════════════════════════════════════════╗
# ║  L1b: reap stands down on a recycled registry pid                ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── L1b: reap identity-checks the registry ───────────────────────"

: > "$WATCHDOG_REGISTRY"
sleep 30 & STRANGER=$!
# Registry entry with a bogus stored start-time == a recycled pid.
printf '%s %s\n' "$STRANGER" "Mon Jan 1 00:00:00 2001" > "$WATCHDOG_REGISTRY"
_reap_watchdogs
if kill -0 "$STRANGER" 2>/dev/null; then
    pass "[L1b] recycled registry pid (stale start-time) is NOT signaled"
else
    fail "[L1b] reap killed a recycled pid — assassinated a stranger"
fi
kill "$STRANGER" 2>/dev/null; wait "$STRANGER" 2>/dev/null || true

# ╔══════════════════════════════════════════════════════════════════╗
# ║  L2: spawn_watchdog fires when identity matches                  ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── L2: spawn_watchdog fires on a real hang ──────────────────────"

: > "$WATCHDOG_REGISTRY"
FLAG="$TMP/flag_match"; rm -f "$FLAG"
sleep 30 & HUNG=$!
spawn_watchdog "$HUNG" 1 "$FLAG" 0 0 "test-match"
if [ -n "$WATCHDOG_SPAWNED_PID" ]; then
    pass "[L2] spawn_watchdog set WATCHDOG_SPAWNED_PID"
else
    fail "[L2] WATCHDOG_SPAWNED_PID unset"
fi
if grep -q "^$WATCHDOG_SPAWNED_PID " "$WATCHDOG_REGISTRY"; then
    pass "[L2] watchdog registered itself"
else
    fail "[L2] watchdog not in registry"
fi
if wait_gone "$HUNG"; then
    pass "[L2] identity match: hung target killed"
else
    fail "[L2] identity match: target survived (watchdog did not fire)"
fi
kill "$WATCHDOG_SPAWNED_PID" 2>/dev/null; wait "$WATCHDOG_SPAWNED_PID" 2>/dev/null || true
if [ -f "$FLAG" ]; then
    pass "[L2] timeout flag touched on kill"
else
    fail "[L2] timeout flag not touched"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  L2b: spawn_watchdog stands down on a recycled target pid        ║
# ║        (the core issue-#77 fix)                                  ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── L2b: spawn_watchdog stands down on a recycled pid ────────────"

: > "$WATCHDOG_REGISTRY"
: > "$DAEMON_LOG_FILE"
# Model identity via a file: spawn captures "ORIGINAL"; we then flip it to
# "RECYCLED" to simulate the pid being reused by a different process before the
# watchdog wakes. The real _proc_starttime would report the same divergence.
STAMP="$TMP/stamp"; echo "ORIGINAL" > "$STAMP"
_proc_starttime() { cat "$STAMP" 2>/dev/null; }

sleep 8 & SURVIVOR=$!
FLAG2="$TMP/flag_recycle"; rm -f "$FLAG2"
spawn_watchdog "$SURVIVOR" 1 "$FLAG2" 0 0 "test-recycle"
echo "RECYCLED" > "$STAMP"   # pid recycled: identity no longer matches
sleep 2.5
if kill -0 "$SURVIVOR" 2>/dev/null; then
    pass "[L2b] recycled pid NOT killed — watchdog stood down"
else
    fail "[L2b] recycled pid was killed — the assassination bug"
fi
if [ ! -f "$FLAG2" ]; then
    pass "[L2b] stand-down leaves timeout flag untouched"
else
    fail "[L2b] stand-down still touched the timeout flag"
fi
if grep -q "recycled — standing down" "$DAEMON_LOG_FILE"; then
    pass "[L2b] stand-down is logged"
else
    fail "[L2b] stand-down not logged"
fi
kill "$SURVIVOR" 2>/dev/null; wait "$SURVIVOR" 2>/dev/null || true

# Restore the real _proc_starttime for the remaining tests.
eval "$FUNC_STARTTIME"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  IDENT: no spurious kill when the target already exited          ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── IDENT: clean-exit target ─────────────────────────────────────"

: > "$WATCHDOG_REGISTRY"
FLAG3="$TMP/flag_gone"; rm -f "$FLAG3"
sleep 1 & QUICK=$!
spawn_watchdog "$QUICK" 3 "$FLAG3" 0 0 "test-gone"
wait "$QUICK" 2>/dev/null || true   # target exits at ~1s, well before the 3s cap
sleep 3
if [ ! -f "$FLAG3" ]; then
    pass "[IDENT] target that exited cleanly does not trip the timeout flag"
else
    fail "[IDENT] watchdog flagged a timeout for a target that had already exited"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Source guards: every watchdog site converted + trap wired       ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── Source guards ────────────────────────────────────────────────"

SPAWN_CALLS=$(grep -c '^[[:space:]]*spawn_watchdog ' "$DAEMON_SH")
if [ "$SPAWN_CALLS" -ge 4 ]; then
    pass "[guard] all watchdog sites call spawn_watchdog ($SPAWN_CALLS found)"
else
    fail "[guard] expected >=4 spawn_watchdog call sites, found $SPAWN_CALLS"
fi
if ! grep -qE '^\s+\(\s*$' <(sed -n '/# Watchdog: fires after QUEEN_WALL_TIME_SECS/,/QUEEN_WATCHDOG_PID=/p' "$DAEMON_SH"); then
    pass "[guard] queen site no longer inlines a raw watchdog subshell"
else
    fail "[guard] queen site still has an inline watchdog subshell"
fi
if grep -q '_reap_watchdogs' "$DAEMON_SH" && \
   sed -n '/^cleanup() {/,/^}/p' "$DAEMON_SH" | grep -q '_reap_watchdogs'; then
    pass "[guard] cleanup trap calls _reap_watchdogs"
else
    fail "[guard] cleanup trap does not call _reap_watchdogs"
fi
if grep -q ': > "\$WATCHDOG_REGISTRY"' "$DAEMON_SH"; then
    pass "[guard] registry truncated at startup"
else
    fail "[guard] registry not truncated at startup"
fi
if grep -q 'recycled — standing down' "$DAEMON_SH"; then
    pass "[guard] identity-check stand-down present in source"
else
    fail "[guard] identity-check stand-down missing from source"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Summary                                                        ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "watchdog-identity: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
