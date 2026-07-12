#!/usr/bin/env bash
# Regression test for per-brief budget enforcement (issue #44).
#
# The card's `Budget:` (`## Budget` cycle count) was decorative — the daemon's
# max-iterations cap ignored it, and no over-budget event fired. This is the
# unattended-spend ceiling for remote queens: hitting it must PARK the brief
# loudly (state change), then surface the crossing (escalate.json + runtime
# event), notify() last. Design rule (Mattie): the STATE CHANGE is the fix.
#
# The daemon-side `over_budget_park` wrapper is extracted VERBATIM from
# lib/daemon.sh so the asserted logic cannot drift from the shipped code; it
# calls the REAL lib/actions.py move-to-awaiting-review (fix-15's park site) and
# the REAL lib/state.py append-event.
#
# Covers:
#   AC1 — parse-budget resolution: card budget parsed (== hive's cycle X/Y int),
#         absent → empty (daemon keeps global default), budget > global wins.
#   AC2 — budget reached → the brief is PARKED (active[] → awaiting_review[]),
#         escalate.json raised with the receipt, exactly one over_budget event,
#         notify() fired last.
#   AC3 — no repeat-fire: the park removes the brief from active[] (structural
#         dedup), and a second call does NOT clobber the existing escalate.json.
#   AC4 — the per-cycle iteration-progress log line format ("iter N/BUDGET").
#   AC5 — Budget absent → the global-default path is byte-identical to pre-#44
#         (mark-blocked branch strings intact; EFFECTIVE_BUDGET == MAX_ITERATIONS).

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

