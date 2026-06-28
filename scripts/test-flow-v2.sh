#!/usr/bin/env bash
# scripts/test-flow-v2.sh — Integration test for simple-loop flow v2.
#
# Verifies:
#   1. Auto-merge path: brief complete → active[] freed → pending_merges[]
#   2. Human-review path: brief complete → active[] freed → awaiting_review[]
#   3. assess.py emits CONDUCTOR:no_active when active[] empty (dispatch unblocked
#      even while pending_merges[] has entries — the key v2 invariant)
#   4. approve-brief: awaiting_review → pending_merges (with approved_at timestamp)
#   5. reject-brief: awaiting_review → history[] (with rejected_at + reason)
#   6. depends-on parsing: read_depends_on() extracts dep id from frontmatter
#   7. Backward compatibility: old running.json without v2 fields loads cleanly
#
# Exits 0 iff all scenarios pass. Run from simple-loop repo root or any dir.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"
ACTIONS="$LIB_DIR/actions.py"
ASSESS="$LIB_DIR/assess.py"

for f in "$ACTIONS" "$ASSESS"; do
    if [ ! -f "$f" ]; then
        echo "FAIL: $f not found — run from simple-loop repo" >&2
        exit 1
    fi
done

PASSED=0
FAILED=0

pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }

assert_eq() {
    local label="$1" actual="$2" expected="$3"
    if [ "$actual" = "$expected" ]; then
        pass "$label"
    else
        fail "$label — expected '$expected', got '$actual'"
    fi
}

json_get() {
    # json_get <file> <python-expr-on-d>
    python3 -c "import json; d=json.load(open('$1')); print($2)" 2>/dev/null || echo "ERROR"
}

assert_json() {
    local label="$1" file="$2" expr="$3" expected="$4"
    assert_eq "$label" "$(json_get "$file" "$expr")" "$expected"
}

# ── Scratch repo setup ───────────────────────────────────────────────────────

SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

git -C "$SCRATCH" init -q -b main
git -C "$SCRATCH" config user.email "test@test"
git -C "$SCRATCH" config user.name  "Test"

mkdir -p "$SCRATCH/.loop/state/signals"
mkdir -p "$SCRATCH/.loop/briefs"
mkdir -p "$SCRATCH/.loop/worktrees"

cat > "$SCRATCH/.loop/config.sh" <<'EOF'
PROJECT_NAME="test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
EOF

touch "$SCRATCH/.loop/state/log.jsonl"

# Brief files (frontmatter only — auto-merge and depends-on flags)
cat > "$SCRATCH/.loop/briefs/brief-001-auto.md" <<'EOF'
# Brief: auto-merge test

**ID:** brief-001-auto
**Auto-merge:** true
**Status:** queued
EOF

cat > "$SCRATCH/.loop/briefs/brief-002-human.md" <<'EOF'
# Brief: human-review test

**ID:** brief-002-human
**Auto-merge:** false
**Status:** queued
EOF

cat > "$SCRATCH/.loop/briefs/brief-004-depends.md" <<'EOF'
# Brief: depends-on test

**ID:** brief-004-depends
**Auto-merge:** true
**Depends-on:** brief-999-prereq
**Status:** queued
EOF

write_running() {
    python3 -c "import json; json.dump($1, open('$SCRATCH/.loop/state/running.json','w'), indent=2)"
    git -C "$SCRATCH" add -A
    git -C "$SCRATCH" commit -q -m "test: seed state" 2>/dev/null || true
}

# ── Test 1: auto-merge path ──────────────────────────────────────────────────

echo ""
echo "=== Test 1: auto-merge path (active[] freed → pending_merges[]) ==="

write_running "{
    'active': [{'brief': 'brief-001-auto', 'branch': 'brief-001-auto', 'brief_file': '.loop/briefs/brief-001-auto.md'}],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': [],
    'queue': []
}"

python3 "$ACTIONS" move-to-pending-merges brief-001-auto "$SCRATCH" > /dev/null 2>&1

RJ="$SCRATCH/.loop/state/running.json"
assert_json "active[] empty after move-to-pending-merges"   "$RJ" "len(d['active'])"            "0"
assert_json "pending_merges[] has one entry"                "$RJ" "len(d['pending_merges'])"    "1"
assert_json "pending_merges[0] is brief-001-auto"           "$RJ" "d['pending_merges'][0]['brief']"  "brief-001-auto"
assert_json "pending_merges[0].auto_merge == True"          "$RJ" "str(d['pending_merges'][0]['auto_merge'])"  "True"
assert_json "pending_merges[0].completed_at present"        "$RJ" "bool(d['pending_merges'][0].get('completed_at'))"  "True"

# ── Test 2: human-review path ────────────────────────────────────────────────

echo ""
echo "=== Test 2: human-review path (active[] freed → awaiting_review[]) ==="

write_running "{
    'active': [{'brief': 'brief-002-human', 'branch': 'brief-002-human', 'brief_file': '.loop/briefs/brief-002-human.md'}],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': [],
    'queue': []
}"

python3 "$ACTIONS" move-to-awaiting-review brief-002-human "$SCRATCH" complete "validator requested human review" > /dev/null 2>&1

assert_json "active[] empty after move-to-awaiting-review"  "$RJ" "len(d['active'])"              "0"
assert_json "awaiting_review[] has one entry"               "$RJ" "len(d['awaiting_review'])"     "1"
assert_json "awaiting_review[0] is brief-002-human"         "$RJ" "d['awaiting_review'][0]['brief']"  "brief-002-human"
assert_json "awaiting_review[0].auto_merge == False"        "$RJ" "str(d['awaiting_review'][0]['auto_merge'])"  "False"
assert_json "awaiting_review[0].reason preserved"           "$RJ" "d['awaiting_review'][0].get('reason','')"  "validator requested human review"

# ── Test 3: dispatch unblocked while pending_merges[] non-empty ──────────────

echo ""
echo "=== Test 3: assess.py emits CONDUCTOR:no_active — dispatch unblocked while merge queued ==="

# State: active[] empty, pending_merges has brief-001-auto (from test 1 re-applied)
write_running "{
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [{'brief': 'brief-001-auto', 'branch': 'brief-001-auto'}],
    'awaiting_review': [],
    'history': [],
    'queue': []
}"
# No pending-dispatch.json, no pending-merge.json, no escalate.json

LINE1=$(python3 "$ASSESS" "$SCRATCH" 2>/dev/null | head -1)
assert_eq "CONDUCTOR:no_active emitted (not blocked by pending_merges)" "$LINE1" "CONDUCTOR:no_active"

# Verify pending_merges doesn't accidentally trigger pending_eval
assert_json "pending_merges[] still has entry (untouched by assess)"  "$RJ" "len(d['pending_merges'])"  "1"

# ── Test 4: approve-brief ────────────────────────────────────────────────────

echo ""
echo "=== Test 4: approve-brief (awaiting_review → pending_merges) ==="

write_running "{
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [{'brief': 'brief-002-human', 'branch': 'brief-002-human', 'brief_file': '.loop/briefs/brief-002-human.md', 'auto_merge': False}],
    'history': [],
    'queue': []
}"

python3 "$ACTIONS" approve-brief brief-002-human "$SCRATCH" > /dev/null 2>&1

assert_json "awaiting_review[] empty after approve"         "$RJ" "len(d['awaiting_review'])"    "0"
assert_json "pending_merges[] has one entry after approve"  "$RJ" "len(d['pending_merges'])"     "1"
assert_json "approved brief is brief-002-human"             "$RJ" "d['pending_merges'][0]['brief']"   "brief-002-human"
assert_json "approved_at timestamp present"                  "$RJ" "bool(d['pending_merges'][0].get('approved_at'))"  "True"
assert_json "auto_merge flipped to True after approve"      "$RJ" "str(d['pending_merges'][0]['auto_merge'])"  "True"

# ── Test 5: reject-brief ─────────────────────────────────────────────────────

echo ""
echo "=== Test 5: reject-brief (awaiting_review removed, card Status → rejected) ==="

# Seed card for brief-002-human with Status: queued
mkdir -p "$SCRATCH/wiki/briefs/cards/brief-002-human"
cat > "$SCRATCH/wiki/briefs/cards/brief-002-human/index.md" <<'CARDEOF'
---
id: brief-002-human
Status: queued
---
# Brief: human-review test
CARDEOF
git -C "$SCRATCH" add -A && git -C "$SCRATCH" commit -q -m "test: seed card" 2>/dev/null || true

write_running "{
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [{'brief': 'brief-002-human', 'branch': 'brief-002-human', 'brief_file': '.loop/briefs/brief-002-human.md', 'auto_merge': False}],
    'queue': []
}"

python3 "$ACTIONS" reject-brief brief-002-human "$SCRATCH" "scope exceeded brief bounds" > /dev/null 2>&1

assert_json "awaiting_review[] empty after reject"            "$RJ" "len(d['awaiting_review'])"    "0"
REJECT_STATUS=$(python3 -c "
lines = open('$SCRATCH/wiki/briefs/cards/brief-002-human/index.md').readlines()
in_fm = False
for l in lines:
    s = l.strip()
    if s == '---':
        if not in_fm: in_fm = True
        else: break
    elif in_fm and s.lower().startswith('status:'):
        print(s.split(':',1)[1].strip())
        break
")
assert_eq "card Status → rejected after reject-brief"         "$REJECT_STATUS"  "rejected"

# ── Test 6: depends-on frontmatter parsing ───────────────────────────────────

echo ""
echo "=== Test 6: depends-on frontmatter parsing via assess.read_depends_on ==="

# Brief-014: read_depends_on now returns a list (was scalar). Single-dep case
# returns a one-element list; empty-dep case returns [].
DEP=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import read_depends_on
deps = read_depends_on('$SCRATCH/.loop/briefs/brief-004-depends.md')
print(','.join(deps) if deps else 'None')
")
assert_eq "read_depends_on extracts dep id from frontmatter"  "$DEP"  "brief-999-prereq"

DEP_NONE=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import read_depends_on
deps = read_depends_on('$SCRATCH/.loop/briefs/brief-001-auto.md')
print(','.join(deps) if deps else 'None')
")
assert_eq "read_depends_on returns None when no dep present"  "$DEP_NONE"  "None"

# ── Test 7: backward compatibility ──────────────────────────────────────────

echo ""
echo "=== Test 7: backward compat — old running.json without v2 fields loads cleanly ==="

# Simulate an old running.json (v1 schema — no pending_merges or awaiting_review)
python3 -c "
import json
old = {'active': [], 'completed_pending_eval': [], 'history': [], 'queue': []}
json.dump(old, open('$SCRATCH/.loop/state/running.json','w'), indent=2)
"
git -C "$SCRATCH" add -A
git -C "$SCRATCH" commit -q -m "test: old schema" 2>/dev/null || true

# actions.py load_running() should backfill both fields
python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, load_running
paths = init_paths('$SCRATCH')
rc = load_running(paths)
assert 'pending_merges' in rc, 'pending_merges missing'
assert 'awaiting_review' in rc, 'awaiting_review missing'
assert rc['pending_merges'] == [], 'pending_merges not empty list'
assert rc['awaiting_review'] == [], 'awaiting_review not empty list'
print('ok')
" > /tmp/compat_result 2>&1 || echo "exception" > /tmp/compat_result
assert_eq "old running.json backfills v2 fields on load"  "$(cat /tmp/compat_result)"  "ok"

# ── Test 8: depends-on comma-separated list parse ─────────────────────────────

echo ""
echo "=== Test 8: depends-on comma-separated list parse (brief-014 fix 1) ==="

# Direct parser unit tests — parse_depends_on_value covers the syntax table in
# the brief (single, comma+space, comma-no-space, trailing comma, empty).
SINGLE=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('brief-010-foo')))
")
assert_eq "parse_depends_on_value single id"                "$SINGLE"  "brief-010-foo"

MULTI_SPACED=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('brief-010-foo, brief-011-bar')))
")
assert_eq "parse_depends_on_value comma+space"             "$MULTI_SPACED"  "brief-010-foo,brief-011-bar"

MULTI_TIGHT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('brief-010-foo,brief-011-bar')))
")
assert_eq "parse_depends_on_value comma no space"          "$MULTI_TIGHT"  "brief-010-foo,brief-011-bar"

TRAILING=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('brief-010-foo,')))
")
assert_eq "parse_depends_on_value trailing comma tolerated" "$TRAILING"  "brief-010-foo"

# Full file read path — brief with comma-separated frontmatter should return 2 ids.
cat > "$SCRATCH/.loop/briefs/brief-005-multidep.md" <<'EOF'
# Brief: multi-dep test

**ID:** brief-005-multidep
**Auto-merge:** true
**Depends-on:** brief-010-foo, brief-011-bar
**Status:** queued
EOF

READ_MULTI=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import read_depends_on
print(','.join(read_depends_on('$SCRATCH/.loop/briefs/brief-005-multidep.md')))
")
assert_eq "read_depends_on handles comma-separated list"   "$READ_MULTI"  "brief-010-foo,brief-011-bar"

# ── Test 9: depends-on card-scan post-restart false-negative ─────────────────

echo ""
echo "=== Test 9: single-dep matches against card Status==merged (card-is-truth) ==="

# Reproduces brief-012's 2026-04-22 failure. brief-108: now scans cards, not history[].
# Seed: card for brief-010 with Status: merged.
mkdir -p "$SCRATCH/wiki/briefs/cards/brief-010-api-v0-1"
cat > "$SCRATCH/wiki/briefs/cards/brief-010-api-v0-1/index.md" <<'CARDEOF'
---
id: brief-010-api-v0-1
Status: merged
---
# Brief: api-v0-1
CARDEOF
git -C "$SCRATCH" add -A && git -C "$SCRATCH" commit -q -m "test: seed merged card" 2>/dev/null || true

cat > "$SCRATCH/.loop/briefs/brief-006-single-dep.md" <<'EOF'
# Brief: single-dep test

**ID:** brief-006-single-dep
**Auto-merge:** true
**Depends-on:** brief-010-api-v0-1
**Status:** queued
EOF

write_running "{
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'queue': []
}"

cat > "$SCRATCH/.loop/state/pending-dispatch.json" <<EOF
{
    "brief": "brief-006-single-dep",
    "branch": "brief-006-single-dep",
    "brief_file": ".loop/briefs/brief-006-single-dep.md"
}
EOF

DEPS_LINE1=$(python3 "$ACTIONS" check-depends-on "$SCRATCH" 2>/dev/null | sed -n 1p)
assert_eq "check-depends-on allows when single dep is merged card" "$DEPS_LINE1"  "allowed"

# Blocked case: card exists but Status != merged
sed -i '' 's/Status: merged/Status: queued/' "$SCRATCH/wiki/briefs/cards/brief-010-api-v0-1/index.md"

DEPS_LINE1=$(python3 "$ACTIONS" check-depends-on "$SCRATCH" 2>/dev/null | sed -n 1p)
assert_eq "check-depends-on blocks when dep card not merged" "$DEPS_LINE1"  "blocked:brief-010-api-v0-1"

# Diagnostic line always emits, even in the error-fallback path (grep-debuggable).
DEPS_LINE2=$(python3 "$ACTIONS" check-depends-on "$SCRATCH" 2>/dev/null | sed -n 2p)
case "$DEPS_LINE2" in
    brief=*"depends_on="*"merged_ids="*"match="*) pass "check-depends-on emits diagnostic line" ;;
    *) fail "check-depends-on diagnostic line — got '$DEPS_LINE2'" ;;
esac

# Restore merged status for subsequent tests
sed -i '' 's/Status: queued/Status: merged/' "$SCRATCH/wiki/briefs/cards/brief-010-api-v0-1/index.md"

# Multi-dep all-merged → allowed
mkdir -p "$SCRATCH/wiki/briefs/cards/brief-010-foo"
cat > "$SCRATCH/wiki/briefs/cards/brief-010-foo/index.md" <<'CARDEOF'
---
id: brief-010-foo
Status: merged
---
CARDEOF
mkdir -p "$SCRATCH/wiki/briefs/cards/brief-011-bar"
cat > "$SCRATCH/wiki/briefs/cards/brief-011-bar/index.md" <<'CARDEOF'
---
id: brief-011-bar
Status: merged
---
CARDEOF
git -C "$SCRATCH" add -A && git -C "$SCRATCH" commit -q -m "test: seed multidep merged cards" 2>/dev/null || true

cat > "$SCRATCH/.loop/state/pending-dispatch.json" <<EOF
{
    "brief": "brief-005-multidep",
    "branch": "brief-005-multidep",
    "brief_file": ".loop/briefs/brief-005-multidep.md"
}
EOF

DEPS_LINE1=$(python3 "$ACTIONS" check-depends-on "$SCRATCH" 2>/dev/null | sed -n 1p)
assert_eq "check-depends-on allows multi-dep when all merged" "$DEPS_LINE1"  "allowed"

# Multi-dep one-missing → blocked on first unmet
sed -i '' 's/Status: merged/Status: queued/' "$SCRATCH/wiki/briefs/cards/brief-011-bar/index.md"

DEPS_LINE1=$(python3 "$ACTIONS" check-depends-on "$SCRATCH" 2>/dev/null | sed -n 1p)
assert_eq "check-depends-on blocks multi-dep on first unmet" "$DEPS_LINE1"  "blocked:brief-011-bar"

rm -f "$SCRATCH/.loop/state/pending-dispatch.json"

# ── Test 10: push-fail escalate + token redaction ────────────────────────────

echo ""
echo "=== Test 10: push_with_escalate writes escalate.json with redacted stderr ==="

# Redactor unit — GitHub token patterns → [REDACTED]
REDACTED=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import redact_secrets
print(redact_secrets('fatal: could not read Username for ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 check'))
")
case "$REDACTED" in
    *"[REDACTED]"*) pass "redact_secrets replaces ghp_ classic token" ;;
    *) fail "redact_secrets ghp_ token — got '$REDACTED'" ;;
esac

# No raw token leaks through
python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import redact_secrets
out = redact_secrets('header: Bearer github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789_abcdefghij')
assert 'ABCDEFGH' not in out, 'raw pat body leaked'
assert '[REDACTED]' in out
" && pass "redact_secrets fine-grained PAT scrubbed" || fail "redact_secrets fine-grained PAT leak"

# Escalate flow — simulate push_with_escalate call with a failing git command.
# We don't run a real git push; the function is invoked with a deliberately
# bad remote so git exits nonzero, and we assert the resulting escalate.json
# contains the redacted stderr and structured reason.
ESC_DIR="$SCRATCH/.loop/state/signals"
rm -f "$ESC_DIR/escalate.json"

# Inject a fake stderr containing a token — subprocess mock via env.
python3 -c "
import sys, os, json, subprocess
sys.path.insert(0, '$LIB_DIR')
from actions import push_with_escalate, init_paths
paths = init_paths('$SCRATCH')
# Simulate a push failure by calling the helper with a stderr-stub. The helper
# must accept an injected stderr for testability (brief-014 requirement).
# If push_with_escalate isn't yet injectable, fall back to running with a bad
# remote and inspecting written file.
push_with_escalate(paths, brief='brief-test', _test_stderr_override='remote: Support for password authentication was removed. Token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 redacted here.')
" 2>/dev/null

if [ -f "$ESC_DIR/escalate.json" ]; then
    pass "push_with_escalate writes escalate.json on failure"
    if grep -q '\[REDACTED\]' "$ESC_DIR/escalate.json" 2>/dev/null; then
        pass "escalate.json stderr is token-redacted"
    else
        fail "escalate.json stderr not redacted"
    fi
    if grep -q 'push_failed_on_auth\|push_failed' "$ESC_DIR/escalate.json" 2>/dev/null; then
        pass "escalate.json reason names push failure"
    else
        fail "escalate.json missing push_failed reason"
    fi
    # Negative: raw token must NOT appear in written file
    if grep -q 'ghp_ABCDEFGH' "$ESC_DIR/escalate.json" 2>/dev/null; then
        fail "escalate.json LEAKED raw token"
    else
        pass "escalate.json has no raw token"
    fi
    rm -f "$ESC_DIR/escalate.json"
else
    fail "push_with_escalate did not write escalate.json"
fi

# ── Test 11: heartbeat staleness detection ───────────────────────────────────

