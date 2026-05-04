#!/usr/bin/env python3
"""Unit tests for lib/queue.py — shared dispatch-queue enumerator (brief-108-cont-a).

Covers every card Status value observed in portal plus running.json exclusion
logic for every relevant queue (active, awaiting_review, pending_merges,
completed_pending_eval, history) and goals.md ordering.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue import enumerate_dispatchable


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_card(cards_dir, brief_id, status):
    card_dir = os.path.join(cards_dir, brief_id)
    os.makedirs(card_dir, exist_ok=True)
    with open(os.path.join(card_dir, "index.md"), "w") as f:
        f.write(f"---\nID: {brief_id}\nStatus: {status}\n---\n\n# {brief_id}\n")


def _make_project(tmp, cards=None, running=None, goals_order=None):
    """Build a minimal project fixture under tmp."""
    cards_dir = os.path.join(tmp, "wiki", "briefs", "cards")
    os.makedirs(cards_dir, exist_ok=True)
    for brief_id, status in (cards or []):
        _make_card(cards_dir, brief_id, status)

    state_dir = os.path.join(tmp, ".loop", "state")
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "running.json"), "w") as f:
        json.dump(running or {}, f)

    if goals_order:
        lines = ["# Goals\n\n## Queued next\n\n"]
        for i, bid in enumerate(goals_order, 1):
            lines.append(f"{i}. **{bid}** — description\n")
        with open(os.path.join(state_dir, "goals.md"), "w") as f:
            f.writelines(lines)

    return tmp


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestEnumerateDispatchable(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Status: queued — should include

    def test_queued_is_included(self):
        _make_project(self.tmp, cards=[("brief-001-foo", "queued")])
        result = enumerate_dispatchable(self.tmp)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["brief"], "brief-001-foo")

    # Every non-queued status — should exclude

    def test_active_is_excluded(self):
        _make_project(self.tmp, cards=[("brief-010-active", "active")])
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_merged_is_excluded(self):
        _make_project(self.tmp, cards=[("brief-011-merged", "merged")])
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_rejected_is_excluded(self):
        _make_project(self.tmp, cards=[("brief-012-rejected", "rejected")])
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_not_doing_is_excluded(self):
        _make_project(self.tmp, cards=[("brief-013-notdoing", "not-doing")])
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_draft_is_excluded(self):
        _make_project(self.tmp, cards=[("brief-014-draft", "draft")])
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_queued_placeholder_variant_is_excluded(self):
        # "queued (placeholder; do not dispatch yet)" is not exactly "queued"
        _make_project(self.tmp, cards=[
            ("brief-015-placeholder", "queued (placeholder; do not dispatch yet)")
        ])
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_not_doing_with_annotation_is_excluded(self):
        _make_project(self.tmp, cards=[
            ("brief-016-nd-ann", "not-doing — superseded by program-002")
        ])
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_draft_with_annotation_is_excluded(self):
        _make_project(self.tmp, cards=[
            ("brief-017-draft-ann", "draft — demoted 2026-04-29")
        ])
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    # running.json exclusion — every queue key

    def test_queued_in_active_is_excluded(self):
        running = {"active": [{"brief": "brief-020-x"}]}
        _make_project(self.tmp, cards=[("brief-020-x", "queued")], running=running)
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_queued_in_awaiting_review_is_excluded(self):
        running = {"awaiting_review": [{"brief": "brief-021-x"}]}
        _make_project(self.tmp, cards=[("brief-021-x", "queued")], running=running)
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_queued_in_pending_merges_is_excluded(self):
        running = {"pending_merges": [{"brief": "brief-022-x"}]}
        _make_project(self.tmp, cards=[("brief-022-x", "queued")], running=running)
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_queued_in_completed_pending_eval_is_excluded(self):
        running = {"completed_pending_eval": [{"brief": "brief-023-x"}]}
        _make_project(self.tmp, cards=[("brief-023-x", "queued")], running=running)
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_queued_in_history_is_excluded(self):
        running = {"history": [{"brief": "brief-024-x"}]}
        _make_project(self.tmp, cards=[("brief-024-x", "queued")], running=running)
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    # Output shape

    def test_brief_file_is_canonical_card_path(self):
        _make_project(self.tmp, cards=[("brief-030-shape", "queued")])
        result = enumerate_dispatchable(self.tmp)
        self.assertEqual(result[0]["brief_file"], "wiki/briefs/cards/brief-030-shape/index.md")

    def test_branch_equals_brief(self):
        _make_project(self.tmp, cards=[("brief-031-branch", "queued")])
        result = enumerate_dispatchable(self.tmp)
        self.assertEqual(result[0]["branch"], result[0]["brief"])

    # Goals.md ordering

    def test_goals_ordering(self):
        _make_project(
            self.tmp,
            cards=[
                ("brief-040-c", "queued"),
                ("brief-041-a", "queued"),
                ("brief-042-b", "queued"),
            ],
            goals_order=["brief-041-a", "brief-042-b", "brief-040-c"],
        )
        result = enumerate_dispatchable(self.tmp)
        self.assertEqual([r["brief"] for r in result],
                         ["brief-041-a", "brief-042-b", "brief-040-c"])

    def test_not_in_goals_sorts_last(self):
        _make_project(
            self.tmp,
            cards=[
                ("brief-050-orphan", "queued"),
                ("brief-051-known", "queued"),
            ],
            goals_order=["brief-051-known"],
        )
        result = enumerate_dispatchable(self.tmp)
        self.assertEqual(result[0]["brief"], "brief-051-known")
        self.assertEqual(result[1]["brief"], "brief-050-orphan")

    # Edge cases

    def test_empty_cards_dir(self):
        _make_project(self.tmp)
        self.assertEqual(enumerate_dispatchable(self.tmp), [])

    def test_running_json_missing_is_tolerated(self):
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        _make_card(cards_dir, "brief-060-x", "queued")
        result = enumerate_dispatchable(self.tmp)
        self.assertEqual(len(result), 1)

    def test_running_kwarg_overrides_disk(self):
        cards_dir = os.path.join(self.tmp, "wiki", "briefs", "cards")
        _make_card(cards_dir, "brief-070-a", "queued")
        _make_card(cards_dir, "brief-071-b", "queued")
        running = {"active": [{"brief": "brief-070-a"}]}
        result = enumerate_dispatchable(self.tmp, running=running)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["brief"], "brief-071-b")

    def test_all_statuses_mixed(self):
        """Comprehensive: only the queued one comes through."""
        _make_project(
            self.tmp,
            cards=[
                ("brief-080-queued", "queued"),
                ("brief-081-active", "active"),
                ("brief-082-merged", "merged"),
                ("brief-083-rejected", "rejected"),
                ("brief-084-not-doing", "not-doing"),
                ("brief-085-draft", "draft"),
            ],
        )
        result = enumerate_dispatchable(self.tmp)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["brief"], "brief-080-queued")


if __name__ == "__main__":
    unittest.main()
