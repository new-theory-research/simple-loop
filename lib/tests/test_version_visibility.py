#!/usr/bin/env python3
"""Tests for issue #41 — version visibility.

The daemon executes from an INSTALL (~/.local/share/simple-loop), not the
source repo, so a merge to master is inert until someone installs it — and
nothing showed which commit the running daemon was actually on. These tests
exercise the one primitive (write_provenance.py writes PROVENANCE.json at
install time) and the two readers that were built on it (actions.read_provenance
/ installed_version_line, consumed by daemon.sh's startup log and `loop
status`; actions.upstream_ahead_count, consumed by `loop status`) — the seam
that makes merge-without-install a visible fact instead of archaeology.
"""

import json
import os
import subprocess
import sys
import tempfile
import shutil
import unittest

_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from actions import read_provenance, installed_version_line, upstream_ahead_count  # noqa: E402

_WRITE_PROVENANCE = os.path.join(_LIB_DIR, "write_provenance.py")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
}


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check, capture_output=True, text=True, env=_GIT_ENV,
    )


class TestWriteProvenance(unittest.TestCase):
    """Install path: write_provenance.py writes PROVENANCE.json into a
    THROWAWAY install dir — never a live install location."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.source_repo = os.path.join(self.tmp, "source")
        self.install_dir = os.path.join(self.tmp, "install")
        os.makedirs(self.source_repo)
        _git(self.source_repo, "init", "--quiet", "-b", "main")
        with open(os.path.join(self.source_repo, "f.txt"), "w") as f:
            f.write("one")
        _git(self.source_repo, "add", "-A")
        _git(self.source_repo, "commit", "--quiet", "-m", "first")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_write_provenance(self):
        return subprocess.run(
            [sys.executable, _WRITE_PROVENANCE, self.source_repo, self.install_dir],
            check=True, capture_output=True, text=True,
        )

    def test_writes_provenance_with_sha_and_date(self):
        """First install: PROVENANCE.json lands in the install dir with the
        source repo's HEAD SHA + an install date — the install artifact
        daemon.sh and `loop status` read back."""
        head_sha = _git(self.source_repo, "rev-parse", "--short", "HEAD").stdout.strip()
        result = self._run_write_provenance()

        provenance_path = os.path.join(self.install_dir, "PROVENANCE.json")
        self.assertTrue(os.path.exists(provenance_path))
        with open(provenance_path) as f:
            data = json.load(f)
        self.assertEqual(data["source_commit"], head_sha)
        self.assertIn("installed_at", data)
        self.assertEqual(data["source_repo"], self.source_repo)
        self.assertFalse(data["source_dirty"])
        self.assertIn("(first install)", result.stdout)

    def test_read_provenance_parses_what_install_wrote(self):
        """The status-path reader (read_provenance) must parse exactly what
        the install-path writer (write_provenance.py) produced."""
        self._run_write_provenance()
        p = read_provenance(self.install_dir)
        self.assertIsNotNone(p)
        self.assertEqual(p["source_commit"],
                          _git(self.source_repo, "rev-parse", "--short", "HEAD").stdout.strip())

    def test_reinstall_prints_old_sha_to_new_sha(self):
        """A second install over a changed source repo prints old -> new —
        the install itself becomes a receipt."""
        self._run_write_provenance()
        old_sha = read_provenance(self.install_dir)["source_commit"]

        with open(os.path.join(self.source_repo, "f.txt"), "w") as f:
            f.write("two")
        _git(self.source_repo, "commit", "--quiet", "-am", "second")
        new_sha = _git(self.source_repo, "rev-parse", "--short", "HEAD").stdout.strip()

        result = self._run_write_provenance()
        self.assertIn(f"{old_sha} -> {new_sha}", result.stdout)
        self.assertEqual(read_provenance(self.install_dir)["source_commit"], new_sha)

    def test_dirty_source_repo_recorded(self):
        with open(os.path.join(self.source_repo, "f.txt"), "w") as f:
            f.write("uncommitted change")
        self._run_write_provenance()
        self.assertTrue(read_provenance(self.install_dir)["source_dirty"])


class TestInstalledVersionLine(unittest.TestCase):
    """daemon.sh's startup log line — must be loud, never blank, when
    PROVENANCE.json is missing (an install that predates this primitive)."""

    def test_missing_provenance_is_loud_not_silent(self):
        with tempfile.TemporaryDirectory() as install_dir:
            line = installed_version_line(install_dir)
        self.assertEqual(line, "unknown (pre-VERSION install)")

    def test_present_provenance_reports_sha_and_date(self):
        with tempfile.TemporaryDirectory() as install_dir:
            with open(os.path.join(install_dir, "PROVENANCE.json"), "w") as f:
                json.dump({
                    "source_commit": "abc1234",
                    "installed_at": "2026-07-05T12:00:00Z",
                    "source_dirty": False,
                }, f)
            line = installed_version_line(install_dir)
        self.assertIn("abc1234", line)
        self.assertIn("2026-07-05T12:00:00Z", line)
        self.assertNotIn("dirty", line)

    def test_dirty_install_flagged_in_line(self):
        with tempfile.TemporaryDirectory() as install_dir:
            with open(os.path.join(install_dir, "PROVENANCE.json"), "w") as f:
                json.dump({
                    "source_commit": "abc1234",
                    "installed_at": "2026-07-05T12:00:00Z",
                    "source_dirty": True,
                }, f)
            line = installed_version_line(install_dir)
        self.assertIn("dirty", line)


class TestUpstreamAheadCount(unittest.TestCase):
    """`loop status`'s upstream-ahead comparison — must degrade gracefully
    (never crash) when the source repo is unreachable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo_with_remote(self):
        remote = os.path.join(self.tmp, "remote.git")
        _git(self.tmp, "init", "--quiet", "--bare", "remote.git")
        clone = os.path.join(self.tmp, "clone")
        os.makedirs(clone)
        _git(clone, "init", "--quiet", "-b", "master")
        _git(clone, "remote", "add", "origin", remote)
        with open(os.path.join(clone, "f.txt"), "w") as f:
            f.write("one")
        _git(clone, "add", "-A")
        _git(clone, "commit", "--quiet", "-m", "first")
        _git(clone, "push", "--quiet", "-u", "origin", "master")
        return clone, remote

    def test_source_repo_missing_reports_unreachable(self):
        count, reason = upstream_ahead_count("/no/such/path", "abc1234")
        self.assertIsNone(count)
        self.assertIsNotNone(reason)

    def test_installed_sha_unknown_reports_unreachable(self):
        clone, _ = self._make_repo_with_remote()
        count, reason = upstream_ahead_count(clone, "unknown")
        self.assertIsNone(count)
        self.assertIsNotNone(reason)

    def test_up_to_date_reports_zero(self):
        clone, _ = self._make_repo_with_remote()
        sha = _git(clone, "rev-parse", "--short", "HEAD").stdout.strip()
        count, reason = upstream_ahead_count(clone, sha)
        self.assertIsNone(reason)
        self.assertEqual(count, 0)

    def test_upstream_ahead_counts_new_commits(self):
        clone, remote = self._make_repo_with_remote()
        installed_sha = _git(clone, "rev-parse", "--short", "HEAD").stdout.strip()

        # Simulate two more commits landing on master upstream, from a
        # second clone, pushed to the shared remote — then fetched (no
        # network here; refs already local) into the "installed" clone.
        other = os.path.join(self.tmp, "other")
        _git(self.tmp, "clone", "--quiet", remote, "other")
        _git(other, "checkout", "--quiet", "-B", "master", "origin/master")
        for i in range(2):
            with open(os.path.join(other, f"f{i}.txt"), "w") as f:
                f.write(str(i))
            _git(other, "add", "-A")
            _git(other, "commit", "--quiet", "-m", f"commit {i}")
        _git(other, "push", "--quiet", "origin", "master")
        _git(clone, "fetch", "--quiet", "origin")

        count, reason = upstream_ahead_count(clone, installed_sha)
        self.assertIsNone(reason)
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
