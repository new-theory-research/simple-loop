---
ID: brief-148-daemon-errors-bypass-dedup-and-loop-status-surfaces
Branch: brief-148-daemon-errors-bypass-dedup-and-loop-status-surfaces
Status: queued
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - lib/daemon.sh
  - bin/loop
Depends-on: _none_
Tags: [harness, daemon, observability, dedup]
---

# Brief: queen errors bypass dedup + `loop status` surfaces last error

!!! abstract "Intent"
    Two-part fix in one brief. (a) When `invoke_conductor` fails — queen prompt missing, `claude` CLI exits non-zero, JSON parse error — that failure must NOT be cached by `CONDUCTOR_DEDUP_TTL_SECS` (1800s today) under the same trigger as a healthy `no_active`. The error path writes a distinct dedup key (or skips dedup outright) so the next tick re-evaluates. (b) `loop status` reads the last queen error from a small persisted state file and prints it at the top of the output, before the routine signals — so an operator who runs `loop status` after a fatal config error sees "daemon errored 5m ago: queen prompt not found" instead of "Briefs: none active."

## Motivation

Per `~/new-theory/portal/wiki/operating-docs/incidents/2026-05-03-harness-pain-points.md` §1, "Compounding factor — Queen's `no_active` dedup TTL":

> 1800s = 30 min TTL. When Queen returns `no_active`, she dedups for 30 min … This is correct behavior in steady state but **wrong at the boundary** where … the cached evaluation is stale.

And §"Concrete case: queen.md rename, 2026-04-29":

> 1. `loop update` didn't sync the renamed prompt … `ERROR: queen prompt not found at .../.loop/prompts/queen.md`
> 2. After the first failed invocation, dedup kicked in (TTL 1800s) and the daemon kept logging "QUEEN: dedup — same trigger, skipping" rather than retrying. Without `loop status` flagging the underlying error, an operator would think "nothing to do" rather than "fatal config error, swallowed by dedup."

Both fixes were proposed in that doc. This brief lands them.

The hackathon constraint sharpens this: if a teammate's commit (or anything else) wedges queen mid-Saturday, the operator must see it on `loop status` within one tick — not 30 min later, and not via "go read daemon.log."

## Starting context

!!! info "Pointers — read in this order"
    1. `~/new-theory/portal/wiki/operating-docs/incidents/2026-05-03-harness-pain-points.md` §1 + queen.md-rename appendix — the framing this brief lands.
    2. `lib/daemon.sh:216-279` — `invoke_conductor`; the error paths (`return 1` after "queen prompt not found", `EXIT_CODE -ne 0` block).
    3. `lib/daemon.sh:1108-1140` — escalate-resolved + `dedup-clear-*` logic; the existing pattern for "break dedup on a state change."
    4. `lib/daemon.sh:1216-1230` — `LAST_CONDUCTOR_TRIGGER` + TTL comparison; the dedup-skip block to bypass on errors.
    5. `bin/loop:994-1110` — `cmd_status`; where to slot the last-error block.
    6. `wiki/briefs/cards/brief-145-loop-install-service-reads-main-branch/index.md` — recent small brief shape.

## Scope

### In

**Part (a) — error path bypasses dedup:**

- **New persisted state file** `.loop/state/last-queen-error.json` written when `invoke_conductor` returns non-zero OR when the queen-prompt-not-found early return fires. Shape: `{"ts": "<iso>", "reason": "<short-text>", "exit_code": <int>, "log_tail": "<last-10-lines-of-turn-log>"}`. Capped at ~2KB.
- **Dedup bypass on error.** In the conductor invocation block (around `lib/daemon.sh:1216`), if `invoke_conductor` returned non-zero on the prior tick, do not write `LAST_CONDUCTOR_TRIGGER`/`LAST_CONDUCTOR_TS` for the error tick — so the next tick's dedup compare doesn't match. Implementation note: simplest is "if EXIT was non-zero, set `LAST_CONDUCTOR_TRIGGER=error_<exit_code>_<ts>` (uniqueified)." That guarantees no two error ticks share a dedup key.
- **Clear `last-queen-error.json` on next successful queen tick.** Symmetric to how `escalate-resolved` resets the dedup marker (`lib/daemon.sh:1119-1124`). The file is "active error," not history.

**Part (b) — `loop status` surface:**

- **New top-of-output block** in `cmd_status`. Print BEFORE the daemon RUNNING/STOPPED line:
  ```
  ⚠ Daemon error (5m ago): queen prompt not found at .loop/prompts/queen.md
    last log: <log_tail line 1>
    fix: see TROUBLESHOOTING.md or daemon.log:<approx line>
  ```
  Suppressed entirely when `last-queen-error.json` is absent or stale (>1h old AND a successful tick has happened since — track `last-queen-success.json` as a heartbeat-like sibling, OR use `running.json#last_queen_ok_ts` if that field exists; if not, add it).
