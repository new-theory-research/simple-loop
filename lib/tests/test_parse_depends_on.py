#!/usr/bin/env python3
"""Unit tests for parse_depends_on_value() / BRIEF_ID_RE — letter-suffixed
sibling brief ids (brief-108a, brief-253a-slug) are an established portal
convention; the id pattern only accepted pure-numeric, so Depends-on entries
silently dropped and dependent briefs could dispatch out of order
(portal brief-253a depends-on drops, 2026-06-11)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from assess import parse_depends_on_value


class TestParseDependsOnValue(unittest.TestCase):

    def test_letter_suffixed_sibling_id_retained(self):
        # The observed drop: portal stderr flooded with
        # `dropping non-brief-id token: "brief-253a-nt0-rename-producer"`.
        result = parse_depends_on_value(
            "brief-253a-nt0-rename-producer, brief-250-newt-cli-global-install"
        )
        self.assertEqual(result, [
            "brief-253a-nt0-rename-producer",
            "brief-250-newt-cli-global-install",
        ])

    def test_short_letter_suffixed_id_retained(self):
        self.assertEqual(parse_depends_on_value("brief-108a"), ["brief-108a"])

    def test_cont_variant_retained(self):
        self.assertEqual(
            parse_depends_on_value("brief-241-cont-b"), ["brief-241-cont-b"]
        )

    def test_plain_numeric_ids_still_retained(self):
        self.assertEqual(
            parse_depends_on_value("brief-010-foo, brief-011"),
            ["brief-010-foo", "brief-011"],
        )

    def test_multi_letter_suffix_still_dropped(self):
        # Only a SINGLE letter-suffix is the convention; don't widen further.
        self.assertEqual(parse_depends_on_value("brief-253ab-foo"), [])

    def test_nonsense_tokens_still_dropped(self):
        # Brief-082 hardening must survive the widening.
        self.assertEqual(
            parse_depends_on_value("none (daemon harness, simple-loop master)"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
