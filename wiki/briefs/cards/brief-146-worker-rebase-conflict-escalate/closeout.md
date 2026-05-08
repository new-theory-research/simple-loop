---
brief: brief-146-worker-rebase-conflict-escalate
status: complete
closed_at: 2026-05-08T19:15:00Z
---

# Closeout — brief-146: worker rebase conflict → escalate signal

## What shipped

| # | Task | File | Notes |
|---|---|---|---|
| 1 | Capture conflict context before rebase --abort | `lib/daemon.sh` | `git diff --name-only --diff-filter=U` + `git log -1` for main HEAD |
| 2 | `emit_rebase_conflict_escalate()` helper | `lib/actions.py:1287` | Writes `signals/escalate.json`; chains into existing escalate if present |
| 3 | CLI dispatch for `emit-rebase-conflict-escalate` | `lib/actions.py:1697,1768` | Wired into `BRIEF_ACTIONS` with brief_id/project_dir positional parsing |
| 4 | Notify string updated | `lib/daemon.sh` | Names file count + teammate SHA short |
| 5 | Unit tests (15 cases) | `lib/tests/test_rebase_conflict_escalate.py` | All pass; 69 total tests |

## Pass criteria

- `signals/escalate.json` written with `reason="rebase_conflict_against_main"`, `kind="rebase-conflict"`, `conflicted_paths`, `main_head`, `worktree` ✓
- Chaining: if escalate.json already exists, conflict appended to `chained_failures[]` ✓
- `move-to-awaiting-review kind=rebase-blocked` call preserved (additive escalate) ✓
- Empty `conflicted_paths` → `note="non-path conflict; see worker log"` added ✓
- `awaiting_review[]` entry carries `kind=rebase-blocked` via state projector ✓
- 54 pre-existing tests unchanged ✓

## Verification run

```
69 passed in 0.33s
```

## Lessons learned

- `conflicted_paths` captured via `git diff --name-only --diff-filter=U` before `rebase --abort`; passed to Python as single newline-separated arg that `splitlines()` parses.
- Chaining shape is clean: existing escalate.json gets a `chained_failures[]` append rather than clobber — important for multi-tenant hackathon where queen may write escalate before the worker tick.
- `emit-rebase-conflict-escalate` must be in `BRIEF_ACTIONS` set so it receives brief_id/project_dir positional parsing.
- Unit-testing `awaiting_review[]` kind requires the state projector path (append_event → project_running_json), not a direct running.json write — the projector is the truth.
