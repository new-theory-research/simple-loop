#!/usr/bin/env python3
"""Pin parse_auto_merge_flag to handle both YAML-frontmatter form
(`Auto-merge: true`) and legacy bold-markdown form (`**Auto-merge:** true`).

Pre-fix: AUTO_MERGE_LINE_RE in auto_merge.py only matched the bold form.
Every YAML-frontmatter card (brief-108+ convention) silently returned
flag=False, routing to awaiting_review even when the card opted in.

Live receipt 2026-06-11: brief-250 frontmatter `Auto-merge: true`;
running.json dispatch record carried auto_merge:false — brief routed to
awaiting_review bucket instead of auto-merge path.

Sibling keys with the same disease were fixed in the same pass:
AUTO_MERGE_LINE_RE, DEPENDS_ON_LINE_RE, DEPENDS_ON_SECRETS_LINE_RE,
CYCLE_WALL_TIME_SECS_LINE_RE in assess.py all updated to dual-format regex.
"""

import io
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from auto_merge import parse_auto_merge_flag


class ParseAutoMergeFlagTest(unittest.TestCase):

    # ── YAML frontmatter form ──────────────────────────────────────────────

    def test_yaml_frontmatter_true(self):
        """Primary fix: `Auto-merge: true` in YAML frontmatter must parse as True."""
        content = (
            "---\n"
            "ID: brief-250\n"
            "Status: running\n"
            "Auto-merge: true\n"
            "---\n"
            "\n"
            "# Brief body\n"
        )
        self.assertTrue(parse_auto_merge_flag(content),
                        "YAML 'Auto-merge: true' must return True")

    def test_yaml_frontmatter_false(self):
        content = "---\nAuto-merge: false\n---\n"
        self.assertFalse(parse_auto_merge_flag(content))

    def test_yaml_frontmatter_true_case_insensitive(self):
        """Case is normalized — `Auto-merge: True` should parse as True."""
        content = "---\nAuto-merge: True\n---\n"
        self.assertTrue(parse_auto_merge_flag(content))

    # ── Legacy bold-markdown form ──────────────────────────────────────────

    def test_bold_markdown_true(self):
        """Legacy `**Auto-merge:** true` form must continue to work."""
        content = "**Auto-merge:** true\n"
        self.assertTrue(parse_auto_merge_flag(content),
                        "legacy bold-markdown form must continue to parse as True")

    def test_bold_markdown_false(self):
        content = "**Auto-merge:** false\n"
        self.assertFalse(parse_auto_merge_flag(content))

    # ── Garbage / unrecognized value → False + loud warning ───────────────

    def test_garbage_value_returns_false_with_warning(self):
        """Unrecognized value (not true/false) → False + stderr WARNING."""
        content = "---\nAuto-merge: yes\n---\n"
        err = io.StringIO()
        with redirect_stderr(err):
            result = parse_auto_merge_flag(content)
        self.assertFalse(result, "unrecognized value must return False")
        self.assertIn("WARNING", err.getvalue(),
                      "unrecognized value must emit WARNING to stderr")
        self.assertIn("yes", err.getvalue())

    def test_absent_flag_returns_false(self):
        """No Auto-merge line → False (default: human-gated)."""
        content = "---\nID: brief-001\nStatus: queued\n---\n"
        self.assertFalse(parse_auto_merge_flag(content))

    def test_empty_content_returns_false(self):
        self.assertFalse(parse_auto_merge_flag(""))

    def test_none_content_returns_false(self):
        self.assertFalse(parse_auto_merge_flag(None))


if __name__ == "__main__":
    unittest.main()
