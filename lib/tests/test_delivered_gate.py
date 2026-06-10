#!/usr/bin/env python3
"""Tests for the delivered gate and superseded projector routing (brief-237).

Three goldens:
  1. "A brief can't claim done while its code exists only on the worker's machine."
     (lineage: brief-230)
  2. "Work that shipped through another door closes in one command."
     (lineage: brief-300, 2026-06-09)
  3. "Portal-only briefs feel nothing."
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from actions import (  # noqa: E402
    _parse_target_repo,
    _parse_edit_surface_repos,
    _external_repos_for_brief,
    _verify_delivered_ref,
    _check_delivered_gate,
    close_as_delivered,
    init_paths,
    move_to_awaiting_review,
    move_to_pending_merges,
    move_to_eval,
)
from state import project_running_json, write_running_json  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_card(tmp: Path, brief_id: str, frontmatter: str) -> str:
    """Write a minimal card index.md and return its path."""
    card_dir = tmp / "wiki" / "briefs" / "cards" / brief_id
    card_dir.mkdir(parents=True, exist_ok=True)
    card_path = card_dir / "index.md"
    card_path.write_text(f"---\nID: {brief_id}\n{frontmatter}---\n\n# {brief_id}\n")
    return str(card_path)


def _make_progress(tmp: Path, brief_id: str, progress: dict):
    """Write progress.json in the expected worktree location."""
    wt_state = tmp / ".loop" / "worktrees" / brief_id / ".loop" / "state"
    wt_state.mkdir(parents=True, exist_ok=True)
    (wt_state / "progress.json").write_text(json.dumps(progress))


def _paths(tmp: Path) -> dict:
    return {
        "project_dir": str(tmp),
        "loop_dir": str(tmp / ".loop"),
        "state_dir": str(tmp / ".loop" / "state"),
        "worktrees_dir": str(tmp / ".loop" / "worktrees"),
        "running_file": str(tmp / ".loop" / "state" / "running.json"),
    }


# ── Golden 1 — "A brief can't claim done while its code exists only on the worker's machine." ──
# lineage: brief-230 — daemon false-completed a cross-repo brief; code sat unpushed;
# three sessions independently re-fixed the same thing.

class TestGoldenBriefCantClaimDone(unittest.TestCase):

    # ── _parse_target_repo ──────────────────────────────────────────────

    def test_parse_single_external_repo(self):
        """Target-repo: simple-loop → ['simple-loop']."""
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-t1", "Target-repo: simple-loop\n")
            self.assertEqual(_parse_target_repo(card), ["simple-loop"])

    def test_parse_portal_only_returns_empty(self):
        """Target-repo: portal → [] (portal-only, gate should not trigger)."""
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-t2", "Target-repo: portal\n")
            self.assertEqual(_parse_target_repo(card), [])

    def test_parse_multi_repo_filters_portal(self):
        """Target-repo: nt-runway + portal → ['nt-runway'] (portal filtered out)."""
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-t3", "Target-repo: nt-runway + portal\n")
            self.assertEqual(_parse_target_repo(card), ["nt-runway"])

    def test_parse_absent_target_repo_returns_empty(self):
        """No Target-repo field → [] (portal-only assumption)."""
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-t4", "Status: active\n")
            self.assertEqual(_parse_target_repo(card), [])

    def test_parse_missing_file_returns_empty(self):
        """Nonexistent card path → [] (gate can't block when card missing)."""
        self.assertEqual(_parse_target_repo("/tmp/does-not-exist/index.md"), [])

    # ── _check_delivered_gate ────────────────────────────────────────────

    def test_missing_delivered_ref_is_refused(self):
        """External repo with no Delivered entry → (False, [error naming the repo]).

        This is the brief-230 failure mode: worker sets status=complete, daemon
        promotes, code never pushed. Gate must refuse and name the repo.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-230-replay"
            card = _make_card(tmp, brief_id, "Target-repo: mock-external-repo\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
            })
            paths = _paths(tmp)

            passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertFalse(passed, "Gate should refuse when Delivered is absent")
            self.assertEqual(len(errors), 1)
            self.assertIn("mock-external-repo", errors[0])
            self.assertIn("REFUSED", errors[0])

    def test_valid_delivered_url_passes_when_gh_absent(self):
        """External repo with Delivered URL → gate passes when gh binary absent.

        gh is best-effort: missing binary means verification is skipped and the
        gate passes. This prevents CI/offline environments from blocking merges.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-230-replay-ok"
            card = _make_card(tmp, brief_id, "Target-repo: mock-external-repo\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                "delivered": {
                    "mock-external-repo": "https://github.com/mock/repo/commit/abc123ef",
                },
            })
            paths = _paths(tmp)

            with patch("shutil.which", return_value=None):
                passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertTrue(passed, f"Gate should pass with valid Delivered URL when gh absent; errors={errors}")
            self.assertEqual(errors, [])

    def test_valid_delivered_url_verified_by_gh(self):
        """External repo with Delivered URL + gh api succeeds → gate passes."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-230-gh-ok"
            card = _make_card(tmp, brief_id, "Target-repo: some-repo\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                "delivered": {
                    "some-repo": "https://github.com/org/some-repo/commit/deadbeef",
                },
            })
            paths = _paths(tmp)

            import subprocess as _subprocess
            mock_result = _subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
            with patch("shutil.which", return_value="/usr/bin/gh"), \
                 patch("subprocess.run", return_value=mock_result):
                passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertTrue(passed, f"Gate should pass when gh api returns 0; errors={errors}")

    def test_unverifiable_delivered_url_is_refused(self):
        """External repo Delivered URL fails gh api → gate refuses with diagnostic."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-230-gh-fail"
            card = _make_card(tmp, brief_id, "Target-repo: some-repo\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                "delivered": {
                    "some-repo": "https://github.com/org/some-repo/commit/notexist",
                },
            })
            paths = _paths(tmp)

            import subprocess as _subprocess
            mock_result = _subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Not Found")
            with patch("shutil.which", return_value="/usr/bin/gh"), \
                 patch("subprocess.run", return_value=mock_result):
                passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertFalse(passed, "Gate should refuse when gh api returns non-zero")
            self.assertEqual(len(errors), 1)
            self.assertIn("REFUSED", errors[0])

    def test_multiple_external_repos_all_must_have_delivered(self):
        """Two external repos — missing one means refused, error names the missing one."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-multi"
            card = _make_card(tmp, brief_id, "Target-repo: repo-a + repo-b\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                "delivered": {
                    "repo-a": "https://github.com/org/repo-a/commit/abc",
                    # repo-b deliberately absent
                },
            })
            paths = _paths(tmp)

            with patch("shutil.which", return_value=None):
                passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertFalse(passed)
            self.assertTrue(any("repo-b" in e for e in errors),
                            f"Error should name 'repo-b'; got: {errors}")


# ── Parser vs. real card grammar ─────────────────────────────────────────────
# Every string below is copied VERBATIM from a live portal card
# (wiki/briefs/cards/*/index.md, 2026-06-09). The parser is tested against
# what cards actually say, not an idealized format.

class TestParserRealCardFormats(unittest.TestCase):

    def _parse(self, value):
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-real", f"Target-repo: {value}\n")
            return _parse_target_repo(card)

    def test_portal_with_parenthetical_annotation_is_portal_only(self):
        """brief-231: `Target-repo: portal (apps/docs)` → [] — the annotation must
        not turn a portal-only brief into a gated one (the false positive the
        reviewer caught)."""
        self.assertEqual(self._parse("portal (apps/docs)"), [])

    def test_modal_is_not_a_git_repo(self):
        """brief-207: `Target-repo: nt-runway + Modal` — Modal is an infra
        surface, not a repo; gating on it would be a permanent refusal."""
        self.assertEqual(self._parse("nt-runway + Modal"), ["nt-runway"])

    def test_railway_is_not_a_git_repo(self):
        """brief-233: `Target-repo: nt-runway + newt-python + Railway`."""
        self.assertEqual(
            self._parse("nt-runway + newt-python + Railway"),
            ["nt-runway", "newt-python"],
        )

    def test_org_prefixed_name_with_annotation_normalizes(self):
        """brief-211: `Target-repo: nt-runway + new-theory-research/newt-python (new) + portal`
        → org prefix and `(new)` annotation stripped so delivered['newt-python'] can match."""
        self.assertEqual(
            self._parse("nt-runway + new-theory-research/newt-python (new) + portal"),
            ["nt-runway", "newt-python"],
        )

    def test_annotation_containing_plus_does_not_split(self):
        """brief-230: `Target-repo: newt-starter-trossen-widowx (+ newt-starter-yam if same pattern)`
        — the '+' lives INSIDE the parenthetical; annotations strip before splitting."""
        self.assertEqual(
            self._parse("newt-starter-trossen-widowx (+ newt-starter-yam if same pattern)"),
            ["newt-starter-trossen-widowx"],
        )

    def test_plus_without_spaces(self):
        """brief-235: `Target-repo: portal+nt-runway`."""
        self.assertEqual(self._parse("portal+nt-runway"), ["nt-runway"])

    def test_starter_with_new_annotation(self):
        """brief-225: `Target-repo: newt-starter-yam (new) + portal`."""
        self.assertEqual(self._parse("newt-starter-yam (new) + portal"), ["newt-starter-yam"])

    def test_three_way_multi_repo(self):
        """brief-228: `Target-repo: newt-python + nt-runway + portal`."""
        self.assertEqual(
            self._parse("newt-python + nt-runway + portal"),
            ["newt-python", "nt-runway"],
        )

    def test_comma_separated_multi_value(self):
        """Comma-separated form (allowed by the grammar)."""
        self.assertEqual(self._parse("nt-runway, newt-python"), ["nt-runway", "newt-python"])

    def test_tbd_placeholder_is_not_a_repo(self):
        """brief-242: `Target-repo: TBD (starter + possibly vendored tooling)` —
        a placeholder must not gate the brief on delivered['TBD'] forever."""
        self.assertEqual(self._parse("TBD (starter + possibly vendored tooling)"), [])


class TestEditSurfaceParser(unittest.TestCase):

    def test_block_list_real_entries(self):
        """Edit-surface block copied from live cards (brief-205-cont-b, brief-233,
        brief-237, brief-153): absolute sibling paths → repo; bare-name+path →
        repo; bare-name (annotation) → repo; Railway (…) → skipped; relative
        portal paths and bare filenames → skipped."""
        fm = (
            "Edit-surface:\n"
            "  - /Users/mattie-newtheory/new-theory/nt-runway/serve_nt0.py\n"
            "  - /Users/mattie-newtheory/new-theory/portal/wiki/specs/streaming-ws-protocol.md\n"
            "  - simple-loop lib/actions.py\n"
            "  - simple-loop bin/loop (or equivalent CLI entry)\n"
            "  - nt-runway (registry service — lifted out of the GPU serve app)\n"
            "  - Railway (new always-on service + config)\n"
            "  - apps/console/\n"
            "  - closeout.md\n"
        )
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-es", fm)
            self.assertEqual(_parse_edit_surface_repos(card), ["nt-runway", "simple-loop"])

    def test_tbd_entry_is_skipped(self):
        """brief-242: `- TBD pending decisions (newt-starter-trossen-widowx + …)`."""
        fm = (
            "Edit-surface:\n"
            "  - TBD pending decisions (newt-starter-trossen-widowx + possibly vendored calibration tooling)\n"
        )
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-es-tbd", fm)
            self.assertEqual(_parse_edit_surface_repos(card), [])

    def test_union_of_target_repo_and_edit_surface(self):
        """Gate input is the UNION: a repo named only in Edit-surface still gates,
        a repo named in both appears once."""
        fm = (
            "Target-repo: nt-runway\n"
            "Edit-surface:\n"
            "  - /Users/mattie-newtheory/new-theory/nt-runway/serve_nt0.py\n"
            "  - /Users/mattie-newtheory/new-theory/newt-python/src/newt/_client/robot.py\n"
        )
        with tempfile.TemporaryDirectory() as d:
            card = _make_card(Path(d), "brief-union", fm)
            self.assertEqual(_external_repos_for_brief(card), ["nt-runway", "newt-python"])


# ── Delivered-ref verification (plain SHA + gh-free fallback) ────────────────

class TestVerifyDeliveredRef(unittest.TestCase):

    def test_plain_sha_on_known_repo_verified_by_gh(self):
        import subprocess as sp
        ok_run = sp.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("shutil.which", return_value="/usr/bin/gh"), \
             patch("subprocess.run", return_value=ok_run):
            ok, reason = _verify_delivered_ref("deadbeefcafe", "nt-runway")
        self.assertTrue(ok, reason)

    def test_plain_sha_on_unknown_repo_with_gh_is_refused_with_actionable_message(self):
        with patch("shutil.which", return_value="/usr/bin/gh"):
            ok, reason = _verify_delivered_ref("deadbeefcafe", "mystery-repo")
        self.assertFalse(ok)
        self.assertIn("commit URL", reason)

    def test_plain_sha_gh_free_ls_remote_fallback_verifies_branch_tip(self):
        """gh absent, git present: ls-remote advertises the SHA as a tip → verified
        without falling open."""
        import subprocess as sp

        def fake_which(name):
            return "/usr/bin/git" if name == "git" else None

        sha = "ab80d0b4ab80d0b4ab80d0b4ab80d0b4ab80d0b4"
        ls_out = f"{sha}\trefs/heads/master\n"
        ls_run = sp.CompletedProcess(args=[], returncode=0, stdout=ls_out, stderr="")
        with patch("shutil.which", side_effect=fake_which), \
             patch("subprocess.run", return_value=ls_run):
            ok, reason = _verify_delivered_ref(sha[:12], "simple-loop")
        self.assertTrue(ok, reason)

    def test_gh_absent_falls_open_loudly_not_silently(self):
        """gh + git both absent → gate falls open but prints the loud SKIPPED
        warning naming the repo (the silent fail-open was the reviewer's flag)."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with patch("shutil.which", return_value=None), redirect_stderr(buf):
            ok, _ = _verify_delivered_ref(
                "https://github.com/new-theory-research/nt-runway/commit/abc123", "nt-runway"
            )
        self.assertTrue(ok)
        err = buf.getvalue()
        self.assertIn("gh unavailable", err)
        self.assertIn("SKIPPED", err)
        self.assertIn("nt-runway", err)


# ── Escape hatch ──────────────────────────────────────────────────────────────

class TestEscapeHatch(unittest.TestCase):

    def test_skip_env_var_bypasses_gate_with_banner(self):
        """SIMPLE_LOOP_SKIP_DELIVERED_GATE=1 → gate passes despite missing
        delivered, and a loud banner lands on stderr."""
        import io
        from contextlib import redirect_stderr
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-skip"
            card = _make_card(tmp, brief_id, "Target-repo: nt-runway\n")
            paths = _paths(tmp)
            buf = io.StringIO()
            with patch.dict(os.environ, {"SIMPLE_LOOP_SKIP_DELIVERED_GATE": "1"}), \
                 redirect_stderr(buf):
                passed, errors = _check_delivered_gate(paths, brief_id, card)
            self.assertTrue(passed)
            self.assertEqual(errors, [])
            self.assertIn("SIMPLE_LOOP_SKIP_DELIVERED_GATE", buf.getvalue())

    def test_refusal_message_is_self_serve(self):
        """The refusal names the expected JSON shape, the file to edit, and the
        escape hatch — a stuck human at 2am can fix it from the message alone."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-2am"
            card = _make_card(tmp, brief_id, "Target-repo: nt-runway\n")
            paths = _paths(tmp)
            passed, errors = _check_delivered_gate(paths, brief_id, card)
            self.assertFalse(passed)
            msg = errors[0]
            self.assertIn('"delivered"', msg)
            self.assertIn("progress.json", msg)
            self.assertIn("nt-runway", msg)
            self.assertIn("SIMPLE_LOOP_SKIP_DELIVERED_GATE", msg)


# ── Golden 3 — "Portal-only briefs feel nothing." ─────────────────────────────
# Portal-only briefs must complete exactly as before — no Delivered requirement.

class TestGoldenPortalOnlyBriefFeelsNothing(unittest.TestCase):

    def test_portal_only_brief_gate_is_not_triggered(self):
        """Target-repo: portal → gate returns (True, []) regardless of Delivered absence.

        A portal-only brief has no cross-repo work to verify; the gate must be
        a complete no-op so existing behavior is unchanged.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-portal-only"
            card = _make_card(tmp, brief_id, "Target-repo: portal\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
                # no 'delivered' field — would fail if gate applied
            })
            paths = _paths(tmp)

            passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertTrue(passed, "Gate must not trigger for portal-only briefs")
            self.assertEqual(errors, [], "No errors expected for portal-only brief")

    def test_absent_target_repo_field_gate_is_not_triggered(self):
        """No Target-repo field → treated as portal-only, gate not triggered."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-no-target"
            card = _make_card(tmp, brief_id, "Status: active\n")
            _make_progress(tmp, brief_id, {
                "status": "complete", "iteration": 1, "tasks_remaining": [],
            })
            paths = _paths(tmp)

            passed, errors = _check_delivered_gate(paths, brief_id, card)

            self.assertTrue(passed)
            self.assertEqual(errors, [])


# ── Golden 2 — "Work that shipped through another door closes in one command." ──
# lineage: brief-300, 2026-06-09 — work landed via design-director land (ab80d0b4)
# + brief-301's merge; ledger still pointed at stale branch; daemon retried
# conflicting merge every tick; human hand-edited running.json four times.

def _make_project_dir(tmp: Path, brief_id: str, status: str = "active") -> None:
    """Set up a minimal project tree: card + state dir."""
    card_dir = tmp / "wiki" / "briefs" / "cards" / brief_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "index.md").write_text(
        f"---\nID: {brief_id}\nBranch: {brief_id}\nStatus: {status}\n---\n\n# {brief_id}\n"
    )
    state_dir = tmp / ".loop" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)


def _write_events(tmp: Path, events: list) -> None:
    events_path = tmp / ".loop" / "state" / "runtime-events.jsonl"
    with open(events_path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


class TestGoldenWorkShippedThroughAnotherDoor(unittest.TestCase):
    """Golden 2: close_as_delivered atomically supersedes a brief from any queue state.

    The brief-300 failure mode: work shipped through another door; the daemon
    retried conflicting merges every tick; a human had to hand-edit running.json
    (four writes, error-prone, done twice in one day — 2026-06-09).
    """

    def _setup_pending_merges(self, tmp: Path, brief_id: str) -> dict:
        """Create a brief that projects into pending_merges[] (dispatched + completed + approved)."""
        _make_project_dir(tmp, brief_id)
        _write_events(tmp, [
            {"ts": "2026-06-09T09:00:00Z", "event": "dispatched", "brief": brief_id,
             "branch": brief_id, "brief_file": f"wiki/briefs/cards/{brief_id}/index.md",
             "worker_slot": 1},
            {"ts": "2026-06-09T09:10:00Z", "event": "completed", "brief": brief_id,
             "kind": "complete", "auto_merge": True},
            {"ts": "2026-06-09T09:11:00Z", "event": "approved", "brief": brief_id,
             "auto_merge": True},
        ])
        paths = init_paths(str(tmp))
        write_running_json(str(tmp))
        return paths

    def test_close_removes_from_pending_merges_to_history(self):
        """Brief in pending_merges[] → close_as_delivered → history[] with superseded status.

        Asserts the four writes happen atomically: card flipped, event appended,
        running.json updated, history[] carries delivered_via pointer.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-300-replay"
            delivered_via = "https://github.com/ScavieFae/simple-loop/commit/ab80d0b4"
            paths = self._setup_pending_merges(tmp, brief_id)

            # Verify pre-condition: brief is in pending_merges
            pre = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in pre["pending_merges"]},
                          "Pre-condition: brief should be in pending_merges before close")

            result = close_as_delivered(paths, brief_id, delivered_via, reason="landed via design-director")

            self.assertTrue(result, "close_as_delivered must return True")

            # Card status flipped to superseded
            card_path = tmp / "wiki" / "briefs" / "cards" / brief_id / "index.md"
            card_text = card_path.read_text()
            self.assertIn("Status: superseded", card_text, "Card must have Status: superseded")

            # Superseded event appended to runtime-events.jsonl
            events_path = tmp / ".loop" / "state" / "runtime-events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
            sup_events = [e for e in events if e.get("event") == "superseded" and e.get("brief") == brief_id]
            self.assertEqual(len(sup_events), 1, "Exactly one superseded event must be appended")
            self.assertEqual(sup_events[0]["delivered_via"], delivered_via)

            # running.json history[] carries the entry; pending_merges[] is empty for this brief
            rc = project_running_json(str(tmp))
            history_briefs = {e["brief"] for e in rc["history"]}
            self.assertIn(brief_id, history_briefs, "Brief must appear in history[] after close")
            pending_briefs = {e["brief"] for e in rc["pending_merges"]}
            self.assertNotIn(brief_id, pending_briefs, "Brief must not remain in pending_merges[] after close")

            history_entry = next(e for e in rc["history"] if e["brief"] == brief_id)
            self.assertEqual(history_entry["status"], "superseded")
            self.assertEqual(history_entry["delivered_via"], delivered_via)

    def test_close_from_awaiting_review(self):
        """Brief in awaiting_review[] → close_as_delivered also works (gap that caused 2026-06-09 hand-edit)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-300-awaiting"
            _make_project_dir(tmp, brief_id)
            _write_events(tmp, [
                {"ts": "2026-06-09T09:00:00Z", "event": "dispatched", "brief": brief_id,
                 "branch": brief_id, "brief_file": f"wiki/briefs/cards/{brief_id}/index.md",
                 "worker_slot": 1},
                {"ts": "2026-06-09T09:10:00Z", "event": "completed", "brief": brief_id,
                 "kind": "complete", "auto_merge": False},
            ])
            paths = init_paths(str(tmp))
            write_running_json(str(tmp))

            pre = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in pre["awaiting_review"]})

            delivered_via = "https://github.com/ScavieFae/simple-loop/pull/15"
            result = close_as_delivered(paths, brief_id, delivered_via)

            self.assertTrue(result)
            rc = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in rc["history"]})
            self.assertNotIn(brief_id, {e["brief"] for e in rc["awaiting_review"]})

    def test_close_is_idempotent(self):
        """Re-running close_as_delivered on an already-superseded brief returns True, no extra event."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-300-idempotent"
            delivered_via = "https://github.com/ScavieFae/simple-loop/commit/abc123ef"
            paths = self._setup_pending_merges(tmp, brief_id)

            # First close
            result1 = close_as_delivered(paths, brief_id, delivered_via, reason="first close")
            self.assertTrue(result1)

            events_path = tmp / ".loop" / "state" / "runtime-events.jsonl"
            events_after_first = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
            sup_count_after_first = sum(
                1 for e in events_after_first
                if e.get("event") == "superseded" and e.get("brief") == brief_id
            )

            # Second close — idempotent re-run
            result2 = close_as_delivered(paths, brief_id, delivered_via, reason="second close")
            self.assertTrue(result2, "Idempotent re-run must return True")

            events_after_second = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
            sup_count_after_second = sum(
                1 for e in events_after_second
                if e.get("event") == "superseded" and e.get("brief") == brief_id
            )
            self.assertEqual(sup_count_after_first, sup_count_after_second,
                             "Idempotent re-run must not append another superseded event")


