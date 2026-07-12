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
FUNC_SURVIVED=$(sed -n '/^_target_survived() {/,/^}/p' "$DAEMON_SH")
FUNC_REGISTER=$(sed -n '/^_watchdog_register() {/,/^}/p' "$DAEMON_SH")
FUNC_REAP=$(sed -n '/^_reap_watchdogs() {/,/^}/p' "$DAEMON_SH")
FUNC_SPAWN=$(sed -n '/^spawn_watchdog() {/,/^}/p' "$DAEMON_SH")

for fn in FUNC_STARTTIME FUNC_SURVIVED FUNC_REGISTER FUNC_REAP FUNC_SPAWN; do
    if [ -z "${!fn}" ]; then
        fail "could not extract $fn from lib/daemon.sh"
        echo "FAILED: 1"
        exit 1
    fi
done
eval "$FUNC_STARTTIME"
eval "$FUNC_SURVIVED"
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
# spawn_watchdog now pages ops when a fired target survives TERM+KILL (issue #89).
# Capture the page to the same file so the ESCALATE test can assert it.
notify() { echo "NOTIFY $*" >> "$DAEMON_LOG_FILE"; }
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
# ║  #89: the fire path is OBSERVABLE — every fire leaves a receipt  ║
# ╚══════════════════════════════════════════════════════════════════╝
# The #89 receipt: a queen ran 1886s past a 600s cap with NO 'wall-time cap
# exceeded — killed' line AND NO 'standing down' line. The bash watchdog logic
# enforces correctly for a well-behaved target (below), but the OLD fire path
# was silent — the only trace of a kill was the caller's flag→log line, and a
# stand-down on an already-gone target logged nothing at all. These assert that
# a fire, a stand-down, and a surviving target now each leave a distinct log.
echo ""
echo "── #89: fire path leaves a receipt ──────────────────────────────"

# FIRE: a real hang past the cap logs the kill FROM THE WATCHDOG (independent of
# the caller/flag path) — the receipt #89 lacked.
: > "$WATCHDOG_REGISTRY"; : > "$DAEMON_LOG_FILE"
FLAGF="$TMP/flag_fire"; rm -f "$FLAGF"
sleep 30 & HANG89=$!
spawn_watchdog "$HANG89" 1 "$FLAGF" 0 0 "queen #408"
wait_gone "$HANG89" || true
sleep 2   # let the post-kill verify poll + log run
if grep -q "queen #408 exceeded 1s cap — killing pid $HANG89" "$DAEMON_LOG_FILE"; then
    pass "[#89-fire] watchdog logs the kill AS IT FIRES (receipt independent of caller)"
else
    fail "[#89-fire] fire left no 'exceeded cap — killing' receipt"
fi
if grep -q "queen #408 target pid $HANG89 killed at 1s cap" "$DAEMON_LOG_FILE"; then
    pass "[#89-fire] watchdog confirms the kill LANDED"
else
    fail "[#89-fire] no post-kill 'killed at cap' confirmation"
fi
if ! grep -q "ESCALATE" "$DAEMON_LOG_FILE"; then
    pass "[#89-fire] a target that DID die does NOT false-ESCALATE (zombie-aware)"
else
    fail "[#89-fire] false ESCALATE on a target that actually died"
fi
kill "$HANG89" 2>/dev/null; wait "$HANG89" 2>/dev/null || true

# STAND-DOWN (already exited): the old code `[ -z "$now" ] && exit 0` was silent
# — a transient empty `ps` on a live pid killed enforcement with no receipt.
# Now it logs. Target exits well before the cap; the watchdog must SAY so.
: > "$WATCHDOG_REGISTRY"; : > "$DAEMON_LOG_FILE"
FLAGG="$TMP/flag_gone2"; rm -f "$FLAGG"
sleep 1 & GONE89=$!
spawn_watchdog "$GONE89" 3 "$FLAGG" 0 0 "queen #409"
wait "$GONE89" 2>/dev/null || true   # exits ~1s, cap is 3s
sleep 3
if grep -q "queen #409 target pid $GONE89 already exited before 3s cap — standing down" "$DAEMON_LOG_FILE"; then
    pass "[#89-standdown] already-exited target now logs a stand-down (was silent)"
