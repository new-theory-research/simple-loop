#!/usr/bin/env python3
"""Tests for issue #64: force-add progress.json on worker branches, strip at
merge-to-main.

BACKGROUND (2026-07-11 Wave-1b): the migration gitignored
`.loop/state/progress.json` everywhere to kill the main-branch
merge-contamination class (#54/#55). Correct for MAIN, but a regression for
worker BRANCHES — `assess.py` (and `auto_merge.py`) read a brief's
progress/status from the COMMITTED branch via `git show <ref>:path`, and the
worker commit paths use a plain `git add`, which now silently skips the
gitignored file. Dispatch-advance then starves on any brief dispatched
post-migration.

The adopted fix (issue #64):
  1. Worker-branch commit sites force-add: `git add -f .loop/state/progress.json`
     (so the committed branch carries the file the read path needs).
  2. merge() strips it back off main (`git rm --cached` + follow-up commit),
     so main's tip tree never tracks it — the migration's goal holds.
  3. With main carrying no progress.json, a progress.json-only rebase conflict
     becomes structurally impossible (main has nothing to conflict against).

These tests exercise the REAL code paths (real git repos, real subprocess git,
the actual assess.git_show and actions.merge functions) — not mocks.
"""

import json
import os
import subprocess
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import assess  # noqa: E402
from actions import init_paths, merge  # noqa: E402
from state import write_running_json  # noqa: E402

PROGRESS_REL = ".loop/state/progress.json"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": os.environ.get("HOME", "/tmp"),
}


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check, capture_output=True, text=True, env=GIT_ENV,
    )


def _write_progress(root: Path, brief: str, iteration: int, status: str = "running"):
    p = root / ".loop" / "state" / "progress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "brief": brief,
        "brief_file": f"wiki/briefs/cards/{brief}/index.md",
        "iteration": iteration,
        "status": status,
        "tasks_completed": [],
        "tasks_remaining": [],
        "learnings": [],
    }, indent=2) + "\n")
    return p


