---
title: "brief-152-queen-lane-wiring review — wire --lane through to brief selection"
brief: brief-152-queen-lane-wiring
category: review
escalated_at: 2026-06-28
status: awaiting-mattie
recommendation: approve
---

# Review gate — wire `--lane` through to brief selection (finish brick 0)

!!! abstract "TL;DR"
    **What shipped:** see [closeout.md](closeout.md) — three pieces that close the
    last gap between brick 0's `--lane` filter and the queen's actual brief
    selection. A `--lane remote-queens` daemon's queen now sees only its lane;
    single-daemon (no-lane) dispatch is byte-for-byte unchanged.

    **Your part:** ~5 min — re-run the parity checks below, eyeball three diffs,
    then approve the merge (`Auto-merge: false`, so it waits on you).

    **Why gated:** `Human-gate: review`. This rewires how every daemon selects
    briefs; the backward-compat guarantee (single-daemon parity) is load-bearing
    and worth a human eyeball before it lands on `master`.

!!! success "Why it matters"
    Until this merges, `--lane` is a half-built brick: the filter is unit-tested
    but nothing calls it where selection happens, so remote-queens lane isolation
    doesn't actually work at runtime. This is the wire that makes lane-scoped
    daemons real.

## What shipped

See **[closeout.md](closeout.md)** for the authoritative record (three pieces,
SHAs, per-criterion results). One-line index:

| # | Piece | SHA |
|---|---|---|
| 1 | `lib/queue.py` empty/whitespace `--lane` → no-filter (`None`) | `1a5f0bc` |
| 2 | `templates/prompts/queen.md` passes `--lane "$LOOP_LANE"` at both selection sites | `8ca0d54` |
| 3 | `bin/loop install-service --lane <name>` writes `LOOP_LANE` into plist | `2de2404` |

Branch `brief-152-queen-lane-wiring` · `Auto-merge: false` · 166 tests green · tree clean.

## What's gated on you

- Confirm the parity checks below still pass on the branch as-is.
- Approve the merge to `master` (or iterate / reject).

Worker can't merge — `Auto-merge: false` and `Human-gate: review` require your
sign-off on the backward-compat guarantee.

## Prerequisites

!!! info "Tooling"
    `python3` + `pytest` (already used by the suite), `git`, `plutil` (macOS,
    for the plist check), `diff`. No network, no daemon restart needed to review.

## Runbook

### Phase 1 — green suite + the parity proofs

**blocking.** ~2 min

```bash
cd <repo root of the brief-152 worktree>

# 1. Full suite stays green (includes TestEmptyLaneIsNoFilter).
python3 -m pytest lib/tests/ -q          # expect: 166 passed

# 2. Piece 1 parity: empty lane ≡ no lane, byte-for-byte (enumerate + fingerprint).
diff <(python3 lib/queue.py . --lane "") <(python3 lib/queue.py .) && echo "PIECE1 PARITY OK"

# 3. Piece 2 parity: the queen's selection invocation with empty LOOP_LANE.
LOOP_LANE=""; diff <(python3 lib/queue.py . --lane "$LOOP_LANE") <(python3 lib/queue.py .) \
  && echo "PIECE2 SEAM PARITY OK"

# 4. Lane actually filters (fail-closed on unlabeled) — sanity, unchanged from 151.
python3 lib/queue.py . --lane remote-queens   # expect: only remote-queens cards, no unlabeled
```

### Phase 2 — plist: with-lane vs stock no-lane

**requires_focus.** ~2 min

Confirm piece 3 adds `LOOP_LANE` only when asked, and leaves the stock install
byte-for-byte alone. Inspect the two generated plists (or read the
`cmd_install_service` sed block in `bin/loop` if you prefer the source):

- **With `--lane remote-queens`:** generated plist passes `plutil -lint` and its
  `EnvironmentVariables` contains `LOOP_LANE = remote-queens`.
- **Without `--lane`:** generated plist has **0** `LOOP_LANE` keys and diffs
  empty against the stock `templates/com.scaviefae.simpleloop.plist` (placeholder
  lines deleted outright).

See [closeout.md → "How single-daemon parity was proven"](closeout.md) for the
exact proofs the worker ran.

## What "works" looks like

- `166 passed`.
- Both `diff` parity checks print their `OK` line (empty diff).
- `queue.py . --lane remote-queens` lists only remote-queens cards, excludes unlabeled.
- With-lane plist lints clean and carries `LOOP_LANE`; no-lane plist has no `LOOP_LANE` key.

## Alternatives if a gate fails

!!! note "If a parity diff is non-empty"
    The single-daemon backward-compat guarantee is broken — do **not** merge.
    Iterate: the regression is almost certainly in piece 1's empty→None coercion
    (`queue.py`) since pieces 2 and 3 flow through it. Re-open the brief.

!!! note "If the no-lane plist gained a LOOP_LANE key"
    Piece 3's line-delete path regressed. Iterate; check the `/{{...}}/d` sed in
    `cmd_install_service`.

## Resolution options

| Option | When to pick | Action |
|---|---|---|
| **Approve** | All Phase 1 + 2 checks pass | Merge `brief-152-queen-lane-wiring` → `master` |
| **Iterate** | A parity check fails, or you want a change | Re-open the brief with the failing check noted |
| **Reject** | The approach is wrong | Close the branch; leave a note on the card |

## Scav recommendation

**Approve.**

The brief's load-bearing guarantee — single-daemon dispatch byte-for-byte
unchanged — is proven independently at all three seams (queue.py fingerprint,
queen.md shell expansion, plist line-delete), and the lane filter itself is
151's already-tested behavior left untouched. The risk surface is the no-lane
default, and that's exactly what the parity diffs pin down. 166 green, scope held
to the three pieces in the brief, no `conductor` naming, no `enumerate_dispatchable`
filter-semantics change.

## References

- [Brief index](index.md)
- [closeout.md](closeout.md) — what shipped, pass criteria, parity proofs, lessons
- Upstream: portal#52 · depends-on brief-151 (brick 0)