else
    fail "[#89-standdown] already-exited stand-down still silent"
fi
if [ ! -f "$FLAGG" ]; then
    pass "[#89-standdown] already-exited stand-down does not touch the flag"
else
    fail "[#89-standdown] stand-down spuriously touched the flag"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  #89: _target_survived — zombie-aware kill verification          ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── #89: _target_survived liveness token ─────────────────────────"

sleep 30 & ALIVE89=$!
TOKA=$(_proc_starttime "$ALIVE89")
if _target_survived "$ALIVE89" "$TOKA"; then
    pass "[#89-survived] live matching pid reads as survived"
else
    fail "[#89-survived] live matching pid mis-read as dead"
fi
if ! _target_survived "$ALIVE89" "Mon Jan 1 00:00:00 2001"; then
    pass "[#89-survived] mismatched token (recycled pid) reads as NOT survived"
else
    fail "[#89-survived] recycled pid mis-read as survived"
fi
# Zombie: SIGKILL it but do NOT reap — ps still reports an lstart, but state Z
# means dead. A bare start-time check would false-positive "survived"; the
# stat-guard must read it as dead.
kill -KILL "$ALIVE89" 2>/dev/null
zi=0
while [ "$zi" -lt 40 ]; do
    zst=$(ps -o stat= -p "$ALIVE89" 2>/dev/null | tr -d ' ')
    case "$zst" in Z*) break ;; "") break ;; esac
    sleep 0.1; zi=$((zi + 1))
done
if ! _target_survived "$ALIVE89" "$TOKA"; then
    pass "[#89-survived] SIGKILLed zombie reads as dead (not a false survival)"
else
    fail "[#89-survived] zombie mis-read as survived — would false-ESCALATE every kill"
fi
wait "$ALIVE89" 2>/dev/null || true
if ! _target_survived "$ALIVE89" "$TOKA"; then
    pass "[#89-survived] reaped/gone pid reads as dead"
else
    fail "[#89-survived] gone pid mis-read as survived"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  #89: a fired target that SURVIVES TERM+KILL pages ESCALATE      ║
# ╚══════════════════════════════════════════════════════════════════╝
# A real process cannot survive SIGKILL, so inject the condition the verify path
# exists to catch: stub _target_survived to report survival. The brief: "a fired
# watchdog whose target survives TERM+KILL must log ESCALATE-grade loudly."
echo ""
echo "── #89: surviving target escalates ──────────────────────────────"

: > "$WATCHDOG_REGISTRY"; : > "$DAEMON_LOG_FILE"
_target_survived() { return 0; }   # force the "survived" verdict
sleep 10 & SURV89=$!
FLAGS="$TMP/flag_surv"; rm -f "$FLAGS"
spawn_watchdog "$SURV89" 1 "$FLAGS" 0 0 "queen #410"
# Fire fires at cap(1) + grace(1); the verify poll then loops the full ~2s
# because the stub always reports survival. Poll the log rather than race a
# fixed sleep (bounded — never hangs the suite).
ei=0
while [ "$ei" -lt 80 ]; do
    grep -q "ESCALATE — queen #410" "$DAEMON_LOG_FILE" && break
    sleep 0.1; ei=$((ei + 1))
done
if grep -q "ESCALATE — queen #410 target pid $SURV89 SURVIVED TERM+KILL" "$DAEMON_LOG_FILE"; then
    pass "[#89-escalate] surviving target logs ESCALATE-grade"
else
    fail "[#89-escalate] surviving target did NOT log ESCALATE"
fi
if grep -q "NOTIFY ops WATCHDOG ESCALATE" "$DAEMON_LOG_FILE"; then
    pass "[#89-escalate] surviving target pages ops"
else
    fail "[#89-escalate] surviving target did NOT page ops"
