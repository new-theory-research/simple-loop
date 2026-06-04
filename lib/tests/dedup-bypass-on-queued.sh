#!/usr/bin/env bash
# Regression test for the queue-aware dedup bypass:
#
# When the daemon is idle and dedup is live under trigger reason `no_active`,
# filing a new brief into the queue (Status: queued card + goals.md mention)
# doesn't change the trigger reason — so without this bypass the daemon
# would silently swallow the new brief for up to 1800s (the full TTL).
#
# Block under test is reproduced here so the asserted logic stays in lockstep
# with lib/daemon.sh's queue-aware bypass block.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
QUEUE_PY="$LIB_DIR/queue.py"

PASSED=0
FAILED=0
pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Block under test — mirrors lib/daemon.sh's queue-aware dedup bypass.
should_bypass_dedup_for_queue() {
    local reason="$1"
    local project_dir="$2"
    [ "$reason" = "no_active" ] || return 1
    local count
    count=$(python3 "$QUEUE_PY" "$project_dir" 2>/dev/null \
        | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null \
        || echo "0")
    [ "${count:-0}" -gt 0 ]
}

# Create a minimal project layout with optional queued card(s) and a goals.md
# that mentions them. queue.py's enumerate_dispatchable() does the rest.
make_project() {
    local root="$1"
    shift
    mkdir -p "$root/.loop/state" "$root/wiki/briefs/cards"
    # Empty running.json (no active, awaiting_review, pending_merges, etc.).
    echo '{"active":[],"awaiting_review":[],"pending_merges":[],"completed_pending_eval":[],"history":[]}' \
        > "$root/.loop/state/running.json"
    : > "$root/.loop/state/goals.md"
    for brief in "$@"; do
        local card_dir="$root/wiki/briefs/cards/$brief"
        mkdir -p "$card_dir"
        cat > "$card_dir/index.md" <<EOF
---
Status: queued
---

# $brief
EOF
        echo "1. $brief" >> "$root/.loop/state/goals.md"
    done
}

# ── Case 1: idle daemon, new brief filed → bypass fires ──────────────────────
# This is the wedge scav described: dedup says skip, but the queue gained a
# brief — bypass must override and let the queen run.
P="$TMP/c1"; make_project "$P" "brief-300-newly-filed"
if should_bypass_dedup_for_queue "no_active" "$P"; then
    pass "queue with one new brief → bypass fires"
else
    fail "queue with one new brief → bypass did NOT fire (would re-wedge the daemon)"
fi

# ── Case 2: idle daemon, no queued briefs → no bypass ────────────────────────
# When the queue is genuinely empty, the dedup is correct — don't override.
P="$TMP/c2"; make_project "$P"  # no briefs
if should_bypass_dedup_for_queue "no_active" "$P"; then
    fail "empty queue → bypass fired unnecessarily (would queen-spam on idle)"
else
    pass "empty queue → bypass does not fire"
fi

# ── Case 3: bypass only triggers on no_active, not on other reasons ──────────
# Other reasons (stale_brief, validator_blocked, brief_blocked) have their own
# clear paths and shouldn't be bypassed by mere queue membership.
P="$TMP/c3"; make_project "$P" "brief-301-foo"
for reason in stale_brief brief_blocked validator_blocked pending_eval active_signal; do
    if should_bypass_dedup_for_queue "$reason" "$P"; then
        fail "$reason → bypass should NOT fire (only no_active is queue-aware)"
    else
        pass "$reason → bypass correctly inert"
    fi
done

# ── Case 4: card not in goals.md still counts (queue.py sorts last but emits) ─
# Filing a card without editing goals.md was hitting Mattie too — verify
# bypass still fires.
P="$TMP/c4"; mkdir -p "$P/.loop/state" "$P/wiki/briefs/cards/brief-302-no-goals-entry"
echo '{"active":[],"awaiting_review":[],"pending_merges":[],"completed_pending_eval":[],"history":[]}' \
    > "$P/.loop/state/running.json"
: > "$P/.loop/state/goals.md"
cat > "$P/wiki/briefs/cards/brief-302-no-goals-entry/index.md" <<'EOF'
---
Status: queued
---

# brief-302
EOF
if should_bypass_dedup_for_queue "no_active" "$P"; then
    pass "queued card not in goals.md → bypass still fires"
else
    fail "queued card not in goals.md → bypass missed it"
fi

# ── Case 5: card with Status: draft does NOT count ───────────────────────────
P="$TMP/c5"; mkdir -p "$P/.loop/state" "$P/wiki/briefs/cards/brief-303-draft"
echo '{"active":[],"awaiting_review":[],"pending_merges":[],"completed_pending_eval":[],"history":[]}' \
    > "$P/.loop/state/running.json"
: > "$P/.loop/state/goals.md"
cat > "$P/wiki/briefs/cards/brief-303-draft/index.md" <<'EOF'
---
Status: draft
---

# brief-303
EOF
if should_bypass_dedup_for_queue "no_active" "$P"; then
    fail "non-queued status → bypass fired (should require Status: queued)"
else
    pass "non-queued status → bypass correctly inert"
fi

# ── Case 6: card already in running.json#active does NOT count ───────────────
# This protects against busting dedup just because of an existing active brief.
P="$TMP/c6"; mkdir -p "$P/.loop/state" "$P/wiki/briefs/cards/brief-304-active"
cat > "$P/.loop/state/running.json" <<'EOF'
{"active":[{"brief":"brief-304-active"}],"awaiting_review":[],"pending_merges":[],"completed_pending_eval":[],"history":[]}
EOF
: > "$P/.loop/state/goals.md"
cat > "$P/wiki/briefs/cards/brief-304-active/index.md" <<'EOF'
---
Status: queued
---

# brief-304
EOF
if should_bypass_dedup_for_queue "no_active" "$P"; then
    fail "already-active brief → bypass fired (queue.py should exclude it)"
else
    pass "already-active brief → bypass correctly inert"
fi

echo ""
echo "Passed: $PASSED   Failed: $FAILED"
[ "$FAILED" -eq 0 ]
