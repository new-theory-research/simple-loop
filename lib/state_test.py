#!/usr/bin/env python3
"""Unit tests for lib/state.py — running.json card-derived projector (brief-108-d).

Covers every (card status × runtime-event) combination for the bucketing logic,
idempotency, and append-event semantics.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state import (
    project_running_json,
    append_event,
    write_running_json,
    _parse_card_frontmatter,
)


# ── Fixtures ──────────────────────────────────────────────────────────

def _make_card(cards_dir, brief_id, status, **extra):
    """Write a YAML-frontmatter card for a brief."""
    card_dir = os.path.join(cards_dir, brief_id)
    os.makedirs(card_dir, exist_ok=True)
    lines = ["---", f"ID: {brief_id}", f"Branch: {brief_id}", f"Status: {status}"]
    for k, v in extra.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {brief_id}")
    with open(os.path.join(card_dir, "index.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _make_project(tmp, cards=None, events=None):
    cards_dir = os.path.join(tmp, "wiki", "briefs", "cards")
    os.makedirs(cards_dir, exist_ok=True)
    for c in cards or []:
        if isinstance(c, tuple):
            brief_id, status = c
            _make_card(cards_dir, brief_id, status)
        else:
            _make_card(cards_dir, **c)

    state_dir = os.path.join(tmp, ".loop", "state")
    os.makedirs(state_dir, exist_ok=True)
    if events is not None:
        events_path = os.path.join(state_dir, "runtime-events.jsonl")
        with open(events_path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    return tmp


# ── Frontmatter parser tests ──────────────────────────────────────────

class TestParseCardFrontmatter(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_yaml_scalar_fields(self):
        path = os.path.join(self.tmp, "card.md")
        with open(path, "w") as f:
            f.write("---\nID: brief-001\nStatus: queued\nAuto-merge: false\n---\n# x\n")
        fm = _parse_card_frontmatter(path)
        self.assertEqual(fm["id"], "brief-001")
        self.assertEqual(fm["status"], "queued")
        self.assertEqual(fm["auto-merge"], "false")

    def test_yaml_list_field(self):
        path = os.path.join(self.tmp, "card.md")
        with open(path, "w") as f:
            f.write(
                "---\nID: brief-001\nStatus: active\nEdit-surface:\n"
                "  - lib/foo.py\n  - lib/bar.py\n---\n# x\n"
            )
        fm = _parse_card_frontmatter(path)
        self.assertEqual(fm["edit-surface"], ["lib/foo.py", "lib/bar.py"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(_parse_card_frontmatter("/nonexistent"), {})


# ── Bucketing tests ───────────────────────────────────────────────────

class TestProjectRunningJson(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Empty state

    def test_empty_state(self):
        _make_project(self.tmp)
        result = project_running_json(self.tmp)
        self.assertEqual(result, {
            "active": [],
            "completed_pending_eval": [],
            "pending_merges": [],
            "awaiting_review": [],
            "history": [],
        })

    def test_no_events_file(self):
        _make_project(self.tmp, cards=[("brief-001", "active")])
        result = project_running_json(self.tmp)
        self.assertEqual(len(result["active"]), 1)
        self.assertEqual(result["active"][0]["brief"], "brief-001")

    # Status: queued — no bucket

    def test_queued_card_in_no_bucket(self):
        _make_project(self.tmp, cards=[("brief-001", "queued")])
        result = project_running_json(self.tmp)
        self.assertEqual(result["active"], [])
        self.assertEqual(result["history"], [])

    # Status: active variants

    def test_active_no_completed(self):
        _make_project(
            self.tmp,
            cards=[("brief-002", "active")],
            events=[
                {"ts": "2026-05-04T10:00:00Z", "event": "dispatched",
                 "brief": "brief-002", "branch": "brief-002",
                 "brief_file": "wiki/briefs/cards/brief-002/index.md",
                 "worker_slot": 0, "throttle": 1, "parallel_safe": False},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(len(result["active"]), 1)
        self.assertEqual(result["awaiting_review"], [])
        self.assertEqual(result["pending_merges"], [])
        e = result["active"][0]
        self.assertEqual(e["brief"], "brief-002")
        self.assertEqual(e["dispatched_at"], "2026-05-04T10:00:00Z")
        self.assertEqual(e["worker_slot"], 0)

    def test_active_completed_no_approval_lands_in_awaiting_review(self):
        _make_project(
            self.tmp,
            cards=[("brief-003", "active")],
            events=[
                {"ts": "2026-05-04T10:00:00Z", "event": "dispatched",
                 "brief": "brief-003", "branch": "brief-003",
                 "brief_file": "wiki/briefs/cards/brief-003/index.md"},
                {"ts": "2026-05-04T11:00:00Z", "event": "completed",
                 "brief": "brief-003", "kind": "complete", "auto_merge": False},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(result["active"], [])
        self.assertEqual(len(result["awaiting_review"]), 1)
        self.assertEqual(result["pending_merges"], [])
        e = result["awaiting_review"][0]
        self.assertEqual(e["completed_at"], "2026-05-04T11:00:00Z")
        self.assertEqual(e["kind"], "complete")
        self.assertFalse(e["auto_merge"])

    def test_active_completed_approved_lands_in_pending_merges(self):
        _make_project(
            self.tmp,
            cards=[("brief-004", "active")],
            events=[
                {"ts": "2026-05-04T10:00:00Z", "event": "dispatched",
                 "brief": "brief-004", "branch": "brief-004"},
                {"ts": "2026-05-04T11:00:00Z", "event": "completed",
                 "brief": "brief-004", "kind": "complete", "auto_merge": False},
                {"ts": "2026-05-04T11:30:00Z", "event": "approved",
                 "brief": "brief-004"},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(result["active"], [])
        self.assertEqual(result["awaiting_review"], [])
        self.assertEqual(len(result["pending_merges"]), 1)
        e = result["pending_merges"][0]
        self.assertTrue(e["auto_merge"])
        self.assertEqual(e["approved_at"], "2026-05-04T11:30:00Z")

    def test_active_with_watchdog_kind(self):
        _make_project(
            self.tmp,
            cards=[("brief-005", "active")],
            events=[
                {"ts": "T1", "event": "dispatched", "brief": "brief-005"},
                {"ts": "T2", "event": "completed", "brief": "brief-005",
                 "kind": "watchdog-timed-out",
                 "reason": "cycle wall-time exceeded — human investigation required"},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(len(result["awaiting_review"]), 1)
        e = result["awaiting_review"][0]
        self.assertEqual(e["kind"], "watchdog-timed-out")
        self.assertIn("cycle wall-time", e["reason"])
        self.assertEqual(e["conflict_note"], e["reason"])

    # Status: merged → history

    def test_merged_with_dispatched_event_uses_full_lifecycle_shape(self):
        _make_project(
            self.tmp,
            cards=[("brief-006", "merged")],
            events=[
                {"ts": "T1", "event": "dispatched", "brief": "brief-006",
                 "branch": "brief-006", "worker_slot": 0,
                 "brief_file": "wiki/briefs/cards/brief-006/index.md"},
                {"ts": "T2", "event": "completed", "brief": "brief-006",
                 "kind": "complete", "auto_merge": False},
                {"ts": "T3", "event": "merged", "brief": "brief-006",
                 "merge_sha": "abc1234", "merged_at": "2026-05-04T12:00:00Z"},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(len(result["history"]), 1)
        h = result["history"][0]
        self.assertEqual(h["brief"], "brief-006")
        self.assertEqual(h["merge_sha"], "abc1234")
        self.assertEqual(h["status"], "merged")
        self.assertEqual(h["kind"], "complete")

    def test_merged_without_dispatched_event_uses_backfilled_shape(self):
        # Pre-projection history entries — only merge_sha + merged_at, nothing else.
        _make_project(
            self.tmp,
            cards=[("brief-007", "merged")],
            events=[
                {"ts": "T3", "event": "merged", "brief": "brief-007",
                 "merge_sha": "def5678", "merged_at": "2026-05-04T13:00:00Z",
                 "reason": "backfilled_from_git"},
            ],
        )
        result = project_running_json(self.tmp)
        h = result["history"][0]
        self.assertEqual(h["brief"], "brief-007")
        self.assertEqual(h["merge_sha"], "def5678")
        self.assertEqual(h["reason"], "backfilled_from_git")
        # backfilled shape doesn't include status/kind/auto_merge
        self.assertNotIn("status", h)

    def test_history_order_follows_merge_order(self):
        _make_project(
            self.tmp,
            cards=[("brief-100", "merged"), ("brief-101", "merged"), ("brief-102", "merged")],
            events=[
                {"ts": "T1", "event": "merged", "brief": "brief-101", "merge_sha": "a"},
                {"ts": "T2", "event": "merged", "brief": "brief-100", "merge_sha": "b"},
                {"ts": "T3", "event": "merged", "brief": "brief-102", "merge_sha": "c"},
            ],
        )
        result = project_running_json(self.tmp)
        order = [h["brief"] for h in result["history"]]
        self.assertEqual(order, ["brief-101", "brief-100", "brief-102"])

    # Edit-surface and parallel-safe propagation

    def test_edit_surface_and_parallel_safe_from_card(self):
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(cards_dir, exist_ok=True)
        _make_card(
            cards_dir, "brief-008", "active",
            **{"Parallel-safe": "true", "Edit-surface": ["lib/x.py", "crates/foo/"]},
        )
        os.makedirs(os.path.join(self.tmp, ".loop", "state"))
        result = project_running_json(self.tmp)
        e = result["active"][0]
        self.assertTrue(e["parallel_safe"])
        self.assertEqual(e["edit_surface"], ["lib/x.py", "crates/foo/"])

    # Idempotency

    def test_idempotency(self):
        _make_project(
            self.tmp,
            cards=[("brief-009", "active"), ("brief-010", "merged")],
            events=[
                {"ts": "T1", "event": "dispatched", "brief": "brief-009"},
                {"ts": "T2", "event": "merged", "brief": "brief-010",
                 "merge_sha": "xyz"},
            ],
        )
        a = project_running_json(self.tmp)
        b = project_running_json(self.tmp)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    # Excluded statuses

    def test_rejected_card_in_no_bucket(self):
        _make_project(self.tmp, cards=[("brief-011", "rejected")])
        result = project_running_json(self.tmp)
        for k in ("active", "awaiting_review", "pending_merges", "history"):
            self.assertEqual(result[k], [])

    def test_draft_card_in_no_bucket(self):
        _make_project(self.tmp, cards=[("brief-012", "draft")])
        result = project_running_json(self.tmp)
        for k in ("active", "awaiting_review", "pending_merges", "history"):
            self.assertEqual(result[k], [])


# ── Append-event tests ────────────────────────────────────────────────

class TestAppendEvent(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_creates_file_and_writes_jsonl(self):
        path = os.path.join(self.tmp, ".loop", "state", "runtime-events.jsonl")
        self.assertFalse(os.path.exists(path))
        e = append_event(self.tmp, "dispatched", "brief-001",
                         branch="brief-001", worker_slot=0)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(e["event"], "dispatched")
        self.assertEqual(e["brief"], "brief-001")
        with open(path) as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["branch"], "brief-001")

    def test_append_preserves_prior_lines(self):
        append_event(self.tmp, "dispatched", "brief-001")
        append_event(self.tmp, "completed", "brief-001", kind="complete")
        path = os.path.join(self.tmp, ".loop", "state", "runtime-events.jsonl")
        with open(path) as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["event"], "dispatched")
        self.assertEqual(lines[1]["event"], "completed")

    def test_cli_keeps_all_digit_sha_as_string(self):
        # Regression: short-SHAs that happen to be all digits (e.g. 92329478)
        # were being coerced to int by the CLI arg parser, which broke
        # downstream typed parsers (notably hive's RunningJson).
        import subprocess
        state_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.py")
        subprocess.run(
            [sys.executable, state_py, "append-event", self.tmp, "merged", "brief-145",
             "merge_sha=92329478", "merged_at=2026-05-06T01:34:36Z", "worker_slot=0"],
            check=True,
        )
        path = os.path.join(self.tmp, ".loop", "state", "runtime-events.jsonl")
        with open(path) as f:
            entry = json.loads(f.read().strip().splitlines()[-1])
        self.assertEqual(entry["merge_sha"], "92329478")
        self.assertIsInstance(entry["merge_sha"], str)
        # Allowlisted int fields still coerce.
        self.assertEqual(entry["worker_slot"], 0)
        self.assertIsInstance(entry["worker_slot"], int)


# ── End-to-end: append + project ──────────────────────────────────────

class TestRoundTrip(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dispatch_then_complete_then_merge(self):
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(cards_dir)
        _make_card(cards_dir, "brief-100", "active")

        # Dispatch event → bucket: active
        append_event(self.tmp, "dispatched", "brief-100",
                     branch="brief-100", worker_slot=0,
                     brief_file="wiki/briefs/cards/brief-100/index.md")
        result = project_running_json(self.tmp)
        self.assertEqual([e["brief"] for e in result["active"]], ["brief-100"])

        # Complete event → bucket: awaiting_review
        append_event(self.tmp, "completed", "brief-100",
                     kind="complete", auto_merge=False)
        result = project_running_json(self.tmp)
        self.assertEqual(result["active"], [])
        self.assertEqual([e["brief"] for e in result["awaiting_review"]], ["brief-100"])

        # Approval + merge: flip card status to merged + add merged event
        _make_card(cards_dir, "brief-100", "merged")  # rewrites the card
        append_event(self.tmp, "approved", "brief-100")
        append_event(self.tmp, "merged", "brief-100",
                     merge_sha="abc1234", merged_at="2026-05-04T12:00:00Z")

        result = project_running_json(self.tmp)
        self.assertEqual(result["active"], [])
        self.assertEqual(result["awaiting_review"], [])
        self.assertEqual(result["pending_merges"], [])
        self.assertEqual(len(result["history"]), 1)
        h = result["history"][0]
        self.assertEqual(h["merge_sha"], "abc1234")
        self.assertEqual(h["status"], "merged")


# ── Write helper ──────────────────────────────────────────────────────

class TestWriteRunningJson(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_file_with_projection(self):
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(cards_dir)
        _make_card(cards_dir, "brief-001", "active")
        os.makedirs(os.path.join(self.tmp, ".loop", "state"))

        write_running_json(self.tmp)
        path = os.path.join(self.tmp, ".loop", "state", "running.json")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(len(data["active"]), 1)


if __name__ == "__main__":
    unittest.main()