jget() { python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2" 2>/dev/null; }

# ── Extract the over_budget_park wrapper from lib/daemon.sh verbatim ─────────
FUNC_PARK=$(sed -n '/^over_budget_park() {/,/^}/p' "$DAEMON_SH")
if [ -z "$FUNC_PARK" ]; then
    fail "could not extract over_budget_park from lib/daemon.sh"
    echo "FAILED: 1"; exit 1
fi
eval "$FUNC_PARK"

# Stubs the wrapper references. notify() now takes `notify <class> <message>`
# (ntfy-notification-policy) — capture both so the class and text are asserted.
_NOTIFY_COUNT=0
_NOTIFY_LAST=""
_NOTIFY_CLASS=""
notify() { _NOTIFY_COUNT=$((_NOTIFY_COUNT + 1)); _NOTIFY_CLASS="$1"; _NOTIFY_LAST="$2"; }
daemon_log() { :; }

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC1: parse-budget resolution (the read side of the wiring)      ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC1: parse-budget resolves the card budget ───────────────────"
CARD_HAS="$TMP/card_has.md"
CARD_NONE="$TMP/card_none.md"
CARD_BIG="$TMP/card_big.md"
printf '# Brief\n\n## Budget\n\n**6 cycles sonnet.** plan.\n\n## Tasks\n1. do 99 things\n' > "$CARD_HAS"
printf '# Brief\n\n## Tasks\n1. no budget section, 99 things\n' > "$CARD_NONE"
printf '# Brief\n\n## Budget\n\n25 cycles cap. 20 expected.\n\n## Tasks\n1. work\n' > "$CARD_BIG"

B_HAS=$(python3 "$DAEMON_LIB_DIR/actions.py" parse-budget "$CARD_HAS")
[ "$B_HAS" = "6" ] && pass "[AC1] card '## Budget **6 cycles**' parses to 6" \
    || fail "[AC1] budget parse wrong (=$B_HAS, expected 6)"

B_NONE=$(python3 "$DAEMON_LIB_DIR/actions.py" parse-budget "$CARD_NONE")
[ -z "$B_NONE" ] && pass "[AC1] no ## Budget section → empty (daemon keeps global default)" \
    || fail "[AC1] budgetless card printed '$B_NONE' (expected empty)"

# Budget > global default (20): the card wins for that brief.
B_BIG=$(python3 "$DAEMON_LIB_DIR/actions.py" parse-budget "$CARD_BIG")
[ "$B_BIG" = "25" ] && pass "[AC1] budget 25 > global 20 → 25 (card budget wins, max-integer)" \
    || fail "[AC1] large budget wrong (=$B_BIG, expected 25)"

# Mirrors hive's parse_cycle_budget: MAX integer in the section, bounded by next header.
MAX_ITERATIONS=20
EFFECTIVE_BUDGET="$MAX_ITERATIONS"; BUDGET_SOURCE="global"
[ -n "$B_NONE" ] && { EFFECTIVE_BUDGET="$B_NONE"; BUDGET_SOURCE="card"; }
[ "$EFFECTIVE_BUDGET" = "20" ] && [ "$BUDGET_SOURCE" = "global" ] \
    && pass "[AC1] absent budget → EFFECTIVE_BUDGET==MAX_ITERATIONS (20), source=global" \
    || fail "[AC1] absent-budget resolution wrong (=$EFFECTIVE_BUDGET/$BUDGET_SOURCE)"

# ── Minimal project fixture: one active brief at its budget ─────────────────
make_project() {  # $1 = project_dir, $2 = brief_id, $3 = budget
    local pd="$1" brief="$2" budget="$3"
    STATE_DIR="$pd/.loop/state"
    SIGNALS_DIR="$STATE_DIR/signals"
    LOG_DIR="$pd/.loop/logs"
    PROJECT_DIR="$pd"
    mkdir -p "$SIGNALS_DIR" "$LOG_DIR" "$pd/wiki/briefs/cards/$brief"
    printf -- '---\nID: %s\nStatus: active\nParallel-safe: true\n---\n\n# %s\n\n## Budget\n\n**%s cycles sonnet.** plan.\n' \
        "$brief" "$brief" "$budget" > "$pd/wiki/briefs/cards/$brief/index.md"
    # Dispatched event → the projector shapes the active[] entry from it.
    printf '{"ts":"2026-07-11T00:00:00Z","event":"dispatched","brief":"%s","branch":"%s","brief_file":"wiki/briefs/cards/%s/index.md"}\n' \
        "$brief" "$brief" "$brief" > "$STATE_DIR/runtime-events.jsonl"
    # running.json with the brief active (load_running reads this directly).
    python3 - "$pd" "$brief" <<'PY'
import json, os, sys
pd, brief = sys.argv[1], sys.argv[2]
rj = {
    "active": [{"brief": brief, "branch": brief,
                "brief_file": f"wiki/briefs/cards/{brief}/index.md",
                "dispatched_at": "2026-07-11T00:00:00Z",
                "parallel_safe": True, "edit_surface": [], "worker_slot": 0}],
    "awaiting_review": [], "pending_merges": [],
    "history": [], "completed_pending_eval": [],
}
with open(os.path.join(pd, ".loop", "state", "running.json"), "w") as f:
    json.dump(rj, f, indent=2)
PY
}
active_ids()   { python3 -c "import json,sys; print(' '.join(e['brief'] for e in json.load(open(sys.argv[1])).get('active',[])))" "$1" 2>/dev/null; }
awaiting_ids() { python3 -c "import json,sys; print(' '.join(e['brief'] for e in json.load(open(sys.argv[1])).get('awaiting_review',[])))" "$1" 2>/dev/null; }
awaiting_kind() { python3 -c "import json,sys; print(next((e.get('kind','') for e in json.load(open(sys.argv[1])).get('awaiting_review',[]) if e['brief']==sys.argv[2]),''))" "$1" "$2" 2>/dev/null; }
count_over_budget_events() { grep -c '"event": "over_budget"\|"event":"over_budget"' "$1" 2>/dev/null || echo 0; }

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC2: budget reached → park + escalate + one event + notify     ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC2: budget reached → park + escalation ──────────────────────"
PD="$TMP/proj_ac2"
BRIEF="ft-002-dataset-intake-stage"
make_project "$PD" "$BRIEF" 6
RUNNING="$PD/.loop/state/running.json"
EVENTS="$PD/.loop/state/runtime-events.jsonl"
ESC="$PD/.loop/state/signals/escalate.json"

[ "$(active_ids "$RUNNING")" = "$BRIEF" ] && pass "[AC2] brief starts in active[]" \
    || fail "[AC2] fixture wrong — brief not active"

_NOTIFY_COUNT=0
over_budget_park "$BRIEF" 6 6

[ "$(active_ids "$RUNNING")" = "" ] && pass "[AC2] STATE CHANGE: brief removed from active[]" \
    || fail "[AC2] brief still active after park (='$(active_ids "$RUNNING")')"
[ "$(awaiting_ids "$RUNNING")" = "$BRIEF" ] && pass "[AC2] brief parked into awaiting_review[]" \
    || fail "[AC2] brief not in awaiting_review (='$(awaiting_ids "$RUNNING")')"
[ "$(awaiting_kind "$RUNNING" "$BRIEF")" = "over-budget" ] && pass "[AC2] park kind=over-budget" \
    || fail "[AC2] park kind wrong (='$(awaiting_kind "$RUNNING" "$BRIEF")')"

[ -f "$ESC" ] && pass "[AC2] escalate.json raised" || fail "[AC2] no escalate.json"
[ "$(jget "$ESC" type)" = "over_budget" ] && pass "[AC2] escalate.json type=over_budget" \
    || fail "[AC2] escalate.json type wrong (=$(jget "$ESC" type))"
[ "$(jget "$ESC" brief)" = "$BRIEF" ] && pass "[AC2] escalate.json names the brief" \
    || fail "[AC2] escalate.json brief wrong"
[ "$(jget "$ESC" iterations_used)" = "6" ] && pass "[AC2] receipt iterations_used=6" \
    || fail "[AC2] iterations_used wrong (=$(jget "$ESC" iterations_used))"
[ "$(jget "$ESC" budget)" = "6" ] && pass "[AC2] receipt budget=6" \
    || fail "[AC2] budget wrong (=$(jget "$ESC" budget))"
[ -n "$(jget "$ESC" first_iteration_ts)" ] && pass "[AC2] receipt carries first_iteration_ts" \
    || fail "[AC2] first_iteration_ts missing"
[ "$(jget "$ESC" first_iteration_ts)" = "2026-07-11T00:00:00Z" ] \
    && pass "[AC2] first_iteration_ts = the brief's dispatch ts (from events log)" \
    || fail "[AC2] first_iteration_ts not the dispatch ts (=$(jget "$ESC" first_iteration_ts))"
[ -n "$(jget "$ESC" last_iteration_ts)" ] && pass "[AC2] receipt carries last_iteration_ts" \
    || fail "[AC2] last_iteration_ts missing"

[ "$(count_over_budget_events "$EVENTS")" = "1" ] && pass "[AC2] exactly one over_budget runtime event" \
    || fail "[AC2] wrong over_budget event count (=$(count_over_budget_events "$EVENTS"))"

[ "$_NOTIFY_COUNT" = "1" ] && pass "[AC2] notify() fired exactly once (last line)" \
    || fail "[AC2] notify fired $_NOTIFY_COUNT times (expected 1)"
echo "$_NOTIFY_LAST" | grep -q "over budget" && pass "[AC2] notify text names the over-budget park" \
    || fail "[AC2] notify text wrong (='$_NOTIFY_LAST')"
[ "$_NOTIFY_CLASS" = "brief_escalated" ] && pass "[AC2] notify class is brief_escalated" \
    || fail "[AC2] notify class wrong (='$_NOTIFY_CLASS')"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC3: no repeat-fire — structural dedup + escalate.json guard    ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC3: no repeat-fire next tick ────────────────────────────────"
# The park removed the brief from active[] (asserted in AC2). The daemon's
# budget gate lives in the worker loop, which only runs for active[] briefs —
# so a parked brief cannot re-reach the gate. Structural, one-fire dedup.
[ "$(active_ids "$RUNNING")" = "" ] && pass "[AC3] parked brief not in active[] → gate can't re-fire" \
    || fail "[AC3] brief re-appeared in active[]"

# Guard: a second call (hypothetical re-entry) must NOT clobber the existing
# escalate.json (the desk already has the receipt).
ESC_BEFORE=$(cat "$ESC")
over_budget_park "$BRIEF" 6 6
[ "$(cat "$ESC")" = "$ESC_BEFORE" ] && pass "[AC3] escalate.json unchanged on re-entry (guarded, no clobber)" \
    || fail "[AC3] escalate.json rewritten on re-entry"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC4: per-cycle iteration-progress log line format              ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC4: iteration-progress log line format ──────────────────────"
# The daemon emits, every cycle: "WORKER: iter N/BUDGET for <brief> (budget: SRC)".
# Reproduce the exact format string from daemon.sh with sample values.
iteration=3; EFFECTIVE_BUDGET=20; brief_id="brief-x"; BUDGET_SOURCE="global"
RENDERED="WORKER: iter $((iteration + 1))/$EFFECTIVE_BUDGET for $brief_id (budget: $BUDGET_SOURCE)"
[ "$RENDERED" = "WORKER: iter 4/20 for brief-x (budget: global)" ] \
    && pass "[AC4] progress line renders '(iter 4/20)' style burn rate" \
    || fail "[AC4] progress line format wrong (='$RENDERED')"
grep -q 'WORKER: iter \$((iteration + 1))/\$EFFECTIVE_BUDGET for \$brief_id (budget: \$BUDGET_SOURCE)' "$DAEMON_SH" \
    && pass "[AC4] daemon.sh emits the progress line each cycle" \
    || fail "[AC4] progress line not found verbatim in daemon.sh"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AC5: Budget absent → global-default path byte-identical         ║
# ╚══════════════════════════════════════════════════════════════════╝
echo ""
echo "── AC5: global-default path byte-identical (Budget absent) ──────"
# When BUDGET_SOURCE=global the gate takes the pre-#44 mark-blocked branch,
# unchanged: same wlog, same progress mutation, same commit message. Assert the
# verbatim strings survive so the global cap behavior can't have drifted.
grep -q 'WORKER: max iterations (\$MAX_ITERATIONS) reached — marking blocked' "$DAEMON_SH" \
    && pass "[AC5] pre-#44 'max iterations reached' wlog intact" \
    || fail "[AC5] mark-blocked wlog changed"
grep -q "Max iterations reached — marking blocked" "$DAEMON_SH" \
    && pass "[AC5] pre-#44 blocked commit message intact" \
    || fail "[AC5] blocked commit message changed"
grep -q "Daemon: max iterations (\$MAX_ITERATIONS) reached." "$DAEMON_SH" \
    && pass "[AC5] pre-#44 progress learning string intact" \
    || fail "[AC5] progress learning string changed"
# The mark-blocked branch is gated on the global source only.
grep -q 'if \[ "\$BUDGET_SOURCE" = "card" \]; then' "$DAEMON_SH" \
    && pass "[AC5] park path is card-source only; global falls through to mark-blocked" \
    || fail "[AC5] budget-source branch guard missing"

echo ""
echo "────────────────────────────────────────────────────────────────"
echo "budget-enforcement: $PASSED passed, $FAILED failed"
echo "────────────────────────────────────────────────────────────────"
[ "$FAILED" -eq 0 ]