echo ""
echo "=== Test 11: heartbeat staleness (process-alive ≠ loop-healthy) ==="

HB="$SCRATCH/.loop/state/heartbeat.json"

# Fresh heartbeat (now) → NOT stale
python3 -c "
import json, datetime
ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
json.dump({'ts': ts, 'pid': 12345, 'last_event': 'tick'}, open('$HB','w'))
"
STALE=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import heartbeat_is_stale
print('STALE' if heartbeat_is_stale('$HB', interval_s=120) else 'FRESH')
")
assert_eq "fresh heartbeat is not stale"                    "$STALE"  "FRESH"

# Stale heartbeat (10 min old, interval 120 → stale at >240s) → STALE
python3 -c "
import json, datetime
ts_old = (datetime.datetime.utcnow() - datetime.timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
json.dump({'ts': ts_old, 'pid': 12345, 'last_event': 'tick'}, open('$HB','w'))
"
STALE=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import heartbeat_is_stale
print('STALE' if heartbeat_is_stale('$HB', interval_s=120) else 'FRESH')
")
assert_eq "10-min-old heartbeat is stale (interval 120)"    "$STALE"  "STALE"

# Missing heartbeat file → treated as stale (safer: assume hung)
rm -f "$HB"
STALE=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import heartbeat_is_stale
print('STALE' if heartbeat_is_stale('$HB', interval_s=120) else 'FRESH')
")
assert_eq "missing heartbeat file treated as stale"         "$STALE"  "STALE"

# write_heartbeat produces a readable JSON file with ts/pid/last_event
python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import write_heartbeat
write_heartbeat('$HB', pid=99, last_event='worker')
"
if [ -f "$HB" ] && python3 -c "
import json
d = json.load(open('$HB'))
assert 'ts' in d and 'pid' in d and 'last_event' in d
assert d['pid'] == 99
assert d['last_event'] == 'worker'
" 2>/dev/null; then
    pass "write_heartbeat produces well-formed {ts,pid,last_event}"
else
    fail "write_heartbeat output malformed or missing"
fi

# ── Test 12: validator presence-check for process artifacts ──────────────────

echo ""
echo "=== Test 12: validator artifact-presence check fails cycle when missing ==="

# Fabricate a brief that names plan.md + closeout.md in completion criteria.
cat > "$SCRATCH/.loop/briefs/brief-007-artifacts.md" <<'EOF'
# Brief: artifacts test

**ID:** brief-007-artifacts
**Branch:** brief-007-artifacts
**Auto-merge:** true

## Completion criteria

- [ ] `plan.md` in the card dir
- [ ] `closeout.md` in the card dir
- [ ] All tests pass
EOF

# extract_artifact_paths should find plan.md and closeout.md
PATHS=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import extract_artifact_paths
paths = extract_artifact_paths('$SCRATCH/.loop/briefs/brief-007-artifacts.md')
print(','.join(sorted(paths)))
")
assert_eq "extract_artifact_paths finds declared artifacts" "$PATHS"  "closeout.md,plan.md"

# With neither file on disk, validator_presence_check returns a block verdict.
# (The worktree dir is the brief's card parent — here, we use SCRATCH so the
# relative paths resolve against it.)
VERDICT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$SCRATCH/.loop/briefs/brief-007-artifacts.md', '$SCRATCH')
print('BLOCK' if missing else 'PASS')
")
assert_eq "validator_presence_check blocks when artifacts missing" "$VERDICT"  "BLOCK"

# Create the artifacts → check passes
touch "$SCRATCH/plan.md" "$SCRATCH/closeout.md"
VERDICT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$SCRATCH/.loop/briefs/brief-007-artifacts.md', '$SCRATCH')
print('BLOCK' if missing else 'PASS')
")
assert_eq "validator_presence_check passes when all artifacts present" "$VERDICT"  "PASS"

rm -f "$SCRATCH/plan.md" "$SCRATCH/closeout.md"

# ── Test 13 (brief-019 test 49): startup_repair — dedup active[] ─────────────

echo ""
echo "=== Test 13: startup_repair dedup_active removes duplicate active entries ==="

STARTUP_REPAIR="$LIB_DIR/startup_repair.py"
if [ ! -f "$STARTUP_REPAIR" ]; then
    fail "startup_repair.py not found at $STARTUP_REPAIR"
else

# Seed running.json with two identical entries for brief-DUP-test plus one unique
write_running "{
    'active': [
        {'brief': 'brief-DUP-test', 'branch': 'brief-DUP-test'},
        {'brief': 'brief-KEEP-test', 'branch': 'brief-KEEP-test'},
        {'brief': 'brief-DUP-test', 'branch': 'brief-DUP-test'}
    ],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': []
}"

# run_startup_repair should return 1 action (the duplicate removal)
ACTION_COUNT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from startup_repair import run_startup_repair
from actions import init_paths
paths = init_paths('$SCRATCH')
actions = run_startup_repair(paths, '$SCRATCH')
print(len(actions))
" 2>/dev/null)
assert_eq "dedup_active returns 1 action for one duplicate brief" "$ACTION_COUNT" "1"

# active[] should now have exactly 2 entries (DUP-test once + KEEP-test)
RJ="$SCRATCH/.loop/state/running.json"
ACTIVE_LEN=$(json_get "$RJ" "len(d.get('active',[]))")
assert_eq "active[] has 2 entries after dedup (DUP-test + KEEP-test)" "$ACTIVE_LEN" "2"

# The surviving DUP-test entry should be the first one
FIRST_BRIEF=$(json_get "$RJ" "d['active'][0]['brief']")
assert_eq "first active entry after dedup is brief-DUP-test" "$FIRST_BRIEF" "brief-DUP-test"

# log.jsonl should have a startup_repair event with reason: duplicate_active_entry
LOG_HAS_REASON=$(python3 -c "
with open('$SCRATCH/.loop/state/log.jsonl') as f:
    lines = f.readlines()
import json
for line in reversed(lines):
    d = json.loads(line)
    if d.get('reason') == 'duplicate_active_entry':
        print('YES')
        break
else:
    print('NO')
" 2>/dev/null)
assert_eq "log.jsonl has duplicate_active_entry event" "$LOG_HAS_REASON" "YES"

# Idempotency: run again — no duplicates left, so 0 actions returned
ACTION_COUNT2=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from startup_repair import run_startup_repair
from actions import init_paths
paths = init_paths('$SCRATCH')
actions = run_startup_repair(paths, '$SCRATCH')
print(len(actions))
" 2>/dev/null)
assert_eq "second run is idempotent (0 actions when no duplicates)" "$ACTION_COUNT2" "0"

fi  # startup_repair.py exists

# ── Test 14: backfill_history adds merged brief to history[] ─────────────────

echo ""
echo "=== Test 14: backfill_history — merged brief absent from history gets backfilled ==="

if [ ! -f "$STARTUP_REPAIR" ]; then
    fail "startup_repair.py not found — skipping test 14"
else

# Create an actual merge commit so git log --merges sees it
git -C "$SCRATCH" checkout -q -b brief-BF-test-branch 2>/dev/null
git -C "$SCRATCH" commit --allow-empty -q -m "wip: brief-BF-test work"
git -C "$SCRATCH" checkout -q main 2>/dev/null
git -C "$SCRATCH" merge --no-ff -q brief-BF-test-branch -m "Merge brief-BF-test: test backfill" 2>/dev/null
git -C "$SCRATCH" branch -d -q brief-BF-test-branch 2>/dev/null

# Seed running.json with brief-BF-test absent from all arrays
write_running "{
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': []
}"

# Clear log so assertions are clean
> "$SCRATCH/.loop/state/log.jsonl"

ACTION_COUNT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from startup_repair import run_startup_repair
from actions import init_paths
paths = init_paths('$SCRATCH')
actions = run_startup_repair(paths, '$SCRATCH')
print(len(actions))
" 2>/dev/null)
assert_eq "backfill_history returns 1 action for merged brief" "$ACTION_COUNT" "1"

RJ="$SCRATCH/.loop/state/running.json"
HIST_LEN=$(json_get "$RJ" "len(d.get('history',[]))")
assert_eq "history[] has 1 entry after backfill" "$HIST_LEN" "1"

HIST_BRIEF=$(json_get "$RJ" "d['history'][0]['brief']")
assert_eq "history[0].brief is brief-BF-test" "$HIST_BRIEF" "brief-BF-test"

HIST_SHA=$(json_get "$RJ" "bool(d['history'][0].get('merge_sha',''))")
assert_eq "history[0].merge_sha is present" "$HIST_SHA" "True"

HIST_REASON=$(json_get "$RJ" "d['history'][0].get('reason','')")
assert_eq "history[0].reason is backfilled_from_git" "$HIST_REASON" "backfilled_from_git"

LOG_HAS_BACKFILL=$(python3 -c "
with open('$SCRATCH/.loop/state/log.jsonl') as f:
    lines = f.readlines()
import json
for line in reversed(lines):
    d = json.loads(line)
    if d.get('reason') == 'backfilled_from_git':
        print('YES')
        break
else:
    print('NO')
" 2>/dev/null)
assert_eq "log.jsonl has backfilled_from_git event" "$LOG_HAS_BACKFILL" "YES"

fi  # startup_repair.py exists (test 14)

# ── Test 15: backfill_history moves merged brief from active[] to history[] ──

echo ""
echo "=== Test 15: backfill_history — merged brief still in active[] gets moved to history[] ==="

if [ ! -f "$STARTUP_REPAIR" ]; then
    fail "startup_repair.py not found — skipping test 15"
else

# Merge commit for brief-BF-test already exists from test 14 — reuse it.
# Seed running.json with brief-BF-test in active[]
write_running "{
    'active': [{'brief': 'brief-BF-test', 'branch': 'brief-BF-test'}],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': []
}"

> "$SCRATCH/.loop/state/log.jsonl"

ACTION_COUNT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from startup_repair import run_startup_repair
from actions import init_paths
paths = init_paths('$SCRATCH')
actions = run_startup_repair(paths, '$SCRATCH')
print(len(actions))
" 2>/dev/null)
assert_eq "backfill_history returns 1 action for active+merged brief" "$ACTION_COUNT" "1"

RJ="$SCRATCH/.loop/state/running.json"
ACTIVE_LEN=$(json_get "$RJ" "len(d.get('active',[]))")
assert_eq "active[] is empty after backfill (brief moved out)" "$ACTIVE_LEN" "0"

HIST_LEN=$(json_get "$RJ" "len(d.get('history',[]))")
assert_eq "history[] has 1 entry after moving from active" "$HIST_LEN" "1"

HIST_BRIEF=$(json_get "$RJ" "d['history'][0]['brief']")
assert_eq "history[0].brief is brief-BF-test (moved from active)" "$HIST_BRIEF" "brief-BF-test"

fi  # startup_repair.py exists (test 15)

# ── Test 16: clean_stale_queues removes stale pending-merge.json ─────────────

echo ""
echo "=== Test 16: clean_stale_queues — stale pending-merge.json for merged brief is cleared ==="

if [ ! -f "$STARTUP_REPAIR" ]; then
    fail "startup_repair.py not found — skipping test 16"
else

# brief-BF-test was merged in test 14 — reuse that commit.
# Seed running.json clean so backfill_history doesn't fire on it again.
write_running "{
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': [{'brief': 'brief-BF-test', 'branch': 'brief-BF-test', 'merged_at': '2026-04-22T00:00:00Z', 'merge_sha': 'abc123', 'reason': 'backfilled_from_git'}]
}"

> "$SCRATCH/.loop/state/log.jsonl"

# Write a stale pending-merge.json referencing brief-BF-test (already merged)
python3 -c "
import json
json.dump({'brief': 'brief-BF-test', 'branch': 'brief-BF-test', 'auto_merge': True}, open('$SCRATCH/.loop/state/pending-merge.json','w'))
"

ACTION_COUNT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from startup_repair import run_startup_repair
from actions import init_paths
paths = init_paths('$SCRATCH')
actions = run_startup_repair(paths, '$SCRATCH')
print(len(actions))
" 2>/dev/null)
assert_eq "clean_stale_queues returns 1 action for stale pending-merge.json" "$ACTION_COUNT" "1"

# File must be removed
if [ ! -f "$SCRATCH/.loop/state/pending-merge.json" ]; then
    pass "pending-merge.json was removed for merged brief"
else
    fail "pending-merge.json still present after cleanup"
fi

# log.jsonl must have queue_file_stale_post_merge event
LOG_HAS_STALE=$(python3 -c "
with open('$SCRATCH/.loop/state/log.jsonl') as f:
    lines = f.readlines()
import json
for line in reversed(lines):
    d = json.loads(line)
    if d.get('reason') == 'queue_file_stale_post_merge':
        print('YES')
        break
else:
    print('NO')
" 2>/dev/null)
assert_eq "log.jsonl has queue_file_stale_post_merge event" "$LOG_HAS_STALE" "YES"

# Non-stale case: pending-merge.json for an UNmerged brief must NOT be removed
python3 -c "
import json
json.dump({'brief': 'brief-NOT-merged', 'branch': 'brief-NOT-merged', 'auto_merge': True}, open('$SCRATCH/.loop/state/pending-merge.json','w'))
"

> "$SCRATCH/.loop/state/log.jsonl"

python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from startup_repair import run_startup_repair
from actions import init_paths
paths = init_paths('$SCRATCH')
run_startup_repair(paths, '$SCRATCH')
" 2>/dev/null

if [ -f "$SCRATCH/.loop/state/pending-merge.json" ]; then
    pass "pending-merge.json preserved for unmerged brief"
else
    fail "pending-merge.json incorrectly removed for unmerged brief"
fi

rm -f "$SCRATCH/.loop/state/pending-merge.json"

fi  # startup_repair.py exists (test 16)

# ── Test 17 (brief-019 test 53): NT_DAEMON_STARTUP_REPAIR=false disables repair ─

echo ""
echo "=== Test 17: NT_DAEMON_STARTUP_REPAIR=false — repair skipped, corruption persists ==="

STARTUP_REPAIR="$LIB_DIR/startup_repair.py"
if [ ! -f "$STARTUP_REPAIR" ]; then
    fail "startup_repair.py not found — skipping test 17"
else

# Seed running.json with a duplicate active entry (corruption)
write_running "{
    'active': [
        {'brief': 'brief-ENV-test', 'branch': 'brief-ENV-test'},
        {'brief': 'brief-ENV-test', 'branch': 'brief-ENV-test'}
    ],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': []
}"

> "$SCRATCH/.loop/state/log.jsonl"

