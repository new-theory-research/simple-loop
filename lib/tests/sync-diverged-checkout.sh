#!/usr/bin/env bash
# Regression test for the project-dir sync (issue #19):
#
# A silently-failed bookkeeping push stranded a `loop:` commit on the
# daemon's checkout; the per-tick `git pull --ff-only ... || true` then
# failed silently every tick, and the daemon projected the whole queue
# from a frozen checkout (2026-06-11 portal: ghost-active brief ~40 min
# after its card was parked on origin).
#
# The function under test is extracted VERBATIM from lib/daemon.sh
# (sed range: `^sync_project_checkout() {` .. first `^}`), so the asserted
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

# ── Extract the function under test from lib/daemon.sh ───────────────────────
FUNC_SRC="$(sed -n '/^sync_project_checkout() {/,/^}/p' "$DAEMON_SH")"
if [ -z "$FUNC_SRC" ]; then
    fail "could not extract sync_project_checkout() from lib/daemon.sh"
    echo "FAILED: $FAILED"
    exit 1
fi
eval "$FUNC_SRC"

# Globals the function reads (daemon.sh defines these at startup)
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
LOG_LINES=""
daemon_log() { LOG_LINES="${LOG_LINES}${1}"$'\n'; }
# Issue #15: the escalation branch now also calls notify() + appends a runtime
# event. Stub notify and point DAEMON_LIB_DIR/PROJECT_DIR at throwaway paths so
# the extracted function runs clean (the append-event call is best-effort, guarded
# by `|| true` in the daemon). None of these affect the escalate.json assertions.
notify() { :; }
DAEMON_LIB_DIR="$TMP/nonexistent-lib"
PROJECT_DIR="$TMP/nonexistent-project"

# Build an "origin + daemon checkout + other actor" fixture.
#   $1 = fixture root. Sets: ORIGIN, DAEMON (daemon's project dir), OTHER.
make_fixture() {
    local root="$1"
    mkdir -p "$root"
    ORIGIN="$root/origin.git"
    DAEMON="$root/daemon"
    OTHER="$root/other"
    git init -q --bare -b main "$ORIGIN"
    git clone -q "$ORIGIN" "$DAEMON" 2>/dev/null
    git -C "$DAEMON" config user.email "daemon@loop"
    git -C "$DAEMON" config user.name "loop-daemon"
    echo "base" > "$DAEMON/file.txt"
    echo "notes" > "$DAEMON/notes.txt"
    mkdir -p "$DAEMON/.loop/state"
    echo '{}' > "$DAEMON/.loop/state/running.json"
    git -C "$DAEMON" add -A
    git -C "$DAEMON" commit -q -m "initial"
    git -C "$DAEMON" push -q origin main
    git clone -q "$ORIGIN" "$OTHER" 2>/dev/null
    git -C "$OTHER" config user.email "other@loop"
    git -C "$OTHER" config user.name "other-actor"
}

# Diverge: daemon makes a local commit (message $1, NOT pushed), other actor
# lands a different commit on origin main. Leaves cwd = $DAEMON, fetched.
diverge() {
    local daemon_msg="$1"
    echo "bookkeeping $RANDOM" > "$DAEMON/.loop/state/running.json"
    git -C "$DAEMON" add -A
    git -C "$DAEMON" commit -q -m "$daemon_msg"
    echo "remote change $RANDOM" >> "$OTHER/file.txt"
    git -C "$OTHER" add -A
    git -C "$OTHER" commit -q -m "card flip on origin"
    git -C "$OTHER" push -q origin main
    cd "$DAEMON"
    git fetch origin -q
}

# ── Case 1: diverged, local commit is loop: bookkeeping → rebase + push ─────
make_fixture "$TMP/c1"
SIGNALS_DIR="$TMP/c1/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=0; LOG_LINES=""
diverge "loop: project running.json"
if sync_project_checkout; then
    pass "loop:-only divergence → sync function returns success"
else
    fail "loop:-only divergence → sync function returned failure"
fi
# Daemon checkout now contains the origin commit (not stale)
if git -C "$DAEMON" merge-base --is-ancestor origin/main HEAD 2>/dev/null; then
    pass "daemon checkout contains origin's commit after auto-heal"
else
    fail "daemon checkout still missing origin's commit"
