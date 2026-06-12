"""SOLO_DRAIN_AFTER_SECS drain-for-solo decision (queue.py).

Closes brief-253a starvation: a parallel-safe:false brief at queue head sat at
position 1 for hours while parallel briefs dispatched past it. The drain gate
holds OTHER dispatches once a solo head has waited past the threshold, so the
board empties and the solo runs next.

Covers:
  - _card_is_solo: YAML/prose Parallel-safe parsing + default-solo fallback.
  - head_solo_drain: off when threshold 0; off when head is parallel-safe;
    on only once a SOLO head has waited past the threshold (git commit time
    as the queued clock, `now` injected); reports the head id + waited secs.

The WHY each test guards:
  - threshold-0-off: feature must be inert by default (byte-preserving rollout).
  - parallel-head-no-drain: we only drain for a SOLO head — never stall the
    board for a brief that could run alongside others.
  - waited-boundary: drain fires strictly AFTER the threshold, not at/under it,
    so a freshly-queued solo doesn't instantly freeze dispatch.
"""

import os
import subprocess
import sys
import tempfile
import unittest

_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import queue as loop_queue  # noqa: E402


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True,
                   capture_output=True, text=True)


def _write_card(cards_dir, brief_id, status="queued", parallel_safe=None):
    card_dir = os.path.join(cards_dir, brief_id)
    os.makedirs(card_dir, exist_ok=True)
    body = ["---", f"ID: {brief_id}", f"Status: {status}"]
    if parallel_safe is not None:
        body.append(f"Parallel-safe: {parallel_safe}")
    body += ["---", "", f"# {brief_id}", ""]
    path = os.path.join(card_dir, "index.md")
    with open(path, "w") as f:
        f.write("\n".join(body))
    return path


class CardIsSolo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cards = os.path.join(self.tmp, "wiki", "briefs", "cards")

    def test_parallel_safe_true_is_not_solo(self):
        p = _write_card(self.cards, "brief-1", parallel_safe="true")
        self.assertFalse(loop_queue._card_is_solo(p))

    def test_parallel_safe_false_is_solo(self):
        p = _write_card(self.cards, "brief-2", parallel_safe="false")
        self.assertTrue(loop_queue._card_is_solo(p))

    def test_missing_field_defaults_solo(self):
        p = _write_card(self.cards, "brief-3", parallel_safe=None)
        self.assertTrue(loop_queue._card_is_solo(p))

    def test_unreadable_path_is_solo_failsafe(self):
        self.assertTrue(loop_queue._card_is_solo(os.path.join(self.tmp, "nope.md")))


class HeadSoloDrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cards = os.path.join(self.tmp, "wiki", "briefs", "cards")
        os.makedirs(self.cards, exist_ok=True)
        state = os.path.join(self.tmp, ".loop", "state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "running.json"), "w") as f:
            f.write('{"active": []}')
        _git(self.tmp, "init", "-q")
        _git(self.tmp, "config", "user.email", "t@t")
        _git(self.tmp, "config", "user.name", "t")

    def _commit_card(self, brief_id, parallel_safe, goals_first=True):
        _write_card(self.cards, brief_id, parallel_safe=parallel_safe)
        if goals_first:
            with open(os.path.join(self.tmp, ".loop", "state", "goals.md"), "w") as f:
                f.write(f"1. **{brief_id}** — head\n")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-q", "-m", f"queue {brief_id}")
        # commit timestamp (epoch) for `now` math
        r = subprocess.run(
            ["git", "-C", self.tmp, "log", "-1", "--format=%ct", "--",
             f"wiki/briefs/cards/{brief_id}/index.md"],
            capture_output=True, text=True,
        )
        return float(r.stdout.strip())

    def test_threshold_zero_never_drains(self):
        committed = self._commit_card("brief-solo", "false")
        d = loop_queue.head_solo_drain(self.tmp, 0, now=committed + 99999)
        self.assertFalse(d["drain"])

    def test_parallel_head_does_not_drain(self):
        committed = self._commit_card("brief-par", "true")
        d = loop_queue.head_solo_drain(self.tmp, 900, now=committed + 99999)
        self.assertFalse(d["drain"])
        self.assertEqual(d["brief"], "brief-par")

    def test_solo_head_under_threshold_does_not_drain(self):
        committed = self._commit_card("brief-solo", "false")
        # waited 100s, threshold 900s → no drain yet.
        d = loop_queue.head_solo_drain(self.tmp, 900, now=committed + 100)
        self.assertFalse(d["drain"])

    def test_solo_head_over_threshold_drains(self):
        committed = self._commit_card("brief-solo", "false")
        d = loop_queue.head_solo_drain(self.tmp, 900, now=committed + 1000)
        self.assertTrue(d["drain"])
        self.assertEqual(d["brief"], "brief-solo")
        self.assertGreater(d["waited"], 900)

    def test_empty_queue_does_not_drain(self):
        # No queued cards → nothing to drain for.
        d = loop_queue.head_solo_drain(self.tmp, 900, now=2_000_000_000)
        self.assertFalse(d["drain"])
        self.assertEqual(d["brief"], "")


if __name__ == "__main__":
    unittest.main()
