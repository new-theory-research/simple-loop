---
ID: brief-145-loop-install-service-reads-main-branch
Branch: brief-145-loop-install-service-reads-main-branch
Status: active
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: true
Edit-surface:
  - bin/loop
Depends-on: _none_
Tags: [harness, cli, install-service, env]
---

# Brief: `loop install-service` should read `config.json#git.main_branch` and set `GIT_MAIN_BRANCH`

!!! abstract "Intent"
    When `loop install-service` writes the per-project LaunchAgent plist (or systemd unit), it reads `.loop/config.json`'s `git.main_branch` value and adds `GIT_MAIN_BRANCH=<value>` to the `EnvironmentVariables` dict. Currently the daemon defaults `GIT_MAIN_BRANCH=main` (`daemon.sh:49`) regardless of the project's actual default branch, which breaks `GIT SYNC` for any project on `master` or other non-`main` defaults.

## Motivation

Surfaced 2026-05-05 during simple-loop's bootstrap (brief-143 ops). simple-loop's default branch is `master`. `loop init` correctly wrote `git.main_branch: "master"` to `config.json`, but `loop install-service` ignored it — the LaunchAgent had no `GIT_MAIN_BRANCH` env var, daemon defaulted to `main`, GIT SYNC said *"main tree on 'master' (not main) — fetch only"* on every tick. Hand-fix was patching the plist via `plistlib` to add `GIT_MAIN_BRANCH=master`, then bootout + bootstrap + kickstart.

This will bite every non-`main`-default project that uses `loop install-service`.

## Scope

### In

- `cmd_install_service()` (`bin/loop:1744`) reads `.loop/config.json` and extracts `git.main_branch`.
- Adds `<key>GIT_MAIN_BRANCH</key><string>${main_branch}</string>` to the plist's `EnvironmentVariables` dict.
- If `config.json` is missing or malformed, fall back to `main` and emit a warning.
- Same logic for `cmd_uninstall_service()` is a no-op (uninstall doesn't touch env).

### Out

- Don't change `daemon.sh:49`'s default — keep `main` as the safe fallback.
- Don't change the plist label scheme.
- Don't add other env vars to the plist (separate decision).

## Tasks

1. **Implement** — extend `cmd_install_service` to read config + emit env var.
2. **Test** — add an assertion to `scripts/test-flow-v2.sh` that runs install-service in a fixture project on a `master`-default repo and asserts the plist's GIT_MAIN_BRANCH env var matches the config.

## Cycle shape

1 cycle, `loop-coder`. Cycle ceiling 3.

## Completion criteria

- [ ] `loop install-service` writes `GIT_MAIN_BRANCH=<config.git.main_branch>` to plist
- [ ] Missing/malformed config falls back to `main` with a warning
- [ ] Test fixture verifies env var matches config
- [ ] `closeout.md` per contract

## Anti-patterns

- Don't change `daemon.sh` defaults
- Don't auto-execute long-running terminal commands

## Artifact

- Patch to `bin/loop:cmd_install_service`
- Test in `scripts/test-flow-v2.sh`
- `closeout.md` + `review.md`