fi
# And the loop: commit reached origin (pushed)
REMOTE_SUBJECTS=$(git -C "$ORIGIN" log --format=%s main)
if echo "$REMOTE_SUBJECTS" | grep -q "^loop: project running.json$"; then
    pass "loop: bookkeeping commit was rebased and pushed to origin"
else
    fail "loop: bookkeeping commit never reached origin"
fi
if [ "$SYNC_FAIL_COUNT" -eq 0 ]; then
    pass "failure counter reset after auto-heal"
else
    fail "failure counter not reset after auto-heal (=$SYNC_FAIL_COUNT)"
fi

# ── Case 2: diverged, local commit is NOT loop: → loud log, no rebase ───────
make_fixture "$TMP/c2"
SIGNALS_DIR="$TMP/c2/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=0; LOG_LINES=""
diverge "[scav] human work parked on the daemon checkout"
HEAD_BEFORE=$(git -C "$DAEMON" rev-parse HEAD)
if sync_project_checkout; then
    fail "non-loop divergence → sync function claimed success"
else
    pass "non-loop divergence → sync function returns failure"
fi
if [ "$(git -C "$DAEMON" rev-parse HEAD)" = "$HEAD_BEFORE" ]; then
    pass "non-loop commit NOT rebased (checkout untouched)"
else
    fail "non-loop commit was rewritten — auto-heal touched a human commit"
fi
if echo "$LOG_LINES" | grep -q "SYNC FAILED: diverged (1 ahead / 1 behind)"; then
    pass "loud log: 'SYNC FAILED: diverged (1 ahead / 1 behind)'"
else
    fail "missing loud SYNC FAILED log (got: $LOG_LINES)"
fi
if [ ! -f "$SIGNALS_DIR/escalate.json" ]; then
    pass "no escalation after a single failure"
else
    fail "escalation written too early (1 failure)"
fi

# ── Case 3: three consecutive failures → escalate.json (push_with_escalate shape)
sync_project_checkout || true
sync_project_checkout || true
if [ "$SYNC_FAIL_COUNT" -eq 3 ]; then
    pass "consecutive failure counter reached 3"
else
    fail "counter wrong after 3 failed syncs (=$SYNC_FAIL_COUNT)"
fi
if [ -f "$SIGNALS_DIR/escalate.json" ]; then
    pass "escalate.json written after 3 consecutive failures"
    if python3 -c "
import json, sys
e = json.load(open('$SIGNALS_DIR/escalate.json'))
assert e['type'] == 'sync_failed', e
assert e['reason'] == 'project_dir_sync_diverged', e
assert e['ahead'] == 1 and e['behind'] == 1, e
assert e['consecutive_failures'] == 3, e
assert e['remote'] == 'origin' and e['branch'] == 'main', e
"; then
        pass "escalate.json is valid JSON with type/reason/ahead/behind fields"
    else
        fail "escalate.json malformed or missing fields"
    fi
else
    fail "no escalate.json after 3 consecutive failures"
fi

# ── Case 4: dirty tracked file + loop: divergence → stash/pop preserves it ──
make_fixture "$TMP/c4"
SIGNALS_DIR="$TMP/c4/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=0; LOG_LINES=""
diverge "loop: heartbeat state"
# Dirty a tracked file origin did NOT touch — the stash/pop must carry it
# across the rebase intact.
echo "dirty tracked content" >> "$DAEMON/notes.txt"
if sync_project_checkout; then
    pass "loop: divergence with dirty tracked file → auto-heal still succeeds"
else
    fail "auto-heal failed with a dirty tracked file"
fi
if grep -q "dirty tracked content" "$DAEMON/notes.txt"; then
    pass "uncommitted tracked change survived the stash/pop"
else
    fail "uncommitted tracked change lost during auto-heal"
fi

# ── Case 5: clean ff-only path resets the counter ────────────────────────────
make_fixture "$TMP/c5"
SIGNALS_DIR="$TMP/c5/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=2; LOG_LINES=""
echo "more" >> "$OTHER/file.txt"
git -C "$OTHER" add -A && git -C "$OTHER" commit -q -m "plain remote commit" && git -C "$OTHER" push -q origin main
cd "$DAEMON" && git fetch origin -q
if sync_project_checkout && [ "$SYNC_FAIL_COUNT" -eq 0 ]; then
    pass "clean ff-only pull succeeds and resets the failure counter"
