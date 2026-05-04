# Bundle install — setting up simple-loop in a new project

One-page runbook for bringing the full simple-loop bundle (daemon, hive TUI, scaffold, docs) into a fresh project root.

---

## 1. Install simple-loop itself

Clone and run the installer. This lands the `loop` CLI at `~/.local/bin/loop`, the `hive` binary at `~/.local/bin/hive`, core skills at `~/.claude/skills/loop-*/`, and core agents at `~/.claude/agents/loop-*.md`.

```bash
git clone https://github.com/ScavieFae/simple-loop.git ~/simple-loop
bash ~/simple-loop/install.sh
```

Verify:

```bash
loop help           # should print usage
hive --version      # should print a version string
```

If `~/.local/bin` isn't on your PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
# Add that line to your ~/.zshrc or ~/.bashrc to make it permanent
```

To update later: `cd ~/simple-loop && git pull && bash install.sh`.

---

## 2. `cd` into your new project root

```bash
cd /path/to/your-project
```

The directory doesn't need to be a git repo yet, but if it's going to be one, `git init` first so `loop init` can write an initial commit.

---

## 3. Run `loop init`

### For a new project (recommended)

```bash
loop init --wiki-full
```

Creates the full bundle scaffold:

```
.loop/
├── config.json
├── state/
│   ├── running.json
│   ├── goals.md
│   └── stewardship-log.md
├── briefs/
│   └── README.md
└── signals/
wiki/
├── CLAUDE.md
├── director-context.md
├── soul.md
├── zensical.toml
├── briefs/
│   ├── _template.md
│   ├── runway.md
│   └── cards/
│       └── README.md
├── decisions/
│   └── README.md
├── operating-docs/
│   └── README.md
└── riffs/
    └── README.md
```

### For a project that already has a wiki

```bash
loop init --minimal
```

Creates only the `.loop/` tree — no `wiki/` scaffold.

### Dry run (preview only)

```bash
loop init --dry-run --wiki-full
```

Prints what would be created; writes nothing.

---

## 4. Edit the project identity files

Open these two files and fill them in for your project:

**`wiki/soul.md`** — what does this project refuse to be? Write it here. One short, honest paragraph is plenty.

**`wiki/CLAUDE.md`** — the working conventions CLAUDE.md. It ships with the director-opening section already filled in (load order, dispatch floor, commit-prefix conventions). Scroll to the `## Project-specific` section at the bottom and add anything that's unique to this project: repo structure, tech stack, who operates here, what not to do.

**`wiki/director-context.md`** — onboarding companion for agents. Covers who operates here, how the work is structured, and which tools are available. Strip the placeholder comments; replace them with your project's actual operators, stack, and conventions.

---

## 5. Author your first brief

```bash
mkdir -p wiki/briefs/cards/brief-001-your-slug
```

Copy the template:

```bash
cp wiki/briefs/_template.md wiki/briefs/cards/brief-001-your-slug/index.md
```

Open it and fill in the brief. At minimum: a title, a goal, a task list, and completion criteria. The template has guidance inline.

---

## 6. Mark the brief as queued

The daemon enumerates dispatchable briefs by globbing `wiki/briefs/cards/*/index.md` and filtering on the card's frontmatter `Status:` field. To make a brief dispatchable, set `Status: queued` in the YAML frontmatter at the top of `index.md`:

```yaml
---
ID: brief-001-your-slug
Branch: brief-001-your-slug
Status: queued
Model: opus
---
```

No symlink needed — the card *is* the queue entry. See `lib/queue.py` for the canonical enumerator.

---

## 7. Start the daemon and verify

```bash
loop start
loop status           # should show the daemon running + your brief in the queue
hive                  # should render the TUI with your brief visible in Queued
```

If `loop status` shows the brief as `dispatchable`, the daemon will pick it up on the next tick. To trigger it immediately: `loop resume` (even if not paused — it prods the daemon).

---

## What's next

- **Add project briefs to `goals.md`** — the `## Queued next` section is what the queen reads to decide what's dispatchable. Briefs in `## Awaiting Mattie (not queued)` and `## Credential-gated` are held until conditions are met.
- **Check the bundle docs** — `~/.local/share/simple-loop/docs/` has conventions (cards, stewardship-log, goals-md, riffs, ADR) and operating docs (overnight-stewardship, hand-merge-brief, daemon-push-auth, and more).
- **Credential gating** — if a brief needs API keys to run, add `**Depends-on-secrets:** VAR1, VAR2` to its frontmatter. The daemon skips it silently until the vars are in the environment.
- **Per-project hive palette** — edit `.loop/config.json` and add a `beehive.palette` section to change hive's colors for this project. See `docs/conventions/goals-md.md` for the schema.

---

## Troubleshooting

**`hive` not found:** check that `~/.local/bin` is on your PATH. If install.sh completed without errors, the binary is at `~/.local/bin/hive`.

**`loop` not found:** same — `~/.local/bin/loop`. Run `export PATH="$HOME/.local/bin:$PATH"` and retry.

**`loop init` refused:** `.loop/` already exists in cwd. Use a fresh directory or file a brief to reset state.

**Daemon starts but doesn't pick up the brief:** check `loop status` — the brief may be in `awaiting_review` or blocked by a `Depends-on:` field. Check `.loop/state/goals.md` — the brief name must appear under `## Queued next`.

**Push fails silently after machine sleep:** run `gh auth setup-git` once from a terminal. This wires git's credential helper to gh's stored OAuth token, which survives keychain locks.
