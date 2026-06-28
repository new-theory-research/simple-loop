---
title: "brief-151 closeout — make a program lane a real lock"
brief: brief-151-lane-lock-and-claim
category: closeout
status: complete
---

# Closeout — brief-151: lane partition + atomic claim

Forensic record of what shipped across three cycles. The keystone of the
`remote-queens` lane: a program lane becomes an enforced partition, and a brief
is claimed via a pushed git ref **before** any worktree exists, so two daemons
sharing one repo+lane can never both execute one brief.

## What shipped

| # | Cycle | Change | Landed as |
|---|---|---|---|
| 1 | 1 | Lane partition in `lib/queue.py` — `enumerate_dispatchable(..., lane=None)` + sibling `_parse_card_program` reading the `Program:` frontmatter key; `--lane` threaded through `lib/daemon.sh` + the `actions.py` drain gate; `queue_fingerprint` lane-namespaced | `3c4b9a0` |
| 2 | 2 | New `lib/claim.py` (`claim_brief` / `release_claim` via `refs/claims/<id>` with empty-expect `--force-with-lease`); claim-before-worktree in `actions.py:dispatch()`; release on 4 terminal transitions; `install.sh` copy | `ee2c017` |
| 3 | 3 | New `lib/tests/test_lane_and_claim.py` (3 goldens incl. load-bearing contention); **fix** to `claim.py` contention classification (see Lessons) | this cycle |

Branch `brief-151-lane-lock-and-claim`, target `ScavieFae/simple-loop master`.
**Not merged, not auto-merged** (Auto-merge: false; self-modification brief).

## Pass criteria — status

- **Golden i (lane filter):** `--lane alpha` returns exactly the two `Program:
  alpha` cards in goals.md order; beta + the unlabeled card are absent
  (fail-closed). Case-insensitive; unknown lane → empty. ✅
- **Golden ii (contention, load-bearing):** one bare remote + two clones racing
  one claim ref. Sequential → one `True`, one `False`. Interleaved threads
  (barrier-synchronized), 12 rounds → exactly one `True` + one `False` every
  round, never two winners or zero. Loser creates **no** worktree (mirror of the
  `actions.py` dispatch gate). `release_claim` → re-claim succeeds. Non-contention
  failure (bogus remote) raises (fail-loud, rule 10). ✅
- **Golden iii (additive invariant):** no `--lane` is byte-for-byte unchanged —
  `Program:` is never read; result equals the independently-computed legacy
  candidate list, equals explicit `lane=None`, and is unmoved by adding/removing
  `Program:` on cards. ✅
- **Regression — pytest:** `python3 -m pytest lib/tests/ lib/queue_test.py` →
  **190 passed** (12 new + the existing suite incl. `test_dispatch_idempotency`
  which exercises the modified dispatch path). ✅
- **Regression — test-flow-v2.sh:** `bash scripts/test-flow-v2.sh` →
  **154 passed, 52 failed**, matching the master baseline. **Correction:** this
  brief initially regressed test-flow-v2 to 152/54 — two concurrency gate tests
  (`empty active THROTTLE=1 → gate_pass` and `THROTTLE=2 + disjoint surfaces →
  gate_pass`) failed because the `cc_run_gate` harness helper mocked
  `ensure_worktree`/`A.git` but not the new `claim_brief`. The unmocked claim push
  hit a real `git push` to a nonexistent remote, fail-loud-raised, and `dispatch()`
  returned False before reaching the `gate_pass` sentinel. Fixed this cycle by
  stubbing `claim.claim_brief = lambda *a, **kw: True` in `cc_run_gate` (production
  claim is unchanged; 12 unit tests + the mutation test prove it against a real
  remote). The remaining 52 failures are pre-existing sandbox-environment failures
  (symlink removal, merge-to-main ops), unrelated to this brief. ✅
- **Escalation triggers:** none fired. No two-winners / zero-winners (lease
  correct); `--force-with-lease` empty-expect portable on git 2.50.1; no
  test-flow regression; golden iii snapshot equality holds.

## Lessons learned

**The load-bearing test earned its keep — it caught a real bug in cycle 2's
`claim.py`.** Under *true* concurrency (the entire reason this feature exists),
the losing daemon does **not** get the `"stale info"` lease rejection that
cycle 2 was written against. It gets `cannot lock ref '...': reference already
exists` / `failed to update ref`. Reason: both pushes pass the empty-lease check
optimistically (the ref looked absent when each read it), then git's
**server-side ref lock** serializes them — the winner creates the ref, the loser
fails *at lock time* with a different message. Cycle 2 classified that message as
a fail-loud `RuntimeError`. The lease was still atomically correct (exactly one
winner), so no escalation trigger fired — but in production the *first* real
two-daemon race would have logged a scary `claim_error` instead of the clean
`claim_skip`, undercutting the brief's whole purpose ("convert silent corruption
into clean, visible contention"). Fix: treat `reference already exists` /
`failed to update ref` as contention (`return False`) alongside `stale info`.
Both mean "the ref already exists — someone claimed it first." Sequential tests
alone would never have exposed this; only barrier-synchronized threads did.

**Two distinct contention messages, one meaning.** `stale info` is the lease
check failing because the local clone's remote-tracking ref already shows the
claim present (sequential / already-fetched). `reference already exists` is the
lock-time failure under live concurrency. Documented both in `claim.py`'s module
docstring so a future reader doesn't "simplify" the matcher back to one string.

**Refs accumulate; assert presence not exclusivity.** The interleaved test uses a
fresh unreleased `bid` per round, so the remote holds N refs after N rounds. The
per-round invariant is "this round's winner ref is *present*," not "it's the only
ref ever minted."

## References

- [Brief index](index.md)
- [review.md](review.md) — the gate-time runbook for Mattie
