# Review gate — brief-162 harness-update propagation

The runbook for the human gate: what to check and how, before you merge. For
the forensic record of what shipped, see [closeout.md](./closeout.md).

## The ask

Confirm that `loop update` is now the one invokable propagation edge from an
updated harness into a live project — and that it earns the trust the name
implies: never clobbers a customized prompt, never silently skips a file the
daemon requires, and never restarts a daemon behind the operator's back.

## Checks, in order

### 1. Run the tests (fast, mechanical)

```bash
# Full suite — expect 240 passed (227 baseline + 13 new for update_prompts)
python3 -m pytest lib/tests/ -q

# Shell test for the bin/loop entrypoint — expect 14/14 PASS
bash lib/tests/update-propagation.sh
```

The shell test covers the two loud-fail guards (missing PROVENANCE.json;
unusable `source_repo`) and drives the three-way refresh through a real git
baseline. The Python tests drive `classify()`/`refresh()` in
`lib/update_prompts.py` through the adversarial cases (customized-while-
template-unchanged, missing baseline, dry-run, per-file reporting).

### 2. Drive `loop update` in a scratch project (the real thing)

```bash
# Scratch project pointed at the branch's bin/loop
mkdir -p /tmp/loop-gate && cd /tmp/loop-gate
bash <branch-checkout>/bin/loop init --minimal
bash <branch-checkout>/bin/loop update
```

Expect, in order: source repo + installed commit + project baseline printed;
`git pull` output; `install.sh` output ending in an `Installed: <old> → <new>`
line; a changelog section (filtered to `templates/ core/ lib/`); a per-file
prompt-refresh report; the skills/modules re-sync; and — only if a daemon is
running for that project — a `loop stop && loop start` instruction.

### 3. Verify the three classifications by hand

Still in the scratch project, stage each case and re-run `loop update`:

```bash
# (a) identical — expect "in sync", file untouched
# (fresh init leaves prompts identical; just re-run update)

# (b) template-newer, project unmodified — expect "updated", file overwritten
#     Simulate by resetting the prompt to an older template revision, or:
echo "extra line" >> ~/.local/share/simple-loop/templates/prompts/worker.md
# (undo after — this edits the installed copy)

# (c) locally customized — expect "CUSTOMIZED — preserved" + a diff/cp
#     three-way-sync instruction naming the file, and your edit intact
echo "MY LOCAL RULE" >> .loop/prompts/queen.md
```

Also delete a prompt (`rm .loop/prompts/worker.md`) and confirm it's
**created**, not skipped — the daemon requires it.

### 4. Guard rails

- `git diff master -- lib/daemon.sh` on the branch is empty (guard: untouched).
- No code path calls `loop stop`/`loop start` — grep `cmd_update` in
  `bin/loop`; the restart is print-only.
- Loud-fail: `SIMPLE_LOOP_HOME=/tmp/nowhere loop update` in the scratch
  project dies non-zero naming PROVENANCE.json, with install.sh guidance,
  prompts untouched.

## What you should feel

Confidence that the five-command manual incantation is now one command, and
that the one command is paranoid in the right places: it refuses loudly when it
can't locate the source, and it treats your customized prompts as yours.
Skepticism is welcome on the classification boundaries — that's what check 3
exercises by hand.
