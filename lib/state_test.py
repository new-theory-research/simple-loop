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


# ── Re-queue generation scoping (brief-249 re-queue bounce) ──────────
#
# Completed/approved events from BEFORE the latest dispatched event are a
# previous generation. A re-dispatched brief must project as active — not get
# re-bucketed into awaiting_review by its own stale completion, which leaves
# the daemon seeing no active brief and never spawning a worker.

class TestRequeueGenerationScoping(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_redispatch_after_completion_projects_active_not_awaiting_review(self):
        # brief-249 shape: completed blocked-on-human, human cleared it,
        # card re-queued, daemon re-dispatched. Stale completed event must
        # not pull the fresh dispatch back into awaiting_review.
        _make_project(
            self.tmp,
            cards=[("brief-249", "active")],
            events=[
                {"ts": "T1", "event": "dispatched", "brief": "brief-249",
                 "branch": "brief-249", "worker_slot": 0},
                {"ts": "T2", "event": "completed", "brief": "brief-249",
                 "kind": "blocked-on-human",
                 "reason": "needs a human to provision the API key"},
                {"ts": "T3", "event": "dispatched", "brief": "brief-249",
                 "branch": "brief-249", "worker_slot": 1},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(result["awaiting_review"], [])
        self.assertEqual(result["pending_merges"], [])
        self.assertEqual(len(result["active"]), 1)
        e = result["active"][0]
        self.assertEqual(e["brief"], "brief-249")
        # Active entry reflects the CURRENT generation's dispatch.
        self.assertEqual(e["dispatched_at"], "T3")
        self.assertEqual(e["worker_slot"], 1)

    def test_second_generation_completion_carries_second_reason(self):
        _make_project(
            self.tmp,
            cards=[("brief-249", "active")],
            events=[
                {"ts": "T1", "event": "dispatched", "brief": "brief-249"},
                {"ts": "T2", "event": "completed", "brief": "brief-249",
                 "kind": "blocked-on-human", "reason": "first-generation reason"},
                {"ts": "T3", "event": "dispatched", "brief": "brief-249"},
                {"ts": "T4", "event": "completed", "brief": "brief-249",
                 "kind": "complete", "reason": "second-generation reason"},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(result["active"], [])
        self.assertEqual(len(result["awaiting_review"]), 1)
        e = result["awaiting_review"][0]
        self.assertEqual(e["completed_at"], "T4")
        self.assertEqual(e["kind"], "complete")
        self.assertEqual(e["reason"], "second-generation reason")

    def test_approval_within_same_generation_still_pending_merges(self):
        _make_project(
            self.tmp,
            cards=[("brief-013", "active")],
            events=[
                {"ts": "T1", "event": "dispatched", "brief": "brief-013"},
                {"ts": "T2", "event": "completed", "brief": "brief-013",
                 "kind": "complete", "auto_merge": False},
                {"ts": "T3", "event": "approved", "brief": "brief-013"},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(result["active"], [])
        self.assertEqual(result["awaiting_review"], [])
        self.assertEqual(len(result["pending_merges"]), 1)
        self.assertEqual(result["pending_merges"][0]["approved_at"], "T3")

    def test_stale_approval_from_previous_generation_ignored(self):
        # Approval before the re-dispatch belongs to the old generation —
        # the new run's completion must land in awaiting_review, not get
        # auto-promoted to pending_merges by the stale approval.
        _make_project(
            self.tmp,
            cards=[("brief-014", "active")],
            events=[
                {"ts": "T1", "event": "dispatched", "brief": "brief-014"},
                {"ts": "T2", "event": "completed", "brief": "brief-014",
                 "kind": "complete"},
                {"ts": "T3", "event": "approved", "brief": "brief-014"},
                {"ts": "T4", "event": "dispatched", "brief": "brief-014"},
                {"ts": "T5", "event": "completed", "brief": "brief-014",
                 "kind": "complete"},
            ],
        )
        result = project_running_json(self.tmp)
        self.assertEqual(result["pending_merges"], [])
        self.assertEqual(len(result["awaiting_review"]), 1)
        self.assertEqual(result["awaiting_review"][0]["completed_at"], "T5")


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


# ── Lane isolation tests (harness-001/003, issue #54) ─────────────────
#
# WHY these tests exist (engineering rule 7):
#   INCIDENT (2026-06-28): a daemon started --lane remote-queens pulled main,
#   which carried a committed running.json with fleets' fleet-005a as active.
#   The lane-scoped daemon inherited that entry, "managed" it as its own, and
#   stalled permanently — never dispatching its own lane's queued brief.
#
# ROOT: project_running_json (and write_running_json) had no lane parameter,
# so they projected ALL cards regardless of Program:. A lane B daemon's
# running.json included lane A's active briefs, giving it false active state.
#
# FIX: project_running_json(lane=X) filters active/awaiting_review/
# pending_merges to only include briefs whose Program: == X (fail-closed:
# a brief with no Program: is excluded from a lane-scoped projection).
#
# These tests FAIL on the unfixed code (no lane param) and PASS after.

class TestLaneIsolation(unittest.TestCase):
    """Core fix tests — reproduce the inherit-and-stall incident."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_card_with_program(self, brief_id, status, program=None):
        """Write a card with an optional Program: field."""
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(cards_dir, exist_ok=True)
        card_dir = os.path.join(cards_dir, brief_id)
        os.makedirs(card_dir, exist_ok=True)
        lines = ["---", f"ID: {brief_id}", f"Branch: {brief_id}", f"Status: {status}"]
        if program is not None:
            lines.append(f"Program: {program}")
        lines += ["---", "", f"# {brief_id}", ""]
        with open(os.path.join(card_dir, "index.md"), "w") as f:
            f.write("\n".join(lines) + "\n")

    # The incident fixture: fleets has an active brief, remote-queens has a
    # queued brief. A daemon scoped --lane remote-queens MUST NOT see fleets'
    # active brief in its running.json.

    def test_lane_scoped_projection_excludes_other_lanes_active(self):
        """Reproduce the 2026-06-28 inherit-and-stall: a daemon started
        --lane remote-queens must never see fleets' active brief in its
        running.json active[]. Without the fix this returns len(active)==1
        (fleet-005a inherited) and the daemon stalls managing a brief it
        never dispatched."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("fleet-005a", "active", "fleets")
        self._make_card_with_program("remote-queen-001", "queued", "remote-queens")

        # Dispatch event for the fleets brief (it's active on main).
        events = [
            {"ts": "T1", "event": "dispatched", "brief": "fleet-005a",
             "branch": "fleet-005a",
             "brief_file": "wiki/briefs/cards/fleet-005a/index.md",
             "worker_slot": 0},
        ]

        result = project_running_json(self.tmp, events=events, lane="remote-queens")

        # The fleets brief must NOT appear — the remote-queens daemon must not
        # inherit it, defer on it, or stall waiting for it to clear.
        active_ids = [e["brief"] for e in result["active"]]
        self.assertNotIn(
            "fleet-005a", active_ids,
            "lane=remote-queens must never inherit fleets' active brief (the stall bug)"
        )
        self.assertEqual(result["active"], [],
                         "remote-queens has no active brief of its own — active must be empty")

    def test_lane_scoped_projection_includes_own_lane_active(self):
        """A brief in the correct lane DOES appear in the scoped projection."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("fleet-005a", "active", "fleets")
        self._make_card_with_program("remote-queen-001", "active", "remote-queens")

        events = [
            {"ts": "T1", "event": "dispatched", "brief": "fleet-005a",
             "branch": "fleet-005a",
             "brief_file": "wiki/briefs/cards/fleet-005a/index.md",
             "worker_slot": 0},
            {"ts": "T2", "event": "dispatched", "brief": "remote-queen-001",
             "branch": "remote-queen-001",
             "brief_file": "wiki/briefs/cards/remote-queen-001/index.md",
             "worker_slot": 0},
        ]

        result = project_running_json(self.tmp, events=events, lane="remote-queens")
        active_ids = [e["brief"] for e in result["active"]]
        self.assertIn("remote-queen-001", active_ids)
        self.assertNotIn("fleet-005a", active_ids)

    def test_lane_scoped_awaiting_review_excludes_other_lanes(self):
        """Lane filter also applies to awaiting_review — the stall logic reads
        that bucket too."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("fleet-005a", "active", "fleets")
        self._make_card_with_program("remote-queen-001", "active", "remote-queens")

        events = [
            {"ts": "T1", "event": "dispatched", "brief": "fleet-005a",
             "branch": "fleet-005a",
             "brief_file": "wiki/briefs/cards/fleet-005a/index.md"},
            {"ts": "T2", "event": "completed", "brief": "fleet-005a",
             "kind": "complete", "auto_merge": False},
            {"ts": "T3", "event": "dispatched", "brief": "remote-queen-001",
             "branch": "remote-queen-001",
             "brief_file": "wiki/briefs/cards/remote-queen-001/index.md"},
        ]

        result = project_running_json(self.tmp, events=events, lane="remote-queens")
        awaiting_ids = [e["brief"] for e in result["awaiting_review"]]
        self.assertNotIn("fleet-005a", awaiting_ids,
                         "fleets brief in awaiting_review must not appear in remote-queens view")
        active_ids = [e["brief"] for e in result["active"]]
        self.assertIn("remote-queen-001", active_ids)

    def test_lane_scoped_pending_merges_excludes_other_lanes(self):
        """Lane filter applies to pending_merges bucket."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("fleet-005a", "active", "fleets")
        self._make_card_with_program("remote-queen-001", "active", "remote-queens")

        events = [
            {"ts": "T1", "event": "dispatched", "brief": "fleet-005a",
             "branch": "fleet-005a",
             "brief_file": "wiki/briefs/cards/fleet-005a/index.md"},
            {"ts": "T2", "event": "completed", "brief": "fleet-005a",
             "kind": "complete", "auto_merge": True},
            {"ts": "T3", "event": "approved", "brief": "fleet-005a"},
            {"ts": "T4", "event": "dispatched", "brief": "remote-queen-001",
             "branch": "remote-queen-001",
             "brief_file": "wiki/briefs/cards/remote-queen-001/index.md"},
        ]

        result = project_running_json(self.tmp, events=events, lane="remote-queens")
        pending_ids = [e["brief"] for e in result["pending_merges"]]
        self.assertNotIn("fleet-005a", pending_ids,
                         "fleets brief in pending_merges must not appear in remote-queens view")

    def test_brief_without_program_excluded_from_lane_projection(self):
        """Fail-closed: a brief with no Program: field is EXCLUDED from a
        lane-scoped projection so it's never accidentally managed by the
        wrong daemon."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        # Brief with no Program: field.
        self._make_card_with_program("unlabeled-001", "active", program=None)
        self._make_card_with_program("remote-queen-001", "active", "remote-queens")

        events = [
            {"ts": "T1", "event": "dispatched", "brief": "unlabeled-001",
             "branch": "unlabeled-001",
             "brief_file": "wiki/briefs/cards/unlabeled-001/index.md"},
            {"ts": "T2", "event": "dispatched", "brief": "remote-queen-001",
             "branch": "remote-queen-001",
             "brief_file": "wiki/briefs/cards/remote-queen-001/index.md"},
        ]

        result = project_running_json(self.tmp, events=events, lane="remote-queens")
        active_ids = [e["brief"] for e in result["active"]]
        self.assertNotIn("unlabeled-001", active_ids, "unlabeled brief must be excluded (fail-closed)")
        self.assertIn("remote-queen-001", active_ids)

    def test_history_is_global_across_lanes(self):
        """history[] is NOT filtered by lane — it's read-only/cosmetic and the
        merged briefs from all lanes are useful for depends-on resolution."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("fleet-005a", "merged", "fleets")
        self._make_card_with_program("remote-queen-001", "merged", "remote-queens")

        events = [
            {"ts": "T1", "event": "merged", "brief": "fleet-005a",
             "merge_sha": "abc", "merged_at": "T1"},
            {"ts": "T2", "event": "merged", "brief": "remote-queen-001",
             "merge_sha": "def", "merged_at": "T2"},
        ]

        result = project_running_json(self.tmp, events=events, lane="remote-queens")
        history_ids = [e["brief"] for e in result["history"]]
        self.assertIn("fleet-005a", history_ids,
                      "history is global — merged briefs from all lanes must appear")
        self.assertIn("remote-queen-001", history_ids)

    def test_no_lane_global_projection_unchanged(self):
        """Single-daemon path (no lane): ALL active briefs from ALL lanes appear.
        Byte-for-byte unchanged from pre-fix behavior."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("fleet-005a", "active", "fleets")
        self._make_card_with_program("remote-queen-001", "active", "remote-queens")

        events = [
            {"ts": "T1", "event": "dispatched", "brief": "fleet-005a",
             "branch": "fleet-005a",
             "brief_file": "wiki/briefs/cards/fleet-005a/index.md",
             "worker_slot": 0},
            {"ts": "T2", "event": "dispatched", "brief": "remote-queen-001",
             "branch": "remote-queen-001",
             "brief_file": "wiki/briefs/cards/remote-queen-001/index.md",
             "worker_slot": 1},
        ]

        result = project_running_json(self.tmp, events=events)  # no lane
        active_ids = [e["brief"] for e in result["active"]]
        self.assertIn("fleet-005a", active_ids, "no-lane must see all briefs")
        self.assertIn("remote-queen-001", active_ids, "no-lane must see all briefs")

    def test_empty_lane_equals_no_lane(self):
        """Empty/whitespace lane → no filter (same as queue.py / brief-152)."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("fleet-005a", "active", "fleets")
        self._make_card_with_program("remote-queen-001", "active", "remote-queens")

        events = [
            {"ts": "T1", "event": "dispatched", "brief": "fleet-005a",
             "branch": "fleet-005a",
             "brief_file": "wiki/briefs/cards/fleet-005a/index.md"},
            {"ts": "T2", "event": "dispatched", "brief": "remote-queen-001",
             "branch": "remote-queen-001",
             "brief_file": "wiki/briefs/cards/remote-queen-001/index.md"},
        ]

        result_none = project_running_json(self.tmp, events=events, lane=None)
        result_empty = project_running_json(self.tmp, events=events, lane="")
        result_ws = project_running_json(self.tmp, events=events, lane="  ")

        for result in (result_empty, result_ws):
            self.assertEqual(
                sorted(e["brief"] for e in result["active"]),
                sorted(e["brief"] for e in result_none["active"]),
                "empty/whitespace lane must equal no-lane"
            )

    def test_write_running_json_lane_param_passed_through(self):
        """write_running_json(lane=X) projects the lane-scoped view and writes it."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("fleet-005a", "active", "fleets")
        self._make_card_with_program("remote-queen-001", "active", "remote-queens")

        events = [
            {"ts": "T1", "event": "dispatched", "brief": "fleet-005a",
             "branch": "fleet-005a",
             "brief_file": "wiki/briefs/cards/fleet-005a/index.md"},
            {"ts": "T2", "event": "dispatched", "brief": "remote-queen-001",
             "branch": "remote-queen-001",
             "brief_file": "wiki/briefs/cards/remote-queen-001/index.md"},
        ]
        events_path = os.path.join(state_dir, "runtime-events.jsonl")
        with open(events_path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        result = write_running_json(self.tmp, lane="remote-queens")
        running_path = os.path.join(state_dir, "running.json")
        self.assertTrue(os.path.exists(running_path))
        with open(running_path) as f:
            on_disk = json.load(f)

        active_ids = [e["brief"] for e in on_disk["active"]]
        self.assertNotIn("fleet-005a", active_ids,
                         "on-disk running.json must be lane-scoped")
        self.assertIn("remote-queen-001", active_ids)

    def test_multi_lane_projection_spans_named_lanes(self):
        """multi-lane-daemon: a comma-separated lane list projects briefs from
        EVERY named lane and still excludes an out-of-set lane. Without the set
        change, `lane="finetune,capture"` exact-matched nothing and the whole
        projection fell dark (active[] empty)."""
        state_dir = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state_dir, exist_ok=True)
        self._make_card_with_program("ft-001", "active", "finetune")
        self._make_card_with_program("cap-001", "active", "capture")
        self._make_card_with_program("fleet-005a", "active", "fleets")

        events = [
            {"ts": "T1", "event": "dispatched", "brief": "ft-001",
             "branch": "ft-001",
             "brief_file": "wiki/briefs/cards/ft-001/index.md"},
            {"ts": "T2", "event": "dispatched", "brief": "cap-001",
             "branch": "cap-001",
             "brief_file": "wiki/briefs/cards/cap-001/index.md"},
            {"ts": "T3", "event": "dispatched", "brief": "fleet-005a",
             "branch": "fleet-005a",
             "brief_file": "wiki/briefs/cards/fleet-005a/index.md"},
        ]

        result = project_running_json(self.tmp, events=events,
                                      lane="finetune,capture")
        active_ids = sorted(e["brief"] for e in result["active"])
        self.assertEqual(active_ids, ["cap-001", "ft-001"],
                         "both named lanes present; fleets excluded")
        # Lane-list order is irrelevant.
        reordered = project_running_json(self.tmp, events=events,
                                         lane="capture,finetune")
        self.assertEqual(
            sorted(e["brief"] for e in reordered["active"]), active_ids)

    def test_gitignore_excludes_running_json(self):
        """Acceptance criterion 2: .gitignore must contain .loop/state/running.json
        so the file is never committed to main."""
        # Walk up to find the simple-loop repo root (this test file is in lib/).
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        gitignore_path = os.path.join(repo_root, ".gitignore")
        self.assertTrue(
            os.path.exists(gitignore_path),
            f".gitignore not found at {gitignore_path}"
        )
        with open(gitignore_path) as f:
            content = f.read()
        self.assertIn(
            ".loop/state/running.json", content,
            ".gitignore must include .loop/state/running.json (harness-001/003)"
        )


if __name__ == "__main__":
    unittest.main()
