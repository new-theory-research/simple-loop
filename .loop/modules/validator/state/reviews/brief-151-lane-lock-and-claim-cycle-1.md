---
cycle: 1
commit: 3c4b9a059e01704eff7d1d6a4ae50e54c19353f1
brief: brief-151-lane-lock-and-claim
branch: brief-151-lane-lock-and-claim
verdict: pass
summary: Cycle 1 scope fully delivered — lane partition + --lane threading correct; 150 existing tests pass.
validator: loop-reviewer
reviewed_at: 2026-06-28T21:09:26Z
---

## Bugs found
- _none_

## Execution concerns
- `_parse_card_program` duplicates the frontmatter line-scanner from `_parse_card_status` rather than extracting a shared `_read_frontmatter_field(card_path, field)` helper. The brief says "do NOT hand-roll a second YAML reader" and describes `_parse_card_program` as "sibling … reuses the same frontmatter reader." The functions are byte-for-byte identical except the key string. Not a blocker for cycle 1 (behavior is correct, no third copy), but cycles 2–3 should not add a third copy; a shared helper is the right fix if the pattern grows.
- `$_LANE_OPT` is unquoted in all three `queue.py` invocations in `daemon.sh`. This is intentional (word-splits `""` to nothing; `"--lane alpha"` to two tokens) and safe given lane names are space-free program slugs. The comment documents the rationale. No action needed unless a lane slug ever contains a space.

## Spec-fit notes
- lane=None byte-for-byte invariant confirmed: `lane_key = lane.lower() if lane is not None else None`; `_parse_card_program` is only called when `lane_key is not None`. Legacy fingerprint format `"%s|%s" % (goals_sig, ids)` is preserved exactly when `lane=None`. ✓
- Fail-closed rule confirmed: cards with no `Program:` field return `""` from `_parse_card_program`; `"" != any real lane_key` so they are excluded from lane-filtered enumeration. ✓
- All three `queue.py` calls in daemon.sh (fingerprint pre-queen, dispatch-count dedup-bypass, fingerprint post-queen) thread `$_LANE_OPT`. ✓
- `actions.py` drain gate reads `os.environ.get("LOOP_LANE") or None` — correctly lane-scopes the solo-drain decision. ✓
- CLI wins over env: `LOOP_LANE="${_CLI_LANE:-${LOOP_LANE:-}}"` — CLI flag parsed first, env fallback only if CLI absent. ✓
- 150 existing Python tests pass with no regressions (`python3 -m pytest lib/tests/ -q`). ✓
- `lib/claim.py`, `install.sh` patch, and `lib/tests/test_lane_and_claim.py` are absent as expected — correctly deferred to cycles 2 and 3.

## Deferred items
- Cycle 2: `lib/claim.py` (claim_brief / release_claim via refs/claims/<id> + --force-with-lease), claim-before-worktree in `actions.py dispatch()`, release on terminal states, `install.sh` copy.
- Cycle 3: `lib/tests/test_lane_and_claim.py` — golden i (lane filter), golden ii (contention load-bearing), golden iii (lane=None byte-for-byte equality). Contention test (golden ii) is the load-bearing verification; must run under both sequential and concurrent/subprocess scenarios.
- Shared helper refactor for `_parse_card_status` / `_parse_card_program` (see Execution concerns) — should be done before a third copy appears.