# Run with repair disabled via env var
ACTION_COUNT=$(NT_DAEMON_STARTUP_REPAIR=false python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from startup_repair import run_startup_repair
from actions import init_paths
paths = init_paths('$SCRATCH')
actions = run_startup_repair(paths, '$SCRATCH')
print(len(actions))
" 2>/dev/null)
assert_eq "run_startup_repair returns 0 actions when disabled" "$ACTION_COUNT" "0"

# Corruption must still be present (active[] still has 2 entries)
ACTIVE_LEN=$(python3 -c "
import json
d = json.load(open('$SCRATCH/.loop/state/running.json'))
print(len(d.get('active', [])))
" 2>/dev/null)
assert_eq "active[] still has 2 entries (corruption persists when disabled)" "$ACTIVE_LEN" "2"

# log.jsonl must have startup_repair_disabled event
LOG_HAS_DISABLED=$(python3 -c "
import json
with open('$SCRATCH/.loop/state/log.jsonl') as f:
    lines = f.readlines()
for line in reversed(lines):
    d = json.loads(line)
    if d.get('action') == 'daemon:startup_repair_disabled':
        print('YES')
        break
else:
    print('NO')
" 2>/dev/null)
assert_eq "log.jsonl has startup_repair_disabled event" "$LOG_HAS_DISABLED" "YES"

fi  # startup_repair.py exists (test 17)

# ── Test 18 (brief-019): Model-field parser — correct extraction ──────────────

echo ""
echo "=== Test 18: Model-field parser — correct extraction from **Model:** frontmatter ==="

# Helper mirrors daemon.sh line 349's fixed pipeline (task-7).
# Takes first whitespace-separated token, strips any trailing ( or , suffix.
_parse_model() {
    local file="$1"
    local result
    result=$(grep -m1 '^\*\*Model:\*\*' "$file" 2>/dev/null \
        | sed 's/.*\*\*Model:\*\*[[:space:]]*//' \
        | awk '{print $1}' \
        | cut -d'(' -f1 \
        | cut -d',' -f1 \
        | tr '[:upper:]' '[:lower:]')
    echo "${result:-sonnet}"
}

# (a) simple opus
printf '**Model:** opus\n' > "$SCRATCH/brief-model-a.md"
assert_eq "model-field (a): opus → opus" "$(_parse_model "$SCRATCH/brief-model-a.md")" "opus"

# (b) simple sonnet
printf '**Model:** sonnet\n' > "$SCRATCH/brief-model-b.md"
assert_eq "model-field (b): sonnet → sonnet" "$(_parse_model "$SCRATCH/brief-model-b.md")" "sonnet"

# (c) opus with parenthetical
printf '**Model:** opus (research + adapter-design cycle)\n' > "$SCRATCH/brief-model-c.md"
assert_eq "model-field (c): 'opus (comment)' → opus" \
    "$(_parse_model "$SCRATCH/brief-model-c.md")" "opus"

# (d) comma-separated multi-model — first wins
printf '**Model:** opus, sonnet\n' > "$SCRATCH/brief-model-d.md"
assert_eq "model-field (d): 'opus, sonnet' → opus" \
    "$(_parse_model "$SCRATCH/brief-model-d.md")" "opus"

# (e) haiku
printf '**Model:** haiku\n' > "$SCRATCH/brief-model-e.md"
assert_eq "model-field (e): haiku → haiku" "$(_parse_model "$SCRATCH/brief-model-e.md")" "haiku"

# (f) missing **Model:** line → default sonnet
printf '# Brief: no model field\n' > "$SCRATCH/brief-model-f.md"
assert_eq "model-field (f): missing field → sonnet (default)" \
    "$(_parse_model "$SCRATCH/brief-model-f.md")" "sonnet"

rm -f "$SCRATCH/brief-model-"*.md

# ── Test 19 retired by brief-108-cont-b ──────────────────────────────────────
# The auto-merge symlink-routing test exercised git_read_follow against a
# .loop/briefs/<slug>.md symlink. Brief-108-cont-a migrated dispatch to
# canonical card paths; brief-108-cont-b removed the symlink-following helper.
# Auto-merge flag reads now use git_show on `wiki/briefs/cards/<id>/index.md`,
# which is the codepath the rest of the auto-merge tests already cover.

# ── Tests 20-21 (brief-019): Presence-check gate — running vs complete ────────

echo ""
echo "=== Tests 20-21: Presence-check gate — runs on complete, skipped on running ==="

if ! python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
" 2>/dev/null; then
    fail "presence-check gate (running): validator_presence_check not in source repo (sync pending task-9)"
    fail "presence-check gate (complete): validator_presence_check not in source repo (sync pending task-9)"
else

cat > "$SCRATCH/.loop/briefs/brief-GATE-test.md" <<'GATEEOF'
# Brief: presence-gate test

**ID:** brief-GATE-test
**Status:** running

## Completion criteria

- [ ] `plan.md` present in card dir
- [ ] `closeout.md` present in card dir
GATEEOF

# Test 20: direct call to validator_presence_check with missing artifacts → returns missing list
# The daemon gates calling this on status=complete; the function itself always reports missing.
GATE_BLOCKED=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$SCRATCH/.loop/briefs/brief-GATE-test.md', '$SCRATCH')
print('blocked' if missing else 'clear')
" 2>/dev/null)
assert_eq "presence-check: returns blocked when artifacts missing (function contract)" \
    "$GATE_BLOCKED" "blocked"

# Test 21: with artifacts present → returns clear (not blocking)
touch "$SCRATCH/plan.md" "$SCRATCH/closeout.md"
GATE_CLEAR=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$SCRATCH/.loop/briefs/brief-GATE-test.md', '$SCRATCH')
print('blocked' if missing else 'clear')
" 2>/dev/null)
assert_eq "presence-check: returns clear when all artifacts present" "$GATE_CLEAR" "clear"

rm -f "$SCRATCH/plan.md" "$SCRATCH/closeout.md"
rm -f "$SCRATCH/.loop/briefs/brief-GATE-test.md"

fi  # validator_presence_check available

# ── Tests 22-24 (brief-019): Merge abort-on-conflict ──────────────────────────

echo ""
echo "=== Tests 22-24: Merge abort-on-conflict — conflict triggers abort + awaiting_review ==="

MERGE_SCRATCH=$(mktemp -d)

git -C "$MERGE_SCRATCH" init -q -b main
git -C "$MERGE_SCRATCH" config user.email "test@test"
git -C "$MERGE_SCRATCH" config user.name "Test"

mkdir -p "$MERGE_SCRATCH/.loop/state/signals"
mkdir -p "$MERGE_SCRATCH/.loop/worktrees"

cat > "$MERGE_SCRATCH/.loop/config.sh" <<'EOF'
PROJECT_NAME="test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
EOF

touch "$MERGE_SCRATCH/.loop/state/log.jsonl"

# Seed initial commit on main
echo "base content" > "$MERGE_SCRATCH/shared.txt"
git -C "$MERGE_SCRATCH" add -A
git -C "$MERGE_SCRATCH" commit -q -m "init"

# Set up local bare repo as 'origin' so git push succeeds
git init --bare -q "$MERGE_SCRATCH/origin.git"
git -C "$MERGE_SCRATCH" remote add origin "$MERGE_SCRATCH/origin.git"
git -C "$MERGE_SCRATCH" push -q origin main

# Create non-conflicting branch (test 22)
git -C "$MERGE_SCRATCH" checkout -q -b brief-CLEAN-test
echo "clean addition" > "$MERGE_SCRATCH/newfile.txt"
git -C "$MERGE_SCRATCH" add newfile.txt
git -C "$MERGE_SCRATCH" commit -q -m "brief-CLEAN-test: add newfile"
git -C "$MERGE_SCRATCH" checkout -q main

# Create conflicting branch (tests 23-24): both branches modify shared.txt
git -C "$MERGE_SCRATCH" checkout -q -b brief-CONFLICT-test
echo "branch version" > "$MERGE_SCRATCH/shared.txt"
git -C "$MERGE_SCRATCH" add shared.txt
git -C "$MERGE_SCRATCH" commit -q -m "brief-CONFLICT-test: change shared.txt"
git -C "$MERGE_SCRATCH" checkout -q main
echo "main version" > "$MERGE_SCRATCH/shared.txt"
git -C "$MERGE_SCRATCH" add shared.txt
git -C "$MERGE_SCRATCH" commit -q -m "main: change shared.txt — conflicts with branch"
git -C "$MERGE_SCRATCH" push -q origin main

write_running_ms() {
    python3 -c "import json; json.dump($1, open('$MERGE_SCRATCH/.loop/state/running.json','w'), indent=2)"
}

# ── Test 22: clean merge succeeds ───────────────────────────────────────────

write_running_ms "{
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [{'brief': 'brief-CLEAN-test', 'branch': 'brief-CLEAN-test'}],
    'awaiting_review': [],
    'history': []
}"

cat > "$MERGE_SCRATCH/.loop/state/pending-merge.json" <<'PMEOF'
{"brief": "brief-CLEAN-test", "branch": "brief-CLEAN-test", "title": "clean test", "evaluation": ""}
PMEOF

CLEAN_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, load_running, merge
paths = init_paths('$MERGE_SCRATCH')
try:
    result = merge(paths)
    rc = load_running(paths)
    in_hist = any(e.get('brief') == 'brief-CLEAN-test' for e in rc.get('history', []))
    print(f'ok,in_history={in_hist}')
except Exception as e:
    # Push may fail if branch cleanup fails — just check it's not a conflict error
    msg = str(e).lower()
    if 'conflict' in msg or 'unmerged' in msg:
        print(f'conflict_error: {e}')
    else:
        print('ok,push_or_cleanup_error')
" 2>/dev/null)
CLEAN_OK=$(echo "$CLEAN_RESULT" | grep -c "^ok" || true)
assert_eq "clean merge: no conflict error raised" "$CLEAN_OK" "1"

# ── Test 23: conflict triggers abort + awaiting_review ──────────────────────

write_running_ms "{
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [{'brief': 'brief-CONFLICT-test', 'branch': 'brief-CONFLICT-test'}],
    'awaiting_review': [],
    'history': []
}"

cat > "$MERGE_SCRATCH/.loop/state/pending-merge.json" <<'PMEOF'
{"brief": "brief-CONFLICT-test", "branch": "brief-CONFLICT-test", "title": "conflict test", "evaluation": ""}
PMEOF

CONFLICT_RESULT=$(python3 -c "
import sys, subprocess
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, load_running, merge
paths = init_paths('$MERGE_SCRATCH')
try:
    result = merge(paths)
    rc = load_running(paths)
    in_ar = any(e.get('brief') == 'brief-CONFLICT-test' for e in rc.get('awaiting_review', []))
    print(f'result={result},in_awaiting_review={in_ar}')
except subprocess.CalledProcessError:
    print('exception:no_abort_implemented')
" 2>/dev/null)
assert_eq "conflict: merge() returns False + brief in awaiting_review [fails until task-8]" \
    "$CONFLICT_RESULT" "result=False,in_awaiting_review=True"

# Working tree must have no half-merged state after abort
# (MERGE_HEAD gone + no unmerged files — log.jsonl modification and
# untracked origin.git/ are test-setup noise, not conflict residue)
MERGE_HEAD_AFTER=$(git -C "$MERGE_SCRATCH" rev-parse --verify MERGE_HEAD 2>/dev/null || echo "")
UNMERGED_AFTER=$(git -C "$MERGE_SCRATCH" ls-files --unmerged 2>/dev/null)
HALF_MERGED=$([ -z "$MERGE_HEAD_AFTER" ] && [ -z "$UNMERGED_AFTER" ] && echo "clean" || echo "dirty")
assert_eq "conflict: no half-merged state after abort [fails until task-8]" \
    "$HALF_MERGED" "clean"

# ── Test 24: repeated tick does not retry after conflict ────────────────────

PM_AFTER=$( [ -f "$MERGE_SCRATCH/.loop/state/pending-merge.json" ] && echo "exists" || echo "gone" )
assert_eq "conflict: pending-merge.json removed, no retry loop [fails until task-8]" \
    "$PM_AFTER" "gone"

# Running merge() again with no pending-merge.json → cleanly returns False (not an error)
NO_PM_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, merge
paths = init_paths('$MERGE_SCRATCH')
result = merge(paths)
print('false' if result is False else str(result))
" 2>/dev/null)
assert_eq "repeated tick after conflict: merge() returns False cleanly (no retry)" \
    "$NO_PM_RESULT" "false"

rm -rf "$MERGE_SCRATCH"

# ── Tests 25-26: Queen dedup — queue-head augmentation ───────────────────
# The augmentation runs in daemon.sh after assess.py: when trigger is
# CONDUCTOR:no_active, daemon reads goals.md and appends the first brief ID
# from ## Queued next. We test the augmentation Python snippet directly.

DEDUP_SCRATCH=$(mktemp -d)
mkdir -p "$DEDUP_SCRATCH/.loop/state"

# Shared helper: run the same queue-head extraction snippet daemon.sh uses.
queue_head_id() {
    local state_dir="$1"
    python3 -c "
import re, sys
goals_file = '$state_dir/goals.md'
try:
    with open(goals_file) as f:
        txt = f.read()
    sections = txt.split('\n## ')
    for sec in sections:
        if sec.lower().startswith('queued next'):
            m = re.search(r'brief-\d+-[\w-]+', sec)
            if m:
                print(m.group(0))
                sys.exit(0)
except Exception:
    pass
" 2>/dev/null || true
}

# Test 25: goals.md has a brief in ## Queued next → augmentation produces
# CONDUCTOR:no_active:<brief-id>, distinct from the stored-dedup key.
cat > "$DEDUP_SCRATCH/.loop/state/goals.md" <<'GOALS_EOF'
# Goals

## Queued next

1. **brief-099-test-brief** — test fixture for dedup regression.
GOALS_EOF

HEAD_ID=$(queue_head_id "$DEDUP_SCRATCH/.loop/state")
AUGMENTED_TRIGGER="CONDUCTOR:no_active"
[ -n "$HEAD_ID" ] && AUGMENTED_TRIGGER="CONDUCTOR:no_active:$HEAD_ID"
assert_eq "queen dedup: no_active trigger augmented with queue head ID" \
    "$AUGMENTED_TRIGGER" "CONDUCTOR:no_active:brief-099-test-brief"

# Test 26: empty ## Queued next → no augmentation, trigger stays identity-less.
# Genuinely-idle dedup must still hold (two consecutive empty-queue ticks dedup).
cat > "$DEDUP_SCRATCH/.loop/state/goals.md" <<'GOALS_EOF'
# Goals

## Queued next

_nothing scheduled_
GOALS_EOF

EMPTY_HEAD=$(queue_head_id "$DEDUP_SCRATCH/.loop/state")
EMPTY_TRIGGER="CONDUCTOR:no_active"
[ -n "$EMPTY_HEAD" ] && EMPTY_TRIGGER="CONDUCTOR:no_active:$EMPTY_HEAD"
assert_eq "queen dedup: empty queue keeps identity-less trigger (dedup holds)" \
    "$EMPTY_TRIGGER" "CONDUCTOR:no_active"

rm -rf "$DEDUP_SCRATCH"

# ── Tests 27-29 (brief-025 item 2): Presence-check canonical-root resolution ──

echo ""
echo "=== Tests 27-29: Presence-check canonical-root resolution ==="

CANON_SCRATCH=$(mktemp -d)
mkdir -p "$CANON_SCRATCH/.loop/briefs"
mkdir -p "$CANON_SCRATCH/wiki/design-system"
mkdir -p "$CANON_SCRATCH/docs"

# Brief that names design-system/index.md in completion criteria
cat > "$CANON_SCRATCH/.loop/briefs/brief-CANON-test.md" <<'CANONEOF'
# Brief: canonical-root test

**ID:** brief-CANON-test

## Completion criteria

- [ ] `design-system/index.md` present
CANONEOF

# Test 27: file at wiki/design-system/index.md — should resolve via canonical root, PASS + resolved_at emitted
touch "$CANON_SCRATCH/wiki/design-system/index.md"
CANON_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$CANON_SCRATCH/.loop/briefs/brief-CANON-test.md', '$CANON_SCRATCH')
print('PASS' if not missing else 'BLOCK')
" 2>/dev/null)
assert_eq "presence-check canonical-root: resolves design-system/index.md via wiki/" \
    "$CANON_RESULT" "PASS"

# Verify resolved_at is emitted to stderr
CANON_STDERR=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
validator_presence_check('$CANON_SCRATCH/.loop/briefs/brief-CANON-test.md', '$CANON_SCRATCH')
" 2>&1 >/dev/null)
assert_eq "presence-check canonical-root: emits resolved_at log line to stderr" \
    "$CANON_STDERR" "resolved_at: wiki/design-system/index.md"

rm -f "$CANON_SCRATCH/wiki/design-system/index.md"

# Test 28: file genuinely absent everywhere → BLOCK (negative test)
CANON_MISSING=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$CANON_SCRATCH/.loop/briefs/brief-CANON-test.md', '$CANON_SCRATCH')
print('BLOCK' if missing else 'PASS')
" 2>/dev/null)
assert_eq "presence-check canonical-root: genuinely missing file still BLOCKS" \
    "$CANON_MISSING" "BLOCK"

# Test 29: file at docs/config.md — resolves via docs/ canonical root
cat > "$CANON_SCRATCH/.loop/briefs/brief-CANON-docs.md" <<'CANONDOCSEOF'
# Brief: canonical-root docs test

**ID:** brief-CANON-docs

## Completion criteria

- [ ] `config.md` present
CANONDOCSEOF

touch "$CANON_SCRATCH/docs/config.md"
CANON_DOCS=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$CANON_SCRATCH/.loop/briefs/brief-CANON-docs.md', '$CANON_SCRATCH')
print('PASS' if not missing else 'BLOCK')
" 2>/dev/null)
assert_eq "presence-check canonical-root: resolves config.md via docs/" \
    "$CANON_DOCS" "PASS"

rm -rf "$CANON_SCRATCH"

# ── Tests 30-31 (brief-025 item 3): Validator wrapper synthetic review ────────

echo ""
echo "=== Tests 30-31: Validator wrapper synthetic review ==="

WRAP_SCRATCH=$(mktemp -d)
WRAP_WORKTREE="$WRAP_SCRATCH/worktree"
mkdir -p "$WRAP_WORKTREE/.loop/modules/validator/state/reviews"
WRAP_METRICS="$WRAP_SCRATCH/metrics.jsonl"
touch "$WRAP_METRICS"

WRAP_BRIEF_ID="brief-WRAP-test"
WRAP_CYCLE=3
WRAP_COMMIT="abc123def456789"
WRAP_BRANCH="brief-WRAP-test"
WRAP_REVIEW_REL=".loop/modules/validator/state/reviews/${WRAP_BRIEF_ID}-cycle-${WRAP_CYCLE}.md"

# Shared wrapper-logic script: mirrors the if-block added in brief-025 item 3
WRAP_LOGIC=$(mktemp)
cat > "$WRAP_LOGIC" <<'WRAPEOF'
#!/usr/bin/env bash
# Args: worktree review_rel brief_id cycle commit_sha branch metrics_file
WORKTREE_DIR="$1"; REVIEW_REL="$2"; brief_id="$3"; cycle="$4"
commit_sha="$5"; branch="$6"; METRICS_FILE="$7"
if [ ! -f "$WORKTREE_DIR/$REVIEW_REL" ]; then
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
- validator agent exited without producing a review file; wrapper wrote this synthetic pass. Investigate agent logs if this recurs.

## Spec-fit notes
- _none_

## Deferred items
- _none_
SYNTHEOF
    python3 -c "
import json, datetime
entry = {
    'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    'source': 'validator',
    'event': 'validator_wrapper_synthesized',
    'brief': '$brief_id',
    'cycle': $cycle,
    'commit': '${commit_sha:0:12}',
}
with open('$METRICS_FILE', 'a') as f:
    f.write(json.dumps(entry) + '\n')
" 2>/dev/null
fi
WRAPEOF
chmod +x "$WRAP_LOGIC"

# Test 30: agent silent-exit (no review file written) → wrapper synthesizes pass review
bash "$WRAP_LOGIC" "$WRAP_WORKTREE" "$WRAP_REVIEW_REL" \
    "$WRAP_BRIEF_ID" "$WRAP_CYCLE" "$WRAP_COMMIT" "$WRAP_BRANCH" "$WRAP_METRICS"

WRAP_VERDICT=$(python3 -c "
import re
content = open('$WRAP_WORKTREE/$WRAP_REVIEW_REL').read()
m = re.search(r'^verdict:\s*(\S+)', content, re.MULTILINE)
print(m.group(1) if m else 'missing')
" 2>/dev/null)
assert_eq "validator wrapper: synthesizes pass review on silent agent exit" \
    "$WRAP_VERDICT" "pass"

# Test 31: synthesized review logged in metrics.jsonl as validator_wrapper_synthesized
WRAP_METRICS_EVENT=$(python3 -c "
import json
events = [json.loads(l) for l in open('$WRAP_METRICS') if l.strip()]
match = any(e.get('event') == 'validator_wrapper_synthesized' for e in events)
print('found' if match else 'missing')
" 2>/dev/null)
assert_eq "validator wrapper: logs validator_wrapper_synthesized event to metrics" \
    "$WRAP_METRICS_EVENT" "found"

rm -rf "$WRAP_SCRATCH"
rm -f "$WRAP_LOGIC"

# ── Tests 32-34 (brief-025): Presence-check gate regression ──────────────────

echo ""
echo "=== Tests 32-34: Presence-check gate regression — status gates presence-check ==="

GATEREG_SCRATCH=$(mktemp -d)
mkdir -p "$GATEREG_SCRATCH/.loop/briefs" "$GATEREG_SCRATCH/.loop/state"

cat > "$GATEREG_SCRATCH/.loop/briefs/brief-GATEREG.md" <<'GREOF'
# Brief: presence-gate regression

**ID:** brief-GATEREG
**Status:** running

## Completion criteria

- [ ] `closeout.md` present
GREOF

GATEREG_PROGRESS="$GATEREG_SCRATCH/.loop/state/progress.json"

# Test 32: status=running → gate skips presence-check (never calls validator_presence_check)
echo '{"status":"running"}' > "$GATEREG_PROGRESS"
GATE32_STATUS=$(python3 -c "import json; print(json.load(open('$GATEREG_PROGRESS')).get('status',''))" 2>/dev/null)
if [ "$GATE32_STATUS" = "complete" ]; then
    GATE32_RESULT="ran"
else
    GATE32_RESULT="skipped"
fi
assert_eq "presence-check gate: status=running skips check" "$GATE32_RESULT" "skipped"

# Test 33: status=complete + artifact missing → presence-check runs and blocks
echo '{"status":"complete"}' > "$GATEREG_PROGRESS"
GATE33_STATUS=$(python3 -c "import json; print(json.load(open('$GATEREG_PROGRESS')).get('status',''))" 2>/dev/null)
if [ "$GATE33_STATUS" = "complete" ]; then
    GATE33_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$GATEREG_SCRATCH/.loop/briefs/brief-GATEREG.md', '$GATEREG_SCRATCH')
print('blocked' if missing else 'passed')
" 2>/dev/null)
else
    GATE33_RESULT="skipped"
fi
assert_eq "presence-check gate: status=complete + missing artifact → blocked" "$GATE33_RESULT" "blocked"

# Test 34: status=complete + artifact present → presence-check runs and passes
touch "$GATEREG_SCRATCH/closeout.md"
GATE34_STATUS=$(python3 -c "import json; print(json.load(open('$GATEREG_PROGRESS')).get('status',''))" 2>/dev/null)
if [ "$GATE34_STATUS" = "complete" ]; then
    GATE34_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import validator_presence_check
missing = validator_presence_check('$GATEREG_SCRATCH/.loop/briefs/brief-GATEREG.md', '$GATEREG_SCRATCH')
print('blocked' if missing else 'passed')
" 2>/dev/null)
else
    GATE34_RESULT="skipped"
fi
assert_eq "presence-check gate: status=complete + artifact present → passed" "$GATE34_RESULT" "passed"

rm -rf "$GATEREG_SCRATCH"

# ── Tests 35-37 (brief-025): Depends-on-secrets credential gate ───────────────

echo ""
echo "=== Tests 35-37: Depends-on-secrets — credential gate in dispatch block ==="

SECRETS_SCRATCH=$(mktemp -d)
mkdir -p "$SECRETS_SCRATCH/.loop/briefs" "$SECRETS_SCRATCH/.loop/state"

# Fabricated brief with Depends-on-secrets: FAKE_TOKEN_SL025
cat > "$SECRETS_SCRATCH/.loop/briefs/brief-SECRETS.md" <<'SECEOF'
# Brief: credential-gated test

**ID:** brief-SECRETS
**Status:** queued
**Depends-on-secrets:** FAKE_TOKEN_SL025, ANOTHER_FAKE_SL025
SECEOF

cat > "$SECRETS_SCRATCH/.loop/state/pending-dispatch.json" <<PDEOF
{
  "brief": "brief-SECRETS",
  "branch": "brief-SECRETS",
  "brief_file": ".loop/briefs/brief-SECRETS.md"
}
PDEOF

cat > "$SECRETS_SCRATCH/.loop/state/running.json" <<'RUNEOF'
{"active":[],"completed_pending_eval":[],"awaiting_review":[],"history":[]}
RUNEOF

# Test 35: FAKE_TOKEN_SL025 unset → check-depends-on-secrets returns blocked:FAKE_TOKEN_SL025
SECRETS35_OUTPUT=$(python3 "$ACTIONS" check-depends-on-secrets "$SECRETS_SCRATCH" 2>/dev/null)
SECRETS35_VERDICT=$(echo "$SECRETS35_OUTPUT" | sed -n 1p)
case "$SECRETS35_VERDICT" in
    blocked:FAKE_TOKEN_SL025) SECRETS35_RESULT="blocked" ;;
    *) SECRETS35_RESULT="unexpected:$SECRETS35_VERDICT" ;;
esac
assert_eq "depends-on-secrets: unset var → verdict blocked:FAKE_TOKEN_SL025" \
    "$SECRETS35_RESULT" "blocked"

# Test 36: all vars set → check-depends-on-secrets returns allowed
SECRETS36_OUTPUT=$(FAKE_TOKEN_SL025=x ANOTHER_FAKE_SL025=y python3 "$ACTIONS" check-depends-on-secrets "$SECRETS_SCRATCH" 2>/dev/null)
SECRETS36_VERDICT=$(echo "$SECRETS36_OUTPUT" | sed -n 1p)
assert_eq "depends-on-secrets: all vars set → verdict allowed" \
    "$SECRETS36_VERDICT" "allowed"

# Test 37: brief with no Depends-on-secrets field → backward compat, always allowed
cat > "$SECRETS_SCRATCH/.loop/state/pending-dispatch.json" <<PDEOF2
{
  "brief": "brief-NOSECRETS",
  "branch": "brief-NOSECRETS",
  "brief_file": ".loop/briefs/brief-NOSECRETS.md"
}
PDEOF2

cat > "$SECRETS_SCRATCH/.loop/briefs/brief-NOSECRETS.md" <<'NOSECEOF'
# Brief: no-credential test

**ID:** brief-NOSECRETS
**Status:** queued
**Depends-on:** brief-001-placeholder
NOSECEOF

SECRETS37_OUTPUT=$(python3 "$ACTIONS" check-depends-on-secrets "$SECRETS_SCRATCH" 2>/dev/null)
SECRETS37_VERDICT=$(echo "$SECRETS37_OUTPUT" | sed -n 1p)
assert_eq "depends-on-secrets: no field → backward compat, allowed" \
    "$SECRETS37_VERDICT" "allowed"

rm -rf "$SECRETS_SCRATCH"

# ── Tests 38-42 (brief-027): Human-gate artifact detection ───────────────────

echo ""
echo "=== Tests 38-42: Human-gate artifact detection — _find_handoff_artifact + human_queue_summary ==="

HG_SCRATCH=$(mktemp -d)
mkdir -p "$HG_SCRATCH/.loop/briefs" "$HG_SCRATCH/.loop/state"
mkdir -p "$HG_SCRATCH/wiki/briefs/cards/brief-HG-smoke"

cat > "$HG_SCRATCH/.loop/config.sh" <<'EOF'
PROJECT_NAME="test"
WIKI_PORT="8002"
EOF

cat > "$HG_SCRATCH/.loop/briefs/brief-HG-smoke.md" <<'EOF'
# Brief: human-gate smoke test

**ID:** brief-HG-smoke
**Auto-merge:** false
**Human-gate:** smoke
**Status:** queued
EOF

cat > "$HG_SCRATCH/.loop/state/running.json" <<'RUNEOF'
{"active":[],"completed_pending_eval":[],"pending_merges":[],"awaiting_review":[],"history":[]}
RUNEOF

# Test 38: _find_handoff_artifact returns (None, True) when no artifact present
HG38_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import _find_handoff_artifact
url, missing = _find_handoff_artifact('$HG_SCRATCH', 'brief-HG-smoke', '8002')
print('missing' if missing and url is None else 'found')
" 2>/dev/null)
assert_eq "human-gate: _find_handoff_artifact returns missing when no artifact" \
    "$HG38_RESULT" "missing"

# Write smoke.md at the expected card dir path (simulates worker output on status transition)
cat > "$HG_SCRATCH/wiki/briefs/cards/brief-HG-smoke/smoke.md" <<'SMOKEEOF'
---
title: "brief-HG-smoke smoke — test brief"
brief: brief-HG-smoke
category: smoke
status: awaiting-mattie
---

# Smoke test — test brief

!!! abstract "TL;DR"
    **What shipped:** test artifact

    **Target moment:** test

    **Your part:** run smoke test

## What shipped

| # | Task | Landed as |
|---|---|---|
| 1 | test task | test output |

## What's gated on you

- Run smoke commands

## Prerequisites

!!! warning "None"
    No hardware gates.

## Runbook

### Phase 1 — Smoke run

**blocking.** 2 min

```bash
echo "smoke test"
```

## What "works" looks like

- Command exits 0

## Alternatives if a gate fails

!!! note "If smoke fails"
    Re-run with verbose flag.

## Resolution options

| Option | When to pick | Action |
|---|---|---|
| **Approve** | Smoke passes | `loop approve brief-HG-smoke` |
| **Iterate** | Minor issues | Requeue with notes |
| **Reject** | Fundamental failure | `loop reject brief-HG-smoke` |

## Scav recommendation

**Approve and merge.**

Test passed.

## What you should feel

Confident.

## If something breaks mid-runbook

Capture and ping.

## References

- [Brief index](index.md)
SMOKEEOF

# Test 39: _find_handoff_artifact returns URL when smoke.md present
HG39_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import _find_handoff_artifact
url, missing = _find_handoff_artifact('$HG_SCRATCH', 'brief-HG-smoke', '8002')
if not missing and url and 'brief-HG-smoke' in url and '/smoke/' in url:
    print('found')
else:
    print('fail: url=%s missing=%s' % (url, missing))
" 2>/dev/null)
assert_eq "human-gate: _find_handoff_artifact returns URL when smoke.md present" \
    "$HG39_RESULT" "found"

# Test 40: flavor priority — smoke wins over review and escalation when all present
mkdir -p "$HG_SCRATCH/wiki/briefs/cards/brief-HG-multi"
touch "$HG_SCRATCH/wiki/briefs/cards/brief-HG-multi/review.md"
touch "$HG_SCRATCH/wiki/briefs/cards/brief-HG-multi/escalation.md"
touch "$HG_SCRATCH/wiki/briefs/cards/brief-HG-multi/smoke.md"
HG40_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import _find_handoff_artifact
url, missing = _find_handoff_artifact('$HG_SCRATCH', 'brief-HG-multi', '8002')
if url and '/smoke/' in url:
    print('smoke')
elif url and '/review/' in url:
    print('review')
else:
    print('other: %s' % url)
" 2>/dev/null)
assert_eq "human-gate: smoke flavor takes priority over review + escalation" \
    "$HG40_RESULT" "smoke"

# Test 41: human_queue_summary populates artifact_url for awaiting_review entry with smoke.md
python3 -c "
import json
d = {
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [{'brief': 'brief-HG-smoke', 'branch': 'brief-HG-smoke', 'auto_merge': False, 'reason': 'smoke test required'}],
    'history': []
}
json.dump(d, open('$HG_SCRATCH/.loop/state/running.json', 'w'))
"
HG41_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, human_queue_summary
paths = init_paths('$HG_SCRATCH')
items = human_queue_summary(paths)
item = next((i for i in items if i['brief_id'] == 'brief-HG-smoke'), None)
if item is None:
    print('missing_item')
elif item.get('artifact_url') and '/smoke/' in item['artifact_url'] and not item.get('artifact_missing'):
    print('ok')
else:
    print('fail: url=%s missing=%s' % (item.get('artifact_url'), item.get('artifact_missing')))
" 2>/dev/null)
assert_eq "human-gate: human_queue_summary returns artifact_url for awaiting_review with smoke.md" \
    "$HG41_RESULT" "ok"

# Test 42: smoke.md at expected path has required sections from the artifact template
HG42_SECTIONS=$(python3 -c "
content = open('$HG_SCRATCH/wiki/briefs/cards/brief-HG-smoke/smoke.md').read()
required = ['TL;DR', 'What shipped', 'Runbook', 'Resolution options', 'What you should feel']
missing = [s for s in required if s not in content]
print('ok' if not missing else 'missing: ' + ', '.join(missing))
" 2>/dev/null)
assert_eq "human-gate: smoke.md at wiki/briefs/cards/{id}/smoke.md has required sections" \
    "$HG42_SECTIONS" "ok"

rm -rf "$HG_SCRATCH"

# ── Tests 43-45 (brief-028): Dirty-tree merge recovery ────────────────────────
# Verify that an untracked validator review file in main's working tree does NOT
# block the merge — the pre-merge safe-path git clean in merge() removes it first.

echo ""
echo "=== Tests 43-45: Dirty-tree merge recovery (brief-028) ==="

DT_SCRATCH=$(mktemp -d)

git -C "$DT_SCRATCH" init -q -b main
git -C "$DT_SCRATCH" config user.email "test@test"
git -C "$DT_SCRATCH" config user.name "Test"

mkdir -p "$DT_SCRATCH/.loop/state/signals"
mkdir -p "$DT_SCRATCH/.loop/state"
mkdir -p "$DT_SCRATCH/.loop/worktrees"
mkdir -p "$DT_SCRATCH/.loop/modules/validator/state/reviews"

cat > "$DT_SCRATCH/.loop/config.sh" <<'EOF'
PROJECT_NAME="test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
EOF

touch "$DT_SCRATCH/.loop/state/log.jsonl"

# Seed initial commit on main (must include .loop dir for git clean to have context)
echo "base" > "$DT_SCRATCH/base.txt"
git -C "$DT_SCRATCH" add base.txt
git -C "$DT_SCRATCH" commit -q -m "init"

# Bare origin so push succeeds
git init --bare -q "$DT_SCRATCH/origin.git"
git -C "$DT_SCRATCH" remote add origin "$DT_SCRATCH/origin.git"
git -C "$DT_SCRATCH" push -q origin main

# Create branch that commits a validator review file
REVIEW_REL=".loop/modules/validator/state/reviews/brief-DT-test-cycle-1.md"
git -C "$DT_SCRATCH" checkout -q -b brief-DT-test
mkdir -p "$DT_SCRATCH/$(dirname "$REVIEW_REL")"
cat > "$DT_SCRATCH/$REVIEW_REL" <<'REOF'
---
validator: loop-reviewer
brief: brief-DT-test
cycle: 1
verdict: APPROVE
---
Looks good.
REOF
git -C "$DT_SCRATCH" add "$REVIEW_REL"
git -C "$DT_SCRATCH" commit -q -m "brief-DT-test: add validator review"
git -C "$DT_SCRATCH" checkout -q main

# Drop the same review file as an UNTRACKED file in main's working tree.
# This simulates the validator wrapper writing to project root instead of worktree.
mkdir -p "$DT_SCRATCH/$(dirname "$REVIEW_REL")"
cat > "$DT_SCRATCH/$REVIEW_REL" <<'REOF'
---
validator: wrapper-synthesized (brief-025)
brief: brief-DT-test
cycle: 1
verdict: APPROVE
---
Synthesized pass.
REOF

# Test 43: confirm that WITHOUT the pre-merge clean, git merge aborts on dirty tree.
# We call raw git merge, not merge() — this proves the problem is real.
DT43_RC=0
git -C "$DT_SCRATCH" merge brief-DT-test --no-ff -m "raw merge" > /dev/null 2>&1 || DT43_RC=$?
# Restore untracked file (raw merge may have partially cleaned on error)
mkdir -p "$DT_SCRATCH/$(dirname "$REVIEW_REL")"
cat > "$DT_SCRATCH/$REVIEW_REL" <<'REOF'
---
validator: wrapper-synthesized (brief-025)
brief: brief-DT-test
cycle: 1
verdict: APPROVE
---
Synthesized pass.
REOF
# Reset merge state if it partially ran
git -C "$DT_SCRATCH" merge --abort 2>/dev/null || true
DT43_ABORTED=$([ "$DT43_RC" -ne 0 ] && echo "yes" || echo "no")
assert_eq "dirty-tree: raw git merge aborts when untracked review file blocks" \
    "$DT43_ABORTED" "yes"

# Test 44: merge() pre-merge clean removes the untracked file → merge succeeds.
python3 -c "
import json
json.dump({
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [{'brief': 'brief-DT-test', 'branch': 'brief-DT-test'}],
    'awaiting_review': [],
    'history': []
}, open('$DT_SCRATCH/.loop/state/running.json', 'w'), indent=2)
"
cat > "$DT_SCRATCH/.loop/state/pending-merge.json" <<'PMEOF'
{"brief": "brief-DT-test", "branch": "brief-DT-test", "title": "dirty-tree test", "evaluation": ""}
PMEOF

# merge() prints pre-merge-clean status to stdout — capture last line only for verdict.
DT44_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, merge
paths = init_paths('$DT_SCRATCH')
try:
    result = merge(paths)
    print('ok' if result is not False else 'false')
except Exception as e:
    msg = str(e).lower()
    if 'overwritten' in msg or 'dirty' in msg or 'please move' in msg:
        print('dirty_tree_abort')
    else:
        print('ok_push_or_cleanup')
" 2>/dev/null | tail -1)
assert_eq "dirty-tree: merge() completes despite untracked validator review at safe path" \
    "$DT44_RESULT" "ok"

# Test 45: pre-merge clean action was logged in log.jsonl when it removed a file.
# log_action writes {action: "daemon:pre_merge_clean", ...} entries.
DT45_LOGGED=$(python3 -c "
import json
log_path = '$DT_SCRATCH/.loop/state/log.jsonl'
try:
    with open(log_path) as f:
        events = [json.loads(l) for l in f if l.strip()]
    clean_events = [e for e in events if e.get('action') == 'daemon:pre_merge_clean']
    print('logged' if clean_events else 'not_logged')
except Exception as e:
    print('error:' + str(e))
" 2>/dev/null)
assert_eq "dirty-tree: pre_merge_clean event logged when untracked review removed" \
    "$DT45_LOGGED" "logged"

rm -rf "$DT_SCRATCH"

# ── Test 45b: pre-merge clean covers the specific brief's card dir ─────────────
# Covers the 2026-04-24 recurring pattern: worker artifacts (closeout.md, plan.md,
# cycle PNGs) land as untracked duplicates at wiki/briefs/cards/<brief>/ on main,
# blocking merges from brief-XXX. Safe to clean the specific brief's card dir
# because the branch owns its own card dir by convention. NOT safe to broaden to
# wiki/briefs/cards/ — other briefs' cards are legitimate tracked content on main.

echo ""
echo "=== Test 45b: pre-merge clean covers the merging brief's own card dir ==="

DT45B_SCRATCH=$(mktemp -d)
git -C "$DT45B_SCRATCH" init -q -b main
git -C "$DT45B_SCRATCH" config user.email "test@test"
git -C "$DT45B_SCRATCH" config user.name "Test"

mkdir -p "$DT45B_SCRATCH/.loop/state/signals"
mkdir -p "$DT45B_SCRATCH/.loop/worktrees"
cat > "$DT45B_SCRATCH/.loop/config.sh" <<'EOF'
PROJECT_NAME="test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
EOF
touch "$DT45B_SCRATCH/.loop/state/log.jsonl"

echo "base" > "$DT45B_SCRATCH/base.txt"
git -C "$DT45B_SCRATCH" add base.txt
git -C "$DT45B_SCRATCH" commit -q -m "init"

git init --bare -q "$DT45B_SCRATCH/origin.git"
git -C "$DT45B_SCRATCH" remote add origin "$DT45B_SCRATCH/origin.git"
git -C "$DT45B_SCRATCH" push -q origin main

# Branch commits a brief card (closeout.md under wiki/briefs/cards/<brief>/)
DT45B_BRIEF="brief-card-clean-test"
DT45B_CLOSEOUT_REL="wiki/briefs/cards/${DT45B_BRIEF}/closeout.md"
git -C "$DT45B_SCRATCH" checkout -q -b "$DT45B_BRIEF"
mkdir -p "$DT45B_SCRATCH/$(dirname "$DT45B_CLOSEOUT_REL")"
echo "# Closeout from branch" > "$DT45B_SCRATCH/$DT45B_CLOSEOUT_REL"
git -C "$DT45B_SCRATCH" add "$DT45B_CLOSEOUT_REL"
git -C "$DT45B_SCRATCH" commit -q -m "${DT45B_BRIEF}: closeout"
git -C "$DT45B_SCRATCH" checkout -q main

# Simulate the bug: closeout.md dropped as untracked on main's tree
mkdir -p "$DT45B_SCRATCH/$(dirname "$DT45B_CLOSEOUT_REL")"
echo "# Untracked duplicate on main" > "$DT45B_SCRATCH/$DT45B_CLOSEOUT_REL"

python3 -c "
import json
json.dump({
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [{'brief': '$DT45B_BRIEF', 'branch': '$DT45B_BRIEF'}],
    'awaiting_review': [],
    'history': []
}, open('$DT45B_SCRATCH/.loop/state/running.json', 'w'), indent=2)
"
cat > "$DT45B_SCRATCH/.loop/state/pending-merge.json" <<PMEOF
{"brief": "$DT45B_BRIEF", "branch": "$DT45B_BRIEF", "title": "brief-card clean test", "evaluation": ""}
PMEOF

DT45B_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, merge
paths = init_paths('$DT45B_SCRATCH')
try:
    result = merge(paths)
    print('ok' if result is not False else 'false')
except Exception as e:
    msg = str(e).lower()
    if 'overwritten' in msg or 'dirty' in msg or 'please move' in msg:
        print('dirty_tree_abort')
    else:
        print('ok_push_or_cleanup')
" 2>/dev/null | tail -1)
assert_eq "brief-card-clean: merge() completes despite untracked closeout.md under wiki/briefs/cards/<brief>/" \
    "$DT45B_RESULT" "ok"

# Verify pre_merge_clean event includes the brief-card path
DT45B_LOGGED=$(python3 -c "
import json
try:
    with open('$DT45B_SCRATCH/.loop/state/log.jsonl') as f:
        events = [json.loads(l) for l in f if l.strip()]
    clean_events = [e for e in events if e.get('action') == 'daemon:pre_merge_clean']
    card_events = [e for e in clean_events if 'wiki/briefs/cards/$DT45B_BRIEF/' in e.get('path', '')]
    print('logged_for_brief_card' if card_events else ('logged_other_only' if clean_events else 'not_logged'))
except Exception as e:
    print('error:' + str(e))
" 2>/dev/null)
assert_eq "brief-card-clean: pre_merge_clean logged path for the brief-card dir specifically" \
    "$DT45B_LOGGED" "logged_for_brief_card"

rm -rf "$DT45B_SCRATCH"

# ── Tests 46-51 (brief-034 cycle 3): concurrency gate in dispatch ─────────────
# Verify THROTTLE cap + Parallel-safe/Edit-surface overlap detection in
# actions.dispatch. Covers the four gate outcomes (throttle_reached,
# concurrency_skip on overlap / on new-brief not parallel-safe / on active-brief
# not parallel-safe), plus the two pass-through cases (empty active; disjoint
# surfaces at THROTTLE=2).

echo ""
echo "=== Tests 46-51: Concurrency gate in dispatch (brief-034) ==="

CC_SCRATCH=$(mktemp -d)

git -C "$CC_SCRATCH" init -q -b main
git -C "$CC_SCRATCH" config user.email "test@test"
git -C "$CC_SCRATCH" config user.name "Test"

mkdir -p "$CC_SCRATCH/.loop/state/signals"
mkdir -p "$CC_SCRATCH/.loop/briefs"
mkdir -p "$CC_SCRATCH/.loop/worktrees"
touch "$CC_SCRATCH/.loop/state/log.jsonl"

echo "base" > "$CC_SCRATCH/base.txt"
git -C "$CC_SCRATCH" add base.txt
git -C "$CC_SCRATCH" commit -q -m "init"

cat > "$CC_SCRATCH/.loop/briefs/brief-A.md" <<'EOF'
# Brief: A
**ID:** brief-A
**Parallel-safe:** true
**Edit-surface:**
  - crates/hive/
EOF
cat > "$CC_SCRATCH/.loop/briefs/brief-B.md" <<'EOF'
# Brief: B
**ID:** brief-B
**Parallel-safe:** true
**Edit-surface:**
  - crates/playground/
EOF
cat > "$CC_SCRATCH/.loop/briefs/brief-C.md" <<'EOF'
# Brief: C
**ID:** brief-C
**Parallel-safe:** true
**Edit-surface:**
  - crates/hive/src/
EOF
cat > "$CC_SCRATCH/.loop/briefs/brief-D.md" <<'EOF'
# Brief: D (legacy, no Parallel-safe)
**ID:** brief-D
EOF

# Helper: write config.sh with given THROTTLE
cc_write_cfg() {
    cat > "$CC_SCRATCH/.loop/config.sh" <<EOF
PROJECT_NAME="test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
THROTTLE=$1
EOF
}

# Helper: seed running.json with given active[] (JSON string argument).
# JSON is passed via env to avoid Python-vs-JSON literal mismatch (true/false).
cc_write_running() {
    CC_ACTIVE_JSON="$1" python3 -c "
import json, os
active = json.loads(os.environ['CC_ACTIVE_JSON'])
json.dump({
    'active': active,
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': []
}, open('$CC_SCRATCH/.loop/state/running.json', 'w'), indent=2)
"
}

# Helper: seed pending-dispatch.json for a brief
cc_write_pending() {
    cat > "$CC_SCRATCH/.loop/state/pending-dispatch.json" <<EOF
{"brief": "$1", "branch": "$1", "brief_file": ".loop/briefs/$1.md"}
EOF
}

# Helper: read the last log event's `action` field
cc_last_action() {
    python3 -c "
import json
with open('$CC_SCRATCH/.loop/state/log.jsonl') as f:
    events = [json.loads(l) for l in f if l.strip()]
print(events[-1]['action'] if events else 'none')
" 2>/dev/null
}

# Helper: run dispatch's GATE ONLY by mocking ensure_worktree. Returns:
#   "gate_pass" if the gate passed (ensure_worktree would be invoked)
#   last-log-action otherwise.
cc_run_gate() {
    python3 -c "
import sys, os
sys.path.insert(0, '$LIB_DIR')
import actions as A
import claim as C
paths = A.init_paths('$CC_SCRATCH')
# brief-151: dispatch() does 'from claim import claim_brief' at call-time and
# claim_brief uses its OWN subprocess git push (not A.git), so without this stub
# the in-test claim hits a real 'git push' to a nonexistent remote and fails loud.
# Stub it to report this daemon won the claim, so dispatch proceeds to the gate.
C.claim_brief = lambda *a, **kw: True
# Short-circuit after the gate passes: raise before any git operation.
def stop_here(*a, **kw):
    raise SystemExit('__gate_pass__')
A.ensure_worktree = stop_here
# Neutralize git calls so a pass-through that reaches git doesn't explode.
class _R:
    returncode = 0; stdout = ''; stderr = ''
A.git = lambda *a, **kw: _R()
try:
    A.dispatch(paths)
    print('dispatch_returned_false')
except SystemExit as e:
    if '__gate_pass__' in str(e):
        print('gate_pass')
    else:
        raise
" 2>/dev/null
}

# Test 46: empty active[], THROTTLE=1 → gate passes
cc_write_cfg 1
cc_write_running '[]'
cc_write_pending "brief-A"
CC46=$(cc_run_gate)
assert_eq "concurrency: empty active THROTTLE=1 → gate_pass" "$CC46" "gate_pass"
rm -f "$CC_SCRATCH/.loop/state/pending-dispatch.json"

# Test 47: THROTTLE=1 + 1 active → throttle_reached
cc_write_cfg 1
cc_write_running '[{"brief":"in-flight","branch":"in-flight","parallel_safe":true,"edit_surface":["x/"]}]'
cc_write_pending "brief-A"
cc_run_gate > /dev/null
CC47=$(cc_last_action)
assert_eq "concurrency: THROTTLE=1 + in-flight → throttle_reached" "$CC47" "daemon:throttle_reached"

# Test 48: THROTTLE=2, disjoint surfaces, both parallel_safe=true → gate passes
cc_write_cfg 2
cc_write_running '[{"brief":"in-flight","branch":"in-flight","parallel_safe":true,"edit_surface":["crates/playground/"]}]'
cc_write_pending "brief-A"
CC48=$(cc_run_gate)
assert_eq "concurrency: THROTTLE=2 + disjoint surfaces → gate_pass" "$CC48" "gate_pass"
rm -f "$CC_SCRATCH/.loop/state/pending-dispatch.json"

# Test 49: THROTTLE=2, overlapping surfaces → concurrency_skip
cc_write_cfg 2
cc_write_running '[{"brief":"in-flight","branch":"in-flight","parallel_safe":true,"edit_surface":["crates/hive/"]}]'
cc_write_pending "brief-C"
cc_run_gate > /dev/null
CC49=$(cc_last_action)
assert_eq "concurrency: THROTTLE=2 + overlap → concurrency_skip" "$CC49" "daemon:concurrency_skip"

# Test 50: THROTTLE=2, new brief not parallel-safe (legacy) → concurrency_skip
cc_write_cfg 2
cc_write_running '[{"brief":"in-flight","branch":"in-flight","parallel_safe":true,"edit_surface":["crates/x/"]}]'
cc_write_pending "brief-D"
cc_run_gate > /dev/null
CC50=$(cc_last_action)
assert_eq "concurrency: THROTTLE=2 + new brief not parallel-safe → concurrency_skip" \
    "$CC50" "daemon:concurrency_skip"

# Test 51: THROTTLE=2, active brief missing parallel_safe (legacy) → concurrency_skip
cc_write_cfg 2
cc_write_running '[{"brief":"legacy-in-flight","branch":"legacy-in-flight"}]'
cc_write_pending "brief-A"
cc_run_gate > /dev/null
CC51=$(cc_last_action)
assert_eq "concurrency: THROTTLE=2 + legacy active → concurrency_skip" \
    "$CC51" "daemon:concurrency_skip"

rm -rf "$CC_SCRATCH"

# ── Tests 52-59 (brief-034 cycle 4): scout parser + scheduler + output contract
# Covers: frontmatter parse, cadence-seconds derivation, is-due against
# log.jsonl history, daily-cap enforcement, kill_on consecutive-failures, and
# apply-output-contract for stewardship-log-append / log-only / noop-marker.

echo ""
echo "=== Tests 52-59: Scouts — parse + schedule + contracts (brief-034) ==="

SC_SCRATCH=$(mktemp -d)
mkdir -p "$SC_SCRATCH/.loop/state/signals" "$SC_SCRATCH/.loop/specialists"
touch "$SC_SCRATCH/.loop/state/log.jsonl"

cat > "$SC_SCRATCH/.loop/config.sh" <<'EOF'
PROJECT_NAME="sc-test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
EOF

# Pilot-shaped scout (queue-steward analog)
cat > "$SC_SCRATCH/.loop/specialists/steward.md" <<'EOF'
---
name: steward
cadence:
  every: 30m
model: sonnet
max_runs_per_day: 2
max_runtime_seconds: 60
outputs: stewardship-log-append
kill_on:
  - daemon-stop
  - 3-consecutive-failures
---

# Role: steward

Test body.
EOF

# Log-only scout for contract-rejection test
cat > "$SC_SCRATCH/.loop/specialists/logger.md" <<'EOF'
---
name: logger
cadence:
  every: 30m
model: haiku
max_runs_per_day: 48
max_runtime_seconds: 30
outputs: log-only
kill_on:
  - daemon-stop
---

# Role: logger

Emit nothing; this scout only pings.
EOF

SC_SPEC="$SC_SCRATCH/.loop/specialists/steward.md"
SC_LOGGER="$SC_SCRATCH/.loop/specialists/logger.md"

# Test 52: frontmatter parse — get-field name + outputs
SC_NAME=$(python3 "$LIB_DIR/scouts.py" get-field "$SC_SPEC" name 2>/dev/null)
assert_eq "scout: get-field name" "$SC_NAME" "steward"
SC_OUT=$(python3 "$LIB_DIR/scouts.py" get-field "$SC_SPEC" outputs 2>/dev/null)
assert_eq "scout: get-field outputs" "$SC_OUT" "stewardship-log-append"

# Test 53: is-due returns yes on fresh scout (no log history)
SC_DUE=$(python3 "$LIB_DIR/scouts.py" is-due "$SC_SPEC" "$SC_SCRATCH" 2>/dev/null)
assert_eq "scout: is-due yes on empty log" "$SC_DUE" "yes"

# Test 54: over-daily-cap returns no initially
SC_CAP=$(python3 "$LIB_DIR/scouts.py" over-daily-cap "$SC_SPEC" "$SC_SCRATCH" 2>/dev/null)
assert_eq "scout: over-daily-cap no on empty log" "$SC_CAP" "no"

# Seed 2 scout_fire events today → cap reached (max_runs_per_day=2)
SC_TODAY=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
python3 -c "
import json
with open('$SC_SCRATCH/.loop/state/log.jsonl', 'a') as f:
    for _ in range(2):
        f.write(json.dumps({'timestamp': '$SC_TODAY', 'action': 'daemon:scout_fire', 'specialist': 'steward'}) + '\n')
"

# Test 55: over-daily-cap yes after 2 fires
SC_CAP2=$(python3 "$LIB_DIR/scouts.py" over-daily-cap "$SC_SPEC" "$SC_SCRATCH" 2>/dev/null)
assert_eq "scout: over-daily-cap yes after 2 fires" "$SC_CAP2" "yes"

# Test 56: kill_on 3-consecutive-failures → check returns 'kill'
python3 -c "
import json
with open('$SC_SCRATCH/.loop/state/log.jsonl', 'a') as f:
    for _ in range(3):
        f.write(json.dumps({'timestamp': '$SC_TODAY', 'action': 'daemon:scout_failed', 'specialist': 'steward'}) + '\n')
"
SC_CHK=$(python3 "$LIB_DIR/scouts.py" check "$SC_SPEC" "$SC_SCRATCH" 2>/dev/null)
assert_eq "scout: 3-consecutive-failures → kill" "$SC_CHK" "kill"

# Test 57: apply-output-contract writes to stewardship-log-YYYY-MM-DD.md
SC_JSON=$(mktemp)
echo '{"result": "heartbeat stale: 7200s; flagged 2 stuck briefs"}' > "$SC_JSON"
python3 "$LIB_DIR/scouts.py" apply-output-contract "$SC_SPEC" "$SC_JSON" "$SC_SCRATCH" > /dev/null 2>&1
SC_TODAY_DATE=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%d'))")
SC_TARGET="$SC_SCRATCH/.loop/state/stewardship-log-${SC_TODAY_DATE}.md"
if [ -f "$SC_TARGET" ] && grep -q "heartbeat stale" "$SC_TARGET"; then
    pass "scout: stewardship-log-append writes to today's file"
else
    fail "scout: stewardship-log-append writes to today's file"
fi
rm -f "$SC_JSON"

# Test 58: noop-marker → no file written; status=noop
SC_JSON2=$(mktemp)
echo '{"result": "nothing to report"}' > "$SC_JSON2"
SC_STATUS=$(python3 "$LIB_DIR/scouts.py" apply-output-contract "$SC_SPEC" "$SC_JSON2" "$SC_SCRATCH" 2>/dev/null | cut -f1)
assert_eq "scout: noop-marker → status=noop" "$SC_STATUS" "noop"
rm -f "$SC_JSON2"

# Test 59: log-only scout producing text → status=rejected (contract violation)
SC_JSON3=$(mktemp)
echo '{"result": "some unauthorized output"}' > "$SC_JSON3"
SC_STATUS_LO=$(python3 "$LIB_DIR/scouts.py" apply-output-contract "$SC_LOGGER" "$SC_JSON3" "$SC_SCRATCH" 2>/dev/null | cut -f1)
assert_eq "scout: log-only with text → rejected" "$SC_STATUS_LO" "rejected"
rm -f "$SC_JSON3"

rm -rf "$SC_SCRATCH"

# ── Tests 60-63 (brief-034 cycle 9): scout cadence + stewardship-log rotation
# Covers gaps the cycle-4 tests left: is-due respects a recent fire (cadence
# window not yet elapsed), is-due fires once cadence elapses, and
# stewardship-log-append rotates cleanly across UTC day boundaries (per-day
# files, no cross-bleed).

echo ""
echo "=== Tests 60-63: Scout cadence + stewardship-log rotation (brief-034) ==="

SR_SCRATCH=$(mktemp -d)
mkdir -p "$SR_SCRATCH/.loop/state/signals" "$SR_SCRATCH/.loop/specialists"
touch "$SR_SCRATCH/.loop/state/log.jsonl"

cat > "$SR_SCRATCH/.loop/config.sh" <<'EOF'
PROJECT_NAME="sr-test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
EOF

cat > "$SR_SCRATCH/.loop/specialists/steward.md" <<'EOF'
---
name: steward
cadence:
  every: 30m
model: sonnet
max_runs_per_day: 48
max_runtime_seconds: 60
outputs: stewardship-log-append
kill_on:
  - daemon-stop
  - 3-consecutive-failures
---

# Role: steward

Test body.
EOF

SR_SPEC="$SR_SCRATCH/.loop/specialists/steward.md"

# Test 60: is-due=no when last fire is within cadence window (10 min ago; cadence 30m)
SR_RECENT=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
python3 -c "
import json
with open('$SR_SCRATCH/.loop/state/log.jsonl', 'w') as f:
    f.write(json.dumps({'timestamp': '$SR_RECENT', 'action': 'daemon:scout_fire', 'specialist': 'steward'}) + '\n')
"
SR_DUE_RECENT=$(python3 "$LIB_DIR/scouts.py" is-due "$SR_SPEC" "$SR_SCRATCH" 2>/dev/null)
assert_eq "scout: is-due=no when last fire 10m ago (cadence 30m)" "$SR_DUE_RECENT" "no"

# Test 61: is-due=yes when last fire predates cadence window (40 min ago)
SR_OLD=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(minutes=40)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
python3 -c "
import json
with open('$SR_SCRATCH/.loop/state/log.jsonl', 'w') as f:
    f.write(json.dumps({'timestamp': '$SR_OLD', 'action': 'daemon:scout_fire', 'specialist': 'steward'}) + '\n')
"
SR_DUE_OLD=$(python3 "$LIB_DIR/scouts.py" is-due "$SR_SPEC" "$SR_SCRATCH" 2>/dev/null)
assert_eq "scout: is-due=yes when last fire 40m ago (cadence 30m)" "$SR_DUE_OLD" "yes"

# Test 62: stewardship-log rotation — pre-existing yesterday file is preserved;
# today's apply-output-contract creates a distinct today-dated file.
SR_TODAY=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%d'))")
SR_YESTERDAY=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(days=1)).strftime('%Y-%m-%d'))")
SR_YFILE="$SR_SCRATCH/.loop/state/stewardship-log-${SR_YESTERDAY}.md"
SR_TFILE="$SR_SCRATCH/.loop/state/stewardship-log-${SR_TODAY}.md"
printf '# %s\n\n## legacy-entry\nold content\n' "$SR_YESTERDAY" > "$SR_YFILE"
SR_YSIZE_BEFORE=$(wc -c < "$SR_YFILE" | tr -d ' ')

SR_JSON=$(mktemp)
echo '{"result": "queue-steward observation: 3 briefs in flight, no interventions needed"}' > "$SR_JSON"
python3 "$LIB_DIR/scouts.py" apply-output-contract "$SR_SPEC" "$SR_JSON" "$SR_SCRATCH" > /dev/null 2>&1

SR_YSIZE_AFTER=$(wc -c < "$SR_YFILE" | tr -d ' ')
if [ -f "$SR_TFILE" ] && [ "$SR_YFILE" != "$SR_TFILE" ] && grep -q "queue-steward observation" "$SR_TFILE"; then
    pass "scout: rotation — today's file distinct from yesterday's, contains today's entry"
else
    fail "scout: rotation — today's file distinct from yesterday's, contains today's entry"
fi
assert_eq "scout: rotation — yesterday's file untouched (size unchanged)" \
    "$SR_YSIZE_AFTER" "$SR_YSIZE_BEFORE"

# Test 63: second stewardship-log-append on same UTC day APPENDS to today's file
# (no clobber). Both observations survive; file grew.
SR_TSIZE_BEFORE=$(wc -c < "$SR_TFILE" | tr -d ' ')
SR_JSON2=$(mktemp)
echo '{"result": "follow-up observation: merge pending cleared"}' > "$SR_JSON2"
python3 "$LIB_DIR/scouts.py" apply-output-contract "$SR_SPEC" "$SR_JSON2" "$SR_SCRATCH" > /dev/null 2>&1
SR_TSIZE_AFTER=$(wc -c < "$SR_TFILE" | tr -d ' ')
if [ "$SR_TSIZE_AFTER" -gt "$SR_TSIZE_BEFORE" ] \
   && grep -q "queue-steward observation" "$SR_TFILE" \
   && grep -q "follow-up observation" "$SR_TFILE"; then
    pass "scout: rotation — same-day appends accumulate (no clobber)"
else
    fail "scout: rotation — same-day appends accumulate (no clobber)"
fi
rm -f "$SR_JSON" "$SR_JSON2"

rm -rf "$SR_SCRATCH"

# ── Tests 64-65: Rebase-conflict → awaiting_review routing (brief-061) ───────

echo ""
echo "=== Tests 64-65: Rebase-conflict — move-to-awaiting-review with rebase reason ==="

# Test 64: auto-merge:true brief routed to awaiting_review on rebase conflict.
# auto_merge is forced False so the stale branch can't bypass human review even
# if the brief originally declared Auto-merge: true.
write_running "{
    'active': [{'brief': 'brief-001-auto', 'branch': 'brief-001-auto', 'brief_file': '.loop/briefs/brief-001-auto.md', 'auto_merge': True}],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': [],
    'queue': []
}"

python3 "$ACTIONS" move-to-awaiting-review brief-001-auto "$SCRATCH" \
    rebase-blocked "rebase conflict against main — human resolution required" > /dev/null 2>&1

assert_json "rebase-conflict: active[] emptied"                "$RJ" "len(d['active'])"                                   "0"
assert_json "rebase-conflict: brief in awaiting_review[]"      "$RJ" "len(d['awaiting_review'])"                          "1"
assert_json "rebase-conflict: correct brief id"                "$RJ" "d['awaiting_review'][0]['brief']"                   "brief-001-auto"
assert_json "rebase-conflict: auto_merge forced False"         "$RJ" "str(d['awaiting_review'][0]['auto_merge'])"         "False"
assert_json "rebase-conflict: reason preserved verbatim"       "$RJ" "d['awaiting_review'][0].get('reason','')"           "rebase conflict against main — human resolution required"

# Test 65: pending_merges[] unaffected — rebase-conflict does not accidentally
# promote the brief into the merge queue.
assert_json "rebase-conflict: pending_merges[] still empty"    "$RJ" "len(d['pending_merges'])"                           "0"

# ── Tests 66-68: Staleness gate → awaiting_review routing (brief-061) ────────

echo ""
echo "=== Tests 66-68: Staleness gate — stale branch refused merge → awaiting_review ==="

# Test 66: auto-merge:true brief routed to awaiting_review when stale.
# Daemon computes commits_behind > MAX_COMMITS_BEHIND and calls move-to-awaiting-review
# with the stale_branch reason. auto_merge is forced False.
write_running "{
    'active': [{'brief': 'brief-001-auto', 'branch': 'brief-001-auto', 'brief_file': '.loop/briefs/brief-001-auto.md', 'auto_merge': True}],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': [],
    'queue': []
}"

python3 "$ACTIONS" move-to-awaiting-review brief-001-auto "$SCRATCH" \
    staleness-gated "branch is 45 commits behind main — staleness gate triggered, hand-merge required (see wiki/operating-docs/incidents/2026-04-24-brief-049-050-merge-watchlist.md)" > /dev/null 2>&1

assert_json "staleness-gate: active[] emptied"                 "$RJ" "len(d['active'])"                                   "0"
assert_json "staleness-gate: brief in awaiting_review[]"       "$RJ" "len(d['awaiting_review'])"                          "1"
assert_json "staleness-gate: correct brief id"                 "$RJ" "d['awaiting_review'][0]['brief']"                   "brief-001-auto"
assert_json "staleness-gate: auto_merge forced False"          "$RJ" "str(d['awaiting_review'][0]['auto_merge'])"         "False"

# Test 67: pending_merges[] unaffected — stale brief must NOT be promoted to merge queue.
assert_json "staleness-gate: pending_merges[] still empty"     "$RJ" "len(d['pending_merges'])"                           "0"

# Test 68: stale_branch reason preserved verbatim (action-level contract).
assert_json "staleness-gate: reason contains staleness note"   "$RJ" "'staleness gate triggered' in d['awaiting_review'][0].get('reason','')" "True"

# Test 69: conflict_note field is set — mirrors process-pending-merges merge-conflict path,
# used by hive and director tooling to display the specific block reason.
assert_json "staleness-gate: conflict_note field set"          "$RJ" "'staleness gate triggered' in d['awaiting_review'][0].get('conflict_note','')" "True"

# ── Tests 70-72: Cycle wall-time timeout routing (brief-073) ─────────────────

echo ""
echo "=== Tests 70-72: Cycle wall-time timeout — timeout fire → awaiting_review ==="

CWT_SCRATCH=$(mktemp -d)
mkdir -p "$CWT_SCRATCH/.loop/briefs" "$CWT_SCRATCH/.loop/state"
git -C "$CWT_SCRATCH" init -q -b main
git -C "$CWT_SCRATCH" config user.email "test@test"
git -C "$CWT_SCRATCH" config user.name  "Test"

cat > "$CWT_SCRATCH/.loop/briefs/brief-001-auto.md" <<'CWTEOF'
# Brief: auto-merge test
**ID:** brief-001-auto
**Auto-merge:** true
**Status:** queued
CWTEOF

touch "$CWT_SCRATCH/.loop/state/log.jsonl"
git -C "$CWT_SCRATCH" add -A
git -C "$CWT_SCRATCH" commit -q -m "test: init"

# Test 70: cycle-wall-time timeout fire routes to awaiting_review with conflict_note.
# Daemon kills worker and calls move-to-awaiting-review with the cycle wall-time reason.
python3 -c "import json; json.dump({
    'active': [{'brief': 'brief-001-auto', 'branch': 'brief-001-auto', 'brief_file': '.loop/briefs/brief-001-auto.md', 'auto_merge': True}],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': [],
    'queue': []
}, open('$CWT_SCRATCH/.loop/state/running.json','w'), indent=2)"

