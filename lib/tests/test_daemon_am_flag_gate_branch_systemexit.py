#!/usr/bin/env python3
"""Regression test for portal#34: bare `except: pass` around the AM_FLAG /
GATE_BRANCH python heredocs in daemon.sh swallowed SystemExit.

SystemExit is a BaseException, not an Exception — a bare `except:` catches
it too. Both heredocs call `print(...); sys.exit(0)` inside a `try:` whose
handler was a bare `except: pass`, so the exit was swallowed and execution
fell through to the trailing `print('false')` (AM_FLAG) or `print('')`
(GATE_BRANCH). Pre-fix, an `Auto-merge: true` card produced AM_FLAG =
"true\nfalse" — which never equals the bash string "true" — so
`[ "$AM_FLAG" = "true" ]` always failed and every auto-merge brief silently
downgraded to human review.

Live receipt: zero move-to-pending-merges log lines in the portal daemon
log since 2026-06-27, despite Auto-merge:true cards completing.

This test extracts the LITERAL snippet out of lib/daemon.sh (not a
hand-copied reimplementation, which could drift from the real script) and
runs it through bash + python3 exactly as the daemon does, against a real
git repo standing in for PROJECT_DIR.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_LIB_DIR = Path(__file__).parent.parent
DAEMON_SH = REPO_LIB_DIR / "daemon.sh"

ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
}


def _extract_assignment(var_name):
    """Pull `VAR=$(python3 -c "..." 2>/dev/null)` verbatim out of daemon.sh,
    preserving bash escaping exactly as written in the real file."""
    text = DAEMON_SH.read_text()
    start_marker = f'{var_name}=$(python3 -c "'
    start = text.index(start_marker)
    end_marker = '\n" 2>/dev/null)'
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True, env=ENV,
    )


class DaemonAutoMergeSystemExitTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "project"
        self.project_dir.mkdir()
        _git(self.project_dir, "init", "-q", "-b", "main")

    def _seed_card(self, auto_merge_line):
        """Write the brief card (with or without an Auto-merge line) onto a
        branch named 'brief-x', matching the running.json entry below."""
        brief_file = "wiki/briefs/cards/brief-x/index.md"
        full_path = self.project_dir / brief_file
        full_path.parent.mkdir(parents=True, exist_ok=True)
        body = "---\nID: brief-x\n"
        if auto_merge_line is not None:
            body += f"Auto-merge: {auto_merge_line}\n"
        body += "---\n\nbody\n"
        full_path.write_text(body)
        _git(self.project_dir, "add", brief_file)
        _git(self.project_dir, "commit", "-q", "-m", "seed")
        _git(self.project_dir, "branch", "brief-x")
        return brief_file

    def _running_file(self, branch, brief_file):
        running_file = Path(self.tmp.name) / "running.json"
        running_file.write_text(json.dumps({
            "active": [{"brief": "brief-x", "branch": branch, "brief_file": brief_file}]
        }))
        return running_file

    def _run_snippet(self, var_name, running_file):
        snippet = _extract_assignment(var_name)
        script = (
            f'RUNNING_FILE="{running_file}"\n'
            f'DAEMON_LIB_DIR="{REPO_LIB_DIR}"\n'
            f'GIT_REMOTE="origin"\n'
            f'PROJECT_DIR="{self.project_dir}"\n'
            f'active_entry="brief-x"\n'
            f'{snippet}\n'
            f'printf \'%s\' "${var_name}"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=ENV)
        self.assertEqual(r.returncode, 0, f"harness bash failed: {r.stderr}")
        return r.stdout

    # ── AM_FLAG ──────────────────────────────────────────────────────────────

    def test_am_flag_auto_merge_true_is_exactly_true(self):
        brief_file = self._seed_card("true")
        running_file = self._running_file("brief-x", brief_file)
        out = self._run_snippet("AM_FLAG", running_file)
        self.assertEqual(out, "true", f"expected exactly 'true', got {out!r}")

    def test_am_flag_auto_merge_false_is_exactly_false(self):
        brief_file = self._seed_card("false")
        running_file = self._running_file("brief-x", brief_file)
        out = self._run_snippet("AM_FLAG", running_file)
        self.assertEqual(out, "false", f"expected exactly 'false', got {out!r}")

    def test_am_flag_auto_merge_absent_is_exactly_false(self):
        brief_file = self._seed_card(None)
        running_file = self._running_file("brief-x", brief_file)
        out = self._run_snippet("AM_FLAG", running_file)
        self.assertEqual(out, "false", f"expected exactly 'false', got {out!r}")

    # ── GATE_BRANCH ──────────────────────────────────────────────────────────

    def test_gate_branch_resolves_exactly_to_branch_name(self):
        brief_file = self._seed_card("true")
        running_file = self._running_file("brief-x", brief_file)
        out = self._run_snippet("GATE_BRANCH", running_file)
        self.assertEqual(out, "brief-x", f"expected exactly 'brief-x', got {out!r}")


if __name__ == "__main__":
    unittest.main()
