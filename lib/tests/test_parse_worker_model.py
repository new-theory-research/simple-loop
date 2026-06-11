"""Pin the worker-model parser to handle both YAML-frontmatter form
(`Model: opus`) and legacy bold-markdown form (`**Model:** opus`).

Pre-fix: daemon.sh used a bash grep that only matched the bold form.
Every YAML-frontmatter card (brief-108+ convention) silently fell through
to the sonnet default, so opus-designated briefs always ran sonnet workers.

Live receipt 2026-06-11: brief-249 frontmatter line 5 `Model: opus`;
running worker process `claude --model sonnet`.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr


def _write_card(body):
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(body)
    return path


class ParseWorkerModelTest(unittest.TestCase):
    def setUp(self):
        lib_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        from actions import parse_worker_model
        self.parse = parse_worker_model

    # ── YAML frontmatter form ───────────────────────────────────────────────

    def test_yaml_frontmatter_opus(self):
        """Primary fix: `Model: opus` in YAML frontmatter must resolve to opus."""
        path = _write_card(
            "---\n"
            "ID: brief-249\n"
            "Status: running\n"
            "Auto-merge: true\n"
            "Model: opus\n"
            "---\n"
            "\n"
            "# Brief body\n"
        )
        try:
            out = io.StringIO()
            with redirect_stderr(io.StringIO()):
                sys.stdout = out
                self.parse(path)
                sys.stdout = sys.__stdout__
            self.assertEqual(out.getvalue().strip(), "opus",
                             "YAML 'Model: opus' must resolve to 'opus'")
        finally:
            sys.stdout = sys.__stdout__
            os.unlink(path)

    def test_yaml_frontmatter_sonnet(self):
        path = _write_card("---\nModel: sonnet\n---\n")
        try:
            out = io.StringIO()
            sys.stdout = out
            self.parse(path)
            sys.stdout = sys.__stdout__
            self.assertEqual(out.getvalue().strip(), "sonnet")
        finally:
            sys.stdout = sys.__stdout__
            os.unlink(path)

    def test_yaml_frontmatter_haiku(self):
        path = _write_card("---\nModel: haiku\n---\n")
        try:
            out = io.StringIO()
            sys.stdout = out
            self.parse(path)
            sys.stdout = sys.__stdout__
            self.assertEqual(out.getvalue().strip(), "haiku")
        finally:
            sys.stdout = sys.__stdout__
            os.unlink(path)

    def test_yaml_frontmatter_uppercase_normalized(self):
        """Case is normalized — `Model: Opus` should parse as 'opus'."""
        path = _write_card("---\nModel: Opus\n---\n")
        try:
            out = io.StringIO()
            sys.stdout = out
            self.parse(path)
            sys.stdout = sys.__stdout__
            self.assertEqual(out.getvalue().strip(), "opus")
        finally:
            sys.stdout = sys.__stdout__
            os.unlink(path)

    # ── Legacy bold-markdown form ───────────────────────────────────────────

    def test_bold_markdown_opus(self):
        """Legacy `**Model:** opus` form must continue to work."""
        path = _write_card("**Model:** opus\n")
        try:
            out = io.StringIO()
            sys.stdout = out
            self.parse(path)
            sys.stdout = sys.__stdout__
            self.assertEqual(out.getvalue().strip(), "opus",
                             "legacy bold-markdown form must continue to parse")
        finally:
            sys.stdout = sys.__stdout__
            os.unlink(path)

    def test_bold_markdown_sonnet(self):
        path = _write_card("**Model:** sonnet\n")
        try:
            out = io.StringIO()
            sys.stdout = out
            self.parse(path)
            sys.stdout = sys.__stdout__
            self.assertEqual(out.getvalue().strip(), "sonnet")
        finally:
            sys.stdout = sys.__stdout__
            os.unlink(path)

    # ── Garbage / unrecognized value → sonnet + loud warning ───────────────

    def test_garbage_value_falls_back_to_sonnet_with_warning(self):
        """Typo'd model: falls back to sonnet, emits warning to stderr (issue #21)."""
        path = _write_card("---\nModel: gpt-4\n---\n")
        try:
            out = io.StringIO()
            err = io.StringIO()
            sys.stdout = out
            with redirect_stderr(err):
                self.parse(path)
            sys.stdout = sys.__stdout__
            self.assertEqual(out.getvalue().strip(), "sonnet",
                             "unrecognized model must fall back to sonnet")
            self.assertIn("WARNING", err.getvalue(),
                          "unrecognized model must emit a WARNING to stderr")
            self.assertIn("gpt-4", err.getvalue())
        finally:
            sys.stdout = sys.__stdout__
            os.unlink(path)

    def test_empty_value_produces_no_output(self):
        """No Model line → prints nothing; caller uses its own default."""
        path = _write_card("---\nID: brief-001\nStatus: queued\n---\n")
        try:
            out = io.StringIO()
            sys.stdout = out
            self.parse(path)
            sys.stdout = sys.__stdout__
            self.assertEqual(out.getvalue().strip(), "",
                             "absent Model line must produce no output")
        finally:
            sys.stdout = sys.__stdout__
            os.unlink(path)

    def test_missing_file_is_safe(self):
        """Non-existent path must not raise; returns silently."""
        out = io.StringIO()
        sys.stdout = out
        try:
            self.parse("/tmp/does-not-exist-1234567890.md")
        finally:
            sys.stdout = sys.__stdout__
        # No output, no exception
        self.assertEqual(out.getvalue().strip(), "")


if __name__ == "__main__":
    unittest.main()