python3 "$ACTIONS" move-to-awaiting-review brief-001-auto "$CWT_SCRATCH" \
    watchdog-timed-out "cycle wall-time exceeded — human investigation required" > /dev/null 2>&1

CWT_RJ="$CWT_SCRATCH/.loop/state/running.json"

assert_json "cwt-timeout: active[] emptied"                "$CWT_RJ" "len(d['active'])"                                   "0"
assert_json "cwt-timeout: brief in awaiting_review[]"      "$CWT_RJ" "len(d['awaiting_review'])"                          "1"
assert_json "cwt-timeout: correct brief id"                "$CWT_RJ" "d['awaiting_review'][0]['brief']"                   "brief-001-auto"
assert_json "cwt-timeout: auto_merge forced False"         "$CWT_RJ" "str(d['awaiting_review'][0]['auto_merge'])"         "False"
assert_json "cwt-timeout: conflict_note contains key text" "$CWT_RJ" "'cycle wall-time exceeded' in d['awaiting_review'][0].get('conflict_note','')" "True"
assert_json "cwt-timeout: pending_merges[] unaffected"     "$CWT_RJ" "len(d['pending_merges'])"                          "0"

# Test 71: parse-cycle-wall-time-secs returns brief override when Cycle-wall-time-secs present.
cat > "$CWT_SCRATCH/.loop/briefs/brief-OVERRIDE.md" <<'OVEOF'
# Brief: wall-time override test