else
    fail "ff-only path broken (rc=$?, count=$SYNC_FAIL_COUNT)"
fi

# ── Case 6: 0/0 (refs identical) → NOT a divergence, no SYNC FAILED log ─────
# Issue #28: git pull --ff-only fails on a dirty tree even when HEAD == origin.
# Before the fix this routed to "SYNC FAILED: diverged (0 ahead / 0 behind)".
#
# To guarantee the FIRST git pull --ff-only genuinely fails at 0/0 we set
# pull.rebase=true in the daemon checkout. With that config, git pull aborts
# on an unstaged tracked-file change ("cannot pull with rebase: You have
# unstaged changes") even when HEAD already equals origin/main. Without
# pull.rebase=true the first pull succeeds (ff-only is a no-op) and the
# issue-#28 block is never reached, so the test would pass on unfixed code.
make_fixture "$TMP/c6"
SIGNALS_DIR="$TMP/c6/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=0; LOG_LINES=""
cd "$DAEMON"
# Force the first pull --ff-only to fail at 0/0 (issue-#28 reproduction).
git config pull.rebase true
git fetch origin -q
# Dirty the tree WITHOUT advancing HEAD: modify a tracked file (triggers the
# pull.rebase abort) plus an untracked file.
echo "runtime scratch" > "$DAEMON/.loop/state/running.json"
echo "dirty untracked" > "$DAEMON/.loop/state/scratch.tmp"
if sync_project_checkout; then
    pass "0/0 dirty-tree: sync_project_checkout returns success (not a divergence)"
else
    fail "0/0 dirty-tree: sync_project_checkout returned failure"
fi
if echo "$LOG_LINES" | grep -q "SYNC FAILED"; then
    fail "0/0 dirty-tree: false-positive 'SYNC FAILED' logged"
else
    pass "0/0 dirty-tree: no 'SYNC FAILED' in log"
fi
if [ "$SYNC_FAIL_COUNT" -eq 0 ]; then
    pass "0/0 dirty-tree: failure counter remains 0"
else
    fail "0/0 dirty-tree: failure counter incremented (=$SYNC_FAIL_COUNT)"
fi
# Assert the issue-#28 recovery branch ran (not just a lucky first-pull success).
if echo "$LOG_LINES" | grep -q "GIT SYNC: dirty working tree with refs in sync"; then
    pass "0/0 dirty-tree: issue-#28 recovery log message present"
else
    fail "0/0 dirty-tree: issue-#28 recovery log message missing (got: $LOG_LINES)"
fi

# ── Case 7: dirty-tree + ff-only pull (behind-by-1) — card reconciles ────────
# This case exercises acceptance criterion #3: a brief card written to origin
# while the daemon's working tree is dirty reconciles after sync, so the
# projector reads the latest state without hand-alignment.
#
# NOTE: this case validates the pre-existing ff-only path (behind==1, ahead==0),
# NOT the issue-#28 0/0 fix. With behind==1, git pull --ff-only succeeds on the
# first call (ff-only handles a dirty unrelated tracked file by fast-forwarding
# over it), so ahead==0 AND behind==0 is never reached and the issue-#28 block
# is not exercised here. The mutation check for issue-#28 lives in Case 6.
make_fixture "$TMP/c7"
SIGNALS_DIR="$TMP/c7/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=0; LOG_LINES=""
# Simulate a brief card written to origin while the daemon's working tree is stale.
mkdir -p "$OTHER/wiki/briefs/cards/brief-153"
echo 'Status: active' > "$OTHER/wiki/briefs/cards/brief-153/index.md"
git -C "$OTHER" add -A && git -C "$OTHER" commit -q -m "brief-153: flip to active"
git -C "$OTHER" push -q origin main
cd "$DAEMON" && git fetch origin -q
# Dirty the daemon working tree (bookkeeping noise, same as the real symptom).
echo "stale running.json" > "$DAEMON/.loop/state/running.json"
if sync_project_checkout; then
    pass "ff-only + dirty-tree (behind-by-1): sync_project_checkout returns success"
else
    fail "ff-only + dirty-tree (behind-by-1): sync_project_checkout returned failure"
