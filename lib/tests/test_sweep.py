#!/usr/bin/env python3
"""Tests for lib/sweep.py — deterministic state validator (brief-077 cycle 1).

Covers all 4 predicates plus auto-route. Uses staged fixtures, no daemon needed.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import sweep


# ── Fixtures ────────────────────────────────────────────────────────────────

def make_project(tmp: Path) -> Path:
    """Minimal .loop/ scaffold under tmp."""
    loop = tmp / ".loop"
    state = loop / "state"
    worktrees = loop / "worktrees"
    state.mkdir(parents=True)
    worktrees.mkdir(parents=True)
    (state / "running.json").write_text(json.dumps({
        "active": [], "completed_pending_eval": [], "history": []
    }))
    return tmp


def add_active(tmp: Path, brief_id: str, dispatched_at: str,
               iteration: int = 1, progress_status: str = "running",
               corrupt_progress: bool = False) -> None:
    """Add a brief to active[] and optionally scaffold its worktree."""
    state = tmp / ".loop" / "state"
    wt = tmp / ".loop" / "worktrees" / brief_id
    wt_state = wt / ".loop" / "state"
    wt_state.mkdir(parents=True)

    if corrupt_progress:
        (wt_state / "progress.json").write_text('{"iteration": 1, "learnings": ["\\s regex "]')
    else:
        (wt_state / "progress.json").write_text(json.dumps({
            "brief": brief_id,
            "iteration": iteration,
            "status": progress_status,
            "tasks_completed": [],
            "tasks_remaining": [],
            "learnings": [],
        }))

    running_path = state / "running.json"
    with open(running_path) as f:
        running = json.load(f)
    running["active"].append({
        "brief": brief_id,
        "branch": brief_id,
        "brief_file": f"wiki/briefs/cards/{brief_id}/index.md",
        "dispatched_at": dispatched_at,
        "parallel_safe": False,
    })
    running_path.write_text(json.dumps(running, indent=2))


def set_heartbeat(tmp: Path, last_event: str) -> None:
    state = tmp / ".loop" / "state"
    ts = sweep.datetime.now(sweep.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (state / "heartbeat.json").write_text(json.dumps({
        "ts": ts, "pid": 12345, "last_event": last_event
    }))


def old_ts(minutes_ago: int) -> str:
    """Return ISO timestamp N minutes in the past."""
    t = time.time() - (minutes_ago * 60)
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Predicate 1: progress-parse ─────────────────────────────────────────────

class TestProgressParse(unittest.TestCase):

    def test_valid_progress_ok(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(5))
            result = sweep.check_progress_parse(
                "brief-001-test",
                str(tmp / ".loop" / "worktrees")
            )
            self.assertEqual(result["status"], "ok")

    def test_corrupt_progress_fail(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(5), corrupt_progress=True)
            result = sweep.check_progress_parse(
                "brief-001-test",
                str(tmp / ".loop" / "worktrees")
            )
            self.assertEqual(result["status"], "fail")
            self.assertIn("parse error", result["evidence"].lower())

    def test_missing_worktree_skip(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            result = sweep.check_progress_parse(
                "brief-nonexistent",
                str(tmp / ".loop" / "worktrees")
            )
            self.assertEqual(result["status"], "skip")

    def test_missing_progress_json_warn(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            # Create worktree dir but no progress.json
            wt = tmp / ".loop" / "worktrees" / "brief-002-test"
            (wt / ".loop" / "state").mkdir(parents=True)
            result = sweep.check_progress_parse(
                "brief-002-test",
                str(tmp / ".loop" / "worktrees")
            )
            self.assertEqual(result["status"], "warn")


# ── Predicate 2: iteration-advance ──────────────────────────────────────────

class TestIterationAdvance(unittest.TestCase):

    def test_recent_dispatch_ok(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(5), iteration=2)
            result = sweep.check_iteration_advance(
                "brief-001-test", old_ts(5),
                str(tmp / ".loop" / "worktrees"), {}
            )
            self.assertEqual(result["status"], "ok")
            self.assertIn("< 30m threshold", result["evidence"])

    def test_no_snapshot_baselines(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(45), iteration=3)
            result = sweep.check_iteration_advance(
                "brief-001-test", old_ts(45),
                str(tmp / ".loop" / "worktrees"), {}
            )
            self.assertEqual(result["status"], "ok")
            self.assertIn("no previous snapshot", result["evidence"])

    def test_iteration_advanced_ok(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(45), iteration=4)
            snapshot = {"brief-001-test": {"iteration": 3}}
            result = sweep.check_iteration_advance(
                "brief-001-test", old_ts(45),
                str(tmp / ".loop" / "worktrees"), snapshot
            )
            self.assertEqual(result["status"], "ok")

    def test_iteration_frozen_fail(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(60), iteration=2)
            snapshot = {"brief-001-test": {"iteration": 2}}
            result = sweep.check_iteration_advance(
                "brief-001-test", old_ts(60),
                str(tmp / ".loop" / "worktrees"), snapshot
            )
            self.assertEqual(result["status"], "fail")
            self.assertIn("frozen", result["evidence"])

    def test_advancing_counter_high_dispatch_age_no_alarm(self):
        """Issue #38: healthy long brief, counter advanced recently — no alarm
        even though total dispatch age is well past STUCK_MIN and this tick's
        current==prev (no new advance since the immediately-prior sweep)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "ft-001-training-derisk-harness", old_ts(78), iteration=5)
            snapshot = {
                "ft-001-training-derisk-harness": {
                    "iteration": 5,
                    "last_advance_ts": old_ts(3),  # counter moved 3→4→5 recently
                }
            }
            result = sweep.check_iteration_advance(
                "ft-001-training-derisk-harness", old_ts(78),
                str(tmp / ".loop" / "worktrees"), snapshot
            )
            self.assertEqual(result["status"], "ok")

    def test_counter_genuinely_unmoved_past_threshold_alarms(self):
        """Issue #38: counter hasn't actually advanced in STUCK_MIN — still fires."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "ft-001-training-derisk-harness", old_ts(78), iteration=5)
            snapshot = {
                "ft-001-training-derisk-harness": {
                    "iteration": 5,
                    "last_advance_ts": old_ts(45),  # genuinely frozen for 45m
                }
            }
            result = sweep.check_iteration_advance(
                "ft-001-training-derisk-harness", old_ts(78),
                str(tmp / ".loop" / "worktrees"), snapshot
            )
            self.assertEqual(result["status"], "fail")
            self.assertIn("frozen", result["evidence"])


# ── Predicate 3: subprocess-exists ──────────────────────────────────────────

class TestSubprocessExists(unittest.TestCase):

    def test_recent_dispatch_ok(self):
        result = sweep.check_subprocess_exists("brief-001-test", old_ts(2))
        self.assertEqual(result["status"], "ok")
        self.assertIn("< 5m grace period", result["evidence"])

    def test_subprocess_found_ok(self):
        fake_ps = "\n".join([
            "USER  PID  %CPU  %MEM  COMMAND",
            "user  1234  0.0  0.1  claude --brief brief-001-test foo",
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = fake_ps
            result = sweep.check_subprocess_exists("brief-001-test", old_ts(10))
        self.assertEqual(result["status"], "ok")
        self.assertIn("1234", result["evidence"])

    def test_subprocess_missing_fail(self):
        fake_ps = "\n".join([
            "USER  PID  %CPU  %MEM  COMMAND",
            "user  5678  0.0  0.1  python3 daemon.sh /some/project",
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = fake_ps
            result = sweep.check_subprocess_exists("brief-001-test", old_ts(10))
        self.assertEqual(result["status"], "fail")
        self.assertIn("orphaned", result["suggested_action"])


# ── Predicate 4: heartbeat-active ───────────────────────────────────────────

class TestHeartbeatActive(unittest.TestCase):

    def test_heartbeat_worker_phase_ok(self):
        hb = {"ts": old_ts(1), "last_event": "phase3_worker:brief-001-test"}
        result = sweep.check_heartbeat_active("brief-001-test", old_ts(45), hb)
        self.assertEqual(result["status"], "ok")

    def test_heartbeat_idle_brief_fresh_ok(self):
        hb = {"ts": old_ts(1), "last_event": "phase5_sleep_idle"}
        result = sweep.check_heartbeat_active("brief-001-test", old_ts(5), hb)
        self.assertEqual(result["status"], "ok")

    def test_heartbeat_idle_brief_stuck_fail(self):
        """Mode-B failure — brief-067 pattern."""
        hb = {"ts": old_ts(1), "last_event": "phase5_sleep_idle"}
        result = sweep.check_heartbeat_active("brief-001-test", old_ts(60), hb)
        self.assertEqual(result["status"], "fail")
        self.assertIn("mode-B stuck state", result["evidence"])
        self.assertIn("brief-067", result["evidence"])


# ── Regression: brief-067 fixture ───────────────────────────────────────────

class TestBrief067Regression(unittest.TestCase):
    """Stage the brief-067 stuck-state scenario and assert all 3 predicates fire.

    The brief-067 incident had:
    - corrupt progress.json (unescaped \\s in a learnings string)
    - no claude subprocess for the brief
    - daemon heartbeat showing phase5_sleep_idle with brief still in active[]
    """

    def test_brief067_regression(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)

            dispatched = old_ts(65)  # 65 minutes ago > STUCK_MIN=30
            add_active(tmp, "brief-067-adr-auto-load", dispatched,
                       corrupt_progress=True)
            set_heartbeat(tmp, "phase5_sleep_idle")

            # Predicate 1: corrupt progress.json
            r1 = sweep.check_progress_parse(
                "brief-067-adr-auto-load",
                str(tmp / ".loop" / "worktrees")
            )
            self.assertEqual(r1["status"], "fail",
                             "P1 must flag corrupt progress.json")

            # Predicate 3: no subprocess (fake ps with no matching process)
            fake_ps = "USER  PID  COMMAND\nuser  99  python3 daemon.sh /foo"
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.stdout = fake_ps
                r3 = sweep.check_subprocess_exists(
                    "brief-067-adr-auto-load", dispatched
                )
            self.assertEqual(r3["status"], "fail",
                             "P3 must flag missing subprocess")

            # Predicate 4: heartbeat mismatch
            heartbeat_path = tmp / ".loop" / "state" / "heartbeat.json"
            with open(heartbeat_path) as f:
                heartbeat = json.load(f)
            r4 = sweep.check_heartbeat_active(
                "brief-067-adr-auto-load", dispatched, heartbeat
            )
            self.assertEqual(r4["status"], "fail",
                             "P4 must flag heartbeat-active mismatch")

    def test_full_sweep_on_brief067_fixture_exits_1(self):
        """run_sweep() must exit with code 1 on the brief-067 fixture."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            dispatched = old_ts(65)
            add_active(tmp, "brief-067-adr-auto-load", dispatched,
                       corrupt_progress=True)
            set_heartbeat(tmp, "phase5_sleep_idle")

            fake_ps = "USER  PID  COMMAND\nuser  99  python3 daemon.sh /foo"
            with patch("subprocess.run") as mock_run, \
                 patch("builtins.print"):  # suppress output noise in tests
                mock_run.return_value.stdout = fake_ps
                exit_code = sweep.run_sweep(
                    str(tmp), quick=False, auto_route=False,
                    snapshot_dir=str(tmp / ".loop" / "state")
                )
            self.assertEqual(exit_code, 1,
                             "sweep must return exit code 1 for stuck-state fixture")


