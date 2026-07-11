"""Tests for the three-way prompt refresh (issues #20, #57).

Covers the four classification outcomes the brief names, plus the apply layer:
  * identical            -> in_sync, file untouched
  * template-newer       -> updated, file overwritten with the new template
  * locally customized   -> customized, file preserved (differs from BOTH baselines)
  * missing baseline     -> drift, file preserved (loud-fail equivalent for prompts)
  * missing project copy -> created (daemon-required file is never silently skipped)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import update_prompts as up


# ── classify() — the pure three-way decision ────────────────────────────────

def test_identical_is_in_sync():
    assert up.classify(new="A", project="A", old="A") == up.IN_SYNC
    # in sync even when the baseline is older — project already matches new
    assert up.classify(new="A", project="A", old="OLD") == up.IN_SYNC


def test_template_newer_unmodified_project_updates():
    # project still matches the old template; template moved on -> safe overwrite
    assert up.classify(new="NEW", project="OLD", old="OLD") == up.UPDATED


def test_locally_customized_is_preserved():
    # project differs from BOTH old and new -> a real local edit, do not clobber
    assert up.classify(new="NEW", project="CUSTOM", old="OLD") == up.CUSTOMIZED


def test_customized_even_when_template_unchanged():
    # template didn't move (new == old) but project diverged -> still customized
    assert up.classify(new="SAME", project="CUSTOM", old="SAME") == up.CUSTOMIZED


def test_missing_baseline_is_drift():
    # no baseline (legacy project / missing PROVENANCE) and project != new
    assert up.classify(new="NEW", project="CUSTOM", old=None) == up.DRIFT
    # ...but an exact match to new is still in_sync even without a baseline
    assert up.classify(new="NEW", project="NEW", old=None) == up.IN_SYNC


def test_missing_project_copy_is_created():
    assert up.classify(new="NEW", project=None, old="OLD") == up.CREATED
    assert up.classify(new="NEW", project=None, old=None) == up.CREATED


# ── refresh() — the apply layer over a real directory pair ───────────────────

def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _setup(tmp_path):
    templates = os.path.join(tmp_path, "templates")
    prompts = os.path.join(tmp_path, "prompts")
    os.makedirs(templates)
    os.makedirs(prompts)
    return templates, prompts


def test_refresh_identical_leaves_file_untouched(tmp_path):
    t, p = _setup(str(tmp_path))
    _write(os.path.join(t, "queen.md"), "SAME")
    _write(os.path.join(p, "queen.md"), "SAME")
    results = up.refresh(t, p, {"queen.md": "SAME"})
    assert results == [{"name": "queen.md", "status": up.IN_SYNC}]
    with open(os.path.join(p, "queen.md")) as f:
        assert f.read() == "SAME"


def test_refresh_template_newer_overwrites(tmp_path):
    t, p = _setup(str(tmp_path))
    _write(os.path.join(t, "queen.md"), "NEW")
    _write(os.path.join(p, "queen.md"), "OLD")
    results = up.refresh(t, p, {"queen.md": "OLD"})
    assert results == [{"name": "queen.md", "status": up.UPDATED}]
    with open(os.path.join(p, "queen.md")) as f:
        assert f.read() == "NEW"


def test_refresh_customized_is_not_clobbered(tmp_path):
    t, p = _setup(str(tmp_path))
    _write(os.path.join(t, "queen.md"), "NEW")
    _write(os.path.join(p, "queen.md"), "MY LOCAL EDIT")
    results = up.refresh(t, p, {"queen.md": "OLD"})
    assert results == [{"name": "queen.md", "status": up.CUSTOMIZED}]
    with open(os.path.join(p, "queen.md")) as f:
        assert f.read() == "MY LOCAL EDIT"  # preserved


def test_refresh_missing_baseline_preserves_project(tmp_path):
    t, p = _setup(str(tmp_path))
    _write(os.path.join(t, "queen.md"), "NEW")
    _write(os.path.join(p, "queen.md"), "LEGACY LOCAL")
    results = up.refresh(t, p, {})  # no baseline recorded
    assert results == [{"name": "queen.md", "status": up.DRIFT}]
    with open(os.path.join(p, "queen.md")) as f:
        assert f.read() == "LEGACY LOCAL"  # never overwritten without a baseline


def test_refresh_creates_missing_daemon_required_file(tmp_path):
    t, p = _setup(str(tmp_path))
    _write(os.path.join(t, "worker.md"), "WORKER TEMPLATE")
    # project copy absent — daemon requires it, so it must be created, not skipped
    results = up.refresh(t, p, {})
    assert results == [{"name": "worker.md", "status": up.CREATED}]
    with open(os.path.join(p, "worker.md")) as f:
        assert f.read() == "WORKER TEMPLATE"


def test_refresh_dry_run_writes_nothing(tmp_path):
    t, p = _setup(str(tmp_path))
    _write(os.path.join(t, "queen.md"), "NEW")
    _write(os.path.join(p, "queen.md"), "OLD")
    results = up.refresh(t, p, {"queen.md": "OLD"}, apply=False)
    assert results == [{"name": "queen.md", "status": up.UPDATED}]
    with open(os.path.join(p, "queen.md")) as f:
        assert f.read() == "OLD"  # dry-run left it alone


def test_refresh_reports_every_file_by_name(tmp_path):
    t, p = _setup(str(tmp_path))
    _write(os.path.join(t, "queen.md"), "Q")
    _write(os.path.join(t, "worker.md"), "W")
    _write(os.path.join(p, "queen.md"), "Q")
    results = up.refresh(t, p, {"queen.md": "Q", "worker.md": None})
    names = {r["name"] for r in results}
    assert names == {"queen.md", "worker.md"}  # nothing silently skipped