# ── Superseded projector routing ──────────────────────────────────────────────

class TestSupersededProjectorRouting(unittest.TestCase):

    def test_superseded_card_routes_to_history(self):
        """Card Status: superseded → projected into history[] with delivered_via."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-sup-1"
            _make_card(tmp, brief_id, "Status: superseded\n")

            events = [
                {"ts": "2026-06-09T10:00:00Z", "event": "superseded", "brief": brief_id,
                 "delivered_via": "https://github.com/org/repo/pull/42", "reason": "landed via PR"},
            ]
            result = project_running_json(str(tmp), events=events)

            history_briefs = [e["brief"] for e in result["history"]]
            self.assertIn(brief_id, history_briefs, "Superseded card must appear in history[]")

            entry = next(e for e in result["history"] if e["brief"] == brief_id)
            self.assertEqual(entry["status"], "superseded")
            self.assertEqual(entry["delivered_via"], "https://github.com/org/repo/pull/42")

    def test_superseded_card_not_in_active_or_pending(self):
        """Superseded card must not appear in active[], awaiting_review[], or pending_merges[]."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-sup-2"
            _make_card(tmp, brief_id, "Status: superseded\n")

            events = [
                {"ts": "2026-06-09T10:00:00Z", "event": "superseded", "brief": brief_id,
                 "delivered_via": "https://github.com/org/repo/commit/abc123", "reason": ""},
            ]
            result = project_running_json(str(tmp), events=events)

            for bucket in ("active", "awaiting_review", "pending_merges"):
                bucket_briefs = [e.get("brief") for e in result[bucket]]
                self.assertNotIn(brief_id, bucket_briefs,
                                 f"Superseded card must not appear in {bucket}[]")


