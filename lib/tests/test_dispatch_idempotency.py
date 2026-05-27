#!/usr/bin/env python3
"""Tests for dispatch() idempotency on the init-commit step (issue #7).

When dispatch() crashes after the init commit lands but before
pending-dispatch.json is removed, the daemon retries the whole flow. The
retry must NOT re-attempt `git commit -m "Initialize brief ..."` — that
fails with "nothing to commit" and traps the brief in a retry loop.

The detector `_init_commit_already_landed(wt_dir, brief)` returns True
exactly when HEAD subject == "Initialize brief {brief}", letting the
caller skip the init block.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from actions import _init_commit_already_landed  # noqa: E402


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


def _make_repo(tmp: Path) -> Path:
    wt = tmp / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    (wt / "README").write_text("seed\n")
    _git(wt, "add", "README")
    _git(wt, "commit", "-q", "-m", "seed")
    return wt


class TestInitCommitAlreadyLanded(unittest.TestCase):

    def test_returns_true_when_head_is_init_commit(self):
        """Retry scenario: init commit landed, dispatch crashed before cleanup."""
        with tempfile.TemporaryDirectory() as d:
            wt = _make_repo(Path(d))
            (wt / "progress.json").write_text("{}")
            _git(wt, "add", "progress.json")
            _git(wt, "commit", "-q", "-m", "Initialize brief brief-200-foo")

            self.assertTrue(_init_commit_already_landed(str(wt), "brief-200-foo"))

    def test_returns_false_when_head_is_seed(self):
        """Fresh dispatch: init commit not yet attempted → must run init block."""
        with tempfile.TemporaryDirectory() as d:
            wt = _make_repo(Path(d))

            self.assertFalse(_init_commit_already_landed(str(wt), "brief-200-foo"))

    def test_returns_false_when_init_is_for_different_brief(self):
        """Worktree carries another brief's init commit → don't skip."""
        with tempfile.TemporaryDirectory() as d:
            wt = _make_repo(Path(d))
            (wt / "progress.json").write_text("{}")
            _git(wt, "add", "progress.json")
            _git(wt, "commit", "-q", "-m", "Initialize brief brief-199-other")

            self.assertFalse(_init_commit_already_landed(str(wt), "brief-200-foo"))

    def test_returns_false_when_worker_committed_on_top(self):
        """Init landed, then worker committed cycle work → don't skip.

        This shouldn't happen in practice (workers don't run while
        pending-dispatch.json exists) but the detector should be precise:
        only HEAD-exactly-matches counts as 'already initialized'.
        """
        with tempfile.TemporaryDirectory() as d:
            wt = _make_repo(Path(d))
            (wt / "progress.json").write_text("{}")
            _git(wt, "add", "progress.json")
            _git(wt, "commit", "-q", "-m", "Initialize brief brief-200-foo")
            _git(wt, "commit", "-q", "--allow-empty", "-m", "cycle 1 work")

            self.assertFalse(_init_commit_already_landed(str(wt), "brief-200-foo"))


class TestRetryReproducesAndUnblocks(unittest.TestCase):
    """End-to-end: without the detector, the second `git commit` fails;
    with the detector skip, the retry path is no-op for the init block."""

    def test_second_init_commit_raises_without_skip(self):
        """Demonstrates the bug: re-running git commit raises CalledProcessError."""
        with tempfile.TemporaryDirectory() as d:
            wt = _make_repo(Path(d))
            (wt / ".loop" / "state").mkdir(parents=True)
            (wt / ".loop" / "state" / "progress.json").write_text('{"brief":"brief-200-foo"}')
            _git(wt, "add", ".loop/state/progress.json")
            _git(wt, "commit", "-q", "-m", "Initialize brief brief-200-foo")

            with self.assertRaises(subprocess.CalledProcessError):
                _git(wt, "commit", "-q", "-m", "Initialize brief brief-200-foo")

    def test_skip_path_keeps_history_clean(self):
        """When the detector fires, the caller skips init → no new commit, no error."""
        with tempfile.TemporaryDirectory() as d:
            wt = _make_repo(Path(d))
            (wt / ".loop" / "state").mkdir(parents=True)
            (wt / ".loop" / "state" / "progress.json").write_text('{"brief":"brief-200-foo"}')
            _git(wt, "add", ".loop/state/progress.json")
            _git(wt, "commit", "-q", "-m", "Initialize brief brief-200-foo")

            before = _git(wt, "rev-parse", "HEAD").stdout.strip()
            self.assertTrue(_init_commit_already_landed(str(wt), "brief-200-foo"))
            # Simulated caller skips the init block; no commit happens.
            after = _git(wt, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