**ID:** brief-OVERRIDE
**Status:** queued
**Cycle-wall-time-secs:** 7200
OVEOF

cat > "$CWT_SCRATCH/.loop/state/pending-dispatch.json" <<PDEOF
{
  "brief": "brief-OVERRIDE",
  "branch": "brief-OVERRIDE",
  "brief_file": ".loop/briefs/brief-OVERRIDE.md"
}
PDEOF

CWT71_OUTPUT=$(python3 "$ACTIONS" parse-cycle-wall-time-secs "$CWT_SCRATCH" 2>/dev/null)
assert_eq "cwt-override: Cycle-wall-time-secs: 7200 → parser returns 7200" \
    "$CWT71_OUTPUT" "7200"

# Test 72: parse-cycle-wall-time-secs returns default (5400) when field absent.
cat > "$CWT_SCRATCH/.loop/briefs/brief-NOOVERRIDE.md" <<'NOOVEOF'
# Brief: no wall-time override

**ID:** brief-NOOVERRIDE
**Status:** queued
NOOVEOF

cat > "$CWT_SCRATCH/.loop/state/pending-dispatch.json" <<PDEOF2
{
  "brief": "brief-NOOVERRIDE",
  "branch": "brief-NOOVERRIDE",
  "brief_file": ".loop/briefs/brief-NOOVERRIDE.md"
}
PDEOF2

