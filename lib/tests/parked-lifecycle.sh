#!/usr/bin/env bash
# brief-160 piece 2 — the parked lifecycle end-to-end (Mattie's ruling, #97).
#
# Blocked-on-external must cost zero throughput. This exercises the real
# actions.py park/unpark against a git fixture, and mirrors the two daemon.sh
# consumption blocks (unpark-signal at tick-top, escalate-resolved auto-unpark)
# kept in lockstep with lib/daemon.sh.
#
#   1. blocked worker exit → park: Status parked + slot freed + claim released
#      + card block written + escalate.json (human owner)
#   2. unpark signal → queued + dedup bust
#   3. escalate-resolve → auto-unpark a parked brief

set -uo pipefail

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIONS="$LIB_DIR/actions.py"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── Fixture: git repo + bare origin + one active brief ──────────────────────
setup_project() {
    local brief_id="$1" status="${2:-active}"
    local origin="$TMP/origin.git" project="$TMP/work"
    rm -rf "$origin" "$project"
    git init --bare -q "$origin"
    git clone -q "$origin" "$project" 2>/dev/null
    git -C "$project" config user.email t@t.co
    git -C "$project" config user.name t
    mkdir -p "$project/.loop/state/signals" "$project/wiki/briefs/cards/$brief_id"
    printf 'GIT_REMOTE="origin"\nGIT_MAIN_BRANCH="master"\n' > "$project/.loop/config.sh"
    cat > "$project/wiki/briefs/cards/$brief_id/index.md" <<EOF
---
ID: $brief_id
Status: $status
Model: sonnet
---

# Brief: $brief_id
Body.
EOF
    cat > "$project/.loop/state/running.json" <<EOF
{"active":[{"brief":"$brief_id","branch":"$brief_id","parallel_safe":false}],"awaiting_review":[],"pending_merges":[],"history":[]}
EOF
    echo "{\"ts\":\"2026-07-14T00:00:00Z\",\"event\":\"dispatched\",\"brief\":\"$brief_id\"}" \
        > "$project/.loop/state/runtime-events.jsonl"
    : > "$project/.loop/state/log.jsonl"
    git -C "$project" add -A
    git -C "$project" commit -qm init
    git -C "$project" push -q origin HEAD:master
    echo "$project"
}

card_status() { python3 "$ACTIONS" card-status "$1" "$2" 2>/dev/null; }
active_count() { python3 -c "import json;print(len(json.load(open('$1/.loop/state/running.json'))['active']))"; }

# ══ Case 1: blocked worker exit → park ══════════════════════════════════════
echo "── Case 1: blocked-on-external → auto-park ──"
P="$(setup_project brief-a)"
python3 "$ACTIONS" park-brief brief-a "$P" \
    --blocker "needs human console auth" --owner human \
    --retrigger "auth granted, then loop unpark" >/dev/null 2>&1

[ "$(card_status brief-a "$P")" = "parked" ] \
    && pass "card Status → parked" || fail "card Status → parked (got $(card_status brief-a "$P"))"
[ "$(active_count "$P")" = "0" ] \
    && pass "slot released (active[] empty)" || fail "slot released"
grep -q "Parked-blocker: needs human console auth" "$P/wiki/briefs/cards/brief-a/index.md" \
    && pass "parked block written to card" || fail "parked block written to card"
grep -q "Parked-owner: human" "$P/wiki/briefs/cards/brief-a/index.md" \
    && pass "owner recorded on card" || fail "owner recorded on card"
grep -q "Parked-retrigger:" "$P/wiki/briefs/cards/brief-a/index.md" \
    && pass "re-trigger recorded on card" || fail "re-trigger recorded on card"
[ -f "$P/.loop/state/signals/escalate.json" ] \
    && pass "human-owned → escalate.json raised" || fail "human-owned → escalate.json raised"
python3 -c "import json;assert json.load(open('$P/.loop/state/signals/escalate.json'))['brief']=='brief-a'" 2>/dev/null \
    && pass "escalate.json names the brief" || fail "escalate.json names the brief"
# Claim release is best-effort git; assert it was attempted via the action log.
grep -q '"action": "daemon:park-brief"' "$P/.loop/state/log.jsonl" \
    && pass "park logged (release-in-op path ran)" || fail "park logged"

# Director-owned park raises NO escalation.
P2="$(setup_project brief-d)"
python3 "$ACTIONS" park-brief brief-d "$P2" --blocker b --owner director --retrigger r >/dev/null 2>&1
[ ! -f "$P2/.loop/state/signals/escalate.json" ] \
    && pass "director-owned → no escalation" || fail "director-owned → no escalation"

