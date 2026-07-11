#!/usr/bin/env python3
"""Diff-aware three-way refresh of a project's .loop/prompts/ against harness templates.

Extracted from `bin/loop cmd_update` so the three-way classification is
unit-testable (issues #20, #57). The module is two layers:

  * `classify()` / `refresh()` — pure logic over string contents. No git, no
    process state. This is what the tests drive.
  * `main()` — the CLI bin/loop calls. Resolves each file's *old* template
    (the template as it stood at the project's baseline commit) via
    `git show <base_sha>:templates/prompts/<file>` and prints a human report.

Three-way decision per prompt file, given new (current template), project
(the project's copy), and old (template at the project's baseline commit):

  * project copy missing            -> CREATED   (daemon requires it; write new)
  * project == new                  -> IN_SYNC   (nothing to do)
  * project == old  (and != new)    -> UPDATED   (unmodified from baseline; safe to overwrite)
  * old known, differs from both    -> CUSTOMIZED (locally edited; preserve, print 3-way sync)
  * old unknown, differs from new    -> DRIFT     (no baseline; preserve, print warning)

Never silently skips a file the daemon requires: a missing project copy is
CREATED, and every other outcome is reported by name.
"""

import argparse
import os
import subprocess
import sys

IN_SYNC = "in_sync"
UPDATED = "updated"
CREATED = "created"
CUSTOMIZED = "customized"
DRIFT = "drift"


def classify(new, project, old):
    """Classify one prompt file. Args are file *contents* as str, or None if absent.

    new     — current template content (None => template file absent)
    project — the project's copy (None => project copy missing)
    old     — template content at the project's baseline commit (None => unknown)
    """
    if project is None:
        return CREATED
    if new is not None and project == new:
        return IN_SYNC
    if old is not None and project == old:
        # Unmodified since last sync; the template moved -> safe to overwrite.
        return UPDATED
    if old is None:
        return DRIFT
    # Differs from both the old and the new template: a local customization.
    return CUSTOMIZED


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def refresh(templates_dir, prompts_dir, old_contents, apply=True):
    """Refresh prompts_dir/*.md from templates_dir/*.md via three-way classification.

    old_contents — dict {filename: old_content_str_or_None}, the baseline template.
    apply        — when True, write CREATED/UPDATED files; otherwise dry-run.

    Returns a list of {name, status} dicts, one per template file, sorted by name.
    """
    results = []
    if not os.path.isdir(templates_dir):
        return results
    for fname in sorted(os.listdir(templates_dir)):
        if not fname.endswith(".md"):
            continue
        tpath = os.path.join(templates_dir, fname)
        if not os.path.isfile(tpath):
            continue
        new = _read(tpath)
        ppath = os.path.join(prompts_dir, fname)
        project = _read(ppath) if os.path.exists(ppath) else None
        old = old_contents.get(fname)
        status = classify(new, project, old)
        if apply and status in (CREATED, UPDATED) and new is not None:
            os.makedirs(prompts_dir, exist_ok=True)
            with open(ppath, "w") as f:
                f.write(new)
        results.append({"name": fname, "status": status})
    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

def _git_show(source_repo, base_sha, rel_path):
    """Return file contents at base_sha, or None if unavailable (new file / bad sha)."""
    if not source_repo or not base_sha or base_sha == "unknown":
        return None
    try:
        out = subprocess.run(
            ["git", "-C", source_repo, "show", f"{base_sha}:{rel_path}"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _print_report(results, templates_dir, prompts_dir):
    label = {
        IN_SYNC: "in sync",
        UPDATED: "updated (was unmodified from baseline)",
        CREATED: "created (was missing — daemon requires it)",
        CUSTOMIZED: "CUSTOMIZED — preserved",
        DRIFT: "DRIFT — preserved (no baseline to compare)",
    }
    any_manual = False
    for r in results:
        name, status = r["name"], r["status"]
        print(f"    {name:<16}{label.get(status, status)}")
        if status in (CUSTOMIZED, DRIFT):
            any_manual = True
            proj = os.path.join(prompts_dir, name)
            tmpl = os.path.join(templates_dir, name)
            if status == CUSTOMIZED:
                print("      → template changed upstream but your copy is customized. Three-way sync:")
            else:
                print("      → can't tell if customized (no baseline). Compare before overwriting:")
            print(f"          diff {proj} {tmpl}")
            print("        reconcile by hand, then (to accept the template):")
            print(f"          cp {tmpl} {proj}")
    return any_manual


def main(argv=None):
    ap = argparse.ArgumentParser(description="Three-way refresh of .loop/prompts/ against harness templates.")
    ap.add_argument("--templates-dir", required=True, help="Installed templates/prompts dir (new templates)")
    ap.add_argument("--prompts-dir", required=True, help="Project .loop/prompts dir")
    ap.add_argument("--source-repo", default="", help="Harness source repo (for git-show of the baseline)")
    ap.add_argument("--base-sha", default="", help="Project's baseline harness commit")
    ap.add_argument("--template-subpath", default="templates/prompts",
                    help="Path of the templates dir within the source repo (for git-show)")
    ap.add_argument("--dry-run", action="store_true", help="Classify and report without writing")
    args = ap.parse_args(argv)

    old_contents = {}
    if os.path.isdir(args.templates_dir):
        for fname in os.listdir(args.templates_dir):
            if not fname.endswith(".md"):
                continue
            rel = f"{args.template_subpath}/{fname}"
            old_contents[fname] = _git_show(args.source_repo, args.base_sha, rel)

    results = refresh(args.templates_dir, args.prompts_dir, old_contents, apply=not args.dry_run)
    if not results:
        print("    (no templates found to compare)")
        return 0
    _print_report(results, args.templates_dir, args.prompts_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