# ── Integration: the gate wired THROUGH the real transition writers ──────────
# These drive move_to_awaiting_review / move_to_pending_merges / move_to_eval
# against a fixture state dir. If the _check_delivered_gate call is deleted
# from any writer, the corresponding refusal test below FAILS (the transition
# would wrongly proceed).

def _fake_subprocess_run(gh_rc=0):
    """Mock at the subprocess boundary (not the gate function): git rev-list
    reports 2 commits (satisfies the cycle gate), gh api returns gh_rc."""
    import subprocess as sp

    def run(args, **kwargs):
        prog = args[0] if args else ""
        if prog == "git" and "rev-list" in args:
            return sp.CompletedProcess(args, 0, stdout="2\n", stderr="")
        if prog == "gh":
            out = "{}" if gh_rc == 0 else ""
            return sp.CompletedProcess(args, gh_rc, stdout=out, stderr="" if gh_rc == 0 else "Not Found")
        return sp.CompletedProcess(args, 0, stdout="", stderr="")

    return run


class TestGateWiredThroughWriters(unittest.TestCase):

    def _setup_active(self, tmp: Path, brief_id: str, frontmatter: str, progress: dict) -> dict:
        """Fixture: a dispatched (active) brief with a card + worktree progress.json."""
        card_dir = tmp / "wiki" / "briefs" / "cards" / brief_id
        card_dir.mkdir(parents=True, exist_ok=True)
        (card_dir / "index.md").write_text(
            f"---\nID: {brief_id}\nBranch: {brief_id}\nStatus: active\n{frontmatter}---\n\n# {brief_id}\n"
        )
        (tmp / ".loop" / "state").mkdir(parents=True, exist_ok=True)
        _write_events(tmp, [
            {"ts": "2026-06-09T09:00:00Z", "event": "dispatched", "brief": brief_id,
             "branch": brief_id, "brief_file": f"wiki/briefs/cards/{brief_id}/index.md",
             "worker_slot": 1},
        ])
        _make_progress(tmp, brief_id, progress)
        paths = init_paths(str(tmp))
        write_running_json(str(tmp))
        return paths

    _COMPLETE_PROGRESS = {"status": "complete", "iteration": 1, "tasks_remaining": []}

    def test_awaiting_review_refused_without_delivered(self):
        """(a) Cross-repo brief (real string `Target-repo: nt-runway + Modal`),
        no delivered → move_to_awaiting_review REFUSES through the real writer;
        brief stays in active[]. Deleting the gate call from the writer makes
        this fail."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-int-a"
            paths = self._setup_active(tmp, brief_id, "Target-repo: nt-runway + Modal\n",
                                       dict(self._COMPLETE_PROGRESS))

            result = move_to_awaiting_review(paths, brief_id, "complete")

            self.assertFalse(result, "Writer must refuse the transition without delivered refs")
            rc = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in rc["active"]},
                          "Brief must remain in active[] after refusal")
            self.assertNotIn(brief_id, {e["brief"] for e in rc["awaiting_review"]})

    def test_pending_merges_refused_without_delivered_edit_surface_only(self):
        """(a, union) Cross-repo signal carried ONLY by Edit-surface (the field the
        brief card spec'd) → move_to_pending_merges REFUSES through the real writer."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-int-b"
            fm = (
                "Edit-surface:\n"
                "  - /Users/mattie-newtheory/new-theory/nt-runway/serve_nt0.py\n"
                "  - apps/console/\n"
            )
            paths = self._setup_active(tmp, brief_id, fm, dict(self._COMPLETE_PROGRESS))

            result = move_to_pending_merges(paths, brief_id)

            self.assertFalse(result)
            rc = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in rc["active"]})
            self.assertNotIn(brief_id, {e["brief"] for e in rc["pending_merges"]})

    def test_awaiting_review_proceeds_with_delivered(self):
        """(b) Same brief WITH a delivered ref, gh/git mocked at the subprocess
        boundary → the real writer proceeds; brief lands in awaiting_review[]."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-int-c"
            progress = dict(self._COMPLETE_PROGRESS)
            progress["delivered"] = {
                "nt-runway": "https://github.com/new-theory-research/nt-runway/commit/abc123def456",
            }
            paths = self._setup_active(tmp, brief_id, "Target-repo: nt-runway + Modal\n", progress)

            with patch("shutil.which", return_value="/usr/bin/gh"), \
                 patch("subprocess.run", new=_fake_subprocess_run(gh_rc=0)):
                result = move_to_awaiting_review(paths, brief_id, "complete")

            self.assertTrue(result, "Writer must proceed when delivered ref verifies")
            rc = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in rc["awaiting_review"]})
            self.assertNotIn(brief_id, {e["brief"] for e in rc["active"]})

    def test_pending_merges_proceeds_with_delivered(self):
        """(b) Auto-merge path: delivered ref present + verifiable → brief lands
        in pending_merges[]."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-int-d"
            progress = dict(self._COMPLETE_PROGRESS)
            progress["delivered"] = {
                "nt-runway": "https://github.com/new-theory-research/nt-runway/commit/abc123def456",
            }
            paths = self._setup_active(tmp, brief_id, "Target-repo: nt-runway\n", progress)

            with patch("shutil.which", return_value="/usr/bin/gh"), \
                 patch("subprocess.run", new=_fake_subprocess_run(gh_rc=0)):
                result = move_to_pending_merges(paths, brief_id)

            self.assertTrue(result)
            rc = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in rc["pending_merges"]})

    def test_portal_only_with_annotation_never_gated(self):
        """(c) Real string `Target-repo: portal (apps/docs)` (brief-231) → the
        annotation must NOT gate a portal-only brief; transition proceeds with
        no delivered field at all."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-int-e"
            paths = self._setup_active(tmp, brief_id, "Target-repo: portal (apps/docs)\n",
                                       dict(self._COMPLETE_PROGRESS))

            result = move_to_awaiting_review(paths, brief_id, "complete")

            self.assertTrue(result, "portal (apps/docs) is portal-only — must never gate")
            rc = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in rc["awaiting_review"]})

    def test_move_to_eval_legacy_path_is_gated(self):
        """Fast-follow (a): move_to_eval routes into awaiting_review too — the
        legacy path must run the same gate, not stay an unguarded back door."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            brief_id = "brief-int-f"
            paths = self._setup_active(tmp, brief_id, "Target-repo: nt-runway\n",
                                       dict(self._COMPLETE_PROGRESS))

            result = move_to_eval(paths, brief_id)

            self.assertFalse(result, "move_to_eval must refuse cross-repo briefs without delivered")
            rc = project_running_json(str(tmp))
            self.assertIn(brief_id, {e["brief"] for e in rc["active"]})


if __name__ == "__main__":
    unittest.main()
