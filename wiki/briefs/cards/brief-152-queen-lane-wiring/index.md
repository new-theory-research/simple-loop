---
ID: brief-152-queen-lane-wiring
Branch: brief-152-queen-lane-wiring
Status: merged
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Edit-surface:
  - templates/prompts/queen.md
  - lib/queue.py
  - bin/loop
  - lib/tests/test_lane_and_claim.py
Depends-on: brief-151
Tags: [harness, daemon, queue, lane, remote-queens, brick-0-fix]
---

# Brief: wire `--lane` through to brief selection (finish brick 0)

!!! abstract "Intent"
    brick 0 (brief-151) shipped `enumerate_dispatchable(lane=...)`, the daemon
    `--lane` arg, and the atomic claim — but the flag never reaches the queen's
    brief *selection*. A daemon launched `--lane remote-queens` still dispatches
    any queue-head brief. Close the wiring so `--lane` actually means "this daemon
    only runs this lane," with single-daemon (no-lane) behavior byte-for-byte
    unchanged. (portal#52.)

## Plain version

The queen — the LLM that actually picks the next brief — runs
`python3 .../queue.py .` with **no `--lane`** (`templates/prompts/queen.md:21,39`).
The daemon's `$_LANE_OPT` only feeds the dedup fingerprint, the dispatch-count
bypass, and a re-stat (`lib/daemon.sh:1648/1662/1693`); `assess.py` is lane-blind.
So `enumerate_dispatchable(lane=X)` is correct and unit-tested in isolation, but
the runtime path (daemon → queen prompt → `queue.py`) never invokes it with the
lane. The filter exists; nothing calls it where selection happens.

## The fix (three pieces)

1. **`lib/queue.py` — make an empty lane mean "no filter."** Today `--lane ""`
   yields `lane_key = "".lower() = ""`, which is fail-closed (excludes every
   unlabeled card). Change the coercion so an empty/whitespace lane is treated as
   `None`: `lane_key = lane.lower() if lane else None` (line ~120), and/or coerce
   in the `--lane` arg parse (`lane = argv[i] or None`). This makes step 2 safe
   for the single-daemon default, where `LOOP_LANE` is empty.

2. **`templates/prompts/queen.md` — pass the lane to selection.** Both
   `queue.py .` invocations (lines ~21 and ~39) become
   `queue.py . --lane "$LOOP_LANE"`. The daemon already exports `LOOP_LANE`
   (`lib/daemon.sh:50-51`); for a single daemon it's empty, which step 1 now
   treats as no-filter. So a `--lane remote-queens` daemon's queen sees only
   remote-queens briefs; a plain daemon's queen sees everything, unchanged.

3. **`bin/loop` install-service — let a lane-scoped daemon be installed.**
   `cmd_install_service` / the plist template (`bin/loop:~1909-1981`) have no
   `--lane` slot and no `LOOP_LANE` in `EnvironmentVariables`. Add a `--lane`
   passthrough that writes `LOOP_LANE` into the plist `EnvironmentVariables` (omit
   the key when no lane is given, so the stock single-daemon install is unchanged).

## Success criteria

- A `queue.py . --lane remote-queens` call returns ONLY cards whose `Program:`
  is `remote-queens` (and excludes unlabeled cards, fail-closed).
- `queue.py . --lane ""` and `queue.py .` return the identical set (no-lane
  default unchanged — assert byte-for-byte against the existing golden).
- `templates/prompts/queen.md` invokes `queue.py` with `--lane "$LOOP_LANE"` at
  every selection site.
- `loop install-service --lane <name>` produces a plist carrying
  `LOOP_LANE=<name>`; without `--lane`, the plist has no `LOOP_LANE` key (diff the
  generated plist).
- Extend `lib/tests/test_lane_and_claim.py`: add a case proving the empty-lane =
  no-filter coercion, and (if testable) that the queen-prompt selection path is
  lane-scoped. `python3 -m pytest lib/tests/ -q` stays green.

## Guards

- Do NOT change `enumerate_dispatchable`'s filter semantics for a *non-empty*
  lane (151's fail-closed-on-unlabeled behavior is correct — keep it).
- No `conductor` naming.
- Single-daemon (no-lane) dispatch must be byte-for-byte unchanged — this is the
  load-bearing backward-compat guarantee, same as brick 0.

## Outputs

- `closeout.md` — what shipped, the three pieces, pass criteria, how single-daemon
  parity was proven.
- `review.md` — gate runbook (Human-gate: review); link closeout for "what shipped."