fi
# After sync, the card written to origin must be present in the working tree.
if [ -f "$DAEMON/wiki/briefs/cards/brief-153/index.md" ] && \
   grep -q "active" "$DAEMON/wiki/briefs/cards/brief-153/index.md"; then
    pass "ff-only + dirty-tree (behind-by-1): working-tree card reconciled to origin"
else
    fail "ff-only + dirty-tree (behind-by-1): working-tree card still stale after sync"
fi
if echo "$LOG_LINES" | grep -q "SYNC FAILED"; then
    fail "ff-only + dirty-tree (behind-by-1): false 'SYNC FAILED' logged"
else
    pass "ff-only + dirty-tree (behind-by-1): no 'SYNC FAILED' in log"
fi

# ── Case 8: behind-only (0 ahead / N behind) whose first ff-only pull FAILS ──
# Issue #95: a behind-only checkout whose first ff-only pull fails (here an
# untracked daemon-local file collides with a path the incoming commit tracks —
# the runtime-events.jsonl resurrection shape) was mislabeled "diverged (0 ahead
# / N behind)" and wedged for 7 ticks. It must classify as BEHIND and fast-forward,
# never log SYNC FAILED. Behind-by-2 proves N>0 is not divergence.
make_fixture "$TMP/c8"
SIGNALS_DIR="$TMP/c8/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=0; LOG_LINES=""
echo "tracked by origin" > "$OTHER/.loop/state/incoming.txt"
git -C "$OTHER" add -A && git -C "$OTHER" commit -q -m "track incoming.txt"
echo "second remote commit" >> "$OTHER/file.txt"
git -C "$OTHER" add -A && git -C "$OTHER" commit -q -m "second remote commit"
git -C "$OTHER" push -q origin main
cd "$DAEMON" && git fetch origin -q
# Daemon holds an untracked file at the same path → ff-only pull refuses.
echo "daemon-local untracked scratch" > "$DAEMON/.loop/state/incoming.txt"
ORIGIN_TIP=$(git -C "$ORIGIN" rev-parse main)
if sync_project_checkout; then
    pass "behind-only ff-fail: sync_project_checkout returns success"
else
    fail "behind-only ff-fail: sync_project_checkout returned failure"
fi
if echo "$LOG_LINES" | grep -q "SYNC FAILED"; then
    fail "behind-only ff-fail: false 'SYNC FAILED' logged (the #95 misclassification)"
else
    pass "behind-only ff-fail: no 'SYNC FAILED' in log"
fi
if echo "$LOG_LINES" | grep -q "behind-only"; then
    pass "behind-only ff-fail: classified + logged as behind-only (not diverged)"
else
    fail "behind-only ff-fail: missing behind-only heal log (got: $LOG_LINES)"
fi
if [ "$(git -C "$DAEMON" rev-parse HEAD)" = "$ORIGIN_TIP" ]; then
    pass "behind-only ff-fail: checkout fast-forwarded to origin tip"
else
    fail "behind-only ff-fail: checkout not advanced to origin"
fi
if [ "$SYNC_FAIL_COUNT" -eq 0 ]; then
    pass "behind-only ff-fail: failure counter stays 0"
else
    fail "behind-only ff-fail: counter incremented (=$SYNC_FAIL_COUNT)"
fi

# ── Case 9: genuine local commit + behind → still diverged/loud (guard) ──────
# The issue-#95 behind-only heal must NOT swallow real divergence: a non-loop
# local commit (ahead>0) with behind>0 stays "SYNC FAILED: diverged", untouched.
make_fixture "$TMP/c9"
SIGNALS_DIR="$TMP/c9/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=0; LOG_LINES=""
echo "human edit" >> "$DAEMON/file.txt"
git -C "$DAEMON" add -A && git -C "$DAEMON" commit -q -m "human: real work on the checkout"
echo "r1" >> "$OTHER/notes.txt"; git -C "$OTHER" add -A && git -C "$OTHER" commit -q -m "remote 1"
echo "r2" >> "$OTHER/notes.txt"; git -C "$OTHER" add -A && git -C "$OTHER" commit -q -m "remote 2"
git -C "$OTHER" push -q origin main
cd "$DAEMON" && git fetch origin -q
HEAD_BEFORE=$(git -C "$DAEMON" rev-parse HEAD)
if sync_project_checkout; then
    fail "genuine commit + behind: sync claimed success (should be diverged)"