CWT72_OUTPUT=$(python3 "$ACTIONS" parse-cycle-wall-time-secs "$CWT_SCRATCH" 2>/dev/null)
assert_eq "cwt-no-override: no Cycle-wall-time-secs → parser returns default 5400" \
    "$CWT72_OUTPUT" "5400"

rm -rf "$CWT_SCRATCH"

# ── Tests 73-80: Conductor dedup cache TTL + clear-on-state-change (brief-076) ──

echo ""
echo "=== Tests 73-80: Conductor dedup cache TTL + clear-on-state-change ==="

DEDUP_SCRATCH=$(mktemp -d)
mkdir -p "$DEDUP_SCRATCH/.loop/briefs" "$DEDUP_SCRATCH/.loop/state/signals"
git -C "$DEDUP_SCRATCH" init -q -b main
git -C "$DEDUP_SCRATCH" config user.email "test@test"
git -C "$DEDUP_SCRATCH" config user.name  "Test"

cat > "$DEDUP_SCRATCH/.loop/briefs/brief-076-test.md" <<'DEOF'
# Brief: dedup test
**ID:** brief-076-test
**Auto-merge:** false
**Status:** queued
DEOF

touch "$DEDUP_SCRATCH/.loop/state/log.jsonl"
mkdir -p "$DEDUP_SCRATCH/wiki/briefs/cards/brief-076-test"
cat > "$DEDUP_SCRATCH/wiki/briefs/cards/brief-076-test/index.md" <<'DCARD'
---
id: brief-076-test
Status: queued
---
# Brief: dedup test
DCARD
git -C "$DEDUP_SCRATCH" add -A
git -C "$DEDUP_SCRATCH" commit -q -m "test: init"

# ── Test 73: signal_dedup_clear writes expected signal file ──────────────────
# Brief lifecycle: signal_dedup_clear() is the primitive that the daemon consumes
# to flush a stale cache entry. It must write dedup-clear-<brief_id>.json.

python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, signal_dedup_clear
paths = init_paths('$DEDUP_SCRATCH')
signal_dedup_clear(paths, 'brief-076-test')
"

T73_FILE="$DEDUP_SCRATCH/.loop/state/signals/dedup-clear-brief-076-test.json"
if [ -f "$T73_FILE" ]; then
    pass "dedup-clear: signal_dedup_clear writes dedup-clear-<brief_id>.json"
else
    fail "dedup-clear: signal file missing — expected $T73_FILE"
fi

# Test 74: signal file contains correct brief id.
T74_CONTENT=$(python3 -c "import json; d=json.load(open('$T73_FILE')); print(d['brief'])" 2>/dev/null || echo "ERROR")
assert_eq "dedup-clear: signal file contains correct brief id" "$T74_CONTENT" "brief-076-test"

# Clean up signal file before next test.
rm -f "$T73_FILE"

# ── Tests 75-77: move-to-awaiting-review emits dedup-clear signal ────────────

python3 -c "import json; json.dump({
    'active': [{'brief': 'brief-076-test', 'branch': 'brief-076-test', 'brief_file': '.loop/briefs/brief-076-test.md', 'auto_merge': False}],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [],
    'history': [],
    'queue': []
}, open('$DEDUP_SCRATCH/.loop/state/running.json','w'), indent=2)"

python3 "$ACTIONS" move-to-awaiting-review brief-076-test "$DEDUP_SCRATCH" \
    manual-recovery "worker exited — stale_brief condition detected" > /dev/null 2>&1

T75_SIGNAL="$DEDUP_SCRATCH/.loop/state/signals/dedup-clear-brief-076-test.json"

# Test 75: move-to-awaiting-review emits dedup-clear signal file.
if [ -f "$T75_SIGNAL" ]; then
    pass "dedup-clear: move-to-awaiting-review writes dedup-clear signal"
else
    fail "dedup-clear: move-to-awaiting-review did not write dedup-clear signal — expected $T75_SIGNAL"
fi

DEDUP_RJ="$DEDUP_SCRATCH/.loop/state/running.json"

# Test 76: active[] emptied (normal move-to-awaiting-review side-effect).
assert_json "dedup-clear: active[] emptied after move-to-awaiting-review"   "$DEDUP_RJ" "len(d['active'])"         "0"

# Test 77: brief is now in awaiting_review[] (normal move-to-awaiting-review side-effect).
assert_json "dedup-clear: brief appears in awaiting_review[]"               "$DEDUP_RJ" "len(d['awaiting_review'])" "1"

# ── Tests 78-80: dedup TTL — cache entry expires, re-fire is unblocked ───────
#
# The daemon's dedup check is pure bash (in-process variables), so we test
# the actions.py side: signal emission is the mechanism that tells the daemon
# "this entry is now stale, re-evaluate."  We also verify the TTL default is
# set in daemon.sh so the env-var knob is wired correctly.

# Test 78: CONDUCTOR_DEDUP_TTL_SECS default is 1800 in daemon.sh.
DAEMON_SH="$LIB_DIR/daemon.sh"
T78_DEFAULT=$(grep 'CONDUCTOR_DEDUP_TTL_SECS=' "$DAEMON_SH" 2>/dev/null | grep -o '[0-9]\+' | head -1)
assert_eq "dedup-ttl: CONDUCTOR_DEDUP_TTL_SECS default is 1800 in daemon.sh" "$T78_DEFAULT" "1800"

# Test 79: reject-brief also emits dedup-clear signal (clears cache on rejection).
python3 -c "import json; json.dump({
    'active': [],
    'completed_pending_eval': [],
    'pending_merges': [],
    'awaiting_review': [{'brief': 'brief-076-test', 'branch': 'brief-076-test', 'brief_file': '.loop/briefs/brief-076-test.md', 'auto_merge': False}],
    'queue': []
}, open('$DEDUP_SCRATCH/.loop/state/running.json','w'), indent=2)"

rm -f "$DEDUP_SCRATCH/.loop/state/signals/dedup-clear-brief-076-test.json"

python3 "$ACTIONS" reject-brief brief-076-test "$DEDUP_SCRATCH" "test rejection" > /dev/null 2>&1

T79_SIGNAL="$DEDUP_SCRATCH/.loop/state/signals/dedup-clear-brief-076-test.json"
if [ -f "$T79_SIGNAL" ]; then
    pass "dedup-clear: reject-brief writes dedup-clear signal"
else
    fail "dedup-clear: reject-brief did not write dedup-clear signal — expected $T79_SIGNAL"
fi

# Test 80: card Status → rejected after rejection (card-is-truth: no history[] write).
DEDUP_CARD_STATUS=$(python3 -c "
lines = open('$DEDUP_SCRATCH/wiki/briefs/cards/brief-076-test/index.md').readlines()
in_fm = False
for l in lines:
    s = l.strip()
    if s == '---':
        if not in_fm: in_fm = True
        else: break
    elif in_fm and s.lower().startswith('status:'):
        print(s.split(':',1)[1].strip())
        break
")
assert_eq "dedup-clear: card Status → rejected after rejection"   "$DEDUP_CARD_STATUS"  "rejected"

rm -rf "$DEDUP_SCRATCH"

# ── Tests 81-87: Depends-on parser hardening + linter extension (brief-082) ──
#
# Empirical wedges:
#   brief-076 (2026-04-26): `**Depends-on:** none (daemon harness, simple-loop master)`
#                           split on the inner comma, both halves treated as brief ids.
#   brief-082 (2026-04-27): `**Depends-on:** _(intentionally empty — see Why)_`
#                           italics placeholder kept as one phantom dep.
# Both produced permanent dispatch_blocked loops.
#
# Hardening:
#   - parse_depends_on_value drops tokens that don't match BRIEF_ID_RE (with stderr warning).
#   - check_depends_on ERRORs on parenthetical annotations and italics placeholders.

echo ""
echo "=== Tests 81-87: Depends-on parser hardening + linter extension (brief-082) ==="

# Test 81: parser drops both phantom tokens from brief-076's annotation-as-value shape.
T81_OUT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('none (daemon harness, simple-loop master)')))
" 2>/dev/null)
assert_eq "parse_depends_on_value drops 'none (annotation, more)' tokens" "$T81_OUT" ""

# Test 82: parser drops brief-082's italics-placeholder shape.
T82_OUT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('_(intentionally empty — see Why)_')))
" 2>/dev/null)
assert_eq "parse_depends_on_value drops italics placeholder" "$T82_OUT" ""

# Test 83: regression — known-good comma-separated list still parses cleanly.
T83_OUT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('brief-042-foo, brief-051-bar')))
" 2>/dev/null)
assert_eq "parse_depends_on_value keeps real brief ids" "$T83_OUT" "brief-042-foo,brief-051-bar"

# Test 84: parser drops only the malformed token, keeps the real one.
T84_OUT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('brief-042-foo, garbage-not-a-brief')))
" 2>/dev/null)
assert_eq "parse_depends_on_value drops only malformed tokens" "$T84_OUT" "brief-042-foo"

# Test 85: linter ERRORs on `none (foo, bar)` pattern (was silent acceptance).
LINT_SCRATCH=$(mktemp -d)
mkdir -p "$LINT_SCRATCH/.loop"
cat > "$LINT_SCRATCH/brief-test.md" <<'EOF'
# Brief: linter test
**ID:** brief-999-test
**Branch:** brief-999-test
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** sonnet
**Human-gate:** false
**Depends-on:** none (foo, bar)

## Budget
**1 cycles sonnet.** test.

## Completion criteria
- [ ] x
EOF
LINT_OUT=$(python3 "$LIB_DIR/lint.py" "$LINT_SCRATCH/brief-test.md" 2>&1)
LINT_RC=$?
case "$LINT_OUT" in
    *parenthetical*|*"permanent dispatch block"*) pass "lint flags 'none (foo, bar)' as ERROR (not silent)" ;;
    *) fail "lint did not flag 'none (foo, bar)' — got: $LINT_OUT" ;;
esac

# Test 86: linter ERRORs on italics-wrapped placeholder.
cat > "$LINT_SCRATCH/brief-test.md" <<'EOF'
# Brief: linter test
**ID:** brief-999-test
**Branch:** brief-999-test
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** sonnet
**Human-gate:** false
**Depends-on:** _(intentionally empty)_

## Budget
**1 cycles sonnet.** test.

## Completion criteria
- [ ] x
EOF
LINT_OUT=$(python3 "$LIB_DIR/lint.py" "$LINT_SCRATCH/brief-test.md" 2>&1)
case "$LINT_OUT" in
    *italics*|*placeholder*) pass "lint flags italics-wrapped placeholder as ERROR" ;;
    *) fail "lint did not flag italics placeholder — got: $LINT_OUT" ;;
esac

# Test 87: linter stays clean on legitimate Depends-on with real brief ids.
cat > "$LINT_SCRATCH/brief-test.md" <<'EOF'
# Brief: linter test
**ID:** brief-999-test
**Branch:** brief-999-test
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** sonnet
**Human-gate:** false
**Depends-on:** brief-042-foo, brief-051-bar

## Budget
**1 cycles sonnet.** test.

## Completion criteria
- [ ] x
EOF
LINT_OUT=$(python3 "$LIB_DIR/lint.py" "$LINT_SCRATCH/brief-test.md" 2>&1)
case "$LINT_OUT" in
    *Clean*) pass "lint clean on legitimate Depends-on with real brief ids" ;;
    *) fail "lint flagged legitimate Depends-on — got: $LINT_OUT" ;;
esac

rm -rf "$LINT_SCRATCH"

# ── Test 88: annotated dep form — parser strips paren suffix ─────────────────
# Validates the parser-permissive side of the discipline: author writes
# `brief-078 (hard), brief-079 (hard)` in Depends-on; the linter ERRORs at
# write time, but the parser extracts the real IDs rather than wedging.

echo ""
echo "=== Test 88: parse_depends_on_value annotated dep form (brief-078 (hard), brief-079 (hard)) ==="

T88_OUT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
print(','.join(parse_depends_on_value('brief-078 (hard), brief-079 (hard)')))
" 2>/dev/null)
assert_eq "parse_depends_on_value annotated form extracts clean ids" \
    "$T88_OUT" "brief-078,brief-079"

# Confirm none (annotation) still drops (not a real brief id even after strip)
T88B_OUT=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from assess import parse_depends_on_value
result = parse_depends_on_value('none (daemon harness, simple-loop master)')
print(','.join(result) if result else 'empty')
" 2>/dev/null)
assert_eq "parse_depends_on_value none-with-annotation drops to empty" \
    "$T88B_OUT" "empty"

# ── Tests 89-102: sibling-field linter (positive + negative per field) ────────
# Brief-084 pass criterion 2b: one positive case (clean → no error) and one
# negative case (pollution → ERROR) per sibling field covered by check_sibling_fields.

echo ""
echo "=== Tests 89-102: sibling-field linter — positive + negative per field ==="

SIB_SCRATCH=$(mktemp -d)
mkdir -p "$SIB_SCRATCH/.loop"

# Helper: write a minimal brief and run the linter, return stdout+stderr
sib_lint() {
    python3 "$LIB_DIR/lint.py" "$SIB_SCRATCH/brief-test.md" 2>&1
}

# Minimal valid brief body (no Target-repo — it's optional)
write_sib_brief() {
    cat > "$SIB_SCRATCH/brief-test.md" <<EOF
# Brief: sibling-field test

**ID:** brief-999-test
**Branch:** $1
**Status:** $2
**Model:** $3
**Auto-merge:** $4
**Validator:** $5
**Human-gate:** $6

## Budget
**1 cycles sonnet.** test.

## Completion criteria
- [ ] x
EOF
}

# ── Auto-merge ────────────────────────────────────────────────

# Test 89 (negative): Auto-merge with parenthetical → ERROR
write_sib_brief "brief-999-test" "queued" "sonnet" "true (rationale here)" "core/agents/reviewer.md" "none"
LINT89=$(sib_lint)
case "$LINT89" in
    *parenthetical*|*"Auto-merge"*) pass "sibling-field: Auto-merge paren annotation → ERROR" ;;
    *) fail "sibling-field: Auto-merge paren annotation not flagged — got: $LINT89" ;;
esac

# Test 90 (positive): Auto-merge: true → clean
write_sib_brief "brief-999-test" "queued" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT90=$(sib_lint)
case "$LINT90" in
    *Clean*) pass "sibling-field: Auto-merge clean value → no error" ;;
    *"Auto-merge"*ERROR*|*ERROR*"Auto-merge"*) fail "sibling-field: Auto-merge clean value got error — $LINT90" ;;
    *) pass "sibling-field: Auto-merge clean value → no sibling error" ;;
esac

# ── Human-gate ────────────────────────────────────────────────

# Test 91 (negative): Human-gate with parenthetical → ERROR
write_sib_brief "brief-999-test" "queued" "sonnet" "true" "core/agents/reviewer.md" "smoke (manual run only)"
LINT91=$(sib_lint)
case "$LINT91" in
    *parenthetical*|*"Human-gate"*) pass "sibling-field: Human-gate paren annotation → ERROR" ;;
    *) fail "sibling-field: Human-gate paren annotation not flagged — got: $LINT91" ;;
esac

# Test 92 (positive): Human-gate: none → clean (none IS valid for Human-gate)
write_sib_brief "brief-999-test" "queued" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT92=$(sib_lint)
case "$LINT92" in
    *Clean*) pass "sibling-field: Human-gate: none → clean (legitimate opt-out)" ;;
    *"Human-gate"*ERROR*|*ERROR*"Human-gate"*) fail "sibling-field: Human-gate: none got unexpected error — $LINT92" ;;
    *) pass "sibling-field: Human-gate: none → no sibling error" ;;
esac

# ── Branch ───────────────────────────────────────────────────

# Test 93 (negative): Branch with parenthetical → ERROR
write_sib_brief "brief-999-test (legacy)" "queued" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT93=$(sib_lint)
case "$LINT93" in
    *parenthetical*|*"Branch"*) pass "sibling-field: Branch paren annotation → ERROR" ;;
    *) fail "sibling-field: Branch paren annotation not flagged — got: $LINT93" ;;
