#!/usr/bin/env python3
"""brief-160 piece 2 — parked as a first-class state.

Receipts for the read-side: the enumerator never dispatches a parked card, the
projector drops it from active[], `loop why` explains it (blocker/owner/
re-trigger), park_brief/unpark_brief are inverses, and the #92 `Owned-live:` flag
makes the staleness predicate skip a director-live arc. Each fixture is a real
scratch git repo (park commits the card status flip) with a bare origin so the
claim release exercises real remote refs.
"""

import importlib.util
import io
import contextlib
import json
import os
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


actions = _load("loop_actions", "actions.py")
state = _load("loop_state", "state.py")
queue = _load("loop_queue", "queue.py")
why = _load("loop_why", "why.py")
assess = _load("loop_assess", "assess.py")


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args],
                          check=check, capture_output=True, text=True)


def _write_card(project, brief_id, status="active", extra_fm=None, body="Body.\n"):
    card_dir = os.path.join(project, "wiki", "briefs", "cards", brief_id)
    os.makedirs(card_dir, exist_ok=True)
    lines = ["---", f"ID: {brief_id}", f"Status: {status}"]
    for k, v in (extra_fm or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", f"# Brief: {brief_id}", "", body]
    with open(os.path.join(card_dir, "index.md"), "w") as f:
        f.write("\n".join(lines))


def _setup_project(brief_id="brief-x", status="active", active=True, extra_fm=None):
    """Real git repo + bare origin + one card + running.json. Returns (project, paths)."""
    tmp = tempfile.mkdtemp()
    origin = os.path.join(tmp, "origin.git")
    project = os.path.join(tmp, "work")
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    subprocess.run(["git", "clone", "-q", origin, project], check=True)
    _git(project, "config", "user.email", "t@t.co")
    _git(project, "config", "user.name", "t")

    os.makedirs(os.path.join(project, ".loop", "state", "signals"), exist_ok=True)
    with open(os.path.join(project, ".loop", "config.sh"), "w") as f:
        f.write('GIT_REMOTE="origin"\nGIT_MAIN_BRANCH="master"\n')
    _write_card(project, brief_id, status=status, extra_fm=extra_fm)
    running = {"active": [], "awaiting_review": [], "pending_merges": [], "history": []}
    if active:
        running["active"] = [{"brief": brief_id, "branch": brief_id, "parallel_safe": False}]
    with open(os.path.join(project, ".loop", "state", "running.json"), "w") as f:
        json.dump(running, f)
    with open(os.path.join(project, ".loop", "state", "runtime-events.jsonl"), "w") as f:
        f.write(json.dumps({"ts": "2026-07-14T00:00:00Z", "event": "dispatched",
                            "brief": brief_id}) + "\n")
    open(os.path.join(project, ".loop", "state", "log.jsonl"), "a").close()
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    _git(project, "push", "-q", "origin", "HEAD:master")
    return project, actions.init_paths(project)


def _card_status(project, brief_id):
    path = os.path.join(project, "wiki", "briefs", "cards", brief_id, "index.md")
    return actions._read_card_status(path)


def _running(paths):
    with open(paths["running_file"]) as f:
        return json.load(f)


class TestPark(unittest.TestCase):
    def test_park_releases_slot_and_writes_block(self):
        project, paths = _setup_project()
        ok = actions.park_brief(paths, "brief-x", blocker="needs human auth",
                                owner="human", retrigger="auth granted")
        self.assertTrue(ok)
        # Card is parked, slot released (active[] empty).
        self.assertEqual(_card_status(project, "brief-x"), "parked")
        self.assertEqual(_running(paths)["active"], [])
        # Parked block written onto the card frontmatter.
        meta = why._read_parked_block(
            os.path.join(project, "wiki", "briefs", "cards", "brief-x", "index.md"))
        self.assertEqual(meta["parked-blocker"], "needs human auth")
        self.assertEqual(meta["parked-owner"], "human")
        self.assertEqual(meta["parked-retrigger"], "auth granted")
        self.assertIn("parked-at", meta)

    def test_human_owner_raises_escalation(self):
        project, paths = _setup_project()
        actions.park_brief(paths, "brief-x", blocker="b", owner="human", retrigger="r")
        esc = os.path.join(paths["signals_dir"], "escalate.json")
        self.assertTrue(os.path.exists(esc))
        with open(esc) as f:
            payload = json.load(f)
        self.assertEqual(payload["brief"], "brief-x")
        self.assertEqual(payload["type"], "brief_parked")

    def test_director_owner_no_escalation(self):
        project, paths = _setup_project()
        actions.park_brief(paths, "brief-x", blocker="b", owner="director", retrigger="r")
        self.assertFalse(os.path.exists(os.path.join(paths["signals_dir"], "escalate.json")))

    def test_park_writes_dedup_clear_signal(self):
        project, paths = _setup_project()
        actions.park_brief(paths, "brief-x", blocker="b", owner="director", retrigger="r")
        self.assertTrue(os.path.exists(
            os.path.join(paths["signals_dir"], "dedup-clear-brief-x.json")))


class TestUnpark(unittest.TestCase):
    def test_unpark_is_inverse(self):
        project, paths = _setup_project()
        actions.park_brief(paths, "brief-x", blocker="b", owner="director", retrigger="r")
        ok = actions.unpark_brief(paths, "brief-x", by="cli", reason="cleared")
        self.assertTrue(ok)
        self.assertEqual(_card_status(project, "brief-x"), "queued")
        # Parked block cleared from frontmatter, moved to Park history.
        card = os.path.join(project, "wiki", "briefs", "cards", "brief-x", "index.md")
        with open(card) as f:
            content = f.read()
        self.assertNotIn("Parked-blocker:", content)
        self.assertIn("## Park history", content)
        self.assertIn("Unparked", content)

    def test_unpark_noop_when_not_parked(self):
        project, paths = _setup_project(status="active")
        self.assertFalse(actions.unpark_brief(paths, "brief-x", by="cli"))


class TestEnumeratorAndProjection(unittest.TestCase):
    def test_enumerator_never_dispatches_parked(self):
        project, paths = _setup_project(status="parked", active=False)
        ids = [c["brief"] for c in queue.enumerate_dispatchable(project)]
        self.assertNotIn("brief-x", ids)

    def test_projection_drops_parked_from_active(self):
        project, paths = _setup_project(status="parked", active=False)
        out = state.project_running_json(project)
        self.assertEqual([b["brief"] for b in out["active"]], [])
        self.assertEqual(out["awaiting_review"], [])
        self.assertEqual(out["pending_merges"], [])


class TestWhyReceipt(unittest.TestCase):
    def test_why_explains_parked(self):
        project, paths = _setup_project(status="active")
        actions.park_brief(paths, "brief-x", blocker="human console auth",
                           owner="director", retrigger="redeploy done")
        checks = why.explain_dispatchability(project, "brief-x")
        parked = [c for c in checks if c.name == "parked"]
        self.assertEqual(len(parked), 1)
        self.assertFalse(parked[0].ok)
        self.assertIn("human console auth", parked[0].receipt)
        self.assertIn("director", parked[0].receipt)
        self.assertIn("redeploy done", parked[0].receipt)


class TestOwnedLiveStalenessSkip(unittest.TestCase):
    def test_owned_live_skips_staleness(self):
        project, _ = _setup_project(status="active", extra_fm={"Owned-live": "Mattie"})
        self.assertTrue(assess._card_owned_live(project, "brief-x"))

    def test_plain_active_not_owned_live(self):
        project, _ = _setup_project(status="active")
        self.assertFalse(assess._card_owned_live(project, "brief-x"))


if __name__ == "__main__":
    unittest.main()
