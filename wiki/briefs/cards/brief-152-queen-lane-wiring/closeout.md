---
title: "brief-152-queen-lane-wiring closeout — wire --lane through to brief selection"
brief: brief-152-queen-lane-wiring
category: closeout
status: complete
depends-on: brief-151
---

# Closeout — wire `--lane` through to brief selection (finish brick 0)

## TL;DR

brick 0 (brief-151) shipped `enumerate_dispatchable(lane=...)`, the daemon
`--lane` arg, and the atomic claim — but the flag never reached the queen's
brief *selection*, so a `--lane remote-queens` daemon still dispatched any
queue-head brief. This brief closed the last three gaps. A lane-scoped daemon's
queen now sees only its lane's briefs; the single-daemon (no-lane) path is
**byte-for-byte unchanged**, proven at every seam.

166 tests green (`python3 -m pytest lib/tests/ -q`). Working tree clean.

## What shipped (three pieces)

| # | Piece | Landed as |
|---|---|---|
| 1 | `lib/queue.py` — empty/whitespace `--lane` coerces to no-filter (`None`), not the fail-closed literal `""` | `1a5f0bc` |
| 2 | `templates/prompts/queen.md` — both `queue.py` selection invocations pass `--lane "$LOOP_LANE"` | `8ca0d54` |
| 3 | `bin/loop install-service --lane <name>` — writes `LOOP_LANE` into the plist `EnvironmentVariables`; omitted when no lane | `2de2404` |

### Piece 1 — empty lane means "no filter"

The gap was twofold. `main()` already coerced an exact `--lane ""` to `None`
(`queue.py:294-297`), but (a) `enumerate_dispatchable(lane="")` called directly
still fail-closed via `lane_key = ""` (excludes every unlabeled card), and
(b) whitespace-only lanes (`"   "`) were coerced nowhere. Both fixed: the
enumerate coercion is now `lane_key = lane.strip().lower() if lane and lane.strip() else None`,
and the `main()` CLI coercion was widened to treat whitespace-only as `None`.
Added `TestEmptyLaneIsNoFilter` (4 cases: `""`, `"   "`, direct-call, CLI).

This is what makes piece 2 safe for the single-daemon default, where
`LOOP_LANE` is empty.

### Piece 2 — pass the lane to selection

Both `queue.py .` invocations in `templates/prompts/queen.md` (the assess step
~line 21 and the dispatch step ~line 39) became `queue.py . --lane "$LOOP_LANE"`.
The daemon already exports `LOOP_LANE` (`lib/daemon.sh:50-51`); for a single
daemon it's empty, which piece 1 now treats as no-filter. Added a one-line note
in the template explaining empty `LOOP_LANE` = no filter.

Note on the seam: `daemon.sh`'s `_LANE_OPT` (`daemon.sh:55-56`) feeds the
daemon's *own* fingerprint/dedup/dispatch-count path — it is separate from the
queen's selection invocation that piece 2 wired. They are now both lane-aware
but via different channels.

### Piece 3 — install a lane-scoped daemon

`cmd_install_service` now parses an optional `--lane <name>` / `--lane=<name>`
(the positional interval is preserved, whitespace trimmed; a whitespace-only
lane stays no-filter, consistent with piece 1). `templates/com.scaviefae.simpleloop.plist`
gained two own-line placeholders `{{LOOP_LANE_KEY}}` / `{{LOOP_LANE_VAL}}`.

The conditional injection uses **no multi-line sed**:
- **lane absent** → `/{{...}}/d` line-delete on both placeholders → plist is
  byte-for-byte the stock template (the load-bearing backward-compat guard).
- **lane set** → single-line `s|...|...|` (the `|` delimiter avoids escaping the
  `</key>` slashes; the lane name is sed-escaped for `|` and `&`) → `LOOP_LANE`
  appears in `EnvironmentVariables`.

The daemon needs **no `ProgramArguments` change** — it reads `LOOP_LANE` from
env (`daemon.sh:50`), so the plist `EnvironmentVariables` block is the only
wiring point.

## Pass criteria (brief → result)

| Criterion | Result |
|---|---|
| `queue.py . --lane remote-queens` returns ONLY `remote-queens` cards (fail-closed on unlabeled) | ✅ unchanged from 151; not touched, still asserted green |
| `queue.py . --lane ""` ≡ `queue.py .` (byte-for-byte vs golden) | ✅ enumerate output + queue fingerprint both identical (`ae018aafb9938ade`) |
| `queen.md` passes `--lane "$LOOP_LANE"` at every selection site | ✅ both sites (~line 21, ~line 39) |
| `loop install-service --lane <name>` → plist carries `LOOP_LANE=<name>`; no `--lane` → no `LOOP_LANE` key | ✅ with-lane passes `plutil`, `LOOP_LANE=remote-queens`; no-lane tail diffs empty vs stock template, 0 `LOOP_LANE` keys |
| `test_lane_and_claim.py` extended; `pytest lib/tests/ -q` green | ✅ `TestEmptyLaneIsNoFilter` added; 166 passed |

## How single-daemon parity was proven

The load-bearing guarantee is "single-daemon (no-lane) dispatch is byte-for-byte
unchanged." Proven independently at each piece's seam:

1. **Piece 1 (queue.py):** `--lane ""` and no-`--lane` produce identical
   `enumerate_dispatchable` output *and* identical `queue_fingerprint`
   (`ae018aafb9938ade`). The fingerprint branches on `lane is None`, so the
   empty→None coercion is load-bearing for parity, not just for enumerate.
2. **Piece 2 (queen.md):** the template runs the lane through shell expansion of
   `--lane "$LOOP_LANE"`. With `LOOP_LANE=""`, `queue.py . --lane "$LOOP_LANE"`
   is byte-for-byte identical (empty `diff`) to legacy `queue.py .` — the
   selection path is provably lane-scoped without changing the no-lane result.
3. **Piece 3 (plist):** with no `--lane`, the generated plist tail diffs empty
   against the pre-change stock template and contains 0 `LOOP_LANE` keys; the
   placeholder lines are deleted outright, so the stock install is untouched.

## Lessons

- **The filter existed; nothing called it where selection happened.** 151 built
  and unit-tested `enumerate_dispatchable(lane=...)` in isolation, but the
  runtime path (daemon → queen prompt → `queue.py`) never invoked it with the
  lane. Unit-tested-in-isolation ≠ wired. The brief correctly diagnosed this as
  "brick 0 unfinished," not a new feature.
- **Empty-as-no-filter is the keystone.** Making `--lane ""` mean "no filter"
  (piece 1) is what lets piece 2 unconditionally pass `--lane "$LOOP_LANE"` at
  every site — the single-daemon default flows through the same code path as a
  lane-scoped daemon, no branching in the prompt template.
- **Prove parity at the seam the code actually runs through.** `queen.md` is a
  prompt template, not pytest-testable; the honest proof was shell expansion +
  `diff`, not a unit test. Match the verification to where the behavior lives.
- **Conditional plist injection without multi-line sed.** Two own-line
  placeholders + line-delete (no-lane) vs single-line substitution (lane) keeps
  the no-lane path byte-for-byte stock and sidesteps `</key>` slash-escaping by
  using a `|` sed delimiter.

## References

- [Brief index](index.md)
- [review.md](review.md) — gate runbook
- Upstream: portal#52 · depends-on brief-151 (brick 0)
- Verify: `python3 -m pytest lib/tests/ -q` → 166 passed