else
    pass "genuine commit + behind: sync returns failure (diverged)"
fi
if echo "$LOG_LINES" | grep -q "SYNC FAILED: diverged (1 ahead / 2 behind)"; then
    pass "genuine commit + behind: loud 'SYNC FAILED: diverged (1 ahead / 2 behind)'"
else
    fail "genuine commit + behind: missing diverged log (got: $LOG_LINES)"
fi
if [ "$(git -C "$DAEMON" rev-parse HEAD)" = "$HEAD_BEFORE" ]; then
    pass "genuine commit + behind: local commit untouched"
else
    fail "genuine commit + behind: auto-heal rewrote a human commit"
fi

# ── Case 10: tracked volatile in HEAD → self-heal (issue #94) ────────────────
# A non-daemon merge re-tracked a STRIP_ON_MAIN volatile on main. Sync must
# untrack it, commit (loop: prefix), push, log + emit a sync_self_heal event —
# and the file must survive on disk, untracked. This case points DAEMON_LIB_DIR
# at the REAL lib so the extracted function imports STRIP_ON_MAIN from actions.py
# and appends via state.py (both live at $SCRIPT_DIR/..).
make_fixture "$TMP/c10"
SIGNALS_DIR="$TMP/c10/signals"; mkdir -p "$SIGNALS_DIR"
SYNC_FAIL_COUNT=0; LOG_LINES=""
_SAVED_LIB="$DAEMON_LIB_DIR"; _SAVED_PROJ="$PROJECT_DIR"
DAEMON_LIB_DIR="$SCRIPT_DIR/.."
PROJECT_DIR="$DAEMON"
_VOL=".loop/state/failure-fingerprints.json"
mkdir -p "$OTHER/.loop/state"
echo '{"resurrected": true}' > "$OTHER/$_VOL"
git -C "$OTHER" add -f "$_VOL"
git -C "$OTHER" commit -q -m "hand-merge: re-tracks a daemon volatile (#94)"
git -C "$OTHER" push -q origin main
cd "$DAEMON" && git fetch origin -q && git pull --ff-only origin main -q 2>/dev/null
if git -C "$DAEMON" ls-files --error-unmatch "$_VOL" >/dev/null 2>&1; then
    pass "self-heal setup: volatile is tracked at HEAD before sync"
else
    fail "self-heal setup: volatile not tracked — fixture wrong"
fi
if sync_project_checkout; then
    pass "self-heal: sync_project_checkout returns success"
else
    fail "self-heal: sync_project_checkout returned failure"
fi
if git -C "$DAEMON" ls-files -- "$_VOL" | grep -q .; then
    fail "self-heal: volatile STILL tracked after sync"
else
    pass "self-heal: volatile untracked after sync"
fi
if [ -f "$DAEMON/$_VOL" ]; then
    pass "self-heal: volatile file survived on disk (untracked)"
else
    fail "self-heal: volatile file deleted from disk"
fi
if git -C "$DAEMON" log -1 --format=%s | grep -q "loop: self-heal"; then
    pass "self-heal: strip commit landed with 'loop: self-heal' subject"
else
    fail "self-heal: strip commit missing (got: $(git -C "$DAEMON" log -1 --format=%s))"
fi
if git -C "$ORIGIN" ls-tree -r --name-only main -- "$_VOL" 2>/dev/null | grep -q .; then
    fail "self-heal: origin STILL tracks the volatile (strip/push didn't land)"
else
    pass "self-heal: origin no longer tracks the volatile (pushed)"
fi
if echo "$LOG_LINES" | grep -q "GIT SYNC: self-heal"; then
    pass "self-heal: loud daemon_log line present"
else
    fail "self-heal: missing self-heal log (got: $LOG_LINES)"
fi
if grep -q "sync_self_heal" "$DAEMON/.loop/state/runtime-events.jsonl" 2>/dev/null; then
    pass "self-heal: sync_self_heal runtime event logged"
else
    fail "self-heal: sync_self_heal event missing from runtime-events.jsonl"
fi
DAEMON_LIB_DIR="$_SAVED_LIB"; PROJECT_DIR="$_SAVED_PROJ"

echo ""
echo "PASSED: $PASSED  FAILED: $FAILED"
[ "$FAILED" -eq 0 ]
