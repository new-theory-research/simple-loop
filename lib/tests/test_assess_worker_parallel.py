"""WORKER_PARALLEL worker-target selection (assess.py).

Covers the pieces added for parallel worker execution:

  - read_config_value / _worker_parallel_enabled / _throttle: config.sh
    parsing with the config.local.sh overlay winning (mirrors daemon.sh
    bash sourcing precedence).
  - last_iteration_ts: newest worker-log mtime per brief; 0.0 when none.
  - order_worker_targets: least-recently-iterated-first ordering, primary
    pinned (line 2 stays walk-order-first → byte-identical flag-off), and
    the THROTTLE cap on total worker targets.

Why these matter, not just what:
  - The primary-pin test guards the byte-identical flag-off contract: line 2
    must remain the walk-order brief regardless of iteration recency.
  - The never-iterated-sorts-first test encodes #24: a brief that has never
    run must not starve behind one that just ran.
  - The throttle-cap test guards the single-flight invariant ceiling: the
    daemon must never be handed more concurrent targets than THROTTLE.
"""

import os
import sys
import tempfile
import time
import unittest

_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import assess  # noqa: E402


class ReadConfigValue(unittest.TestCase):
    def test_absent_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                assess.read_config_value(d, "WORKER_PARALLEL", "false"), "false"
            )

    def test_reads_config_sh(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.sh"), "w") as f:
                f.write('WORKER_PARALLEL="true"\nTHROTTLE=2\n')
            self.assertTrue(assess._worker_parallel_enabled(d))
            self.assertEqual(assess._throttle(d), 2)

    def test_strips_inline_comment(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.sh"), "w") as f:
                f.write("THROTTLE=3   # up to three concurrent\n")
            self.assertEqual(assess._throttle(d), 3)

    def test_local_overlay_wins(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.sh"), "w") as f:
                f.write("WORKER_PARALLEL=false\n")
            with open(os.path.join(d, "config.local.sh"), "w") as f:
                f.write("WORKER_PARALLEL=true\n")
            self.assertTrue(assess._worker_parallel_enabled(d))

    def test_throttle_floor_is_one(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.sh"), "w") as f:
                f.write("THROTTLE=0\n")
            self.assertEqual(assess._throttle(d), 1)

    def test_throttle_garbage_falls_back_to_one(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.sh"), "w") as f:
                f.write("THROTTLE=banana\n")
            self.assertEqual(assess._throttle(d), 1)


class LastIterationTs(unittest.TestCase):
    def test_no_log_dir_returns_zero(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(assess.last_iteration_ts(d, "brief-1"), 0.0)

    def test_never_iterated_returns_zero(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".loop", "logs"))
            self.assertEqual(assess.last_iteration_ts(d, "brief-1"), 0.0)

    def test_returns_newest_matching_log_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            log_dir = os.path.join(d, ".loop", "logs")
            os.makedirs(log_dir)
            old = os.path.join(log_dir, "worker_brief-1_20260101_000000.log")
            new = os.path.join(log_dir, "worker_brief-1_20260101_010000.log")
            other = os.path.join(log_dir, "worker_brief-2_20260101_020000.log")
            for p in (old, new, other):
                open(p, "w").close()
            os.utime(old, (1000, 1000))
            os.utime(new, (2000, 2000))
            os.utime(other, (3000, 3000))  # different brief — must be ignored
            self.assertEqual(assess.last_iteration_ts(d, "brief-1"), 2000.0)

    def test_prefix_is_exact_not_substring(self):
        # brief-1 must not pick up brief-10's logs.
        with tempfile.TemporaryDirectory() as d:
            log_dir = os.path.join(d, ".loop", "logs")
            os.makedirs(log_dir)
            p = os.path.join(log_dir, "worker_brief-10_20260101_000000.log")
            open(p, "w").close()
            os.utime(p, (5000, 5000))
            # brief-1 has no own log → 0.0 (worker_brief-1_ prefix won't match
            # worker_brief-10_ because of the trailing underscore).
            self.assertEqual(assess.last_iteration_ts(d, "brief-1"), 0.0)


class OrderWorkerTargets(unittest.TestCase):
    def test_primary_pinned_extras_least_recently_iterated_first(self):
        primary = "WORKER:brief-a,brief-a"
        candidates = [
            (300.0, "WORKER:brief-a,brief-a"),  # primary (recent)
            (100.0, "WORKER:brief-b,brief-b"),  # oldest
            (200.0, "WORKER:brief-c,brief-c"),
        ]
        extras = assess.order_worker_targets(primary, candidates, throttle=3)
        # primary excluded; remaining ordered oldest-first.
        self.assertEqual(
            extras, ["WORKER:brief-b,brief-b", "WORKER:brief-c,brief-c"]
        )

    def test_never_iterated_sorts_first(self):
        # #24: a never-run brief (ts 0.0) must lead.
        primary = "WORKER:brief-a,brief-a"
        candidates = [
            (50.0, "WORKER:brief-a,brief-a"),
            (99.0, "WORKER:brief-b,brief-b"),
            (0.0, "WORKER:brief-c,brief-c"),  # never iterated
        ]
        extras = assess.order_worker_targets(primary, candidates, throttle=3)
        self.assertEqual(extras[0], "WORKER:brief-c,brief-c")

    def test_throttle_caps_total_targets(self):
        # THROTTLE=2 → 1 primary + at most 1 extra.
        primary = "WORKER:brief-a,brief-a"
        candidates = [
            (10.0, "WORKER:brief-a,brief-a"),
            (1.0, "WORKER:brief-b,brief-b"),
            (2.0, "WORKER:brief-c,brief-c"),
        ]
        extras = assess.order_worker_targets(primary, candidates, throttle=2)
        self.assertEqual(extras, ["WORKER:brief-b,brief-b"])

    def test_throttle_one_emits_no_extras(self):
        primary = "WORKER:brief-a,brief-a"
        candidates = [
            (10.0, "WORKER:brief-a,brief-a"),
            (1.0, "WORKER:brief-b,brief-b"),
        ]
        self.assertEqual(
            assess.order_worker_targets(primary, candidates, throttle=1), []
        )

    def test_primary_not_in_candidates_still_capped(self):
        # Defensive: even if primary isn't among candidates, total stays ≤ throttle.
        primary = "WORKER:brief-z,brief-z"
        candidates = [
            (1.0, "WORKER:brief-b,brief-b"),
            (2.0, "WORKER:brief-c,brief-c"),
            (3.0, "WORKER:brief-d,brief-d"),
        ]
        extras = assess.order_worker_targets(primary, candidates, throttle=2)
        self.assertEqual(extras, ["WORKER:brief-b,brief-b"])


if __name__ == "__main__":
    unittest.main()
