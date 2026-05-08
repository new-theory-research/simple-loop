---
title: "brief-146 review — worker rebase conflict → escalate signal"
brief: brief-146-worker-rebase-conflict-escalate
category: review
status: awaiting-mattie
recommendation: approve
---

# Review Gate — brief-146: worker rebase conflict → escalate signal

!!! abstract "TL;DR"
    **What shipped:** When a worker rebase fails against main, hive now lights up coral `!` (escalate) in the Pending panel with conflicting paths and the teammate's SHA visible — instead of the previous amber `~` (awaiting-review) with no diagnostic context.

    **Target moment:** Hackathon 2026-05-09. Teammate pushes to portal/main; worker mid-cycle; rebase fails. Mattie sees the escalate row immediately, decides in seconds whether to land his change first or manually rebase the brief.

    **Your part:** Review the diff (~60 lines of daemon.sh + ~70 lines of actions.py), verify the test suite passes, approve when ready.

!!! success "Why it matters"
    The old flow swallowed the diagnostic — no escalate.json meant no coral `!` in hive, and no conflicted-paths/teammate-SHA meant archaeology to figure out what happened. This closes both gaps before the hackathon.

## What shipped

See [closeout.md](closeout.md) for the full record. Summary:

- `lib/daemon.sh`: capture conflicted paths + main HEAD before `rebase --abort`; call `emit-rebase-conflict-escalate`; update notify string.
- `lib/actions.py`: `emit_rebase_conflict_escalate()` writes `signals/escalate.json` with the diagnostic fields; chains into existing escalate rather than clobbering.
- `lib/tests/test_rebase_conflict_escalate.py`: 15 unit tests; 69 total pass.

Branch: `brief-146-worker-rebase-conflict-escalate`

## What's gated on you

- Read the diff and approve (or flag) before merge.
- Optionally run the test suite yourself: `python3 -m pytest lib/tests/ -q`
- No live repro needed before the hackathon — the production path was manually verified against the daemon.sh logic.

Worker can't self-approve a human-gate review brief.

## Prerequisites

!!! info "Tooling"
    `python3 -m pytest lib/tests/ -q` — 69 tests, ~0.33s.

## Runbook

### Phase 1 — Read the diff

**blocking.** ~5 min.

```bash
git diff master...brief-146-worker-rebase-conflict-escalate -- lib/daemon.sh lib/actions.py
```

Key things to check:
- `lib/daemon.sh`: conflicted-paths capture uses `--diff-filter=U` (correct for unmerged paths); main-head capture uses `git log -1 --format='%h %an %s'`.
- `lib/actions.py`: `emit_rebase_conflict_escalate` shape matches `push_with_escalate` dict shape (hive compatibility).
- Chaining logic: existing escalate.json gets `chained_failures[]` append, not clobber.

### Phase 2 — Run tests

**background.** ~1 min.

```bash
python3 -m pytest lib/tests/ -q
# expect: 69 passed
```

### Phase 3 — Approve

**blocking.** ~1 min.

```bash
loop approve brief-146-worker-rebase-conflict-escalate
```

## What "works" looks like

- `python3 -m pytest lib/tests/ -q` → 69 passed, 0 failures.
- Reviewing the daemon.sh diff: rebase conflict block now has `emit-rebase-conflict-escalate` call before `move-to-awaiting-review`.
- `signals/escalate.json` shape has `reason`, `kind`, `conflicted_paths`, `main_head`, `worktree`, `timestamp`.

## Resolution options

| Option | When to pick | Action |
|---|---|---|
| **Approve** | Tests pass, diff looks right | `loop approve brief-146-worker-rebase-conflict-escalate` |
| **Iterate** | Something in the escalate shape needs adjustment | push feedback; spawn cycle 3 |
| **Reject** | Won't merge before hackathon | `loop reject brief-146-worker-rebase-conflict-escalate` |

## Scav recommendation

**Approve.** The change is small, additive (doesn't remove existing `move-to-awaiting-review` call), and the 15 unit tests cover all the branches including chaining and empty-paths edge cases. The hackathon is tomorrow.

## References

- [Brief index](index.md)
- [closeout.md](closeout.md) — what shipped, pass criteria, lessons
