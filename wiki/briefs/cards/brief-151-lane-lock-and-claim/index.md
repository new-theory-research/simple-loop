---
ID: brief-151-lane-lock-and-claim
Branch: brief-151-lane-lock-and-claim
Status: merged
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - lib/queue.py
  - lib/daemon.sh
  - lib/actions.py
  - lib/claim.py (new)
  - lib/tests/test_lane_and_claim.py (new)
  - install.sh
Depends-on: _none_
Tags: [harness, daemon, queue, lane, claim, multi-queen, remote-queens, keystone]
---

# Brief: make a program lane a real lock — partition + atomic claim

!!! abstract "Intent"
    Turn a program lane from documentation into an enforced partition, and add an
    atomic cross-box claim so two daemons sharing one repo+lane can never both
    execute one brief. The artifact: `enumerate_dispatchable()` reads `Program:`
    as a partition key, the daemon honors a `--lane <name>` filter, and a brief is
    claimed via a pushed git ref (`claims/<brief>`, `--force-with-lease`) BEFORE any
    worktree exists. With no `--lane`, single-daemon behavior is byte-for-byte unchanged.

## Plain version

Today nothing stops two daemons against the same remote from grabbing the same
brief — both enumerate `Status: queued`, both branch (the branch name *is* the
brief id), both spawn a worker, and the losing `git push` fails *non-fatally*
(`daemon.sh:627`) so neither backs off. Two workers on one brief, an unmergeable
tangle, no error raised. This brief closes that hole. It's the keystone of the
`remote-queens` lane (spec: portal `wiki/specs/remote-queens/index.md`): the brick
everything else stands on. It lands here, in simple-loop, because it changes the
daemon's own dispatch and queue code.

## Motivation / receipts

- **The `Program:` field already exists in card frontmatter and is read by ZERO
  code.** Cards carry it (`fleet-001`→`fleets`; `brief-150/151/152/153`→`program-004`
  in portal) but there are no hits in `queue.py`/`actions.py`/`daemon.sh`. Brick 0 is
  partly "make the field that already exists do something."
- **The only mutual exclusion today is a per-checkout PID file** (`daemon.sh:113-121`)
  — invisible across checkouts and across boxes.
- **The committed card `Status:` is the only cross-box signal.** `running.json` is
  projected per-box from each box's local `runtime-events.jsonl` (`actions.py:255-276`)
  and worktrees are never serialized — so a claim MUST live in committed/pushed git
  state, not `running.json`. A claim that lives only locally is a comment, not a claim.
- **`--force-with-lease` is already in-house** — the daemon uses it for branch pushes,
  so the claim primitive is not exotic.
- **We've seen this failure off a different cause:** hand-running briefs produced
  "three uncontrolled copies of one fix and an unverifiable tangle" (portal CLAUDE.md
  receipt, 2026-06-04). Multi-queen without a claim is that, mechanized.

## Self-modification caveat (read first)

This brief edits the very files the running daemon executes (`lib/queue.py`,
`lib/daemon.sh`, `lib/actions.py`). The worker runs in a worktree, so it edits a
copy; the new `--lane`/claim behavior goes live only after `install.sh` re-copies
`lib/` to the install dir and the service restarts. Do NOT expect the live daemon to
exhibit new behavior pre-reinstall, and do NOT auto-merge. Mirrors brief-150.

## Scope

### In

**1. Lane partition — `lib/queue.py`:**
- Extend `enumerate_dispatchable(project_dir, running=None, lane=None)` with an
  optional `lane` arg. Parse `Program:` from each card's frontmatter (reuse the
  existing `_parse_card_status` frontmatter reader — add a sibling `_parse_card_program`,
  do NOT hand-roll a second YAML reader).
- When `lane is None`: behavior is **byte-for-byte unchanged** — no `Program:` read
  enters the filter, every queued card is a candidate as today. (Success criterion iii,
  tested as exact-equality against the pre-patch candidate list.)
- When `lane="X"`: keep only cards whose `Program:` lowercases to `X`. A card with NO
  `Program:` field is **excluded** from any lane-filtered enumeration (fail-closed: an
  unlabeled brief never gets silently grabbed by a lane queen). Document the rule; don't
  silently drop.

**2. `--lane` filter — `lib/daemon.sh` + dispatch path (`lib/actions.py`):**
- Daemon accepts `--lane <name>` (and/or `LOOP_LANE` env). Thread it to the
  `enumerate_dispatchable(... lane=...)` call in the dispatch tick.
- No `--lane` given → `lane=None` → unchanged path. Purely additive.