# ── Auto-route ───────────────────────────────────────────────────────────────

class TestAutoRoute(unittest.TestCase):

    def test_orphaned_brief_moves_to_awaiting_review(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            dispatched = old_ts(65)
            add_active(tmp, "brief-001-test", dispatched)
            set_heartbeat(tmp, "phase5_sleep_idle")

            running_path = str(tmp / ".loop" / "state" / "running.json")
            sweep.auto_route_brief("brief-001-test", "brief-001-test",
                                   running_path, "orphaned subprocess")

            with open(running_path) as f:
                state = json.load(f)

            self.assertEqual(state["active"], [],
                             "active[] should be empty after auto-route")
            self.assertEqual(len(state["awaiting_review"]), 1,
                             "awaiting_review should contain 1 entry")
            entry = state["awaiting_review"][0]
            self.assertIn("sweep: orphaned subprocess", entry.get("conflict_note", ""))

    def test_auto_route_releases_claim(self):
        """brief-160: the auto-route move must release the brief's claim ref in
        the same operation (the serve-009 leak) — loudly, never silently."""
        import subprocess
        from claim import claim_brief, _ref_for

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)

            def _g(cwd, *a):
                return subprocess.run(["git", "-C", str(cwd), *a],
                                      check=True, capture_output=True, text=True)

            remote = tmp / "remote.git"
            _g(tmp, "init", "--quiet", "--bare", "remote.git")
            project = tmp / "project"
            project.mkdir()
            _g(project, "init", "--quiet", "-b", "main")
            _g(project, "remote", "add", "origin", str(remote))
            # Minimal .loop/ config so read_config resolves the remote name.
            state = project / ".loop" / "state"
            state.mkdir(parents=True)
            (project / ".loop" / "config.sh").write_text('GIT_REMOTE="origin"\n')

            bid = "brief-700-route"
            self.assertTrue(claim_brief(str(project), bid, str(remote)))
            self.assertEqual(
                _g(remote, "for-each-ref", "--format=%(refname)", "refs/claims/").stdout.strip(),
                _ref_for(bid))

            sweep.release_claim_on_route(str(project), bid)

            self.assertEqual(
                _g(remote, "for-each-ref", "--format=%(refname)", "refs/claims/").stdout.strip(),
                "", "auto-route must delete the claim ref")


