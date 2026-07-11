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


class TestLanePrefixedIds(unittest.TestCase):
    """Issue #50: lane-prefixed card ids (ft-*, capture-*, rq-*, …) were silently
    dropped because the shape assumed a literal `brief-` prefix."""

    def test_issue50_repro_both_tokens_extracted(self):
        # The exact reproduction from the issue body — before the fix this
        # returned [] (both tokens dropped to stderr, silently). The `(merged)`
        # suffix is handled the same way `brief-* (merged)` is: parenthetical
        # stripped before matching.
        line = ("capture-004-per-key-identity, "
                "ft-004-serve-finetuned-checkpoint (merged)")
        self.assertEqual(
            parse_depends_on_value(line),
            ["capture-004-per-key-identity", "ft-004-serve-finetuned-checkpoint"],
        )

    def test_lane_prefixes_retained(self):
        for tok in ("ft-006-newt-finetune-verb", "capture-004-per-key-identity",
                    "rq-001-first-remote-run", "fleet-012-x", "serve-003-y",
                    "harness-007-z"):
            self.assertEqual(parse_depends_on_value(tok), [tok])

    def test_lane_id_letter_suffix_retained(self):
        self.assertEqual(parse_depends_on_value("capture-004a"), ["capture-004a"])

    def test_lane_id_merged_suffix_stripped(self):
        # `(merged)` annotation handled identically to the brief-* path.
        self.assertEqual(
            parse_depends_on_value("capture-002-nt-cloud-sink (merged)"),
            ["capture-002-nt-cloud-sink"],
        )

    def test_lane_prefixed_multi_letter_suffix_still_dropped(self):
        # Single-letter suffix is the convention; a double suffix is not an id.
        self.assertEqual(parse_depends_on_value("capture-004ab-foo"), [])


class TestBriefIdGolden(unittest.TestCase):
    """Golden: the pre-#50 brief-* behavior is byte-for-byte unchanged."""

    CASES = [
        ("brief-010-foo", ["brief-010-foo"]),
        ("brief-010-foo, brief-011-bar", ["brief-010-foo", "brief-011-bar"]),
        ("brief-010-foo,brief-011-bar", ["brief-010-foo", "brief-011-bar"]),
        ("brief-010-foo,", ["brief-010-foo"]),
        ("brief-108a", ["brief-108a"]),
        ("brief-253a-nt0-rename-producer", ["brief-253a-nt0-rename-producer"]),
        ("brief-241-cont-b", ["brief-241-cont-b"]),
        ("brief-078 (hard)", ["brief-078"]),
        ("brief-253ab-foo", []),          # double suffix still dropped
        ("none", []),                     # nonsense still dropped
        ("_(intentionally empty)_", []),  # brief-082 wedge still dropped
        ("", []),
    ]

    def test_brief_star_behavior_unchanged(self):
        for raw, expected in self.CASES:
            self.assertEqual(
                parse_depends_on_value(raw), expected,
                msg=f"golden mismatch for {raw!r}",
            )


if __name__ == "__main__":
    unittest.main()