# ══ Case 2: unpark signal → queued + dedup bust ═════════════════════════════
echo "── Case 2: unpark signal → queued + dedup bust ──"
# Mirror of lib/daemon.sh tick-top unpark-signal consumption. Kept in lockstep.
consume_unpark_signals() {
    local SIGNALS_DIR="$1" PROJECT_DIR="$2"
    _UNPARK_COUNT=0
    for _UNPARK_FILE in "$SIGNALS_DIR"/unpark-*.json; do
        [ -f "$_UNPARK_FILE" ] || continue
        _UNPARK_FNAME=$(basename "$_UNPARK_FILE")
        _UNPARK_BRIEF="${_UNPARK_FNAME#unpark-}"
        _UNPARK_BRIEF="${_UNPARK_BRIEF%.json}"
        if python3 "$ACTIONS" unpark-brief "$_UNPARK_BRIEF" "$PROJECT_DIR" --by signal >/dev/null 2>&1; then
            _UNPARK_COUNT=$((_UNPARK_COUNT + 1))
        fi
        rm -f "$_UNPARK_FILE"
    done
    if [ "$_UNPARK_COUNT" -gt 0 ]; then
        LAST_CONDUCTOR_TRIGGER=""
        LAST_CONDUCTOR_TRIGGER_TS=0
    fi
}

P="$(setup_project brief-b)"
python3 "$ACTIONS" park-brief brief-b "$P" --blocker b --owner human --retrigger r >/dev/null 2>&1
# Fire the re-trigger by dropping the signal, as `loop unpark` / a scout would.
echo '{"brief":"brief-b"}' > "$P/.loop/state/signals/unpark-brief-b.json"
rm -f "$P/.loop/state/signals/dedup-clear-brief-b.json"  # clear the park's own signal
LAST_CONDUCTOR_TRIGGER="CONDUCTOR:no_active"
LAST_CONDUCTOR_TRIGGER_TS=999
consume_unpark_signals "$P/.loop/state/signals" "$P"

[ "$(card_status brief-b "$P")" = "queued" ] \
    && pass "unpark signal → Status queued" || fail "unpark signal → Status queued (got $(card_status brief-b "$P"))"
[ ! -f "$P/.loop/state/signals/unpark-brief-b.json" ] \
    && pass "signal consumed (removed)" || fail "signal consumed"
[ -z "$LAST_CONDUCTOR_TRIGGER" ] \
    && pass "dedup cache busted" || fail "dedup cache busted (got '$LAST_CONDUCTOR_TRIGGER')"
grep -q "## Park history" "$P/wiki/briefs/cards/brief-b/index.md" \
    && pass "parked block cleared into history" || fail "parked block cleared into history"

# ══ Case 3: escalate-resolve → auto-unpark ══════════════════════════════════
echo "── Case 3: escalate-resolve → auto-unpark ──"
# Mirror of lib/daemon.sh escalate-resolved detection. Kept in lockstep.
P="$(setup_project brief-c)"
python3 "$ACTIONS" park-brief brief-c "$P" --blocker b --owner human --retrigger r >/dev/null 2>&1
SIGNALS_DIR="$P/.loop/state/signals"
LAST_ESCALATE_PRESENT=true
LAST_ESCALATE_BRIEF=$(python3 -c "import json;print(json.load(open('$SIGNALS_DIR/escalate.json')).get('brief',''))" 2>/dev/null)
# Human resolves: rename escalate.json aside (mirrors escalate.json.resolved-*).
mv "$SIGNALS_DIR/escalate.json" "$SIGNALS_DIR/escalate.json.resolved-test"
# Next tick sees the file gone → auto-unpark the named brief.
if [ -f "$SIGNALS_DIR/escalate.json" ]; then CURRENT_ESCALATE_PRESENT=true; else CURRENT_ESCALATE_PRESENT=false; fi
UNPARKED=false
if [ "$LAST_ESCALATE_PRESENT" = "true" ] && [ "$CURRENT_ESCALATE_PRESENT" = "false" ]; then
    if [ -n "${LAST_ESCALATE_BRIEF:-}" ]; then
        python3 "$ACTIONS" unpark-brief "$LAST_ESCALATE_BRIEF" "$P" --by escalate-resolved >/dev/null 2>&1 \
            && UNPARKED=true
    fi
fi
[ "$UNPARKED" = "true" ] && pass "escalate-resolve fired unpark" || fail "escalate-resolve fired unpark"
[ "$(card_status brief-c "$P")" = "queued" ] \
    && pass "resolved-escalation brief → queued" || fail "resolved-escalation brief → queued (got $(card_status brief-c "$P"))"

echo ""
echo "────────────────────────────────────────────────────────────────"
echo "parked-lifecycle: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