- **Color/symbol convention:** match existing `cmd_status` style — `⚠` prefix, no ANSI changes from current shape. The block is text-only.

### Out

- **Don't change the dedup TTL value** (1800s). The bug is "errors share a key with successes," not "TTL is wrong."
- **Don't add a new daemon command to clear the error.** Clearing is automatic (next successful tick) or via `loop reset` (existing) which already nukes signals.
- **Don't surface worker errors** — scope is queen invocation only. Worker failures already route through awaiting_review (kind=watchdog-timed-out etc.) and are visible via the existing `Awaiting you` block. Workers are out of scope here; if the same dedup-class issue exists worker-side, file a follow-up.
- **Don't auto-fix the underlying error** (e.g. don't `cp templates/prompts/queen.md` on detection). Surface; let the operator fix.

### Residue

- A follow-up could rotate `last-queen-error.json` into `errors.jsonl` history. Out of scope.
- A follow-up could surface the same in hive's Status panel (one-line warning row). Out of scope; `loop status` is the canonical operator surface today.

## Cycle plan

- Cycle 1 (`loop-coder`, sonnet) — Part (a): write `last-queen-error.json` from `invoke_conductor` error paths; bypass dedup by uniquifying `LAST_CONDUCTOR_TRIGGER` on error; clear on next success. Validator runs after.
- Cycle 2 (`loop-coder`, sonnet) — Part (b): `cmd_status` last-error block (suppressed when stale); update `bin/loop help` if needed; smoke via `scripts/test-flow-v2.sh` extension that synthesizes a queen-error tick.

## Verification

```bash
# Synthetic repro:
# 1. Stand up sandbox project; rename .loop/prompts/queen.md → queen.md.bak
# 2. loop start
# 3. Wait one tick (~30s)

cat .loop/state/last-queen-error.json | jq '.reason, .exit_code'
# expect: "queen prompt not found at .loop/prompts/queen.md", 1

loop status
# expect: top line shows "⚠ Daemon error (Ns ago): queen prompt not found …"

# Wait two ticks (>= 60s); confirm daemon still re-evaluates instead of
# deduping. Check daemon.log for two distinct QUEEN invocations:
grep "QUEEN #" .loop/state/log/daemon.log | tail -5
# expect: at least 2 invocations within 90s, not "dedup — same trigger"

# Recovery:
mv .loop/prompts/queen.md.bak .loop/prompts/queen.md
# Wait one tick

ls .loop/state/last-queen-error.json
# expect: file removed

loop status
# expect: top-of-output error block is gone
```

## Escalation triggers

- **`last-queen-error.json` write races with concurrent ticks** (multi-instance daemon, brief-076 territory). Use the existing `flock`/atomic-rename pattern from `lib/actions.py`'s state writes; if the pattern doesn't exist for shell, write to a temp + `mv`. If race-safety can't be solved in 1 cycle, escalate.
- **`loop status` parses `last-queen-error.json` and the file is malformed** (truncated write, hand-edited) — render `⚠ Daemon error (file unreadable; see daemon.log)` and continue. Don't fail `loop status`.
- **Existing `LAST_CONDUCTOR_TRIGGER` interactions break** (the escalate-resolved logic at `lib/daemon.sh:1108-1124` depends on the trigger marker for its reset). Run the flow-v2 test suite end-to-end; if any test regresses, escalate and unwind the dedup-bypass implementation in favor of a separate `LAST_CONDUCTOR_ERROR_TS` field.

## Anti-patterns

- Don't change the TTL value.
- Don't surface healthy `no_active` ticks as errors.
- Don't auto-execute long-running terminal commands in cycles.
- Don't add ANSI colors — `cmd_status` is plain-text.

## Artifact

- Patch to `lib/daemon.sh` (`invoke_conductor` error paths + dedup uniquify).
- Patch to `bin/loop` (`cmd_status` last-error block).
- Test in `scripts/test-flow-v2.sh` covering both parts.
- `closeout.md` + `review.md` per contracts.

## What this unlocks

The queen.md-rename incident becomes a 30-second debug instead of a 30-minute "why isn't anything happening." During the hackathon, any swallowed-error class (modal auth drift, claude CLI rate limit, prompt path drift) gets visible on `loop status` within one tick. Closes one of the two compounding factors named in the 2026-05-03 harness pain-points doc.
