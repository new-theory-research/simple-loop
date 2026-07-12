#!/usr/bin/env python3
"""Goldens for lib/why.py — the dispatchability explainer.

One case per predicate failing in isolation, the all-green case, and the
serve-003 scenario (a dependency that completed via a director arc — card
Status: complete, NOT merged — reads as UNMET because the enforcer recognizes
daemon-merged only). Each fixture is a real scratch git repo with a bare origin
so the claim-ref (git ls-remote) predicate exercises real remote state.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_LIB_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


why = _load("loop_why", "why.py")
claim = _load("loop_claim", "claim.py")


def _git(repo, *args, check=True, env=None):
    run_env = None
    if env:
        run_env = dict(os.environ)
        run_env.update(env)
    return subprocess.run(["git", "-C", repo, *args],
                          check=check, capture_output=True, text=True,
                          env=run_env)


def _write_card(project, brief_id, status="queued", program=None,
                parallel_safe=None, depends_on=None, edit_surface=None):
    card_dir = os.path.join(project, "wiki", "briefs", "cards", brief_id)
    os.makedirs(card_dir, exist_ok=True)
    body = ["---", f"ID: {brief_id}", f"Status: {status}"]
    if program is not None:
        body.append(f"Program: {program}")
    if parallel_safe is not None:
        body.append(f"Parallel-safe: {parallel_safe}")
    if depends_on is not None:
        body.append(f"Depends-on: {depends_on}")
    if edit_surface is not None:
        body.append(f"Edit-surface: {edit_surface}")
    body += ["---", "", f"# {brief_id}", ""]
    with open(os.path.join(card_dir, "index.md"), "w") as f:
        f.write("\n".join(body))


def _append_config(project, *lines):
    with open(os.path.join(project, ".loop", "config.sh"), "a") as f:
        f.write("\n".join(lines) + "\n")


def _commit_all_backdated(project, when="2020-01-01T00:00:00"):
    """Commit the tree with an old committer date, so queue.py's queued-age
    proxy (last commit time of the card file) reads as 'waited a long time'."""
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "cards",
         env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})


def _write_goals(project, order):
    with open(os.path.join(project, ".loop", "state", "goals.md"), "w") as f:
        for i, bid in enumerate(order, 1):
            f.write(f"{i}. {bid}\n")


def _make_project():
    """Scratch project with .loop/config.sh + a bare origin remote."""
    tmp = tempfile.mkdtemp()
    project = os.path.join(tmp, "proj")
    remote = os.path.join(tmp, "remote.git")
    os.makedirs(os.path.join(project, ".loop", "state"))
    os.makedirs(os.path.join(project, "wiki", "briefs", "cards"))
    with open(os.path.join(project, ".loop", "config.sh"), "w") as f:
        f.write("GIT_REMOTE=origin\nGIT_MAIN_BRANCH=main\n")

    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@t")
    _git(project, "config", "user.name", "t")
    with open(os.path.join(project, "seed"), "w") as f:
        f.write("x\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "seed")

    os.makedirs(remote)
    _git(remote, "init", "--bare", "-q")
    _git(project, "remote", "add", "origin", remote)
    return tmp, project


def _read(path):
    with open(path) as f:
        return f.read()


def _seed_papercuts(project):
    d = os.path.join(project, "wiki", "harness-operations")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "papercuts.md")
    with open(path, "w") as f:
        f.write("# Daemon papercuts\n\n> incidence log.\n")
    return path


def _checks(project, brief_id, running=None, lane=""):
    """Run the explainer with an explicit no-lane default so tests are
    deterministic regardless of a stray LOOP_LANE in the environment."""
    return {c.name: c for c in why.explain_dispatchability(
        project, brief_id, running=running, lane=lane)}


class WhyTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.project = _make_project()
        # Deterministic lane resolution for main()-based tests.
        self._saved_lane = os.environ.pop("LOOP_LANE", None)
        why._PAPERCUT_NOTE_SHOWN[0] = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._saved_lane is not None:
            os.environ["LOOP_LANE"] = self._saved_lane

    def _dispatchable(self, checks):
        return all(c.ok for c in checks.values())

    # ── all green ────────────────────────────────────────────────────
    def test_all_green(self):
        _write_card(self.project, "serve-010", status="queued")
        c = _checks(self.project, "serve-010", running={"active": []})
        self.assertTrue(self._dispatchable(c), c)
        self.assertTrue(c["queued"].ok)
        self.assertTrue(c["claim_ref"].ok)
        self.assertIn("no claim ref", c["claim_ref"].receipt)

    # ── 1. queued ────────────────────────────────────────────────────
    def test_not_queued(self):
        _write_card(self.project, "serve-010", status="draft")
        c = _checks(self.project, "serve-010", running={"active": []})
        self.assertFalse(c["queued"].ok)
        self.assertIn("not queued", c["queued"].receipt)
        self.assertFalse(self._dispatchable(c))

    def test_no_card(self):
        c = _checks(self.project, "ghost-001", running={"active": []})
        self.assertFalse(c["queued"].ok)
        self.assertIn("no card at", c["queued"].receipt)

    # ── 2. lane ──────────────────────────────────────────────────────
    def test_lane_mismatch(self):
        _write_card(self.project, "serve-010", status="queued", program="serving")
        c = _checks(self.project, "serve-010", running={"active": []},
                    lane="finetune,capture,fleets")
        self.assertFalse(c["lane"].ok)
        self.assertIn("'serving'", c["lane"].receipt)
        self.assertIn("not in daemon roster", c["lane"].receipt)

    def test_lane_unlabeled_failclosed(self):
        _write_card(self.project, "serve-010", status="queued")  # no Program:
        c = _checks(self.project, "serve-010", running={"active": []},
                    lane="finetune")
        self.assertFalse(c["lane"].ok)
        self.assertIn("no Program: lane", c["lane"].receipt)

    def test_lane_match(self):
        _write_card(self.project, "serve-010", status="queued", program="finetune")
        c = _checks(self.project, "serve-010", running={"active": []},
                    lane="finetune,capture")
        self.assertTrue(c["lane"].ok)

    # ── 3. parallel_safe ─────────────────────────────────────────────
    def test_parallel_safe_blocked_by_active(self):
        _write_card(self.project, "serve-010", status="queued")  # absent → false
        running = {"active": [{"brief": "other-001", "branch": "other-001"}]}
        c = _checks(self.project, "serve-010", running=running)
        self.assertFalse(c["parallel_safe"].ok)
        self.assertIn("defaults false (single slot)", c["parallel_safe"].receipt)
        self.assertIn("other-001", c["parallel_safe"].receipt)

    def test_parallel_safe_solo_empty_board_ok(self):
        _write_card(self.project, "serve-010", status="queued")
        c = _checks(self.project, "serve-010", running={"active": []})
        self.assertTrue(c["parallel_safe"].ok)

    # ── 4. depends_on ────────────────────────────────────────────────
    def test_serve_003_dependency_complete_not_merged(self):
        # The receipt scenario: serve-001 finished via a director arc → card
        # Status: complete (NOT merged). The enforcer recognizes daemon-merged
        # only, so serve-003 stays blocked. why.py reports it the same way.
        _write_card(self.project, "serve-001", status="complete")
        _write_card(self.project, "serve-003", status="queued",
                    depends_on="serve-001")
        c = _checks(self.project, "serve-003", running={"active": []})
        self.assertFalse(c["depends_on"].ok)
        self.assertIn("serve-001", c["depends_on"].receipt)
        self.assertIn("'complete'", c["depends_on"].receipt)
        self.assertIn("recognizes daemon-merged only", c["depends_on"].receipt)
        self.assertFalse(self._dispatchable(c))

    def test_depends_on_merged_ok(self):
        _write_card(self.project, "serve-001", status="merged")
        _write_card(self.project, "serve-003", status="queued",
                    depends_on="serve-001")
        c = _checks(self.project, "serve-003", running={"active": []})
        self.assertTrue(c["depends_on"].ok)

    # ── 5. claim_ref ─────────────────────────────────────────────────
    def test_claim_ref_present(self):
        _write_card(self.project, "serve-010", status="queued")
        # Real claim: push refs/claims/serve-010 to origin.
        self.assertTrue(claim.claim_brief(self.project, "serve-010", "origin"))
        c = _checks(self.project, "serve-010", running={"active": []})
        self.assertFalse(c["claim_ref"].ok)
        self.assertIn("already claimed", c["claim_ref"].receipt)
        self.assertFalse(self._dispatchable(c))

    # ── 6. not_running ───────────────────────────────────────────────
    def test_already_in_running(self):
        _write_card(self.project, "serve-010", status="queued")
        running = {"active": [{"brief": "serve-010", "branch": "serve-010"}]}
        c = _checks(self.project, "serve-010", running=running)
        self.assertFalse(c["not_running"].ok)
        self.assertIn("active[]", c["not_running"].receipt)

    def test_all_predicates_present(self):
        _write_card(self.project, "serve-010", status="queued")
        c = _checks(self.project, "serve-010", running={"active": []})
        self.assertEqual(
            set(c),
            {"queued", "lane", "parallel_safe", "depends_on",
             "claim_ref", "not_running", "throttle", "solo_drain"})

    # ── 7. throttle ──────────────────────────────────────────────────
    def test_throttle_at_cap(self):
        _append_config(self.project, "THROTTLE=2")
        _write_card(self.project, "serve-010", status="queued",
                    parallel_safe="true", edit_surface="lib/")
        running = {"active": [
            {"brief": "other-001", "branch": "other-001",
             "parallel_safe": True, "edit_surface": ["docs/"]},
            {"brief": "other-002", "branch": "other-002",
             "parallel_safe": True, "edit_surface": ["scripts/"]},
        ]}
        c = _checks(self.project, "serve-010", running=running)
        self.assertFalse(c["throttle"].ok)
        self.assertIn("board at THROTTLE cap 2/2", c["throttle"].receipt)
        self.assertIn("other-001, other-002", c["throttle"].receipt)
        self.assertFalse(self._dispatchable(c))

    def test_throttle_capacity(self):
        _append_config(self.project, "THROTTLE=3")
        _write_card(self.project, "serve-010", status="queued",
                    parallel_safe="true", edit_surface="lib/")
        running = {"active": [
            {"brief": "other-001", "branch": "other-001",
             "parallel_safe": True, "edit_surface": ["docs/"]},
        ]}
        c = _checks(self.project, "serve-010", running=running)
        self.assertTrue(c["throttle"].ok)
        self.assertIn("board 1/3", c["throttle"].receipt)

    # ── 8. solo_drain ────────────────────────────────────────────────
    def _drain_board(self):
        """Tonight's-missing-hour fixture: a Parallel-safe:false brief at the
        queue head past the drain threshold, a parallel-safe brief behind it,
        and a non-overlapping parallel-safe brief active on the board."""
        _append_config(self.project, "THROTTLE=3", "SOLO_DRAIN_AFTER_SECS=60")
        _write_card(self.project, "aaa-001", status="queued")  # solo head
        _write_card(self.project, "serve-010", status="queued",
                    parallel_safe="true", edit_surface="lib/")
        _write_goals(self.project, ["aaa-001", "serve-010"])
        _commit_all_backdated(self.project)  # head has "waited" years > 60s
        return {"active": [
            {"brief": "other-001", "branch": "other-001",
             "parallel_safe": True, "edit_surface": ["docs/"]},
        ]}

    def test_solo_drain_hold(self):
        running = self._drain_board()
        c = _checks(self.project, "serve-010", running=running)
        self.assertFalse(c["solo_drain"].ok)
        self.assertIn("held: Parallel-safe:false brief aaa-001 at queue head",
                      c["solo_drain"].receipt)
        self.assertIn("all other dispatch held until board empties",
                      c["solo_drain"].receipt)
        # Everything else about serve-010 is green — this is exactly the
        # false-green loop why would have shown without the drain check.
        self.assertTrue(c["parallel_safe"].ok)
        self.assertTrue(c["throttle"].ok)
        self.assertFalse(self._dispatchable(c))

    def test_solo_drain_head_itself_allowed(self):
        running = self._drain_board()
        c = _checks(self.project, "aaa-001", running=running)
        self.assertTrue(c["solo_drain"].ok)
        self.assertIn("IS the draining solo head", c["solo_drain"].receipt)

    def test_throttle_and_solo_drain_both_green(self):
        _append_config(self.project, "THROTTLE=3", "SOLO_DRAIN_AFTER_SECS=60")
        _write_card(self.project, "serve-010", status="queued",
                    parallel_safe="true", edit_surface="lib/")
        _commit_all_backdated(self.project)
        running = {"active": [
            {"brief": "other-001", "branch": "other-001",
             "parallel_safe": True, "edit_surface": ["docs/"]},
        ]}
        c = _checks(self.project, "serve-010", running=running)
        self.assertTrue(c["throttle"].ok)
        self.assertTrue(c["solo_drain"].ok)
        self.assertIn("no solo brief draining", c["solo_drain"].receipt)
        self.assertTrue(self._dispatchable(c))

    # ── papercuts ledger ─────────────────────────────────────────────
    def test_papercut_appended_when_blocked(self):
        path = _seed_papercuts(self.project)
        _write_card(self.project, "serve-001", status="complete")
        _write_card(self.project, "serve-003", status="queued", depends_on="serve-001")
        before = _read(path)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = why.main([self.project, "serve-003"])
        self.assertEqual(rc, 1)
        added = _read(path)[len(before):]
        self.assertIn("loop why serve-003", added)
        self.assertIn("blocked", added)
        self.assertIn("Expected:", added)
        self.assertTrue(added.strip().startswith("- **"))

    def test_papercut_appended_when_all_green(self):
        path = _seed_papercuts(self.project)
        _write_card(self.project, "serve-010", status="queued")
        before = _read(path)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = why.main([self.project, "serve-010"])
        self.assertEqual(rc, 0)
        added = _read(path)[len(before):]
        self.assertIn("dispatchable", added)
        self.assertIn("Expected:", added)

    def test_papercut_note_when_absent(self):
        _write_card(self.project, "serve-010", status="queued")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = why.main([self.project, "serve-010"])
        self.assertEqual(rc, 0)
        self.assertIn("papercuts.md not found", buf.getvalue())
        self.assertFalse(os.path.isfile(
            os.path.join(self.project, "wiki", "harness-operations", "papercuts.md")))

    def test_papercut_sweep_only_blocked(self):
        path = _seed_papercuts(self.project)
        _write_card(self.project, "serve-001", status="complete")
        _write_card(self.project, "serve-003", status="queued", depends_on="serve-001")
        _write_card(self.project, "serve-010", status="queued")  # green
        before = _read(path)
        with contextlib.redirect_stdout(io.StringIO()):
            why.main([self.project])  # sweep
        added = _read(path)[len(before):]
        self.assertIn("serve-003 (preflight)", added)
        self.assertNotIn("serve-010", added)


if __name__ == "__main__":
    unittest.main()