class TestForceAddDespiteGitignore(unittest.TestCase):
    """Invariant 1: a worker-branch commit carries progress.json even though it
    is gitignored — plain `git add` drops it, `git add -f` lands it."""

    def test_plain_add_drops_but_force_add_lands(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            repo = tmp / "repo"
            repo.mkdir()
            _git(repo, "init", "-q", "-b", "brief-x")
            _git(repo, "config", "user.email", "t@t")
            _git(repo, "config", "user.name", "t")
            (repo / ".gitignore").write_text(PROGRESS_REL + "\n")
            _git(repo, "add", ".gitignore")
            _git(repo, "commit", "-q", "-m", "seed gitignore")

            _write_progress(repo, "brief-x", 1)

            # Control: plain `git add` must NOT stage the gitignored file.
            _git(repo, "add", PROGRESS_REL, check=False)
            staged = _git(repo, "diff", "--cached", "--name-only").stdout
            self.assertNotIn(
                PROGRESS_REL, staged,
                "pre-condition: gitignore must be active — plain `git add` must "
                "leave progress.json unstaged (else the test proves nothing)",
            )

            # Fix: force-add stages it; the resulting commit carries it.
            _git(repo, "add", "-f", PROGRESS_REL)
            _git(repo, "commit", "-q", "-m", "worker: progress")

            # git show <branch>:path — the exact read path assess/auto_merge use.
            shown = _git(repo, "show", f"HEAD:{PROGRESS_REL}", check=False)
            self.assertEqual(
                shown.returncode, 0,
                "force-added progress.json must be present in the committed tree",
            )
            self.assertEqual(json.loads(shown.stdout)["iteration"], 1)
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


class TestAssessGitShowReadPath(unittest.TestCase):
    """Invariant 3: assess.py's actual git_show read path returns fresh branch
    progress end-to-end — a dispatch-like force-add commit, read back via the
    real function that assess.emit uses to drive dispatch-advance."""

    def test_git_show_returns_fresh_branch_progress(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            repo = tmp / "project"
            repo.mkdir()
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@t")
            _git(repo, "config", "user.name", "t")
            # Wave-1b: gitignore committed on main; main never tracks progress.json.
            (repo / ".gitignore").write_text(PROGRESS_REL + "\n")
            _git(repo, "add", ".gitignore")
            _git(repo, "commit", "-q", "-m", "seed")

            branch = "brief-64-demo"
            _git(repo, "checkout", "-q", "-b", branch)

            # Dispatch-like init commit: force-add progress.json (iteration 0).
            _write_progress(repo, branch, 0)
            _git(repo, "add", "-f", PROGRESS_REL)
            _git(repo, "commit", "-q", "-m", f"Initialize brief {branch}")

            # assess's real read function must see the branch's committed progress.
            raw = assess.git_show(str(repo), branch, PROGRESS_REL)
            self.assertIsNotNone(
                raw,
                "assess.git_show must return the branch's committed progress.json "
                "(returning None is the starvation bug this fix closes)",
            )
            self.assertEqual(json.loads(raw)["iteration"], 0)

            # Worker iteration advances progress; force-add + commit again.
            _write_progress(repo, branch, 4, status="running")
            _git(repo, "add", "-f", PROGRESS_REL)
            _git(repo, "commit", "-q", "-m", "worker: iteration 4")

            raw2 = assess.git_show(str(repo), branch, PROGRESS_REL)
            self.assertEqual(
                json.loads(raw2)["iteration"], 4,
                "git_show must return the FRESH committed progress after a "
                "subsequent worker force-add commit",
            )
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


class TestMergeStripsProgressFromMain(unittest.TestCase):
    """Invariant 2: after merge(), main's tree does NOT track progress.json,
    even though the merged branch committed it. Drives the real actions.merge()."""

    BRIEF = "brief-64-strip"

    def _setup(self, tmp: Path):
        brief = self.BRIEF
        origin = tmp / "origin.git"
        origin.mkdir()
        _git(origin, "init", "--bare", "-q", "-b", "main")

        project = tmp / "project"
        project.mkdir()
        _git(project, "init", "-q", "-b", "main")
        _git(project, "config", "user.email", "t@t")
        _git(project, "config", "user.name", "t")
        _git(project, "remote", "add", "origin", str(origin))

        state = project / ".loop" / "state"
        state.mkdir(parents=True)
        # Wave-1b gitignore on main + tracked runtime-events (dispatch plumbing).
        (project / ".gitignore").write_text(PROGRESS_REL + "\n")
        (state / "runtime-events.jsonl").write_text(
            '{"event":"dispatched","brief":"' + brief + '"}\n'
        )
        (project / "README").write_text("seed\n")
        _git(project, "add", "README", ".gitignore", ".loop/state/runtime-events.jsonl")
        _git(project, "commit", "-q", "-m", "seed")
        _git(project, "push", "-u", "origin", "main", "-q")

        # Brief branch: real work + force-added progress.json (as fixed dispatch/
        # worker paths do).
        _git(project, "checkout", "-q", "-b", brief)
        (project / "work.txt").write_text("work output\n")
        _write_progress(project, brief, 2, status="complete")
        _git(project, "add", "work.txt")
        _git(project, "add", "-f", PROGRESS_REL)
        _git(project, "commit", "-q", "-m", f"[worker] {brief} done")
        _git(project, "push", "-u", "origin", brief, "-q")
        _git(project, "checkout", "-q", "main")

        # Card: active + auto-merge.
        card = project / "wiki" / "briefs" / "cards" / brief
        card.mkdir(parents=True)
        (card / "index.md").write_text(
            f"---\nID: {brief}\nStatus: active\nAuto-merge: true\n---\n\n# {brief}\n"
        )
        _git(project, "add", f"wiki/briefs/cards/{brief}/index.md")
        _git(project, "commit", "-q", "-m", f"loop: card active for {brief}")
        _git(project, "push", "origin", "main", "-q")

        (project / ".loop" / "config.sh").write_text(
            "GIT_REMOTE=origin\nGIT_MAIN_BRANCH=main\n"
        )
        (state / "log.jsonl").write_text("")
        # Events placing the brief in pending_merges[].
        with open(state / "runtime-events.jsonl", "w") as f:
            for e in (
                {"event": "dispatched", "brief": brief, "branch": brief},
                {"event": "completed", "brief": brief, "kind": "complete",
                 "auto_merge": True},
                {"event": "approved", "brief": brief},
            ):
                f.write(json.dumps(e) + "\n")
        write_running_json(str(project))
        with open(state / "pending-merge.json", "w") as f:
            json.dump({"brief": brief, "branch": brief, "title": brief}, f)
        return project

    def test_main_tree_has_no_progress_after_merge(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            project = self._setup(tmp)

            # Pre-condition: the BRANCH tracks progress.json (force-added).
            self.assertEqual(
                _git(project, "show", f"{self.BRIEF}:{PROGRESS_REL}",
                     check=False).returncode, 0,
                "pre-condition: brief branch must carry committed progress.json",
            )

            paths = init_paths(str(project))
            self.assertTrue(merge(paths), "merge() must succeed")

            # The worker's real change landed on main...
            self.assertIn("work.txt", _git(project, "ls-files").stdout,
                          "worker's real content must land on main")
            # ...but progress.json must NOT be tracked on main's tip tree.
            self.assertNotIn(
                PROGRESS_REL, _git(project, "ls-files").stdout,
                "main must NOT track progress.json after merge (Wave-1b goal): "
                "the strip step must untrack it",
            )
            self.assertNotEqual(
                _git(project, "show", f"main:{PROGRESS_REL}",
                     check=False).returncode, 0,
                "git show main:progress.json must fail — main's tree carries no "
                "progress.json",
            )
            # runtime-events.jsonl is intentionally NOT stripped — it has no
            # branch read path and is legitimately committed to main.
            self.assertIn(
                ".loop/state/runtime-events.jsonl", _git(project, "ls-files").stdout,
                "runtime-events.jsonl must remain tracked on main (not stripped)",
            )
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


class TestRebaseInvariantNoConflict(unittest.TestCase):
    """Invariant (#55/PR#56 interaction): a worker branch WITH committed
    progress.json rebasing onto a main WITHOUT it produces NO conflict, and the
    branch's progress.json survives. With main carrying nothing at that path,
    the progress.json-only rebase conflict is structurally impossible."""

    def test_rebase_onto_clean_main_no_conflict_branch_copy_survives(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            repo = tmp / "project"
            repo.mkdir()
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@t")
            _git(repo, "config", "user.name", "t")
            (repo / ".gitignore").write_text(PROGRESS_REL + "\n")
            (repo / "shared.txt").write_text("base\n")
            _git(repo, "add", ".gitignore", "shared.txt")
            _git(repo, "commit", "-q", "-m", "seed")

            # Worker branch off seed: force-added progress.json + its own edit.
            branch = "brief-64-rebase"
            _git(repo, "checkout", "-q", "-b", branch)
            _write_progress(repo, branch, 3, status="running")
            (repo / "worker.txt").write_text("worker work\n")
            _git(repo, "add", "worker.txt")
            _git(repo, "add", "-f", PROGRESS_REL)
            _git(repo, "commit", "-q", "-m", f"[worker] {branch}")

            # main advances WITHOUT ever tracking progress.json (the post-strip
            # steady state).
            _git(repo, "checkout", "-q", "main")
            (repo / "shared.txt").write_text("base\nmain advance\n")
            _git(repo, "add", "shared.txt")
            _git(repo, "commit", "-q", "-m", "main advances")
            self.assertNotEqual(
                _git(repo, "show", f"main:{PROGRESS_REL}", check=False).returncode, 0,
                "pre-condition: main must NOT track progress.json",
            )

            # Rebase the worker branch onto the advanced main.
            _git(repo, "checkout", "-q", branch)
            rebase = _git(repo, "rebase", "main", check=False)
            self.assertEqual(
                rebase.returncode, 0,
                "rebase onto a progress.json-free main must NOT conflict "
                f"(rc={rebase.returncode}): {rebase.stdout}\n{rebase.stderr}",
            )
            # No unmerged paths remain.
            self.assertEqual(
                _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip(),
                "",
                "no conflicted paths must remain after the rebase",
            )
            # The branch's progress.json survives the rebase intact.
            shown = _git(repo, "show", f"HEAD:{PROGRESS_REL}", check=False)
            self.assertEqual(shown.returncode, 0,
                             "branch's committed progress.json must survive rebase")
            self.assertEqual(json.loads(shown.stdout)["iteration"], 3,
                             "branch's progress.json content must be preserved")
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
