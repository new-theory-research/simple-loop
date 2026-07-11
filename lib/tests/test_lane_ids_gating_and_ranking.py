"""Issue #50: lane-prefixed card ids (ft-*, capture-*, rq-*, …) must flow through
both dependency-gating (actions.check_depends_on) and goals.md priority ranking
(queue._goals_order / enumerate_dispatchable), not just `brief-NNN` ids.

Before the fix both mechanisms hardcoded a literal `brief-` prefix, so a
lane-prefixed Depends-on evaluated to `depends_on=[]` (dispatch allowed with a
real unmet dependency) and a lane-prefixed goals.md entry never ranked (cards
fell back to alphabetical order).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import actions  # noqa: E402
import queue as loop_queue  # noqa: E402


def _write_card(cards_dir, card_id, status="queued", depends_on=None):
    card_dir = os.path.join(cards_dir, card_id)
    os.makedirs(card_dir, exist_ok=True)
    body = ["---", f"ID: {card_id}", f"Status: {status}"]
    if depends_on is not None:
        body.append(f"Depends-on: {depends_on}")
    body += ["---", "", f"# {card_id}", ""]
    with open(os.path.join(card_dir, "index.md"), "w") as f:
        f.write("\n".join(body))


def _run_check_depends_on(project_dir, brief_id, brief_file):
    """Call actions.check_depends_on and return its first stdout line (verdict)."""
    state_dir = os.path.join(project_dir, ".loop", "state")
    os.makedirs(state_dir, exist_ok=True)
    pd_path = os.path.join(state_dir, "pending-dispatch.json")
    with open(pd_path, "w") as f:
        json.dump({"brief": brief_id, "brief_file": brief_file}, f)
    paths = {"project_dir": project_dir, "pending_dispatch": pd_path}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        actions.check_depends_on(paths)
    return buf.getvalue().splitlines()[0]


class LanePrefixedGating(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cards = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(self.cards)

    def _dependent_file(self, card_id):
        return f"wiki/briefs/cards/{card_id}/index.md"

    def test_lane_dep_unmet_blocks(self):
        # capture-004 is a real card that is NOT merged → dispatch must block.
        _write_card(self.cards, "capture-004-per-key-identity", status="active")
        dep_card = "capture-005-cont-rerun-handoff"
        _write_card(self.cards, dep_card, status="queued",
                    depends_on="capture-004-per-key-identity")
        verdict = _run_check_depends_on(
            self.tmp, dep_card, self._dependent_file(dep_card))
        self.assertEqual(verdict, "blocked:capture-004-per-key-identity")

    def test_lane_dep_merged_allows(self):
        _write_card(self.cards, "capture-004-per-key-identity", status="merged")
        dep_card = "capture-005-cont-rerun-handoff"
        _write_card(self.cards, dep_card, status="queued",
                    depends_on="capture-004-per-key-identity")
        verdict = _run_check_depends_on(
            self.tmp, dep_card, self._dependent_file(dep_card))
        self.assertEqual(verdict, "allowed")

    def test_mixed_chain_first_unmet_reported(self):
        # capture-002 merged, capture-004 active → blocked on the unmet one.
        _write_card(self.cards, "capture-002-nt-cloud-sink", status="merged")
        _write_card(self.cards, "capture-004-per-key-identity", status="active")
        dep_card = "ft-006-newt-finetune-verb"
        _write_card(
            self.cards, dep_card, status="queued",
            depends_on="capture-002-nt-cloud-sink (merged), capture-004-per-key-identity")
        verdict = _run_check_depends_on(
            self.tmp, dep_card, self._dependent_file(dep_card))
        self.assertEqual(verdict, "blocked:capture-004-per-key-identity")


class LanePrefixedGoalsRanking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cards = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(self.cards)
        self.state = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(self.state)

    def _write_goals(self, text):
        with open(os.path.join(self.state, "goals.md"), "w") as f:
            f.write(text)

    def test_goals_order_extracts_lane_ids(self):
        self._write_goals(
            "# Goals\n\n"
            "1. **ft-011-fp3t5-base-serve** — serve the base checkpoint\n"
            "2. capture-004-per-key-identity blah\n"
            "3. rq-001-first-remote-run\n")
        got = loop_queue._goals_order(os.path.join(self.state, "goals.md"))
        self.assertEqual(
            got,
            ["ft-011-fp3t5-base-serve", "capture-004-per-key-identity",
             "rq-001-first-remote-run"],
        )

    def test_ranking_orders_lane_cards_by_goals(self):
        # Cards on disk sort alphabetically by default (capture < ft < rq);
        # goals.md must override that to ft, capture, rq.
        for cid in ("capture-004-per-key-identity", "ft-011-fp3t5-base-serve",
                    "rq-001-first-remote-run"):
            _write_card(self.cards, cid, status="queued")
        self._write_goals(
            "1. ft-011-fp3t5-base-serve\n"
            "2. capture-004-per-key-identity\n"
            "3. rq-001-first-remote-run\n")
        cands = loop_queue.enumerate_dispatchable(self.tmp, running={})
        self.assertEqual(
            [c["brief"] for c in cands],
            ["ft-011-fp3t5-base-serve", "capture-004-per-key-identity",
             "rq-001-first-remote-run"],
        )

    def test_goals_order_ignores_dates_and_prose(self):
        # A date (`2026-07-11`) and a `word 3` (space, not hyphen) must NOT be
        # mistaken for card ids; only the real lane id is extracted.
        self._write_goals(
            "## Active program (2026-07-11)\n\n"
            "Finish brick 0 then land ft-011-fp3t5-base-serve.\n")
        self.assertEqual(
            loop_queue._goals_order(os.path.join(self.state, "goals.md")),
            ["ft-011-fp3t5-base-serve"],
        )

    def test_brief_ranking_golden_unchanged(self):
        for cid in ("brief-010-alpha", "brief-011-beta", "brief-012-gamma"):
            _write_card(self.cards, cid, status="queued")
        self._write_goals(
            "1. brief-012-gamma\n2. brief-010-alpha\n3. brief-011-beta\n")
        cands = loop_queue.enumerate_dispatchable(self.tmp, running={})
        self.assertEqual(
            [c["brief"] for c in cands],
            ["brief-012-gamma", "brief-010-alpha", "brief-011-beta"],
        )


if __name__ == "__main__":
    unittest.main()