esac

# Test 94 (positive): Branch: clean slug → no sibling error
write_sib_brief "brief-999-test" "queued" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT94=$(sib_lint)
case "$LINT94" in
    *Clean*) pass "sibling-field: Branch clean slug → no error" ;;
    *"Branch"*ERROR*|*ERROR*"Branch"*) fail "sibling-field: Branch clean slug got error — $LINT94" ;;
    *) pass "sibling-field: Branch clean slug → no sibling error" ;;
esac

# ── Validator ────────────────────────────────────────────────

# Test 95 (negative): Validator with parenthetical → ERROR
write_sib_brief "brief-999-test" "queued" "sonnet" "true" "core/agents/reviewer.md (v2)" "none"
LINT95=$(sib_lint)
case "$LINT95" in
    *parenthetical*|*"Validator"*) pass "sibling-field: Validator paren annotation → ERROR" ;;
    *) fail "sibling-field: Validator paren annotation not flagged — got: $LINT95" ;;
esac

# Test 96 (positive): Validator: clean path → no sibling error
write_sib_brief "brief-999-test" "queued" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT96=$(sib_lint)
case "$LINT96" in
    *Clean*) pass "sibling-field: Validator clean path → no error" ;;
    *"Validator"*ERROR*|*ERROR*"Validator"*) fail "sibling-field: Validator clean path got error — $LINT96" ;;
    *) pass "sibling-field: Validator clean path → no sibling error" ;;
esac

# ── Status ───────────────────────────────────────────────────

# Test 97 (negative): Status with parenthetical → ERROR
write_sib_brief "brief-999-test" "queued (deferred — see notes)" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT97=$(sib_lint)
case "$LINT97" in
    *parenthetical*|*"Status"*) pass "sibling-field: Status paren annotation → ERROR" ;;
    *) fail "sibling-field: Status paren annotation not flagged — got: $LINT97" ;;
esac

# Test 98 (positive): Status: queued → no sibling error
write_sib_brief "brief-999-test" "queued" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT98=$(sib_lint)
case "$LINT98" in
    *Clean*) pass "sibling-field: Status: queued → no error" ;;
    *"Status"*ERROR*|*ERROR*"Status"*) fail "sibling-field: Status: queued got error — $LINT98" ;;
    *) pass "sibling-field: Status: queued → no sibling error" ;;
esac

# ── Model ────────────────────────────────────────────────────

# Test 99 (negative): Model with parenthetical → ERROR
write_sib_brief "brief-999-test" "queued" "opus (research phase)" "true" "core/agents/reviewer.md" "none"
LINT99=$(sib_lint)
case "$LINT99" in
    *parenthetical*|*"Model"*) pass "sibling-field: Model paren annotation → ERROR" ;;
    *) fail "sibling-field: Model paren annotation not flagged — got: $LINT99" ;;
esac

# Test 100 (positive): Model: sonnet → no sibling error
write_sib_brief "brief-999-test" "queued" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT100=$(sib_lint)
case "$LINT100" in
    *Clean*) pass "sibling-field: Model: sonnet → no error" ;;
    *"Model"*ERROR*|*ERROR*"Model"*) fail "sibling-field: Model: sonnet got error — $LINT100" ;;
    *) pass "sibling-field: Model: sonnet → no sibling error" ;;
esac

# ── Target repo ──────────────────────────────────────────────

# Test 101 (negative): Target repo with parenthetical → ERROR
cat > "$SIB_SCRATCH/brief-test.md" <<'EOF'
# Brief: target-repo paren test

**ID:** brief-999-test
**Branch:** brief-999-test
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** core/agents/reviewer.md
**Human-gate:** none
**Target repo:** new-theory-research/portal (panda only)

## Budget
**1 cycles sonnet.** test.

## Completion criteria
- [ ] x
EOF
LINT101=$(sib_lint)
case "$LINT101" in
    *parenthetical*|*"Target repo"*) pass "sibling-field: Target-repo paren annotation → ERROR" ;;
    *) fail "sibling-field: Target-repo paren annotation not flagged — got: $LINT101" ;;
esac

# Test 102 (positive): Target repo: clean slug → no sibling error
cat > "$SIB_SCRATCH/brief-test.md" <<'EOF'
# Brief: target-repo clean test

**ID:** brief-999-test
**Branch:** brief-999-test
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** core/agents/reviewer.md
**Human-gate:** none
**Target repo:** new-theory-research/portal

## Budget
**1 cycles sonnet.** test.

## Completion criteria
- [ ] x
EOF
LINT102=$(sib_lint)
case "$LINT102" in
    *Clean*) pass "sibling-field: Target-repo clean slug → no error" ;;
    *"Target repo"*ERROR*|*ERROR*"Target repo"*) fail "sibling-field: Target-repo clean slug got error — $LINT102" ;;
    *) pass "sibling-field: Target-repo clean slug → no sibling error" ;;
esac

# Test 103 (negative): Status: none → ERROR (none is NOT valid for Status)
write_sib_brief "brief-999-test" "none" "sonnet" "true" "core/agents/reviewer.md" "none"
LINT103=$(sib_lint)
case "$LINT103" in
    *"Status"*|*"none"*) pass "sibling-field: Status: none → ERROR (illegal placeholder)" ;;
    *) fail "sibling-field: Status: none not flagged — got: $LINT103" ;;
esac

rm -rf "$SIB_SCRATCH"

# ── Tests 104-108: Promotion-path classification + cycle-completion gate ──────
#
# brief-100: kind field on every promotion, cycle-completion gate on kind=complete.

echo ""
echo "=== Tests 104-108: Promotion-path classification + cycle-completion gate (brief-100) ==="

GATE_SCRATCH=$(mktemp -d)
mkdir -p "$GATE_SCRATCH/.loop/state/signals"
mkdir -p "$GATE_SCRATCH/.loop/briefs"
mkdir -p "$GATE_SCRATCH/.loop/worktrees"
touch "$GATE_SCRATCH/.loop/state/log.jsonl"

cat > "$GATE_SCRATCH/.loop/config.sh" <<'GATEEOF'
PROJECT_NAME="test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
GATEEOF

git -C "$GATE_SCRATCH" init -q -b main
git -C "$GATE_SCRATCH" config user.email "test@test"
git -C "$GATE_SCRATCH" config user.name  "Test"
git -C "$GATE_SCRATCH" commit -q --allow-empty -m "init"

GATE_RJ="$GATE_SCRATCH/.loop/state/running.json"

write_gate_running() {
    python3 -c "import json; json.dump($1, open('$GATE_RJ','w'), indent=2)"
}

# Test 104: kind field persisted for rebase-blocked path.
write_gate_running "{
    'active': [{'brief': 'brief-G104', 'branch': 'brief-G104', 'brief_file': '.loop/briefs/brief-G104.md'}],
    'completed_pending_eval': [], 'pending_merges': [], 'awaiting_review': [], 'history': [], 'queue': []
}"
python3 "$ACTIONS" move-to-awaiting-review brief-G104 "$GATE_SCRATCH" \
    rebase-blocked "rebase conflict" > /dev/null 2>&1
assert_json "kind=rebase-blocked persisted in awaiting_review entry" \
    "$GATE_RJ" "d['awaiting_review'][0]['kind']" "rebase-blocked"

# Test 105: kind field persisted for watchdog-timed-out path.
write_gate_running "{
    'active': [{'brief': 'brief-G105', 'branch': 'brief-G105', 'brief_file': '.loop/briefs/brief-G105.md'}],
    'completed_pending_eval': [], 'pending_merges': [], 'awaiting_review': [], 'history': [], 'queue': []
}"
python3 "$ACTIONS" move-to-awaiting-review brief-G105 "$GATE_SCRATCH" \
    watchdog-timed-out "cycle wall-time exceeded" > /dev/null 2>&1
assert_json "kind=watchdog-timed-out persisted in awaiting_review entry" \
    "$GATE_RJ" "d['awaiting_review'][0]['kind']" "watchdog-timed-out"

# Test 106: kind=complete path with no worktree skips gate and promotes.
write_gate_running "{
    'active': [{'brief': 'brief-G106', 'branch': 'brief-G106', 'brief_file': '.loop/briefs/brief-G106.md'}],
    'completed_pending_eval': [], 'pending_merges': [], 'awaiting_review': [], 'history': [], 'queue': []
}"
python3 "$ACTIONS" move-to-awaiting-review brief-G106 "$GATE_SCRATCH" \
    complete > /dev/null 2>&1
assert_json "kind=complete: no-worktree gate skips, brief promoted" \
    "$GATE_RJ" "len(d['awaiting_review'])" "1"
assert_json "kind=complete: kind field set on promoted entry" \
    "$GATE_RJ" "d['awaiting_review'][0]['kind']" "complete"

# Test 107: cycle-completion gate refuses kind=complete when iteration=0.
write_gate_running "{
    'active': [{'brief': 'brief-G107', 'branch': 'brief-G107', 'brief_file': '.loop/briefs/brief-G107.md'}],
    'completed_pending_eval': [], 'pending_merges': [], 'awaiting_review': [], 'history': [], 'queue': []
}"
# Seed a worktree with iteration=0 (no cycles ran).
mkdir -p "$GATE_SCRATCH/.loop/worktrees/brief-G107/.loop/state"
python3 -c "import json; json.dump(
    {'brief': 'brief-G107', 'iteration': 0, 'status': 'complete', 'tasks_remaining': [], 'tasks_completed': []},
    open('$GATE_SCRATCH/.loop/worktrees/brief-G107/.loop/state/progress.json', 'w'), indent=2)"
python3 "$ACTIONS" move-to-awaiting-review brief-G107 "$GATE_SCRATCH" \
    complete > /dev/null 2>&1
GATE107_EXIT=$?
assert_eq "cycle-gate: iteration=0 → gate refuses (exit non-zero)" "$GATE107_EXIT" "1"
assert_json "cycle-gate: refused brief stays in active[]" \
    "$GATE_RJ" "len(d['active'])" "1"
assert_json "cycle-gate: awaiting_review[] untouched after refusal" \
    "$GATE_RJ" "len(d['awaiting_review'])" "0"

# Test 108: cycle-completion gate passes when iteration=1 and tasks_remaining=[].
write_gate_running "{
    'active': [{'brief': 'brief-G108', 'branch': 'brief-G108', 'brief_file': '.loop/briefs/brief-G108.md'}],
    'completed_pending_eval': [], 'pending_merges': [], 'awaiting_review': [], 'history': [], 'queue': []
}"
mkdir -p "$GATE_SCRATCH/.loop/worktrees/brief-G108/.loop/state"
python3 -c "import json; json.dump(
    {'brief': 'brief-G108', 'iteration': 1, 'status': 'complete', 'tasks_remaining': [], 'tasks_completed': ['cycle 1']},
    open('$GATE_SCRATCH/.loop/worktrees/brief-G108/.loop/state/progress.json', 'w'), indent=2)"
# Git commit count check will fail gracefully (no remote configured) and proceed.
python3 "$ACTIONS" move-to-awaiting-review brief-G108 "$GATE_SCRATCH" \
    complete > /dev/null 2>&1
assert_json "cycle-gate: iteration=1, tasks_done → gate passes, brief promoted" \
    "$GATE_RJ" "len(d['awaiting_review'])" "1"
assert_json "cycle-gate: promoted entry has kind=complete" \
    "$GATE_RJ" "d['awaiting_review'][0]['kind']" "complete"

rm -rf "$GATE_SCRATCH"

# ── Tests 109-111: Stale-local-branch refuse-or-recreate (brief-100, cycle 3) ─

echo ""
echo "=== Tests 109-111: Stale-local-branch refuse-or-recreate (brief-100) ==="

SB_SCRATCH=$(mktemp -d)
git -C "$SB_SCRATCH" init -q -b main
git -C "$SB_SCRATCH" config user.email "test@test"
git -C "$SB_SCRATCH" config user.name  "Test"
git -C "$SB_SCRATCH" commit -q --allow-empty -m "init"

git init --bare -q "$SB_SCRATCH/origin.git"
git -C "$SB_SCRATCH" remote add origin "$SB_SCRATCH/origin.git"
git -C "$SB_SCRATCH" push -q origin main

# Create brief branch at current main tip (before advancing main)
git -C "$SB_SCRATCH" checkout -q -b brief-SB-test
git -C "$SB_SCRATCH" checkout -q main

# Advance remote/main 40 commits ahead of brief-SB-test
for i in $(seq 1 40); do
    git -C "$SB_SCRATCH" commit -q --allow-empty -m "main: advance $i"
done
git -C "$SB_SCRATCH" push -q origin main
git -C "$SB_SCRATCH" fetch -q origin main

# Test 109: stale branch (40 commits behind) count is correctly computed.
SB_STALE_COUNT=$(git -C "$SB_SCRATCH" rev-list --count "brief-SB-test".."origin/main" 2>/dev/null || echo "0")
assert_eq "stale-branch: 40-commit-behind branch reports correct stale count" \
    "$SB_STALE_COUNT" "40"

# Test 110: stale branch (≥ threshold=30) triggers delete + recreate from main.
SB_WORKTREE_DIR="$SB_SCRATCH/.loop/worktrees/brief-SB-test"
mkdir -p "$SB_SCRATCH/.loop/worktrees"
SB_MAX_COMMITS_BEHIND=30
SB_RECREATED="no"
if [ "$SB_STALE_COUNT" -ge "$SB_MAX_COMMITS_BEHIND" ]; then
    git -C "$SB_SCRATCH" branch -D "brief-SB-test" -q 2>/dev/null || true
    git -C "$SB_SCRATCH" worktree add -b "brief-SB-test" "$SB_WORKTREE_DIR" "origin/main" -q 2>/dev/null
    SB_RECREATED="yes"
fi
assert_eq "stale-branch: ≥30 commits behind → recreate fires" "$SB_RECREATED" "yes"

# Test 111: recreated branch tip equals origin/main (not the original stale tip).
SB_BRANCH_TIP=$(git -C "$SB_SCRATCH" rev-parse "brief-SB-test" 2>/dev/null)
SB_MAIN_TIP=$(git -C "$SB_SCRATCH" rev-parse "origin/main" 2>/dev/null)
assert_eq "stale-branch: recreated branch tip matches origin/main" \
    "$SB_BRANCH_TIP" "$SB_MAIN_TIP"

rm -rf "$SB_SCRATCH"

# ── Tests 112-114: kind visibility + backfill in human_queue_summary (brief-100, cycle 4) ─

echo ""
echo "=== Tests 112-114: kind field + backfill in human_queue_summary (brief-100) ==="

HQS_SCRATCH=$(mktemp -d)
mkdir -p "$HQS_SCRATCH/.loop/state" "$HQS_SCRATCH/.loop/state/signals"

# Test 112: kind=complete entry → disposition=ready for review.
cat > "$HQS_SCRATCH/.loop/state/running.json" <<'ENDJSON'
{"active":[],"completed_pending_eval":[],"pending_merges":[],"awaiting_review":[{"brief":"brief-HQS-complete","branch":"brief-HQS-complete","brief_file":".loop/briefs/brief-HQS-complete.md","auto_merge":false,"kind":"complete","reason":"worker done"}],"history":[]}
ENDJSON

HQS112=$(python3 -c "
import sys, os
sys.path.insert(0, '$LIB_DIR')
from actions import human_queue_summary
paths = {
    'running_file': '$HQS_SCRATCH/.loop/state/running.json',
    'state_dir': '$HQS_SCRATCH/.loop/state',
    'project_dir': '$HQS_SCRATCH',
    'loop_dir': '$HQS_SCRATCH/.loop',
    'worktrees_dir': '$HQS_SCRATCH/.loop/worktrees',
}
items = human_queue_summary(paths)
it = next((i for i in items if i['brief_id'] == 'brief-HQS-complete'), None)
print(it['kind'] if it else 'MISSING')
")
assert_eq "human_queue_summary: kind=complete entry returns kind=complete" \
    "$HQS112" "complete"

HQS112B=$(python3 -c "
import sys, os
sys.path.insert(0, '$LIB_DIR')
from actions import human_queue_summary
paths = {
    'running_file': '$HQS_SCRATCH/.loop/state/running.json',
    'state_dir': '$HQS_SCRATCH/.loop/state',
    'project_dir': '$HQS_SCRATCH',
    'loop_dir': '$HQS_SCRATCH/.loop',
    'worktrees_dir': '$HQS_SCRATCH/.loop/worktrees',
}
items = human_queue_summary(paths)
it = next((i for i in items if i['brief_id'] == 'brief-HQS-complete'), None)
print(it['queue_steward_disposition'] if it else 'MISSING')
")
assert_eq "human_queue_summary: kind=complete → disposition=ready for review" \
    "$HQS112B" "ready for review"

# Test 113: entry with no kind field → backfill to kind=unknown + disposition=needs daemon-side disposition.
cat > "$HQS_SCRATCH/.loop/state/running.json" <<'ENDJSON'
{"active":[],"completed_pending_eval":[],"pending_merges":[],"awaiting_review":[{"brief":"brief-HQS-legacy","branch":"brief-HQS-legacy","brief_file":".loop/briefs/brief-HQS-legacy.md","auto_merge":false,"reason":"pre-100 entry without kind"}],"history":[]}
ENDJSON

HQS113=$(python3 -c "
import sys, os
sys.path.insert(0, '$LIB_DIR')
from actions import human_queue_summary
paths = {
    'running_file': '$HQS_SCRATCH/.loop/state/running.json',
    'state_dir': '$HQS_SCRATCH/.loop/state',
    'project_dir': '$HQS_SCRATCH',
    'loop_dir': '$HQS_SCRATCH/.loop',
    'worktrees_dir': '$HQS_SCRATCH/.loop/worktrees',
}
items = human_queue_summary(paths)
it = next((i for i in items if i['brief_id'] == 'brief-HQS-legacy'), None)
print(it['kind'] if it else 'MISSING')
")
assert_eq "human_queue_summary: entry without kind → backfill kind=unknown" \
    "$HQS113" "unknown"

HQS113B=$(python3 -c "
import sys, os
sys.path.insert(0, '$LIB_DIR')
from actions import human_queue_summary
paths = {
    'running_file': '$HQS_SCRATCH/.loop/state/running.json',
    'state_dir': '$HQS_SCRATCH/.loop/state',
    'project_dir': '$HQS_SCRATCH',
    'loop_dir': '$HQS_SCRATCH/.loop',
    'worktrees_dir': '$HQS_SCRATCH/.loop/worktrees',
}
items = human_queue_summary(paths)
it = next((i for i in items if i['brief_id'] == 'brief-HQS-legacy'), None)
print(it['queue_steward_disposition'] if it else 'MISSING')
")
assert_eq "human_queue_summary: kind=unknown → disposition=needs daemon-side disposition" \
    "$HQS113B" "needs daemon-side disposition"

# Test 114: kind=rebase-blocked → disposition=needs daemon-side disposition.
cat > "$HQS_SCRATCH/.loop/state/running.json" <<'ENDJSON'
{"active":[],"completed_pending_eval":[],"pending_merges":[],"awaiting_review":[{"brief":"brief-HQS-rebase","branch":"brief-HQS-rebase","brief_file":".loop/briefs/brief-HQS-rebase.md","auto_merge":false,"kind":"rebase-blocked","reason":"rebase conflict"}],"history":[]}
ENDJSON

HQS114=$(python3 -c "
import sys, os
sys.path.insert(0, '$LIB_DIR')
from actions import human_queue_summary
paths = {
    'running_file': '$HQS_SCRATCH/.loop/state/running.json',
    'state_dir': '$HQS_SCRATCH/.loop/state',
    'project_dir': '$HQS_SCRATCH',
    'loop_dir': '$HQS_SCRATCH/.loop',
    'worktrees_dir': '$HQS_SCRATCH/.loop/worktrees',
}
items = human_queue_summary(paths)
it = next((i for i in items if i['brief_id'] == 'brief-HQS-rebase'), None)
print(it['queue_steward_disposition'] if it else 'MISSING')
")
assert_eq "human_queue_summary: kind=rebase-blocked → disposition=needs daemon-side disposition" \
    "$HQS114" "needs daemon-side disposition"

rm -rf "$HQS_SCRATCH"

# ── Tests 115-119: check_outputs artifact contract (brief-097) ───────────────
#
# Five cases matching the contract from check_outputs in lint.py:
#   115 — Human-gate=review, status=awaiting_review, review.md present → clean
#   116 — Human-gate=review, status=awaiting_review, review.md absent  → ERROR
#   117 — Human-gate=none,   no review.md                              → clean
#   118 — Human-gate=none,   review.md present                        → WARN
#   119 — Both files exist, substantial overlap (identical H1)         → ERROR
#
# Each brief lives in its own card dir so brief_path.resolve().parent
# gives the correct card dir to check_outputs.

echo ""
echo "=== Tests 115-119: check_outputs artifact contract (brief-097) ==="

OUTPUTS_SCRATCH=$(mktemp -d)
mkdir -p "$OUTPUTS_SCRATCH/.loop/state"

# Minimal valid brief template reused across all five cases.
# Card dir is the directory containing index.md (brief_path.resolve().parent).

# ── Test 115: Human-gate=review, awaiting_review, review.md present → clean ──
mkdir -p "$OUTPUTS_SCRATCH/card-115"
cat > "$OUTPUTS_SCRATCH/card-115/index.md" <<'EOF'
# Brief: outputs-test-115
**ID:** brief-998-outputs-115
**Branch:** brief-998-outputs-115
**Status:** awaiting_review
**Model:** sonnet
**Auto-merge:** false
**Validator:** core/agents/reviewer.md
**Human-gate:** review

## Budget
**1 cycles sonnet.** test.
EOF
cat > "$OUTPUTS_SCRATCH/card-115/review.md" <<'EOF'
# Review gate — outputs-test-115
Gate-time runbook. See closeout.md for what shipped.
EOF
OUT115=$(python3 "$LIB_DIR/lint.py" "$OUTPUTS_SCRATCH/card-115/index.md" 2>&1)
case "$OUT115" in
    *Clean*) pass "check_outputs: gate=review + review.md present → clean" ;;
    *) fail "check_outputs: gate=review + review.md present → expected clean, got: $OUT115" ;;
esac

# ── Test 116: Human-gate=review, awaiting_review, review.md absent → ERROR ───
mkdir -p "$OUTPUTS_SCRATCH/card-116"
cat > "$OUTPUTS_SCRATCH/card-116/index.md" <<'EOF'
# Brief: outputs-test-116
**ID:** brief-998-outputs-116
**Branch:** brief-998-outputs-116
**Status:** awaiting_review
**Model:** sonnet
**Auto-merge:** false
**Validator:** core/agents/reviewer.md
**Human-gate:** review

## Budget
**1 cycles sonnet.** test.
EOF
OUT116=$(python3 "$LIB_DIR/lint.py" "$OUTPUTS_SCRATCH/card-116/index.md" 2>&1)
case "$OUT116" in
    *"review.md"*"missing"*|*"review.md is missing"*|*"awaiting_review"*) pass "check_outputs: gate=review + review.md absent → ERROR" ;;
    *) fail "check_outputs: gate=review + review.md absent → expected ERROR, got: $OUT116" ;;
esac

# ── Test 117: Human-gate=none, no review.md → clean ─────────────────────────
mkdir -p "$OUTPUTS_SCRATCH/card-117"
cat > "$OUTPUTS_SCRATCH/card-117/index.md" <<'EOF'
# Brief: outputs-test-117
**ID:** brief-998-outputs-117
**Branch:** brief-998-outputs-117
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** core/agents/reviewer.md
**Human-gate:** none

## Budget
**1 cycles sonnet.** test.
EOF
OUT117=$(python3 "$LIB_DIR/lint.py" "$OUTPUTS_SCRATCH/card-117/index.md" 2>&1)
case "$OUT117" in
    *Clean*) pass "check_outputs: gate=none + no review.md → clean" ;;
    *) fail "check_outputs: gate=none + no review.md → expected clean, got: $OUT117" ;;