# ── Snapshot I/O ────────────────────────────────────────────────────────────

class TestSnapshot(unittest.TestCase):

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(10), iteration=5)

            state_dir = str(tmp / ".loop" / "state")
            worktrees_dir = str(tmp / ".loop" / "worktrees")
            active = [{"brief": "brief-001-test"}]

            sweep.save_snapshot(state_dir, active, worktrees_dir)
            loaded = sweep.load_snapshot(state_dir)

            self.assertIn("brief-001-test", loaded)
            self.assertEqual(loaded["brief-001-test"]["iteration"], 5)

    def test_load_missing_snapshot_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            loaded = sweep.load_snapshot(d)
            self.assertEqual(loaded, {})

    def test_last_advance_ts_updates_when_iteration_advances(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(10), iteration=5)
            state_dir = str(tmp / ".loop" / "state")
            worktrees_dir = str(tmp / ".loop" / "worktrees")
            active = [{"brief": "brief-001-test"}]
            prev_snapshot = {"brief-001-test": {"iteration": 4, "last_advance_ts": old_ts(20)}}

            sweep.save_snapshot(state_dir, active, worktrees_dir, prev_snapshot=prev_snapshot)
            loaded = sweep.load_snapshot(state_dir)

            self.assertEqual(loaded["brief-001-test"]["iteration"], 5)
            # advanced this tick — last_advance_ts should be fresh (~now), not the old 20m-ago value
            self.assertLess(sweep.age_minutes(loaded["brief-001-test"]["last_advance_ts"]), 1)

    def test_last_advance_ts_carries_forward_when_unmoved(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_project(tmp)
            add_active(tmp, "brief-001-test", old_ts(10), iteration=5)
            state_dir = str(tmp / ".loop" / "state")
            worktrees_dir = str(tmp / ".loop" / "worktrees")
            active = [{"brief": "brief-001-test"}]
            prev_snapshot = {"brief-001-test": {"iteration": 5, "last_advance_ts": old_ts(20)}}

            sweep.save_snapshot(state_dir, active, worktrees_dir, prev_snapshot=prev_snapshot)
            loaded = sweep.load_snapshot(state_dir)

            self.assertEqual(loaded["brief-001-test"]["iteration"], 5)
            # unchanged this tick — last_advance_ts should carry forward the old value (~20m ago)
            self.assertGreater(sweep.age_minutes(loaded["brief-001-test"]["last_advance_ts"]), 15)


if __name__ == "__main__":
    unittest.main()
