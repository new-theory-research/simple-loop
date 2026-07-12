#!/usr/bin/env python3
"""brief-165 presence plane — append_event schema is additive + byte-compatible.

The live runtime-events line is {ts, event, brief, ...}. BRICK 1 makes {box, lane}
stampable (from LOOP_BOX / LOOP_LANE, or an explicit kwarg) without changing the
byte output when neither is present — the additive-only guard.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from state import append_event  # noqa: E402


def _read_last_event(project_dir):
    path = Path(project_dir) / ".loop" / "state" / "runtime-events.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    return json.loads(lines[-1])


class PresenceSchemaTest(unittest.TestCase):
    def setUp(self):
        # Isolate from any ambient LOOP_BOX / LOOP_LANE in the runner env.
        self._saved = {k: os.environ.pop(k, None) for k in ("LOOP_BOX", "LOOP_LANE")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_no_env_no_kwargs_is_byte_compatible(self):
        # Absent env + absent kwargs → the pre-165 line exactly: no box/lane keys.
        with tempfile.TemporaryDirectory() as d:
            append_event(d, "dispatched", "brief-042")
            ev = _read_last_event(d)
            self.assertEqual(set(ev.keys()), {"ts", "event", "brief"})
            self.assertNotIn("box", ev)
            self.assertNotIn("lane", ev)

    def test_env_stamps_box_and_lane(self):
        os.environ["LOOP_BOX"] = "lady-titania"
        os.environ["LOOP_LANE"] = "harness-improvements"
        with tempfile.TemporaryDirectory() as d:
            append_event(d, "dispatched", "brief-165")
            ev = _read_last_event(d)
            self.assertEqual(ev["box"], "lady-titania")
            self.assertEqual(ev["lane"], "harness-improvements")

    def test_explicit_kwarg_wins_over_env(self):
        os.environ["LOOP_BOX"] = "env-box"
        with tempfile.TemporaryDirectory() as d:
            append_event(d, "completed", "brief-165", box="explicit-box")
            ev = _read_last_event(d)
            self.assertEqual(ev["box"], "explicit-box")

    def test_explicit_none_lane_is_dropped_not_written(self):
        # A caller passing lane=None with no env must not leave a null field.
        with tempfile.TemporaryDirectory() as d:
            append_event(d, "completed", "brief-165", lane=None, box=None)
            ev = _read_last_event(d)
            self.assertNotIn("lane", ev)
            self.assertNotIn("box", ev)


if __name__ == "__main__":
    unittest.main()