**3. Atomic claim — new `lib/claim.py` + dispatch path:**
- `claim_brief(project_dir, brief_id, remote) -> bool` — pushes a ref
  `refs/claims/<brief_id>` with `--force-with-lease` (lease against "ref does not
  exist"). Returns `True` iff THIS daemon created the ref; `False` if rejected because
  another daemon already created it.
- Called in `dispatch()` **before** `create_worktree` (`actions.py:1191-1203`). On
  claim failure: log `loop: brief <id> already claimed — skipping`, move to next
  candidate, create NO branch/worktree.
- `release_claim(...)` on terminal states (merge, reject, escalate) — delete the ref so
  a re-queued brief is re-claimable. Best-effort, non-fatal.
- **Fail-loud:** a claim push failing for any reason OTHER than "already exists" (auth,
  network) aborts dispatch of that brief and logs the real error — never fall through to
  worktree creation. (Engineering rule 10.)

**4. `install.sh`:** one-line — copy `lib/claim.py` next to the other `lib/` copies.

### Out

- Lane-capacity limits / admission control / backpressure / cap on queens-per-lane —
  explicitly NOT specced (remote-queens guiding principle: observe breakage, don't
  pre-empt). The claim guards each brief's correctness; crowding a lane is the experiment.
- Cross-lane Edit-surface overlap enforcement (spec §6.1, open).
- The card-status-flip claim variant (git-ref chosen — don't build both).
- Hive visibility, `launch-queen`, provider/box indirection (bricks 1–3, portal).
- Card frontmatter format changes (`Program:` already exists).

### Residue

- Leaked claim refs (claimed then daemon died before release): a `loop claims` lister +
  stale-claim reaper is a likely follow-up; do NOT build here.
- Lease portability: if `--force-with-lease` empty-lease form is unportable across git
  versions on target boxes, escalate before changing the chosen primitive.

## Cycle plan

- Cycle 1 (loop-coder, opus) — lane partition in `queue.py` + `--lane` threading.
- Cycle 2 (loop-coder, opus) — `lib/claim.py` + claim-before-worktree + release + install.sh.
- Cycle 3 (loop-coder, opus) — test suite (contention test is load-bearing).
- Cycle ceiling: 6. Opus: git-ref atomicity races and the lane=None invariant are easy
  to get subtly wrong; blast radius is "daemon double-executes briefs across all projects."

## Verification

Tests encode WHY (rule 7) and verify the claim **under simulated contention**. New
`lib/tests/test_lane_and_claim.py`:

- **Golden i — `--lane X` enumerates ONLY lane-X briefs.** Fixture: two `Program: alpha`,
  two `Program: beta`, one unlabeled. `enumerate_dispatchable(dir, lane="alpha")` → exactly
  the two alpha, in goals.md order; beta + unlabeled absent.
- **Golden ii — two daemons, one repo+lane, never both execute one brief** (load-bearing,
  contention). One bare remote + two clones. Both `claim_brief(...)` same ref → exactly one
  `True`, one `False`. Run (a) sequential, (b) interleaved threads/subprocesses, looped N
  times. Loser creates NO branch/worktree (assert `create_worktree` not reached).
  `release_claim` then re-claim → succeeds.
- **Golden iii — no `--lane` → byte-for-byte unchanged.** Snapshot pre-patch
  `enumerate_dispatchable(dir)` (no lane); assert exact equality post-patch on a mixed fixture.
- **Regression:** `bash scripts/test-flow-v2.sh` baseline pass count unchanged;
  `python3 -m pytest lib/tests/test_lane_and_claim.py -v` all green.

## Escalation triggers

- Two `True` winners (or zero) in the contention test → lease expression wrong; STOP,
  escalate, no retry-loop papering.
- `--force-with-lease` unportable → escalate before substituting.
- `test-flow-v2.sh` regression → escalate.
- Golden iii snapshot-equality fails → the additive-only contract is broken; escalate
  (non-negotiable invariant).

## Anti-patterns

- Don't store the claim in `running.json`/any per-box local file — invisible across boxes.
- Don't add lane caps / admission control / backpressure.
- Don't hand-roll a second frontmatter parser — reuse the card reader.
- Don't make `lane=None` read `Program:` at all.
- Don't auto-merge; don't expect the live daemon to change pre-reinstall.
- Don't push the claim AFTER worktree creation — claim-first is the whole point.
- Don't disable any existing state-corruption / fetch-only guard.

## Artifact

Extended `lib/queue.py` (+ `_parse_card_program`); `--lane` threading in
`daemon.sh`/`actions.py:dispatch()`; new `lib/claim.py`; `install.sh` patch; new
`lib/tests/test_lane_and_claim.py` (3 goldens incl. contention); `review.md` + `closeout.md`.

## What this unlocks

The keystone of remote-queens (spec §5). A lane becomes a real lock, so a queen pinned
to a lane on a non-laptop box (brick 1, Titania queen, portal `rq-001`) never double-runs
a brief. Converts silent double-execution corruption (spec §2, §8) into clean, visible
contention — the fail-loud floor the whole multi-queen design stands on.