fi
kill "$SURV89" 2>/dev/null; wait "$SURV89" 2>/dev/null || true
eval "$FUNC_SURVIVED"   # restore the real helper

# ╔══════════════════════════════════════════════════════════════════╗
# ║  #89: END-TO-END — invoke_conductor's exact watchdog path        ║
# ╚══════════════════════════════════════════════════════════════════╝
# Drive the REAL spawn shape (setpgrp+execvp, own process group) exactly as
# invoke_conductor does, with a long dummy standing in for the claude call, and
# replicate the caller's wait → flag-check → kill-log tail. The #89 assertion:
# the target is killed AT the cap, the flag is touched, and the caller logs.
echo ""
echo "── #89: end-to-end cap enforcement (invoke_conductor path) ──────"

: > "$WATCHDOG_REGISTRY"; : > "$DAEMON_LOG_FILE"
QUEEN_WALL_TIME_SECS=2
E2E_FLAG="$TMP/e2e_flag"; rm -f "$E2E_FLAG"
# EXACT invoke_conductor spawn: setpgrp + execvp, backgrounded, own group.
python3 -c "import os,sys; os.setpgrp(); os.execvp(sys.argv[1],sys.argv[1:])" \
    sleep 30 > /dev/null 2>&1 &
E2E_QUEEN=$!
spawn_watchdog "$E2E_QUEEN" "$QUEEN_WALL_TIME_SECS" "$E2E_FLAG" 1 0 "queen #411"
E2E_WD=$WATCHDOG_SPAWNED_PID
E2E_START=$(date +%s)
wait "$E2E_QUEEN" 2>/dev/null
E2E_RC=$?
E2E_DUR=$(( $(date +%s) - E2E_START ))
kill "$E2E_WD" 2>/dev/null; wait "$E2E_WD" 2>/dev/null || true
# Caller tail (invoke_conductor lines 636-648): flag present ⇒ log the kill.
if [ -f "$E2E_FLAG" ]; then
    daemon_log "QUEEN #411: wall-time cap exceeded ${QUEEN_WALL_TIME_SECS}s — killed (${E2E_DUR}s)"
fi
if [ "$E2E_DUR" -lt 10 ]; then
    pass "[#89-e2e] queen killed at the cap (${E2E_DUR}s ≪ 30s target)"
else
    fail "[#89-e2e] queen ran full 30s — cap NOT enforced (${E2E_DUR}s)"
fi
if [ -f "$E2E_FLAG" ]; then
    pass "[#89-e2e] timeout flag touched — caller sees the kill"
else
    fail "[#89-e2e] flag not touched — caller would mis-record a clean exit"
fi
if grep -q "QUEEN #411: wall-time cap exceeded 2s — killed" "$DAEMON_LOG_FILE"; then
    pass "[#89-e2e] caller logs 'wall-time cap exceeded — killed' (the #89 receipt)"
else
    fail "[#89-e2e] caller did NOT log the wall-time kill"
fi
if grep -q "queen #411 exceeded 2s cap — killing pid $E2E_QUEEN (process group)" "$DAEMON_LOG_FILE"; then
    pass "[#89-e2e] watchdog logs the pgid fire independently of the caller"
else
    fail "[#89-e2e] watchdog fire left no independent receipt"
fi
rm -f "$E2E_FLAG"

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
# #89 observability guards: the fire path must log, and a survivor must escalate.
if grep -q 'exceeded ${timeout}s cap — killing pid' "$DAEMON_SH"; then
    pass "[guard #89] watchdog logs the fire before signaling"
else
    fail "[guard #89] fire-log receipt missing from source"
fi
if grep -q 'SURVIVED TERM+KILL' "$DAEMON_SH" && grep -q '_target_survived' "$DAEMON_SH"; then
    pass "[guard #89] surviving-target ESCALATE present in source"
else
    fail "[guard #89] surviving-target ESCALATE missing from source"
fi

# ╔══════════════════════════════════════════════════════════════════╗
# ║  Summary                                                        ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "watchdog-identity: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
