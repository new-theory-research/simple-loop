# Multi-instance simple-loop — what simple-loop IS, how it travels

**Status:** evolving philosophy. Captured during a clay-wetting conversation about installing simple-loop in a second project. Updates as thinking sharpens.

## What prompted this

Installing simple-loop in a second project surfaced a quiet shift: the thing being installed isn't just the daemon. It's the whole bundle.

## What simple-loop is becoming

Simple-loop started as the daemon — queen, worker, validator loop. Over time, orbital tooling earned its way in:

- **Zensical-backed wiki** with `briefs/cards/` + `operating-docs/` + `decisions/` + riffs + the CLAUDE.md director opening — the shape of the repo's own brain.
- **Beehive** (`crates/hive/`) — the ratatui TUI for glancing at queue + active + pending briefs.
- **The cards pattern** — each brief gets a directory with `index.md` + `plan.md` + `closeout.md` + any artifacts. Doubles as an observability surface and as the dispatch primitive (the daemon enumerates `wiki/briefs/cards/*/index.md` directly, filtering on frontmatter `Status:`).
- **Stewardship-log** — `.loop/state/stewardship-log.md` as the narrative log of non-automated interventions.
- **Goals.md + Queued/Awaiting/Done sections** — the queen's queue source + human-readable overview.

So: simple-loop-the-thing-we-install-in-a-new-project = daemon + beehive + cards convention + zensical wiki scaffold + stewardship-log + goals.md convention. Not all code, some of it is *conventions*. The conventions are as load-bearing as the code.

## How it travels (the key insight)

Simple-loop itself isn't a workspace. Nobody runs sessions "in simple-loop" — improvements always come from inside a consuming project. A consumer project queues briefs that happen to target `ScavieFae/simple-loop` master.

Implications:
- **Simple-loop master never gets its own beehive or queen** — those are the *deliverables* it ships to consumers, not the machinery it uses to receive updates.
- **The test harness has to be self-contained** so simple-loop master can verify changes without a consumer project's state. `scripts/test-flow-v2.sh` is shaped this way.

## Flow direction for improvements

```
[consumer project A]  →  [simple-loop master]  ←  [consumer project B]
       ↓                        ↓                         ↓
local .loop/state/       authoritative lib/*        local .loop/state/
local ./bin/loop         authoritative bin/loop     local ./bin/loop
local beehive binary     authoritative beehive src  local beehive binary
```

Improvements land first in the consumer project that discovered the need. The brief's implementation commits directly to `ScavieFae/simple-loop` master (via the target-repo pattern in brief frontmatter). Other consumers pull after merge via their sync mechanism.

## Per-project flavoring

Beehive shows state from the *local* `.loop/state/` only — that's already how it works. Per-project customization (colors, column layouts, TUI preferences) uses `.loop/config.json` with a `beehive` section for palette + layout overrides. Code stays shared; palette stays local.

Same principle extends to queen prompts, validator prompts, stewardship-log format — the *mechanism* is shared, the *flavoring* (palette, voice, specific phrasing) is per-project.

## Open questions

1. **`loop update` for multi-consumer sync.** Currently sync is manual (`~/.local/share/simple-loop/` copy step in brief closeouts). This won't scale to N consumers. Candidate: a `loop update` CLI command that pulls latest from master + re-syncs `~/.local/share/simple-loop/` + optionally rebuilds beehive. Defer until two consumers are live.

2. **Cross-instance stewardship-log coordination.** Today it's per-project. If multiple operators run instances concurrently, coordination across them may need either per-project logs with a periodic digest, or a shared top-level log (risky — cross-repo writes). Defer until coordination actually requires it.

3. **Is there a "simple-loop operator" role across instances?** If different operators run different instances, the operator-role pattern wants a name + a convention. Not urgent.

4. **Can the same daemon process serve multiple projects?** Unlikely clean — current daemon assumes one project dir, one goals.md, one running.json. Multi-process (one daemon per project) is the simpler model. Revisit only if single-daemon resource weight becomes an issue.

## The install story

From `loop init` forward, the installation path is:

1. `./install.sh` — installs daemon, CLI, hive binary, templates, docs
2. `loop init --wiki-full` (or `--minimal`) in the project root — scaffolds `.loop/` + wiki skeleton
3. Author `wiki/soul.md` + `wiki/CLAUDE.md` with project-specific content
4. Write the first brief, symlink it, add to `goals.md`
5. `loop start`

See [bundle-install.md](../bundle-install.md) for the full runbook.