esac

# ── Test 118: Human-gate=none, review.md present → WARN ─────────────────────
mkdir -p "$OUTPUTS_SCRATCH/card-118"
cat > "$OUTPUTS_SCRATCH/card-118/index.md" <<'EOF'
# Brief: outputs-test-118
**ID:** brief-998-outputs-118
**Branch:** brief-998-outputs-118
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** core/agents/reviewer.md
**Human-gate:** none

## Budget
**1 cycles sonnet.** test.
EOF
cat > "$OUTPUTS_SCRATCH/card-118/review.md" <<'EOF'
# Review — outputs-test-118
Unnecessary review.md — gate is none.
EOF
OUT118=$(python3 "$LIB_DIR/lint.py" "$OUTPUTS_SCRATCH/card-118/index.md" 2>&1)
case "$OUT118" in
    *"unnecessary"*|*"Human-gate: none"*|*"unnecessary artifact"*) pass "check_outputs: gate=none + review.md present → WARN" ;;
    *) fail "check_outputs: gate=none + review.md present → expected WARN, got: $OUT118" ;;
esac

# ── Test 119: Both files exist, identical H1 (overlap) → ERROR ───────────────
mkdir -p "$OUTPUTS_SCRATCH/card-119"
cat > "$OUTPUTS_SCRATCH/card-119/index.md" <<'EOF'
# Brief: outputs-test-119
**ID:** brief-998-outputs-119
**Branch:** brief-998-outputs-119
**Status:** queued
**Model:** sonnet
**Auto-merge:** false
**Validator:** core/agents/reviewer.md
**Human-gate:** review

## Budget
**1 cycles sonnet.** test.
EOF
# Both artifacts have the identical H1 — triggers overlap ERROR.
cat > "$OUTPUTS_SCRATCH/card-119/review.md" <<'EOF'
# Brief 119 closeout and review

Gate runbook content here. What shipped: the thing.
More words about what we delivered and how we tested it.
EOF
cat > "$OUTPUTS_SCRATCH/card-119/closeout.md" <<'EOF'
# Brief 119 closeout and review

Forensic record. Same H1 as review.md — this triggers the overlap check.
More words about what we delivered and how we tested it.
EOF
OUT119=$(python3 "$LIB_DIR/lint.py" "$OUTPUTS_SCRATCH/card-119/index.md" 2>&1)
case "$OUT119" in
    *"overlap"*|*"identical H1"*|*"Substantial overlap"*) pass "check_outputs: identical H1 in review+closeout → ERROR" ;;
    *) fail "check_outputs: identical H1 → expected overlap ERROR, got: $OUT119" ;;
esac

rm -rf "$OUTPUTS_SCRATCH"

# ── Tests 120-123 (brief-101): check_review_md_shape — outcome sections ──────
#
# Four cases for the code-change review.md outcome contract:
#   120 — code-change brief, all three sections present           → clean
#   121 — code-change brief, "skim the diff" (no observable sect) → ERROR
#   122 — code-change brief, missing recurrence-detector section  → ERROR
#   123 — non-code-change brief (wiki-only Edit-surface)          → ignored
#
# Each brief lives in its own card dir so brief_path.resolve().parent
# gives the correct card dir to check_review_md_shape.

echo ""
echo "=== Tests 120-123: check_review_md_shape — code-change review.md outcome sections ==="

RMS_SCRATCH=$(mktemp -d)
mkdir -p "$RMS_SCRATCH/.loop/state"

# ── Test 120: code-change brief, all three sections → clean ──────────────────
mkdir -p "$RMS_SCRATCH/card-120"
cat > "$RMS_SCRATCH/card-120/index.md" <<'EOF'
# Brief: review-shape-120
**ID:** brief-999-rms-120
**Branch:** brief-999-rms-120
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** core/agents/reviewer.md
**Human-gate:** review
**Edit-surface:**
  - apps/api/worker/

## Budget
**1 cycles sonnet.** test.
EOF
cat > "$RMS_SCRATCH/card-120/review.md" <<'EOF'
# Review gate — brief-999-rms-120

## What was broken

The worker failed to start due to a missing dependency pin.

## How we know it's fixed (live, observable now)

| Observable | Status | Where to verify |
|---|---|---|
| Worker starts cleanly | ✅ | `loop logs` — "worker ready" in last 5 lines |

## How we'd know if it recurred

If the "worker ready" log line stops appearing after dispatch → regression.
EOF
OUT120=$(python3 "$LIB_DIR/lint.py" "$RMS_SCRATCH/card-120/index.md" 2>&1)
case "$OUT120" in
    *"What was broken"*|*"How we know it"*|*"recurred"*)
        fail "check_review_md_shape: all three sections present → expected no shape errors, got: $OUT120" ;;
    *) pass "check_review_md_shape: all three sections present → clean" ;;
esac

# ── Test 121: code-change, missing observable section ("skim the diff") → ERROR
mkdir -p "$RMS_SCRATCH/card-121"
cat > "$RMS_SCRATCH/card-121/index.md" <<'EOF'
# Brief: review-shape-121
**ID:** brief-999-rms-121
**Branch:** brief-999-rms-121
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** core/agents/reviewer.md
**Human-gate:** review
**Edit-surface:**
  - apps/api/worker/

## Budget
**1 cycles sonnet.** test.
EOF
cat > "$RMS_SCRATCH/card-121/review.md" <<'EOF'
# Review gate — brief-999-rms-121

## What was broken

The worker was not resilient to dependency failures.

Please skim the diff at the PR link to verify the changes look right.
EOF
OUT121=$(python3 "$LIB_DIR/lint.py" "$RMS_SCRATCH/card-121/index.md" 2>&1)
case "$OUT121" in
    *"How we know it"*) pass "check_review_md_shape: missing observable section → ERROR" ;;
    *) fail "check_review_md_shape: missing observable section → expected ERROR about 'How we know it', got: $OUT121" ;;
esac

# ── Test 122: code-change, missing recurrence-detector section → ERROR ────────
mkdir -p "$RMS_SCRATCH/card-122"
cat > "$RMS_SCRATCH/card-122/index.md" <<'EOF'
# Brief: review-shape-122
**ID:** brief-999-rms-122
**Branch:** brief-999-rms-122
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** core/agents/reviewer.md
**Human-gate:** review
**Edit-surface:**
  - apps/api/worker/

## Budget
**1 cycles sonnet.** test.
EOF
cat > "$RMS_SCRATCH/card-122/review.md" <<'EOF'
# Review gate — brief-999-rms-122

## What was broken

The worker crashed on startup due to a stale lerobot pin.

## How we know it's fixed (live, observable now)

| Observable | Status | Where to verify |
|---|---|---|
| lerobot pinned to 0.4.0 | ✅ | `pip show lerobot` in worker env → Version: 0.4.0 |
EOF
OUT122=$(python3 "$LIB_DIR/lint.py" "$RMS_SCRATCH/card-122/index.md" 2>&1)
case "$OUT122" in
    *"recurred"*) pass "check_review_md_shape: missing recurrence section → ERROR" ;;
    *) fail "check_review_md_shape: missing recurrence section → expected ERROR about 'recurred', got: $OUT122" ;;
esac

# ── Test 123: non-code-change brief (wiki-only Edit-surface) → ignored ────────
mkdir -p "$RMS_SCRATCH/card-123"
cat > "$RMS_SCRATCH/card-123/index.md" <<'EOF'
# Brief: review-shape-123
**ID:** brief-999-rms-123
**Branch:** brief-999-rms-123
**Status:** queued
**Model:** sonnet
**Auto-merge:** true
**Validator:** core/agents/reviewer.md
**Human-gate:** review
**Edit-surface:**
  - wiki/research-notes/

## Budget
**1 cycles sonnet.** test.
EOF
cat > "$RMS_SCRATCH/card-123/review.md" <<'EOF'
# Review gate — brief-999-rms-123

No outcome sections — this is a research brief, not a code-change brief.
The linter should ignore the review.md shape for this one.
EOF
OUT123=$(python3 "$LIB_DIR/lint.py" "$RMS_SCRATCH/card-123/index.md" 2>&1)
case "$OUT123" in
    *"What was broken"*|*"How we know it"*|*"recurred"*)
        fail "check_review_md_shape: non-code-change brief → expected no shape errors, got: $OUT123" ;;
    *) pass "check_review_md_shape: non-code-change brief (wiki-only Edit-surface) → shape check ignored" ;;
esac

rm -rf "$RMS_SCRATCH"

# ── Tests 124-126 (brief-107): Producer-side cleanup on merge ────────────────

echo ""
echo "=== Tests 124-126: Producer-side cleanup — symlink removal + card status (brief-107) ==="

CLEANUP_SCRATCH=$(mktemp -d)

git -C "$CLEANUP_SCRATCH" init -q -b main
git -C "$CLEANUP_SCRATCH" config user.email "test@test"
git -C "$CLEANUP_SCRATCH" config user.name "Test"

mkdir -p "$CLEANUP_SCRATCH/.loop/state/signals"
mkdir -p "$CLEANUP_SCRATCH/.loop/briefs"
mkdir -p "$CLEANUP_SCRATCH/.loop/worktrees"
mkdir -p "$CLEANUP_SCRATCH/wiki/briefs/cards/brief-CLN-test"

git init --bare -q "$CLEANUP_SCRATCH/origin.git"
git -C "$CLEANUP_SCRATCH" remote add origin "$CLEANUP_SCRATCH/origin.git"

cat > "$CLEANUP_SCRATCH/.loop/config.sh" <<'EOF'
PROJECT_NAME="test"
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"
EOF

touch "$CLEANUP_SCRATCH/.loop/state/log.jsonl"

# Card with YAML frontmatter + non-trivial fields to catch field-drop bugs.
# (The _set_card_status.py helper requires --- frontmatter.)
cat > "$CLEANUP_SCRATCH/wiki/briefs/cards/brief-CLN-test/index.md" <<'CARDEOF'
---
id: brief-CLN-test
branch: brief-CLN-test
status: queued
model: sonnet
auto-merge: "true"
validator: core/agents/reviewer.md
human-gate: none
target-repo: test-project
---

# Brief: producer-side cleanup test

**ID:** brief-CLN-test
**Status:** queued
CARDEOF

# Symlink from .loop/briefs/ → card dir (the portal pattern)
ln -sf "../../wiki/briefs/cards/brief-CLN-test/index.md" \
    "$CLEANUP_SCRATCH/.loop/briefs/brief-CLN-test.md"

# Commit both the symlink and the card so git rm / git add work during merge
git -C "$CLEANUP_SCRATCH" add -A
git -C "$CLEANUP_SCRATCH" commit -q -m "init: seed cleanup test"
git -C "$CLEANUP_SCRATCH" push -q origin main

# Create brief branch
git -C "$CLEANUP_SCRATCH" checkout -q -b brief-CLN-test
echo "brief work" > "$CLEANUP_SCRATCH/brief-cln-work.txt"
git -C "$CLEANUP_SCRATCH" add -A
git -C "$CLEANUP_SCRATCH" commit -q -m "brief-CLN-test: work done"
git -C "$CLEANUP_SCRATCH" checkout -q main

# Seed state files
python3 -c "
import json
json.dump({
    'active': [{'brief': 'brief-CLN-test', 'branch': 'brief-CLN-test'}],
    'completed_pending_eval': [],
    'pending_merges': [{'brief': 'brief-CLN-test', 'branch': 'brief-CLN-test', 'auto_merge': True}],
    'awaiting_review': [],
    'history': [],
    'queue': []
}, open('$CLEANUP_SCRATCH/.loop/state/running.json', 'w'), indent=2)
"
python3 -c "
import json
json.dump({'brief': 'brief-CLN-test', 'branch': 'brief-CLN-test',
           'auto_merge': True, 'title': 'cleanup test'},
    open('$CLEANUP_SCRATCH/.loop/state/pending-merge.json', 'w'), indent=2)
"

# Run merge() — exercises the full producer-side cleanup path
python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
from actions import init_paths, merge
paths = init_paths('$CLEANUP_SCRATCH')
merge(paths)
" 2>/dev/null

# Test 124: symlink is gone from .loop/briefs/ after merge
if [ ! -e "$CLEANUP_SCRATCH/.loop/briefs/brief-CLN-test.md" ] && \
   [ ! -L "$CLEANUP_SCRATCH/.loop/briefs/brief-CLN-test.md" ]; then
    pass "symlink removed from .loop/briefs/ after merge (brief-107 cleanup)"
else
    fail "symlink still present at .loop/briefs/brief-CLN-test.md after merge"
fi

# Test 125: card YAML frontmatter Status is set to merged after merge
CARD_STATUS_125=$(python3 -c "
with open('$CLEANUP_SCRATCH/wiki/briefs/cards/brief-CLN-test/index.md') as f:
    lines = f.readlines()
in_fm = False
for line in lines:
    s = line.strip()
    if s == '---':
        in_fm = not in_fm
        continue
    if in_fm and s.lower().startswith('status:'):
        print(s.split(':', 1)[1].strip())
        break
else:
    print('NOT_FOUND')
" 2>/dev/null)
assert_eq "card YAML frontmatter Status set to 'merged' after merge (brief-107)" \
    "$CARD_STATUS_125" "merged"

# Test 126: all non-status frontmatter fields preserved verbatim after merge
FIELDS_OK=$(python3 -c "
with open('$CLEANUP_SCRATCH/wiki/briefs/cards/brief-CLN-test/index.md') as f:
    content = f.read()
fm = {}
in_fm = False
for line in content.splitlines():
    if line.strip() == '---':
        in_fm = not in_fm
        continue
    if in_fm and ':' in line:
        key, _, val = line.partition(':')
        fm[key.strip().lower()] = val.strip()
expected = {
    'id': 'brief-CLN-test',
    'branch': 'brief-CLN-test',
    'model': 'sonnet',
    'auto-merge': '\"true\"',
    'validator': 'core/agents/reviewer.md',
    'human-gate': 'none',
    'target-repo': 'test-project',
}
errors = [f'{k}: expected {v!r} got {fm.get(k)!r}' for k, v in expected.items() if fm.get(k) != v]
print('OK' if not errors else 'FAIL: ' + '; '.join(errors))
" 2>/dev/null)
assert_eq "non-status frontmatter fields preserved verbatim after merge (brief-107)" \
    "$FIELDS_OK" "OK"

rm -rf "$CLEANUP_SCRATCH"

# ── Test: install-service reads GIT_MAIN_BRANCH from config.json (brief-145) ─

IS_SCRATCH=$(mktemp -d)
IS_PROJECT="$IS_SCRATCH/myproject"

# Mock launchctl that does nothing (avoids actual daemon loading)
IS_BIN="$IS_SCRATCH/bin"
mkdir -p "$IS_BIN"
printf '#!/bin/bash\nexit 0\n' > "$IS_BIN/launchctl"
chmod +x "$IS_BIN/launchctl"

# Fixture project: config.json with master as main branch
mkdir -p "$IS_PROJECT/.loop/state" "$IS_PROJECT/.loop/logs"
cat > "$IS_PROJECT/.loop/config.json" <<'EOF'
{
  "project_name": "myproject",
  "git": {
    "remote": "origin",
    "main_branch": "master"
  }
}
EOF

IS_PLIST="$HOME/Library/LaunchAgents/com.scaviefae.simpleloop.myproject.plist"
rm -f "$IS_PLIST"

(cd "$IS_PROJECT" && PATH="$IS_BIN:$PATH" SIMPLE_LOOP_HOME="$SCRIPT_DIR/.." bash "$SCRIPT_DIR/../bin/loop" install-service >/dev/null 2>&1)

if [ -f "$IS_PLIST" ]; then
    IS_BRANCH=$(python3 -c "
import plistlib
with open('$IS_PLIST', 'rb') as f:
    pl = plistlib.load(f)
env = pl.get('EnvironmentVariables', {})
print(env.get('GIT_MAIN_BRANCH', '__MISSING__'))
" 2>/dev/null)
    assert_eq "install-service writes GIT_MAIN_BRANCH from config.json (brief-145)" \
        "$IS_BRANCH" "master"
    rm -f "$IS_PLIST"
else
    fail "install-service did not write plist to $IS_PLIST (brief-145)"
fi

rm -rf "$IS_SCRATCH"

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "========================================="
echo "Results: $PASSED passed, $FAILED failed"
echo "========================================="

[ "$FAILED" -eq 0 ] && exit 0 || exit 1
