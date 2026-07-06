#!/usr/bin/env python3
"""Daemon-side state transitions — mechanical operations that don't need Claude.

Called by daemon.sh to execute deterministic state changes
(JSON splices, git branch operations).

Usage:
    python3 lib/actions.py move-to-eval <brief_id> <project_dir>
    python3 lib/actions.py dispatch <project_dir>
    python3 lib/actions.py merge <project_dir>
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


# Brief-014: token redactor. Applied to any text destined for escalate.json
# or log files that could contain raw git stderr. Patterns match GitHub-issued
# credentials in their canonical forms (classic personal tokens, OAuth user
# tokens, server tokens, fine-grained PATs). Belt-and-suspenders — even if we
# never write these normally, a push-fail path could hand us a header echo.
_REDACT_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"ghs_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
]


def redact_secrets(text):
    """Redact GitHub tokens from arbitrary text. Returns sanitized string.

    Never raises. Input that isn't a str is returned as-is (caller's problem).
    """
    if not isinstance(text, str):
        return text
    out = text
    for pat in _REDACT_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def init_paths(project_dir):
    """Initialize paths from project directory."""
    loop_dir = os.path.join(project_dir, ".loop")
    state_dir = os.path.join(loop_dir, "state")
    signals_dir = os.path.join(state_dir, "signals")
    return {
        "project_dir": project_dir,
        "loop_dir": loop_dir,
        "state_dir": state_dir,
        "signals_dir": signals_dir,
        "worktrees_dir": os.path.join(loop_dir, "worktrees"),
        "running_file": os.path.join(state_dir, "running.json"),
        "pending_dispatch": os.path.join(state_dir, "pending-dispatch.json"),
        "pending_merge": os.path.join(state_dir, "pending-merge.json"),
        "log_file": os.path.join(state_dir, "log.jsonl"),
        "progress_file": os.path.join(state_dir, "progress.json"),
    }


def read_config(loop_dir):
    """Read config.sh values into a dict."""
    config = {
        "GIT_REMOTE": "origin",
        "GIT_MAIN_BRANCH": "main",
    }
    config_file = os.path.join(loop_dir, "config.sh")
    if os.path.exists(config_file):
        with open(config_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                config[key] = val
    return config


def log_action(paths, action, details):
    """Append to log.jsonl."""
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": f"daemon:{action}",
        **details,
    }
    with open(paths["log_file"], "a") as f:
        f.write(json.dumps(entry) + "\n")


def signal_dedup_clear(paths, brief_id):
    """Write a signal file so the daemon clears its dedup cache entry for this brief."""
    signals_dir = paths.get("signals_dir", os.path.join(paths["state_dir"], "signals"))
    os.makedirs(signals_dir, exist_ok=True)
    signal_file = os.path.join(signals_dir, f"dedup-clear-{brief_id}.json")
    with open(signal_file, "w") as f:
        json.dump({"brief": brief_id}, f)
        f.write("\n")


def git(project_dir, *args, check=True):
    """Run a git command in the project directory."""
    result = subprocess.run(
        ["git", "-C", project_dir] + list(args),
        capture_output=True, text=True, timeout=60,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, f"git {' '.join(args)}",
            output=result.stdout, stderr=result.stderr,
        )
    return result


def load_running(paths):
    """Load running.json, backfilling v2 fields with defaults for backward compatibility."""
    with open(paths["running_file"]) as f:
        rc = json.load(f)
    rc.setdefault("pending_merges", [])
    rc.setdefault("awaiting_review", [])
    return rc


# ─── brief-034 concurrency: frontmatter parse + overlap detection ────

# Match both prose form (`**Parallel-safe:** true`) and YAML-frontmatter form
# (`Parallel-safe: true`). Pre-fix this only matched the prose form, which
# silently parsed every YAML-frontmatter card as parallel_safe=False — meaning
# THROTTLE>1 has been dead since brief-108 collapsed cards to YAML.
PARALLEL_SAFE_LINE_RE = re.compile(r"^\s*(?:\*\*Parallel-safe:\*\*|Parallel-safe:)\s*(\S+)", re.IGNORECASE)
EDIT_SURFACE_LINE_RE = re.compile(r"^\s*(?:\*\*Edit-surface:\*\*|Edit-surface:)\s*(.*?)\s*$", re.IGNORECASE)
TARGET_REPO_LINE_RE = re.compile(r"^\s*(?:\*\*Target-repo:\*\*|Target-repo:)\s*(.*?)\s*$", re.IGNORECASE)
# Match both YAML-frontmatter form (`Model: opus`) and legacy bold-markdown form
# (`**Model:** opus`). YAML form was added with the brief-108 card collapse; the
# original bash grep only matched the bold form, silently ignoring every YAML card
# (live receipt 2026-06-11: brief-249 frontmatter `Model: opus`, worker ran sonnet).
MODEL_LINE_RE = re.compile(r"^\s*(?:\*\*Model:\*\*|Model:)\s*(\S+)", re.IGNORECASE)
_ALLOWED_WORKER_MODELS = {"sonnet", "opus", "haiku"}
_DEFAULT_WORKER_MODEL = "sonnet"


def _normalize_surface_path(p):
    p = p.strip()
    if p.startswith("./"):
        p = p[2:]
    return p


def _paths_overlap(a, b):
    """Pair-wise overlap check for two edit-surface paths.

    Exact match, directory-prefix (trailing /), or fnmatch-style glob match
    (bidirectional) all count as overlap. Empty string on either side → overlap
    (claims-everything sentinel is handled by the list-level caller).
    """
    import fnmatch
    a = _normalize_surface_path(a)
    b = _normalize_surface_path(b)
    if not a or not b:
        return True
    if a == b:
        return True
    if "*" in a and fnmatch.fnmatch(b, a):
        return True
    if "*" in b and fnmatch.fnmatch(a, b):
        return True
    if a.endswith("/") and (b.startswith(a) or b == a.rstrip("/")):
        return True
    if b.endswith("/") and (a.startswith(b) or a == b.rstrip("/")):
        return True
    return False


def edit_surfaces_overlap(a, b):
    """True if any path in list a overlaps any path in list b.

    Empty list = claims-everything = always overlaps.
    """
    if not a or not b:
        return True
    for pa in a:
        for pb in b:
            if _paths_overlap(pa, pb):
                return True
    return False


def parse_concurrency_frontmatter(brief_file_path):
    """Parse Parallel-safe + Edit-surface fields from a brief markdown file.

    Returns (parallel_safe: bool, edit_surface: list[str]).
    Missing/unparseable → (False, []), equivalent to pre-034 "runs alone".
    Template placeholders matching "[...]" are filtered out of edit_surface.
    """
    parallel_safe = False
    edit_surface = []
    if not brief_file_path or not os.path.exists(brief_file_path):
        return parallel_safe, edit_surface
    try:
        with open(brief_file_path) as f:
            lines = f.readlines()
    except (IOError, OSError):
        return parallel_safe, edit_surface

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        ps_m = PARALLEL_SAFE_LINE_RE.match(line)
        if ps_m:
            val = ps_m.group(1).strip().strip('"').strip("'").lower()
            parallel_safe = (val == "true")
            i += 1
            continue

        es_m = EDIT_SURFACE_LINE_RE.match(line)
        if es_m:
            inline = es_m.group(1).strip()
            if inline:
                edit_surface = [
                    p.strip() for p in inline.split(",")
                    if p.strip() and not (p.strip().startswith("[") and p.strip().endswith("]"))
                ]
                i += 1
                continue
            j = i + 1
            while j < n:
                next_line = lines[j]
                item_m = re.match(r"^\s+-\s*(.+?)\s*$", next_line)
                if item_m:
                    item = item_m.group(1).strip()
                    if item and not (item.startswith("[") and item.endswith("]")):
                        edit_surface.append(item)
                    j += 1
                    continue
                if next_line.strip() == "":
                    j += 1
                    continue
                break
            i = j
            continue
        i += 1

    return parallel_safe, edit_surface


def save_running(paths, data):
    """DEPRECATED — kept for compat with callers we haven't migrated.

    brief-108-d: running.json is a projected file. Direct writes are forbidden
    by lint outside `state.write_running_json` (the projector's owner). This
    function now projects from cards + runtime-events.jsonl and writes the
    result, ignoring the `data` argument. The rc-mutation pattern that used
    to live in this module is being torn out cycle-by-cycle.

    harness-001/003: running.json is daemon-local — never committed. The
    git add + commit that used to live here are removed.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from state import write_running_json
    write_running_json(paths["project_dir"])


def project_running(paths, lane=None):
    """Project running.json from cards + runtime-events.jsonl.

    The single canonical writer of running.json. Returns the projected dict.
    Lint enforces: no direct writes to running.json outside this path.

    Args:
        paths: init_paths() dict.
        lane: optional program-lane partition key (harness-001/003). When set
            (non-empty), only briefs in this lane appear in active/
            awaiting_review/pending_merges. When None (the default), the value
            is read from the LOOP_LANE environment variable — so action
            handlers spawned by a lane-scoped daemon.sh automatically project
            the correct lane without needing each call site to pass lane
            explicitly. Empty env var → None → global projection (single-
            daemon, backward-compat). Passed through to state.write_running_json.
    """
    if lane is None:
        lane = os.environ.get("LOOP_LANE") or None
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from state import write_running_json
    return write_running_json(paths["project_dir"], lane=lane)


def runtime_event(paths, event_type, brief, **fields):
    """Append a runtime-event line to .loop/state/runtime-events.jsonl.

    Convenience wrapper around state.append_event that pulls project_dir from
    paths. Use this from action handlers — runtime facts (dispatched_at,
    completed_at, merge_sha, worker_slot, kind, reason) belong in the events
    log, NOT in running.json (which the projector regenerates).
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from state import append_event
    return append_event(paths["project_dir"], event_type, brief, **fields)


# ─── Re-queued briefs (brief-102) ───────────────────────────────────

_BLOCKED_ON_RE = re.compile(r"^\s*\*\*Blocked-on:\*\*\s+(brief-\d+(?:-[\w-]+)?)", re.MULTILINE)
_LIST_ITEM_BRIEF_RE = re.compile(r"^\s{0,3}\d+\.\s+\*{0,2}(brief-\d+[\w-]*)")


def _brief_id_matches(a: str, b: str) -> bool:
    """True when a and b refer to the same brief (handles truncated vs full IDs).

    running.json history often has truncated IDs ("brief-102") while goals.md
    and the filesystem use full slugs ("brief-102-loop-status-blocked-state-surface").
    Matching is symmetric: either argument can be the truncated form.
    """
    return a == b or a.startswith(b + "-") or b.startswith(a + "-")


def parse_requeued_briefs(goals_path, running_path=None):
    """Parse goals.md for re-queued entries with a **Blocked-on:** marker.

    Returns a list of dicts:
      { brief_id, blocked_on, description, ready_to_dispatch }

    ready_to_dispatch is True when the blocking brief appears in
    running.json#history[] with a merge_sha (precondition cleared).
    """
    try:
        with open(goals_path) as f:
            contents = f.read()
    except OSError:
        return []

    # Build set of merged brief IDs by scanning cards for Status: merged.
    # Card-is-truth: running.json history[] is no longer the authoritative source.
    merged_briefs = set()
    try:
        import pathlib
        goals_parent = pathlib.Path(goals_path).resolve().parent  # .loop/state
        project_dir = goals_parent.parent.parent  # project root
        cards_dir = project_dir / "wiki" / "briefs" / "cards"
        if cards_dir.is_dir():
            for card_dir in cards_dir.iterdir():
                if not card_dir.is_dir() or card_dir.name.startswith('.'):
                    continue
                index_path = card_dir / "index.md"
                if not index_path.exists():
                    continue
                try:
                    content = index_path.read_text()
                    for line in content.splitlines():
                        low = line.lower().strip()
                        if low.startswith("status:") or "**status:**" in low:
                            val = low.split("status:")[-1].strip().strip("*.,;").strip()
                            if val == "merged":
                                merged_briefs.add(card_dir.name)
                            break
                except Exception:
                    pass
    except Exception:
        pass

    results = []
    lines = contents.splitlines()
    current_entry = None  # (brief_id, description)

    for line in lines:
        # Top-level numbered list items introduce a new brief entry.
        lm = _LIST_ITEM_BRIEF_RE.match(line)
        if lm:
            current_entry = (lm.group(1), line.strip())
            continue

        # Look for **Blocked-on:** on continuation lines (indented).
        bm = _BLOCKED_ON_RE.match(line)
        if bm and current_entry:
            blocked_on = bm.group(1)
            brief_id, raw_desc = current_entry
            # Extract one-line description: strip numbering + markdown emphasis.
            desc = re.sub(r"^\d+\.\s+", "", raw_desc)
            desc = re.sub(r"\*+", "", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."
            results.append({
                "brief_id": brief_id,
                "blocked_on": blocked_on,
                "description": desc,
                "ready_to_dispatch": any(_brief_id_matches(blocked_on, m) for m in merged_briefs),
            })
            current_entry = None  # consume — don't double-emit

    return results


# ─── Human queue summary (brief-021) ────────────────────────────────

_CREDENTIAL_GATE_RE = re.compile(r"\*\*Requires\b", re.IGNORECASE)
_REQUIRES_KEYWORD_RE = re.compile(r"Requires[^*\n]*:\**\s*(.+?)(?:\s*$|\s{2,})", re.IGNORECASE)

# Artifact flavors checked in priority order.
_ARTIFACT_FLAVORS = ("smoke", "review", "escalation")


def _find_handoff_artifact(project_dir, brief_id, wiki_port):
    """Return (artifact_url, artifact_missing) for a brief's handoff artifact.

    Scans wiki/briefs/cards/{brief_id}/ for smoke.md, review.md, escalation.md
    in that order. Returns the Zensical URL of the first found, or (None, True)
    if none exists. wiki_port is read from WIKI_PORT in config.sh (default 8002).
    """
    card_dir = os.path.join(project_dir, "wiki", "briefs", "cards", brief_id)
    for flavor in _ARTIFACT_FLAVORS:
        if os.path.exists(os.path.join(card_dir, f"{flavor}.md")):
            url = f"http://localhost:{wiki_port}/briefs/cards/{brief_id}/{flavor}/"
            return (url, False)
    return (None, True)


def human_queue_summary(paths):
    """Return human-gated items from three sources.

    Each item: {source, brief_id, summary, action_hint, artifact_url,
    artifact_missing}. Three sources: awaiting_review[] from running.json,
    live signal files, and credential-gated entries in goals.md. Returns
    empty list when nothing is waiting — callers suppress the section
    entirely in that case.

    artifact_url is the Zensical URL of the brief's handoff artifact
    (smoke/review/escalation.md) when it exists, else None.
    artifact_missing is True for awaiting_review/escalate items with no
    handoff artifact present — signal to show a warning.
    credential-gated items always have artifact_missing=False (no gate yet).
    """
    project_dir = paths.get("project_dir", "")
    loop_dir = paths.get("loop_dir", os.path.join(project_dir, ".loop"))
    config = read_config(loop_dir)
    wiki_port = config.get("WIKI_PORT", "8002")

    items = []

    # Source A: awaiting_review[] from running.json
    try:
        rc = load_running(paths)
        for entry in rc.get("awaiting_review", []):
            brief_id = entry.get("brief", "")
            if not brief_id:
                continue
            reason = entry.get("reason", "human approval needed")
            # Backfill: entries written before brief-100 carry no kind field.
            kind = entry.get("kind", "unknown")
            if kind == "complete":
                disposition = "ready for review"
            else:
                disposition = "needs daemon-side disposition"
            artifact_url, artifact_missing = _find_handoff_artifact(project_dir, brief_id, wiki_port)
            items.append({
                "source": "awaiting_review",
                "brief_id": brief_id,
                "kind": kind,
                "queue_steward_disposition": disposition,
                "summary": reason[:60],
                "action_hint": f"loop approve {brief_id}",
                "artifact_url": artifact_url,
                "artifact_missing": artifact_missing,
            })
    except Exception:
        pass

    # Source B: live signal files in signals/ — skip archived-suffix and pause.json
    try:
        signals_dir = os.path.join(paths["state_dir"], "signals")
        if os.path.isdir(signals_dir):
            for fname in sorted(os.listdir(signals_dir)):
                if not fname.endswith(".json"):
                    continue
                if ".resolved-" in fname or ".archived-" in fname:
                    continue
                if fname == "pause.json":
                    continue
                sig_path = os.path.join(signals_dir, fname)
                try:
                    with open(sig_path) as f:
                        sig = json.load(f)
                except Exception:
                    continue
                brief_id = sig.get("brief", os.path.splitext(fname)[0])
                category = sig.get("category", sig.get("type", fname.replace(".json", "")))
                raw_summary = sig.get("summary", sig.get("reason", category))
                artifact_url, artifact_missing = _find_handoff_artifact(project_dir, brief_id, wiki_port)
                items.append({
                    "source": "escalate",
                    "brief_id": brief_id,
                    "summary": str(raw_summary)[:60],
                    "action_hint": f"resolve signals/{fname}",
                    "artifact_url": artifact_url,
                    "artifact_missing": artifact_missing,
                })
    except Exception:
        pass

    # Source C: credential-gated items in goals.md
    # Two detection paths: section-based (## Credential-gated heading) and
    # keyword-based (**Requires**) for bullets in any section.
    # Only bullet items are considered — prose paragraphs are skipped to avoid
    # false positives from mentions of "credential-gated" in description text.
    try:
        goals_file = os.path.join(paths["state_dir"], "goals.md")
        if os.path.exists(goals_file):
            with open(goals_file) as f:
                in_credential_section = False
                for line in f:
                    stripped = line.lstrip()
                    if stripped.startswith('#'):
                        in_credential_section = 'credential-gated' in line.lower()
                        continue
                    is_bullet = stripped.startswith('-') or stripped.startswith('*') or (
                        stripped and stripped[0].isdigit() and '. ' in stripped[:4]
                    )
                    if not is_bullet:
                        continue
                    if not in_credential_section and not _CREDENTIAL_GATE_RE.search(line):
                        continue
                    m = re.search(r"brief-\d+-[\w-]+", line)
                    if not m:
                        m = re.search(r"brief-\d+", line)
                    brief_id = m.group(0) if m else ""
                    if not brief_id:
                        continue
                    kw_m = _REQUIRES_KEYWORD_RE.search(line)
                    keyword = kw_m.group(1).strip()[:60] if kw_m else "credentials required"
                    items.append({
                        "source": "credential-gated",
                        "brief_id": brief_id,
                        "summary": keyword,
                        "action_hint": None,
                        "artifact_url": None,
                        "artifact_missing": False,
                    })
    except Exception:
        pass

    return items


# ─── Delivered gate (brief-237) ─────────────────────────────────────

# Infra surfaces that appear in Target-repo:/Edit-surface: but are NOT git
# repos — there is no commit URL to verify, so the gate skips them.
# Case-insensitive membership check.
NON_GIT_TARGETS = {"modal", "railway", "vercel"}

# Placeholder tokens observed in live cards (e.g. `Target-repo: TBD (...)`,
# `Depends-on: _none_`) — not repos; never gate on them.
PLACEHOLDER_TOKENS = {"tbd", "none", "n/a", "na", "_none_"}

# Known sibling repos → GitHub remotes. Used for the gh-free `git ls-remote`
# fallback and for verifying plain-SHA delivered refs (where the ref alone
# doesn't name an org). Extend when a new sibling repo joins the ecosystem.
KNOWN_REPO_REMOTES = {
    "nt-runway": "https://github.com/new-theory-research/nt-runway",
    "newt-python": "https://github.com/new-theory-research/newt-python",
    "newt-starter-trossen-widowx": "https://github.com/new-theory-research/newt-starter-trossen-widowx",
    "newt-starter-yam": "https://github.com/new-theory-research/newt-starter-yam",
    "imitation_learning": "https://github.com/new-theory-research/imitation_learning",
    "simple-loop": "https://github.com/ScavieFae/simple-loop",
}

_ANNOTATION_RE = re.compile(r"\([^)]*\)")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_GH_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/(commit|pull)/([^/\s]+)")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _normalize_repo_token(token):
    """One raw token → bare external repo name, or None if it isn't one.

    Handles the observed card grammar:
      - parenthetical annotations: `portal (apps/docs)`, `newt-python (new)`
      - org-prefixed names: `new-theory-research/newt-python` → `newt-python`
      - portal itself (the host repo — never gated)
      - non-git infra surfaces (Modal/Railway/Vercel — nothing to verify)
    """
    t = _ANNOTATION_RE.sub(" ", token).strip().strip("`*").strip()
    if "/" in t:
        t = t.rstrip("/").rsplit("/", 1)[-1].strip()
    if not t or not _REPO_NAME_RE.match(t):
        return None
    tl = t.lower()
    if tl == "portal" or tl in NON_GIT_TARGETS or tl in PLACEHOLDER_TOKENS:
        return None
    return t


def _parse_target_repo(brief_file_path):
    """Parse Target-repo: frontmatter. Returns list of external (non-portal) repo names.

    Observed grammar across live portal cards (the parser is written against
    these real strings, not an idealized format):
      Target-repo: portal (apps/docs)
      Target-repo: nt-runway + Modal
      Target-repo: nt-runway + newt-python + Railway
      Target-repo: nt-runway + new-theory-research/newt-python (new) + portal
      Target-repo: newt-starter-trossen-widowx (+ newt-starter-yam if same pattern)
      Target-repo: portal+nt-runway

    Rule: strip parenthetical annotations FIRST (they can contain '+'), then
    split on comma or '+', then normalize each token (org-prefix → bare name,
    drop portal and non-git infra targets). Absent/empty field → [].
    """
    if not brief_file_path or not os.path.exists(brief_file_path):
        return []
    try:
        with open(brief_file_path) as f:
            for line in f:
                m = TARGET_REPO_LINE_RE.match(line)
                if m:
                    raw = _ANNOTATION_RE.sub(" ", m.group(1).strip())
                    repos = []
                    for tok in re.split(r"[,+]", raw):
                        r = _normalize_repo_token(tok)
                        if r and r not in repos:
                            repos.append(r)
                    return repos
    except (IOError, OSError):
        pass
    return []


def _edit_surface_entry_repo(entry):
    """One Edit-surface list entry → bare external repo name, or None.

    Observed entry shapes that name a sibling repo:
      - /Users/<user>/new-theory/nt-runway/serve_nt0.py   (absolute path through
        the sibling-checkout parent dir — repo is the component after new-theory/)
      - simple-loop lib/actions.py                        (bare repo name + path)
      - Railway (new always-on service + config)          (infra target — skipped)

    Everything else (relative portal paths like `apps/console/`, bare filenames
    like `closeout.md`) is portal-internal → None.
    """
    m = re.search(r"/new-theory/([A-Za-z0-9._-]+)", entry)
    if m:
        return _normalize_repo_token(m.group(1))
    stripped = _ANNOTATION_RE.sub(" ", entry).strip()
    first = stripped.split()[0] if stripped.split() else ""
    if first and "/" not in first and "." not in first:
        return _normalize_repo_token(first)
    return None


def _parse_edit_surface_repos(brief_file_path):
    """Parse Edit-surface: frontmatter (inline or block-list form) → external repo names.

    Block form (the common one):
        Edit-surface:
          - /Users/.../new-theory/nt-runway/serve_nt0.py
          - apps/console/
    Inline form is parsed with the same token rules as Target-repo.
    Only the first Edit-surface: block in the file is read (frontmatter).
    """
    if not brief_file_path or not os.path.exists(brief_file_path):
        return []
    repos = []
    try:
        with open(brief_file_path) as f:
            in_block = False
            for line in f:
                if in_block:
                    item = re.match(r"^\s+-\s+(.*\S)\s*$", line)
                    if item:
                        r = _edit_surface_entry_repo(item.group(1))
                        if r and r not in repos:
                            repos.append(r)
                        continue
                    break  # contiguous block ended
                m = EDIT_SURFACE_LINE_RE.match(line)
                if m:
                    inline = m.group(1).strip()
                    if inline:
                        raw = _ANNOTATION_RE.sub(" ", inline)
                        for tok in re.split(r"[,+]", raw):
                            r = _normalize_repo_token(tok)
                            if r and r not in repos:
                                repos.append(r)
                        return repos
                    in_block = True
    except (IOError, OSError):
        pass
    return repos


def _external_repos_for_brief(brief_file_path):
    """External repos a brief touches: UNION of Target-repo: and Edit-surface:.

    WHY the union (director call, 2026-06-09 review of brief-237): live cards
    are inconsistent about which field carries the cross-repo signal. The
    brief-237 card itself names the gate input as Edit-surface: (three times),
    while the first implementation keyed off Target-repo:. Some cards carry
    only one of the two; either field alone misses real cases. Gating on the
    deduplicated union — with the same parser, portal exclusion, and non-git
    skip applied to both — means a brief is gated iff ANY field names a
    sibling git repo, regardless of which convention the card author used.
    """
    repos = _parse_target_repo(brief_file_path)
    for r in _parse_edit_surface_repos(brief_file_path):
        if r not in repos:
            repos.append(r)
    return repos


def _verify_delivered_ref(ref, repo):
    """Verify a delivered ref (GitHub commit/PR URL, or plain commit SHA) on the remote.

    Returns (ok: bool, reason: str).

    Verification ladder:
      1. gh available → `gh api` the commit/PR (plain SHAs resolve via
         KNOWN_REPO_REMOTES to get the org).
      2. gh unavailable, plain SHA on a known repo → `git ls-remote` the remote
         and accept the SHA if it matches an advertised ref tip.
      3. Otherwise fall OPEN — but loudly: a warning line on stderr names the
         repo and the skipped verification. Never silently.
    """
    import shutil
    url_m = _GH_URL_RE.match(ref)
    sha_m = _SHA_RE.match(ref)
    if not url_m and not sha_m:
        return False, (
            f"not a recognized GitHub commit/PR URL or plain commit SHA: {ref!r}. "
            f"Expected https://github.com/<org>/{repo}/commit/<sha>, .../pull/<n>, or a bare SHA"
        )

    if shutil.which("gh"):
        if url_m:
            owner, repo_name, kind, gref = url_m.groups()
            api_path = (
                f"repos/{owner}/{repo_name}/commits/{gref}"
                if kind == "commit"
                else f"repos/{owner}/{repo_name}/pulls/{gref}"
            )
        else:
            remote = KNOWN_REPO_REMOTES.get(repo)
            if not remote:
                return False, (
                    f"plain SHA given for repo {repo!r} not in KNOWN_REPO_REMOTES — "
                    f"record a full commit URL (https://github.com/<org>/{repo}/commit/<sha>) instead"
                )
            owner_repo = remote.split("github.com/", 1)[1]
            api_path = f"repos/{owner_repo}/commits/{ref}"
        try:
            r = subprocess.run(
                ["gh", "api", api_path],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if r.returncode == 0:
                return True, ""
            return False, (
                f"gh api {api_path} → exit {r.returncode}. "
                f"If gh is unauthenticated, run `gh auth login`; "
                f"emergency override: SIMPLE_LOOP_SKIP_DELIVERED_GATE=1"
            )
        except subprocess.TimeoutExpired:
            return False, "gh api timed out"
        except Exception as e:
            return False, str(e)

    # gh unavailable — try the gh-free ls-remote fallback for plain SHAs on known repos.
    if sha_m and repo in KNOWN_REPO_REMOTES and shutil.which("git"):
        try:
            r = subprocess.run(
                ["git", "ls-remote", KNOWN_REPO_REMOTES[repo]],
                capture_output=True, text=True, timeout=20, check=False,
            )
            if r.returncode == 0:
                tips = {ln.split("\t", 1)[0] for ln in r.stdout.splitlines() if ln.strip()}
                if any(t.startswith(ref.lower()) for t in tips):
                    return True, ""
        except Exception:
            pass

    print(
        f"delivered-gate: gh unavailable — remote verification SKIPPED for {repo} "
        f"(ref {ref!r} accepted unverified)",
        file=sys.stderr,
    )
    return True, ""


def _check_delivered_gate(paths, brief_id, brief_file_path):
    """Refuse completion if cross-repo work isn't verifiably on the remote.

    For each external repo named in the UNION of Target-repo: and Edit-surface:
    frontmatter, checks that progress.json carries a 'delivered' dict entry
    (commit/PR URL or plain SHA) and that the ref is reachable on the remote.

    Returns (passed: bool, errors: list[str]).
    Passes immediately if the brief names no external repos.
    Escape hatch: SIMPLE_LOOP_SKIP_DELIVERED_GATE=1 skips the gate (loudly).
    """
    external_repos = _external_repos_for_brief(brief_file_path)
    if not external_repos:
        return True, []

    if os.environ.get("SIMPLE_LOOP_SKIP_DELIVERED_GATE") == "1":
        print(
            "=" * 72 + "\n"
            f"delivered-gate: SKIPPED for {brief_id} via SIMPLE_LOOP_SKIP_DELIVERED_GATE=1\n"
            f"  external repos NOT verified: {', '.join(external_repos)}\n"
            f"  if this brief's cross-repo work is not actually pushed, you are\n"
            f"  reintroducing the brief-230 failure mode. Unset the env var after use.\n"
            + "=" * 72,
            file=sys.stderr,
        )
        return True, []

    wt_progress = os.path.join(
        paths["worktrees_dir"], brief_id, ".loop", "state", "progress.json"
    )
    delivered = {}
    if os.path.exists(wt_progress):
        try:
            with open(wt_progress) as f:
                prog = json.load(f)
            delivered = prog.get("delivered") or {}
        except Exception:
            pass

    errors = []
    for repo in external_repos:
        url = delivered.get(repo)
        if not url:
            errors.append(
                f"delivered-gate: REFUSED — {brief_id} missing delivered['{repo}'] in progress.json.\n"
                f"  Cross-repo work must be pushed before this brief can complete.\n"
                f"  Fix: push the {repo} commits, then add to\n"
                f"  {wt_progress}:\n"
                f'    "delivered": {{"{repo}": "https://github.com/<org>/{repo}/commit/<sha>"}}\n'
                f"  (a PR URL or a bare pushed commit SHA also works).\n"
                f"  Emergency override (use sparingly): SIMPLE_LOOP_SKIP_DELIVERED_GATE=1"
            )
            continue
        ok, reason = _verify_delivered_ref(url, repo)
        if not ok:
            errors.append(
                f"delivered-gate: REFUSED — {brief_id} delivered['{repo}'] = {url!r} "
                f"is not verifiable on the remote: {reason}"
            )

    return len(errors) == 0, errors


# ─── Action: move-to-eval ────────────────────────────────────────────

def move_to_eval(paths, brief_id):
    """Move a brief from active to completed_pending_eval.

    brief-108-d note: completed_pending_eval is a legacy bucket the projector
    treats as always-empty. This action remains for API compat — it appends
    a `completed` event so the projector routes the brief into awaiting_review
    (the modern equivalent of completed_pending_eval).
    """
    rc = load_running(paths)
    active_briefs = {e.get("brief") for e in rc.get("active", [])}
    if brief_id not in active_briefs:
        print(f"Warning: brief '{brief_id}' not found in active list", file=sys.stderr)
        return False

    # Delivered gate: move_to_eval is a legacy path but still routes briefs
    # into awaiting_review (kind=complete) — without this call it is an
    # unguarded back door around the cross-repo delivered gate.
    bf_path = os.path.join(paths["project_dir"], "wiki", "briefs", "cards", brief_id, "index.md")
    passed, gate_errors = _check_delivered_gate(paths, brief_id, bf_path)
    if not passed:
        for err in gate_errors:
            print(err, file=sys.stderr)
        return False

    runtime_event(paths, "completed", brief_id, kind="complete", auto_merge=False)
    project_running(paths)

    log_action(paths, "move-to-eval", {"brief": brief_id})
    print(f"Moved {brief_id} to awaiting_review (kind=complete via move-to-eval)")
    return True


# ─── Action: move-to-pending-merges ─────────────────────────────────

def move_to_pending_merges(paths, brief_id):
    """Move a brief from active[] to pending_merges[] (auto-merge path).

    brief-108-d: appends `completed` (auto_merge=true) + `approved` events.
    Projector routes the brief into pending_merges[] on next projection.
    """
    rc = load_running(paths)
    active_briefs = {e.get("brief") for e in rc.get("active", [])}
    if brief_id not in active_briefs:
        print(f"Warning: brief '{brief_id}' not found in active list", file=sys.stderr)
        return False

    # Delivered gate: cross-repo briefs must prove delivery before completion.
    bf_path = os.path.join(paths["project_dir"], "wiki", "briefs", "cards", brief_id, "index.md")
    passed, gate_errors = _check_delivered_gate(paths, brief_id, bf_path)
    if not passed:
        for err in gate_errors:
            print(err, file=sys.stderr)
        return False

    runtime_event(paths, "completed", brief_id, kind="complete", auto_merge=True)
    runtime_event(paths, "approved", brief_id)
    project_running(paths)

    log_action(paths, "move-to-pending-merges", {"brief": brief_id})
    print(f"Moved {brief_id} to pending_merges")
    return True


# ─── Action: move-to-awaiting-review ────────────────────────────────

def move_to_awaiting_review(paths, brief_id, kind, reason=""):
    """Move a brief from active[] to awaiting_review[] (human approval path).

    kind: one of 'complete', 'rebase-blocked', 'watchdog-timed-out',
          'manual-recovery', 'staleness-gated', 'merge-conflict'.
          Persisted in the awaiting_review[] entry so callers can distinguish
          taste-gate entries (kind=complete) from structural failures.
    """
    # Cycle-completion gate: refuse kind=complete promotions where no cycles ran.
    # Closes the phantom-completion class (brief-067, brief-099).
    if kind == "complete":
        wt_progress = os.path.join(
            paths["worktrees_dir"], brief_id, ".loop", "state", "progress.json"
        )
        if os.path.exists(wt_progress):
            try:
                with open(wt_progress) as f:
                    prog = json.load(f)
                status = prog.get("status", "")
                remaining = prog.get("tasks_remaining", [])
                iteration = prog.get("iteration", 0)
                if status != "complete":
                    print(
                        f"cycle-gate: REFUSED — {brief_id} progress status={status!r} (expected 'complete')",
                        file=sys.stderr,
                    )
                    return False
                if remaining:
                    print(
                        f"cycle-gate: REFUSED — {brief_id} tasks_remaining non-empty ({len(remaining)} tasks)",
                        file=sys.stderr,
                    )
                    return False
                if iteration == 0:
                    print(
                        f"cycle-gate: REFUSED — {brief_id} iteration=0 (no cycles completed)",
                        file=sys.stderr,
                    )
                    return False
                # Belt-and-suspenders: verify ≥1 cycle commit beyond Initialize brief.
                try:
                    rc_check = load_running(paths)
                    branch = next(
                        (b["branch"] for b in rc_check.get("active", []) if b.get("brief") == brief_id),
                        None,
                    )
                    if branch:
                        config = read_config(paths["loop_dir"])
                        remote = config.get("GIT_REMOTE", "origin")
                        main_br = config.get("GIT_MAIN_BRANCH", "main")
                        r = subprocess.run(
                            ["git", "-C", paths["project_dir"], "rev-list", "--count",
                             f"{remote}/{main_br}..{branch}"],
                            capture_output=True, text=True, check=False, timeout=10,
                        )
                        if r.returncode == 0:
                            n = int(r.stdout.strip() or "0")
                            if n <= 1:
                                print(
                                    f"cycle-gate: REFUSED — {brief_id} has {n} commit(s) beyond "
                                    f"{remote}/{main_br} (no cycle work)",
                                    file=sys.stderr,
                                )
                                return False
                except Exception as git_err:
                    print(f"cycle-gate: git commit check skipped for {brief_id}: {git_err}", file=sys.stderr)
            except Exception as e:
                print(f"cycle-gate: failed to read progress for {brief_id}: {e} — skipping gate", file=sys.stderr)
        else:
            print(f"cycle-gate: worktree not found for {brief_id} — skipping gate", file=sys.stderr)

        # Delivered gate: cross-repo briefs must prove delivery before completion.
        bf_path = os.path.join(paths["project_dir"], "wiki", "briefs", "cards", brief_id, "index.md")
        passed, gate_errors = _check_delivered_gate(paths, brief_id, bf_path)
        if not passed:
            for err in gate_errors:
                print(err, file=sys.stderr)
            return False

    rc = load_running(paths)
    active_briefs = {e.get("brief") for e in rc.get("active", [])}
    if brief_id not in active_briefs:
        print(f"Warning: brief '{brief_id}' not found in active list", file=sys.stderr)
        return False

    # brief-108-d: append completed event; projector routes to awaiting_review.
    runtime_event(paths, "completed", brief_id,
                  kind=kind, auto_merge=False, reason=reason or "")
    project_running(paths)

    signal_dedup_clear(paths, brief_id)
    # brief-151: the brief has left active execution for the human gate
    # (taste-gate, escalation, or structural failure). Card Status stays
    # `active` so no other daemon re-dispatches it; release the claim ref so a
    # human re-queue (Status → queued) is re-claimable. Best-effort.
    _release_claim_quiet(paths, brief_id)
    log_action(paths, "move-to-awaiting-review", {"brief": brief_id, "kind": kind, "reason": reason})
    print(f"Moved {brief_id} to awaiting_review (kind={kind})")
    return True


# ─── Action: process-pending-merges ─────────────────────────────────

def process_pending_merges(paths):
    """Pop one brief from pending_merges[], write pending-merge.json, execute merge.

    brief-108-d: pending_merges[] is projected from runtime-events. The "pop"
    is implicit — merge() flips card status to merged + appends a `merged`
    event, after which the next projection drops the entry from pending_merges
    and adds it to history.
    """
    rc = load_running(paths)
    queue = rc.get("pending_merges", [])

    if not queue:
        print("No pending_merges to process", file=sys.stderr)
        return False

    if os.path.exists(paths["pending_merge"]):
        print("pending-merge.json already exists — merge already in progress", file=sys.stderr)
        return False

    entry = queue[0]
    brief = entry.get("brief", "")
    branch = entry.get("branch", "")

    spec = {
        "brief": brief,
        "branch": branch,
        "title": brief,
        "evaluation": entry.get("evaluation", ""),
    }
    with open(paths["pending_merge"], "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")

    log_action(paths, "process-pending-merges", {"brief": brief})
    print(f"Wrote pending-merge.json for {brief}, executing merge")

    return merge(paths)


# ─── Action: approve-brief ───────────────────────────────────────────

def approve_brief(paths, brief_id):
    """Move a brief from awaiting_review[] to pending_merges[].

    brief-108-d: appends an `approved` event. Projector routes the brief from
    awaiting_review[] into pending_merges[] on next projection.
    """
    rc = load_running(paths)
    waiting_briefs = {e.get("brief") for e in rc.get("awaiting_review", [])}
    if brief_id not in waiting_briefs:
        print(f"Warning: brief '{brief_id}' not found in awaiting_review", file=sys.stderr)
        return False

    runtime_event(paths, "approved", brief_id)
    project_running(paths)

    log_action(paths, "approve-brief", {"brief": brief_id})
    print(f"Approved {brief_id}: moved to pending_merges")
    return True


# ─── Action: reject-brief ────────────────────────────────────────────

def reject_brief(paths, brief_id, reason=""):
    """Move a brief from awaiting_review[] and set card Status → rejected.

    brief-108-d: card-status flip is the truth; projector drops rejected cards
    from all running.json buckets. No history[] write (card-is-truth).
    """
    rc = load_running(paths)
    waiting_briefs = {e.get("brief") for e in rc.get("awaiting_review", [])}
    if brief_id not in waiting_briefs:
        print(f"Warning: brief '{brief_id}' not found in awaiting_review", file=sys.stderr)
        return False

    # Update card Status → rejected (card-is-truth: no history[] write)
    project_dir = paths["project_dir"]
    _card_path = os.path.join(project_dir, "wiki", "briefs", "cards", brief_id, "index.md")
    if os.path.exists(_card_path):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from _set_card_status import set_card_status as _set_card_status_fn
            _changed = _set_card_status_fn(_card_path, "rejected")
            if _changed:
                git(project_dir, "add", _card_path, check=False)
                git(project_dir, "commit", "-m", f"loop: card status → rejected for {brief_id}", check=False)
                log_action(paths, "card_status_set_rejected", {"brief": brief_id})
        except Exception as e:
            print(f"reject: card status update failed for {brief_id}: {e} (non-fatal)", file=sys.stderr)

    project_running(paths)
    signal_dedup_clear(paths, brief_id)
    # brief-151: rejected is terminal — release the claim so a re-queue can
    # re-claim. Best-effort.
    _release_claim_quiet(paths, brief_id)
    log_action(paths, "reject-brief", {"brief": brief_id, "reason": reason})
    print(f"Rejected {brief_id}: card Status → rejected")
    return True


# ─── Action: close-as-delivered ──────────────────────────────────────

def close_as_delivered(paths, brief_id, delivered_via, reason=""):
    """Close a brief as delivered-elsewhere: card superseded + event + project.

    Atomic replacement for the four-write hand-merge recipe when work has shipped
    through another door (a PR, a direct land, another brief). Works from any
    queue state — active, awaiting_review, or pending_merges.

    Idempotent: if the card is already Status: superseded, re-projects and
    returns True without appending another event.
    """
    _card_path = os.path.join(
        paths["project_dir"], "wiki", "briefs", "cards", brief_id, "index.md"
    )

    # Early idempotency: if card is already superseded, skip state writes.
    if os.path.exists(_card_path):
        try:
            with open(_card_path) as _f:
                for _line in _f:
                    _s = _line.strip()
                    if re.match(r"^Status\s*:", _s, re.IGNORECASE):
                        if _s.split(":", 1)[1].strip().lower() == "superseded":
                            project_running(paths)
                            print(f"close-as-delivered: {brief_id} already superseded (idempotent re-run)")
                            return True
                        break
        except (IOError, OSError):
            pass

    # Verify the brief is in a closeable queue state.
    rc = load_running(paths)
    all_closeable = (
        {e.get("brief") for e in rc.get("active", [])} |
        {e.get("brief") for e in rc.get("awaiting_review", [])} |
        {e.get("brief") for e in rc.get("pending_merges", [])}
    )
    if brief_id not in all_closeable:
        print(
            f"close-as-delivered: brief '{brief_id}' not found in active, "
            f"awaiting_review, or pending_merges",
            file=sys.stderr,
        )
        return False

    # Card Status → superseded (card-is-truth; projector routes to history[]).
    if os.path.exists(_card_path):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from _set_card_status import set_card_status as _set_card_status_fn
            _changed = _set_card_status_fn(_card_path, "superseded")
            if _changed:
                git(paths["project_dir"], "add", _card_path, check=False)
                git(paths["project_dir"], "commit", "-m",
                    f"loop: card status → superseded for {brief_id}", check=False)
                log_action(paths, "card_status_set_superseded", {"brief": brief_id})
        except Exception as e:
            print(
                f"close-as-delivered: card status update failed for {brief_id}: {e} (non-fatal)",
                file=sys.stderr,
            )

    # Append superseded runtime event (delivered_via + reason land in history[]).
    runtime_event(paths, "superseded", brief_id, delivered_via=delivered_via, reason=reason or "")

    # Re-project running.json from cards + events.
    project_running(paths)

    # Remove worktree (preserve branch for forensics).
    remove_worktree(paths, brief_id)

    signal_dedup_clear(paths, brief_id)
    # brief-151: delivered-elsewhere is terminal — release the claim.
    _release_claim_quiet(paths, brief_id)
    log_action(paths, "close-as-delivered", {
        "brief": brief_id, "delivered_via": delivered_via, "reason": reason or "",
    })
    print(f"Closed {brief_id} as superseded (delivered via: {delivered_via})")
    return True


# ─── Action: dispatch ─────────────────────────────────────────────────

def worktree_dir_for(paths, brief):
    """Return the worktree path for a brief."""
    return os.path.join(paths["worktrees_dir"], brief)


def ensure_worktree(paths, brief, branch, config=None):
    """Create a worktree for a brief if it doesn't already exist. Returns worktree path."""
    wt_dir = worktree_dir_for(paths, brief)
    if os.path.exists(wt_dir):
        return wt_dir

    if config is None:
        config = read_config(paths["loop_dir"])
    remote = config["GIT_REMOTE"]
    main_branch = config["GIT_MAIN_BRANCH"]
    project_dir = paths["project_dir"]

    os.makedirs(paths["worktrees_dir"], exist_ok=True)

    # Try existing local branch, then remote tracking, then create new
    if git(project_dir, "show-ref", "--verify", "--quiet",
           f"refs/heads/{branch}", check=False).returncode == 0:
        git(project_dir, "worktree", "add", wt_dir, branch)
    elif git(project_dir, "show-ref", "--verify", "--quiet",
             f"refs/remotes/{remote}/{branch}", check=False).returncode == 0:
        git(project_dir, "worktree", "add", wt_dir, branch)
    else:
        git(project_dir, "worktree", "add", "-b", branch, wt_dir, main_branch)

    return wt_dir


def remove_worktree(paths, brief):
    """Remove a worktree for a brief."""
    wt_dir = worktree_dir_for(paths, brief)
    if os.path.exists(wt_dir):
        git(paths["project_dir"], "worktree", "remove", wt_dir, "--force", check=False)
    # Clean up any stale worktree entries
    git(paths["project_dir"], "worktree", "prune", check=False)


def _init_commit_already_landed(wt_dir, brief):
    """True iff HEAD in wt_dir is already the init commit for `brief`.

    Detects mid-dispatch crash recovery (issue #7): when dispatch() crashes
    after `git commit -m "Initialize brief ..."` lands but before
    pending-dispatch.json is consumed, the daemon retries the whole flow on
    the next tick. The retry would re-attempt the same commit and fail with
    "nothing to commit", leaving pending-dispatch.json on disk forever.
    """
    expected = f"Initialize brief {brief}"
    head = git(wt_dir, "log", "-1", "--format=%s", check=False)
    return head.returncode == 0 and head.stdout.strip() == expected


def _release_claim_quiet(paths, brief_id):
    """brief-151: best-effort delete of refs/claims/<brief_id> on a terminal/exit
    transition (merge, reject, superseded, awaiting-review/escalate) so a
    re-queued brief is re-claimable. Non-fatal: a failed release only leaks a
    ref (Residue: stale-claim reaper is a follow-up) and must never break the
    state transition."""
    try:
        config = read_config(paths["loop_dir"])
        remote = config["GIT_REMOTE"]
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from claim import release_claim
        if release_claim(paths["project_dir"], brief_id, remote):
            log_action(paths, "claim_released", {"brief": brief_id})
    except Exception as e:
        print(f"release_claim: {brief_id} non-fatal failure: {e}", file=sys.stderr)


def dispatch(paths):
    """Process pending-dispatch.json: concurrency gate + worktree + progress init.

    Brief-034: enforces THROTTLE cap and Parallel-safe/Edit-surface checks before
    dispatching. When blocked, logs concurrency_skip / throttle_reached and
    removes pending-dispatch.json (queen will re-queue next tick if still
    wanted). When clean, adds per-entry concurrency metadata to active[].
    """
    if not os.path.exists(paths["pending_dispatch"]):
        print("No pending-dispatch.json found", file=sys.stderr)
        return False

    config = read_config(paths["loop_dir"])
    remote = config["GIT_REMOTE"]
    main_branch = config["GIT_MAIN_BRANCH"]
    try:
        throttle = int(str(config.get("THROTTLE", "1")).split("#", 1)[0].strip() or "1")
    except (ValueError, TypeError):
        throttle = 1
    if throttle < 1:
        throttle = 1

    with open(paths["pending_dispatch"]) as f:
        spec = json.load(f)

    brief = spec["brief"]
    branch = spec["branch"]
    brief_file = spec["brief_file"]
    notes = spec.get("notes", "")

    project_dir = paths["project_dir"]

    # ── Concurrency gate ─────────────────────────────────────────────
    rc = load_running(paths)
    active = rc.get("active", [])
    in_flight = len(active)

    # ── Drain-for-solo gate (SOLO_DRAIN_AFTER_SECS; default off/0) ────
    # When the dispatch queue HEAD is a parallel-safe:false brief that has
    # waited longer than the threshold, stop feeding OTHER briefs past it so
    # the board drains and the solo head runs next. Closes the brief-253a
    # starvation (a solo head sat at position 1 for hours while parallel
    # briefs dispatched past it). Deterministic code gate — not the queen's
    # judgment. The drained head itself is still allowed through (it dispatches
    # once the board is empty).
    try:
        drain_secs = int(str(config.get("SOLO_DRAIN_AFTER_SECS", "0")).split("#", 1)[0].strip() or "0")
    except (ValueError, TypeError):
        drain_secs = 0
    if drain_secs > 0 and active:
        # lib/queue.py collides with stdlib `queue`, so load it by explicit
        # path via importlib rather than `import queue` (which could resolve to
        # the stdlib module if it were ever pre-imported). No sys.modules
        # collision; no dependency on cwd or sys.path ordering.
        import importlib.util
        _qpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue.py")
        _spec = importlib.util.spec_from_file_location("loop_queue", _qpath)
        _loop_queue = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_loop_queue)
        # brief-151: when this daemon is lane-partitioned (LOOP_LANE exported by
        # daemon.sh), the drain gate must consider only this lane's queue head —
        # otherwise a solo brief in another lane could wrongly hold our dispatch.
        _lane = os.environ.get("LOOP_LANE") or None
        decision = _loop_queue.head_solo_drain(project_dir, drain_secs, running=rc, lane=_lane)
        if decision["drain"] and decision["brief"] != brief:
            log_action(paths, "solo_drain_hold", {
                "brief": brief,
                "draining_for": decision["brief"],
                "waited_secs": round(decision["waited"], 1),
                "threshold_secs": drain_secs,
            })
            print(
                f"DRAIN: holding dispatches for {decision['brief']} "
                f"(solo, waited {int(decision['waited'])}s) — {brief} deferred",
                file=sys.stderr,
            )
            os.remove(paths["pending_dispatch"])
            return False

    if in_flight >= throttle:
        log_action(paths, "throttle_reached", {
            "brief": brief,
            "throttle": throttle,
            "in_flight_count": in_flight,
        })
        print(f"throttle_reached: {brief} deferred "
              f"(in_flight={in_flight}, throttle={throttle})", file=sys.stderr)
        os.remove(paths["pending_dispatch"])
        return False

    brief_file_abs = (brief_file if os.path.isabs(brief_file)
                      else os.path.join(project_dir, brief_file))
    parallel_safe, edit_surface = parse_concurrency_frontmatter(brief_file_abs)

    if active:
        block_reason = None
        blocked_by = None
        overlap_paths = []
        if not parallel_safe:
            block_reason = "new_brief_not_parallel_safe"
            blocked_by = active[0].get("brief", "")
        else:
            for entry in active:
                other_ps = entry.get("parallel_safe", False)
                other_es = entry.get("edit_surface", [])
                if not other_ps:
                    block_reason = "active_brief_not_parallel_safe"
                    blocked_by = entry.get("brief", "")
                    break
                if edit_surfaces_overlap(edit_surface, other_es):
                    block_reason = "edit_surface_overlap"
                    blocked_by = entry.get("brief", "")
                    overlap_paths = sorted(
                        {p for p in edit_surface for q in other_es if _paths_overlap(p, q)} |
                        {q for p in edit_surface for q in other_es if _paths_overlap(p, q)}
                    )
                    break

        if block_reason:
            log_action(paths, "concurrency_skip", {
                "brief": brief,
                "blocked_by": blocked_by,
                "reason": block_reason,
                "overlap_paths": overlap_paths,
            })
            print(f"concurrency_skip: {brief} blocked by {blocked_by} ({block_reason})",
                  file=sys.stderr)
            os.remove(paths["pending_dispatch"])
            return False

    # ── Proceed with dispatch ────────────────────────────────────────
    # Fetch latest (no checkout needed — main tree untouched)
    git(project_dir, "fetch", remote, check=False)

    # brief-151: atomic cross-box claim BEFORE any worktree exists. Two daemons
    # sharing this repo+lane race to push refs/claims/<brief>; exactly one wins.
    # On contention (another daemon holds it) skip cleanly; on a real push error
    # (auth/network) fail loud and create NO branch/worktree (engineering rule
    # 10 — claim-first is the whole point, anti-pattern: never push after).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from claim import claim_brief
    try:
        claimed = claim_brief(project_dir, brief, remote)
    except Exception as e:
        log_action(paths, "claim_error", {"brief": brief, "error": str(e)})
        print(f"dispatch: claim push for {brief} failed (fail-loud, no worktree): {e}",
              file=sys.stderr)
        os.remove(paths["pending_dispatch"])
        return False
    if not claimed:
        log_action(paths, "claim_skip", {"brief": brief})
        print(f"loop: brief {brief} already claimed — skipping", file=sys.stderr)
        os.remove(paths["pending_dispatch"])
        return False

    # Create worktree with new branch
    wt_dir = ensure_worktree(paths, brief, branch, config)

    # Initialize progress.json + commit, unless this is a retry where the
    # init commit already landed but the rest of the transaction didn't
    # finish (issue #7 — dispatch idempotency on init-commit).
    if _init_commit_already_landed(wt_dir, brief):
        print(f"dispatch: init commit for {brief} already landed — skipping init block (retry)",
              file=sys.stderr)
    else:
        wt_progress = os.path.join(wt_dir, ".loop", "state", "progress.json")
        os.makedirs(os.path.dirname(wt_progress), exist_ok=True)

        progress = {
            "brief": brief,
            "brief_file": brief_file,
            "iteration": 0,
            "status": "running",
            "tasks_completed": [],
            "tasks_remaining": [],
            "learnings": [],
        }
        with open(wt_progress, "w") as f:
            json.dump(progress, f, indent=2)
            f.write("\n")

        git(wt_dir, "add", ".loop/state/progress.json")
        git(wt_dir, "commit", "-m", f"Initialize brief {brief}")

    # Push is idempotent — up-to-date branches push as no-op.
    git(wt_dir, "push", "-u", remote, branch)

    # Compute worker_slot from current state (project before-state).
    rc = load_running(paths)
    worker_slot = len(rc.get("active", []))

    # brief-108-d: append the dispatch event. Runtime facts (worker_slot,
    # parallel_safe, edit_surface, dispatched_at) live in runtime-events.jsonl;
    # running.json is projected from cards + events, not hand-spliced.
    runtime_event(
        paths, "dispatched", brief,
        branch=branch,
        brief_file=brief_file,
        worker_slot=worker_slot,
        throttle=throttle,
        parallel_safe=parallel_safe,
        edit_surface=edit_surface,
    )

    # Update card Status → active (card-is-truth: queued → active on dispatch)
    # Uses plumbing: reads from main, transforms in memory, commits to main.
    # Working tree is never touched — immune to worktree branch drift.
    _card_repo_path = f"wiki/briefs/cards/{brief}/index.md"
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from _set_card_status import transform_card_status_content as _transform_fn
        from git_plumbing import (
            read_file_at_branch as _read_branch,
            commit_file_to_branch as _commit_file,
        )
        _card_content = _read_branch(project_dir, _card_repo_path, main_branch)
        _new_content, _changed = _transform_fn(_card_content, "active")
        if _changed:
            _tmp_fd, _tmp_path = tempfile.mkstemp(prefix="card-status-", suffix=".md")
            try:
                os.write(_tmp_fd, _new_content.encode())
                os.close(_tmp_fd)
                _commit_file(project_dir, _tmp_path, _card_repo_path, main_branch,
                             f"loop: card status → active for {brief}")
            finally:
                try:
                    os.unlink(_tmp_path)
                except OSError:
                    pass
            log_action(paths, "card_status_set_active", {"brief": brief})
    except Exception as e:
        print(f"dispatch: card status update failed for {brief}: {e} (non-fatal)", file=sys.stderr)

    # brief-108-d: project running.json from cards + events. Single-write owner.
    # harness-001/003: running.json is daemon-local — project locally but do
    # NOT commit it. Only runtime-events.jsonl goes to main.
    project_running(paths)
    # Commit state files to main via plumbing — immune to worktree branch drift.
    # running.json is explicitly excluded: it is daemon-local volatile state.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from git_plumbing import commit_files_to_branch as _commit_files_fn
        _events_path = os.path.join(project_dir, ".loop", "state", "runtime-events.jsonl")
        _state_files = []
        if os.path.exists(_events_path):
            _state_files.append((_events_path,
                                  os.path.relpath(_events_path, project_dir)))
        if _state_files:
            _commit_files_fn(
                project_dir, _state_files, main_branch,
                f"loop: append runtime-events (dispatch {brief})",
            )
    except Exception as e:
        print(f"dispatch: runtime-events plumbing commit failed for {brief}: {e} (non-fatal)",
              file=sys.stderr)

    push_bookkeeping(paths, remote, main_branch, f"dispatch {brief}")

    # Remove queue file
    os.remove(paths["pending_dispatch"])

    log_action(paths, "dispatch", {
        "brief": brief, "branch": branch, "notes": notes,
        "worker_slot": worker_slot, "throttle": throttle,
        "parallel_safe": parallel_safe,
    })
    print(f"Dispatched {brief} on branch {branch} "
          f"(slot={worker_slot}, throttle={throttle})")
    return True


# ─── Action: merge ────────────────────────────────────────────────────

def merge(paths):
    """Process pending-merge.json: merge branch to main, remove worktree."""
    if not os.path.exists(paths["pending_merge"]):
        print("No pending-merge.json found", file=sys.stderr)
        return False

    config = read_config(paths["loop_dir"])
    remote = config["GIT_REMOTE"]
    main_branch = config["GIT_MAIN_BRANCH"]

    with open(paths["pending_merge"]) as f:
        spec = json.load(f)

    brief = spec["brief"]
    branch = spec["branch"]
    title = spec.get("title", brief)
    evaluation = spec.get("evaluation", "")

    project_dir = paths["project_dir"]

    # ── Portal#50 gate: reject stale pending-merge.json ─────────────────────
    # A pending-merge.json left over from a prior approval cycle (e.g. a failed
    # push, daemon restart, or an aborted merge) fires the legacy daemon path
    # even when the brief has since been re-queued and is now in awaiting_review
    # (no current-generation approved event). Receipt: fleet-001 (Auto-merge:false
    # / Human-gate:review) merged with approved_by=None on 2026-06-28 because
    # pending-merge.json survived the re-queue.
    #
    # Fix: check that running.json has the brief in pending_merges[]. We read the
    # on-disk running.json first — it is the already-projected state written by
    # project_running() before this call in the normal daemon flow, and is the
    # authoritative view of where the brief sits. If the brief IS in
    # pending_merges[] there, the merge is legitimate. If it is NOT there, we
    # re-project from cards+runtime-events to confirm (catching the re-queue case
    # where running.json may also be stale), then refuse and clean up.
    try:
        _rc_ondisk = None
        if os.path.exists(paths["running_file"]):
            try:
                with open(paths["running_file"]) as _rf:
                    _rc_ondisk = json.load(_rf)
            except Exception:
                pass

        _ondisk_pending_ids = (
            {e.get("brief") for e in _rc_ondisk.get("pending_merges", [])}
            if _rc_ondisk is not None
            else set()
        )

        if brief in _ondisk_pending_ids:
            # On-disk running.json confirms brief is legitimately in pending_merges[].
            # Gate passes — proceed with merge.
            pass
        else:
            # Brief is absent from on-disk pending_merges[]. Re-project from
            # cards+events to get the authoritative current location (the brief
            # may have been re-queued, moving it to awaiting_review).
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from state import project_running_json as _project_running_json
            _rc = _project_running_json(project_dir)
            _pending_brief_ids = {e.get("brief") for e in _rc.get("pending_merges", [])}
            if brief not in _pending_brief_ids:
                _bucket = None
                for _bkt in ("awaiting_review", "active", "history"):
                    if any(e.get("brief") == brief for e in _rc.get(_bkt, [])):
                        _bucket = _bkt
                        break
                _location = f"in {_bucket}" if _bucket else "not found in any active bucket"
                print(
                    f"merge: REFUSED — {brief} is not in pending_merges[] ({_location}); "
                    f"pending-merge.json is stale (portal#50 gate). Cleaning up stale file.",
                    file=sys.stderr,
                )
                try:
                    log_action(paths, "merge_refused_stale_pending_merge", {
                        "brief": brief, "branch": branch, "location": _location or "none",
                    })
                except Exception:
                    pass
                try:
                    os.remove(paths["pending_merge"])
                except OSError:
                    pass
                return False
    except Exception as _gate_err:
        # If the gate itself errors (e.g. card directory missing), fail loud —
        # never merge on an unverifiable state (engineering rule 10).
        print(
            f"merge: gate check failed for {brief}: {_gate_err} — refusing merge to avoid unverified state",
            file=sys.stderr,
        )
        return False
    # ── end portal#50 gate ──────────────────────────────────────────────────

    # Verify main tree is on main branch (should always be true with worktrees)
    current = git(project_dir, "branch", "--show-current", check=False).stdout.strip()
    if current != main_branch:
        git(project_dir, "checkout", main_branch)

    git(project_dir, "fetch", remote, check=False)
    git(project_dir, "pull", "--ff-only", remote, main_branch, check=False)

    # Pre-merge safe-path clean: remove untracked files that the worker /
    # validator wrapper may have written to main's working tree. These are
    # never valuable in main's tree — the branch commits carry the canonical
    # versions. Clean is safe-path-only (explicit allowlist, never -fdX,
    # never broad).
    #
    # Paths that apply to every merge:
    #   - validator review files (per brief-028; wrapper writes to main root)
    #
    # Paths that apply to THIS merge only:
    #   - the specific brief's card directory — worker artifacts (plan.md,
    #     closeout.md, review.md, smoke.md, cycle PNGs) sometimes land as
    #     untracked duplicates in main's tree. Safe to clean for the brief
    #     being merged because the branch owns its card dir by convention.
    #     NOT safe to broaden to `wiki/briefs/cards/` — other briefs' cards
    #     are legitimate tracked content on main.
    SAFE_CLEAN_PATHS = [".loop/modules/validator/state/reviews/"]
    brief_card_path = f"wiki/briefs/cards/{brief}/"
    if os.path.isdir(os.path.join(project_dir, brief_card_path)):
        SAFE_CLEAN_PATHS.append(brief_card_path)
    for clean_path in SAFE_CLEAN_PATHS:
        result = git(project_dir, "clean", "-fd", clean_path, check=False)
        if result.returncode == 0 and result.stdout.strip():
            log_action(paths, "pre_merge_clean", {
                "brief": brief, "path": clean_path, "removed": result.stdout.strip()
            })
            print(f"pre-merge clean [{clean_path}]: {result.stdout.strip()}")

    # Merge the branch (using local ref if available, otherwise remote)
    merge_msg = f"Merge {brief}: {title}"
    if evaluation:
        merge_msg += f"\n\nEvaluation: {evaluation}"

    # Issue #28 (Python path): `git merge` refuses with exit 2 when the main
    # working tree has dirty TRACKED files that the merge would overwrite —
    # runtime-events.jsonl is the primary trigger (it is committed on the
    # worker branch via dispatch plumbing, so the merge wants to update it,
    # but the daemon's working tree has it modified/dirty). Untracked files
    # (.loop/state/ evaluations/, stewardship-log-*.md, last-queen-success.json)
    # do NOT block git merge and must NOT be stashed (stashing --include-untracked
    # would remove log.jsonl and other live state files from the working tree).
    # Mirror the daemon.sh issue-#28 pattern, but stash only tracked changes.
    _autostash_taken = False
    _tracked_dirty = git(project_dir, "status", "--porcelain", "--untracked-files=no",
                         check=False).stdout.strip()
    if _tracked_dirty:
        stash_result = git(
            project_dir, "stash", "push",
            "-m", f"loop: merge dirty-tree autostash for {brief} (issue #28)",
            check=False,
        )
        if stash_result.returncode == 0 and "No local changes" not in stash_result.stdout:
            _autostash_taken = True
            log_action(paths, "merge_autostash_push", {
                "brief": brief, "branch": branch,
                "dirty_lines": len(_tracked_dirty.splitlines()),
            })
            print(f"merge autostash: stashed dirty tracked files before merge ({len(_tracked_dirty.splitlines())} entries)")

    if git(project_dir, "show-ref", "--verify", "--quiet",
           f"refs/heads/{branch}", check=False).returncode == 0:
        merge_result = git(project_dir, "merge", branch, "--no-ff", "-m", merge_msg, check=False)
    else:
        merge_result = git(project_dir, "merge", f"{remote}/{branch}", "--no-ff", "-m", merge_msg, check=False)

    if merge_result.returncode != 0:
        # Restore autostash before bailing out so no data is lost.
        if _autostash_taken:
            git(project_dir, "stash", "pop", "--index", check=False)
            _autostash_taken = False

        combined = merge_result.stdout + merge_result.stderr
        is_conflict = merge_result.returncode in (1, 128) and (
            "CONFLICT" in combined or "Automatic merge failed" in combined
            or "unmerged" in combined.lower()
        )
        if is_conflict:
            git(project_dir, "merge", "--abort", check=False)
            # brief-108-d: append a `completed` event with merge-conflict kind.
            # The brief was already in pending_merges (had a `completed` +
            # `approved` event); we re-emit `completed` with the new kind so
            # the projector flips it back to awaiting_review[]. Card status
            # stays `active` (the brief never reached `merged`).
            runtime_event(paths, "completed", brief,
                          kind="merge-conflict",
                          reason="merge conflict — human resolution required",
                          auto_merge=False)
            project_running(paths)
            os.remove(paths["pending_merge"])
            log_action(paths, "merge_conflict_abort", {
                "brief": brief, "branch": branch,
                "reason": "merge_conflict_routed_to_awaiting_review",
            })
            print(f"Merge conflict on {brief}: aborted, routed to awaiting_review", file=sys.stderr)
            return False
        raise subprocess.CalledProcessError(
            merge_result.returncode, f"git merge {branch}",
            output=merge_result.stdout, stderr=merge_result.stderr,
        )

    # Merge succeeded. Restore the autostash if one was taken.
    # Only tracked files were stashed (not untracked — those were left in
    # place to avoid disrupting live daemon state files like log.jsonl).
    # runtime-events.jsonl is the most likely stash conflict: the stash holds
    # a pre-merge dirty version; the merge brought in the branch's version.
    # We ALWAYS want the post-merge (HEAD) version because post-merge code
    # below regenerates runtime-events.jsonl via project_running() anyway.
    # Strategy: pop; if it fails due to a tracked-file conflict, take HEAD
    # versions for conflicted files and drop the stash.
    if _autostash_taken:
        pop_result = git(project_dir, "stash", "pop", check=False)
        if pop_result.returncode != 0:
            # Pop conflicted (most likely runtime-events.jsonl). Restore HEAD
            # versions for any conflicted tracked files and clear the stash.
            git(project_dir, "checkout", "HEAD", "--",
                os.path.join(".loop", "state", "runtime-events.jsonl"),
                check=False)
            # Clear any remaining index conflict markers from the failed pop.
            git(project_dir, "reset", "HEAD", "--",
                os.path.join(".loop", "state", "runtime-events.jsonl"),
                check=False)
            git(project_dir, "stash", "drop", check=False)
            log_action(paths, "merge_autostash_pop_conflict_resolved", {
                "brief": brief, "branch": branch,
                "note": "autostash pop conflicted on runtime-events.jsonl; HEAD version kept, stash dropped",
            })
            print(f"merge autostash: pop conflicted (runtime-events.jsonl) — kept HEAD version, stash dropped")
        else:
            log_action(paths, "merge_autostash_pop", {"brief": brief, "branch": branch})
            print(f"merge autostash: restored dirty tree after merge")

    # Remove worktree before deleting branch
    remove_worktree(paths, brief)

    # Delete branches
    git(project_dir, "push", remote, "--delete", branch, check=False)
    git(project_dir, "branch", "-d", branch, check=False)

    # Push main — brief-014 fix 3: never silent. If this push fails (the
    # 2026-04-22 keychain-lock scenario that left brief-013's merge unpushed
    # for ~14h), the redactor-aware push_with_escalate writes escalate.json
    # with reason=push_failed_on_auth and a token-redacted stderr. Raise
    # after so the merge action is marked failed and the queen escalates.
    if not push_with_escalate(paths, remote=remote, branch=main_branch, brief=brief):
        raise RuntimeError(
            f"push_failed_on_auth: merge {brief} complete locally but push to "
            f"{remote}/{main_branch} failed. See escalate.json."
        )

    # --- Producer-side cleanup (brief-107, brief-108, brief-108-d) ---
    # Card Status → merged + `merged` event in runtime-events.jsonl is the
    # truth. running.json is projected from those — no hand-splice.
    _cleanup_staged = False

    _card_path = os.path.join(project_dir, "wiki", "briefs", "cards", brief, "index.md")
    if os.path.exists(_card_path):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from _set_card_status import set_card_status as _set_card_status_fn
            _changed = _set_card_status_fn(_card_path, "merged")
            if _changed:
                git(project_dir, "add", _card_path, check=False)
                _cleanup_staged = True
                print(f"cleanup: card status → merged for {brief}")
                log_action(paths, "cleanup_card_status_set", {"brief": brief})
            else:
                print(f"cleanup: card status already merged for {brief} (no-op)")
        except Exception as e:
            print(f"cleanup: card status update failed for {brief}: {e} (non-fatal)", file=sys.stderr)

    # brief-108-d: append `merged` event so projector populates history[].
    merge_sha = ""
    try:
        sha_r = git(project_dir, "rev-parse", "HEAD", check=False)
        merge_sha = (sha_r.stdout or "").strip()[:8]
    except Exception:
        pass
    runtime_event(
        paths, "merged", brief,
        merge_sha=merge_sha,
        merged_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        evaluation=evaluation,
    )

    # Project running.json from the new card+events truth (daemon-local only —
    # harness-001/003: running.json is never committed).
    project_running(paths)
    git(project_dir, "add",
        os.path.join(project_dir, ".loop", "state", "runtime-events.jsonl"),
        check=False)

    if _cleanup_staged:
        git(project_dir, "commit", "-m", f"loop: post-merge cleanup for {brief}", check=False)
    else:
        # Card already merged but events still need committing.
        git(project_dir, "commit", "-m", f"loop: append runtime-events (merge {brief})", check=False)
    # --- End producer-side cleanup ---

    signal_dedup_clear(paths, brief)
    push_bookkeeping(paths, remote, main_branch, f"post-merge cleanup {brief}")

    # brief-151: brief is delivered — release the claim so a future re-queue is
    # re-claimable. Best-effort; never blocks the completed merge.
    _release_claim_quiet(paths, brief)

    # Remove queue file
    os.remove(paths["pending_merge"])

    log_action(paths, "merge", {"brief": brief, "branch": branch, "title": title})
    print(f"Merged {brief} to {main_branch}")
    return True


# ─── Action: cleanup ─────────────────────────────────────────────────

def cleanup_worktrees(paths):
    """Remove worktrees for briefs that are no longer active."""
    project_dir = paths["project_dir"]
    worktrees_dir = paths["worktrees_dir"]

    if not os.path.exists(worktrees_dir):
        print("No worktrees directory.")
        return True

    # Prune stale git worktree entries
    git(project_dir, "worktree", "prune", check=False)

    # Get active brief IDs (all queues that still have a live worktree)
    rc = load_running(paths)
    active_briefs = set()
    for entry in rc.get("active", []):
        active_briefs.add(entry.get("brief", ""))
    for entry in rc.get("completed_pending_eval", []):
        active_briefs.add(entry.get("brief", ""))
    for entry in rc.get("pending_merges", []):
        active_briefs.add(entry.get("brief", ""))
    for entry in rc.get("awaiting_review", []):
        active_briefs.add(entry.get("brief", ""))

    cleaned = 0
    for name in os.listdir(worktrees_dir):
        wt_path = os.path.join(worktrees_dir, name)
        if not os.path.isdir(wt_path):
            continue
        if name not in active_briefs:
            print(f"  Removing worktree: {name}")
            git(project_dir, "worktree", "remove", wt_path, "--force", check=False)
            # Fallback if git worktree remove fails
            if os.path.exists(wt_path):
                import shutil
                shutil.rmtree(wt_path, ignore_errors=True)
            cleaned += 1

    if cleaned:
        git(project_dir, "worktree", "prune", check=False)
        print(f"  Cleaned {cleaned} worktree(s).")
    else:
        print("  Nothing to clean up.")

    log_action(paths, "cleanup", {"cleaned": cleaned})
    return True


# ─── Bookkeeping push: non-fatal but never silent (issue #19) ────────
#
# Dispatch/merge bookkeeping pushes (`loop:` commits) stay non-fatal — the
# daemon's per-tick sync_project_checkout auto-heals a stranded commit and
# escalates after 3 consecutive failures, so failing the whole action here
# would be worse than the disease. But silence is what let one failed push
# freeze the daemon's checkout for 40 minutes with zero log signal: the
# divergence only became visible when a human noticed state that "can't be."
# Log the failure at the moment it happens, with the actual stderr, so the
# later SYNC FAILED lines have a paper trail pointing at root cause.

def push_bookkeeping(paths, remote, branch, context):
    """Push bookkeeping commits to remote; log loudly on failure, never raise.

    Returns True on success, False on failure (failure already logged to
    stderr + log.jsonl as `push_failed_bookkeeping`).
    """
    r = git(paths["project_dir"], "push", remote, branch, check=False)
    if r.returncode == 0:
        return True
    redacted = redact_secrets((r.stderr or "").strip())
    print(
        f"push failed ({context}): rc={r.returncode} {redacted} "
        f"— local loop: commit stranded; daemon sync will log/auto-heal",
        file=sys.stderr,
    )
    try:
        log_action(paths, "push_failed_bookkeeping", {
            "context": context, "remote": remote, "branch": branch,
            "returncode": r.returncode, "stderr": redacted,
        })
    except Exception:
        pass  # don't let a logging failure compound a push failure
    return False


# ─── Push with escalate-on-failure (brief-014 fix 3) ────────────────
#
# Every git push that was previously `|| true` now goes through here. On
# failure: redact stderr, write escalate.json, return False. Callers that
# need fatal-push-fail propagation can raise from there.

def push_with_escalate(paths, remote=None, branch=None, brief=None,
                       cwd=None, _test_stderr_override=None):
    """Push to remote with escalate-on-failure + token redaction.

    Args:
        paths: init_paths() dict.
        remote: git remote name (default: read from config.sh).
        branch: branch to push (default: main).
        brief: brief id to tag the escalate with (optional).
        cwd: directory to run git in (default: paths["project_dir"]).
        _test_stderr_override: test hook — when set, skip the real push
            and simulate a failure with this stderr. Production callers
            should never pass this.

    Returns:
        True on push success. False on failure (escalate written).
    """
    config = read_config(paths["loop_dir"])
    remote = remote or config.get("GIT_REMOTE", "origin")
    branch = branch or config.get("GIT_MAIN_BRANCH", "main")
    cwd = cwd or paths["project_dir"]

    if _test_stderr_override is not None:
        returncode = 1
        stderr = _test_stderr_override
    else:
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "push", remote, branch],
                capture_output=True, text=True, timeout=60,
            )
            returncode = r.returncode
            stderr = r.stderr or ""
        except subprocess.TimeoutExpired as e:
            returncode = 124
            stderr = f"git push timed out: {e}"
        except Exception as e:
            returncode = 1
            stderr = f"git push failed to invoke: {e}"

    if returncode == 0:
        return True

    redacted = redact_secrets(stderr)
    signals_dir = os.path.join(paths["state_dir"], "signals")
    os.makedirs(signals_dir, exist_ok=True)
    escalate_path = os.path.join(signals_dir, "escalate.json")

    payload = {
        "type": "push_failed",
        "reason": "push_failed_on_auth",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "remote": remote,
        "branch": branch,
        "returncode": returncode,
        "stderr": redacted,
    }
    if brief:
        payload["brief"] = brief

    with open(escalate_path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    # Also log a structured line for grep-debugging.
    try:
        log_action(paths, "push_failed", {
            "brief": brief or "",
            "remote": remote,
            "branch": branch,
            "returncode": returncode,
            "stderr": redacted,
        })
    except Exception:
        pass  # don't let a logging failure compound a push failure

    return False


# ─── Heartbeat (brief-014 fix 4) ────────────────────────────────────

def write_heartbeat(heartbeat_path, pid=None, last_event="tick"):
    """Write .loop/state/heartbeat.json atomically on every daemon tick.

    Format: {ts, pid, last_event} — readable with cat + jq. External watchers
    (loop status, stewardship cron) parse this to distinguish process-alive
    from loop-healthy. Stale heartbeat = hung daemon, even if PID is alive.
    """
    os.makedirs(os.path.dirname(heartbeat_path), exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pid": pid if pid is not None else os.getpid(),
        "last_event": last_event,
    }
    tmp_path = heartbeat_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
        f.write("\n")
    os.replace(tmp_path, heartbeat_path)


def heartbeat_is_stale(heartbeat_path, interval_s=300):
    """Return True if the heartbeat is older than 2× interval (or missing).

    Missing file treated as stale — safer default. "Daemon never started" and
    "daemon wrote once then froze 11h ago" look the same to an external
    watcher; both should alert.
    """
    threshold = 2 * interval_s
    try:
        with open(heartbeat_path) as f:
            hb = json.load(f)
        ts = hb.get("ts")
        if not ts:
            return True
        # Parse ISO8601; tolerate trailing Z or offset.
        if ts.endswith("Z"):
            ts_parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        else:
            ts_parsed = datetime.fromisoformat(ts)
            if ts_parsed.tzinfo is None:
                ts_parsed = ts_parsed.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_s = (now - ts_parsed).total_seconds()
        return age_s > threshold
    except (FileNotFoundError, IOError, OSError, json.JSONDecodeError, ValueError):
        return True


# ─── Validator artifact-presence check (brief-014 fix 5) ────────────

# Matches checkbox lines that name a backticked path — the common form in
# brief completion-criteria sections ("- [ ] `plan.md` in card dir").
_ARTIFACT_CHECKBOX_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s*.*?`([^`]+)`", re.MULTILINE)


def extract_artifact_paths(brief_file_path):
    """Grep completion-criteria / artifact sections for backticked file paths.

    Returns a set of path strings. Matches any `foo.md`-style backticked
    token on a checkbox line — filters to *.md / *.txt / *.json / *.yaml
    extensions, plus bare filenames without slashes (plan.md, closeout.md).
    Non-file tokens (\`verdict: pass\`, \`running.json\`-ish config refs)
    slip in sometimes; callers check existence on the filesystem, so extra
    tokens just cause cheap no-op stats.
    """
    if not brief_file_path or not os.path.exists(brief_file_path):
        return set()
    try:
        with open(brief_file_path) as f:
            text = f.read()
    except (IOError, OSError):
        return set()

    # Only scan the completion-criteria / artifact sections to reduce noise.
    # If a brief doesn't use a standard heading, fall back to full-file scan.
    sections = re.split(r"\n##\s+", text)
    scanned = []
    for sec in sections:
        head = sec.splitlines()[0].lower() if sec else ""
        if "completion" in head or "artifact" in head:
            scanned.append(sec)
    if not scanned:
        scanned = [text]

    out = set()
    for sec in scanned:
        for m in _ARTIFACT_CHECKBOX_RE.finditer(sec):
            tok = m.group(1).strip()
            # File-shaped filter: must contain a dot (extension) OR end in .md
            if "." in tok and "/" not in tok.strip("."):
                # Drop things that clearly aren't project artifacts.
                if tok.startswith("http") or tok.startswith("git@"):
                    continue
                out.add(tok)
            elif tok.endswith(".md") or tok.endswith(".json"):
                out.add(tok)
    return out


def validator_presence_check(brief_file_path, worktree_dir):
    """Return a list of artifact paths declared in the brief but missing.

    Resolution: paths are tried first relative to the brief's parent dir
    (the card dir convention: plan.md and closeout.md live next to index.md),
    then relative to the worktree root, then absolute if they start with /.
    """
    declared = extract_artifact_paths(brief_file_path)
    if not declared:
        return []

    brief_parent = os.path.dirname(os.path.realpath(brief_file_path))
    missing = []
    for rel in declared:
        if os.path.isabs(rel):
            if not os.path.exists(rel):
                missing.append(rel)
            continue
        candidates = [
            os.path.join(brief_parent, rel),
            os.path.join(worktree_dir, rel),
        ]
        if not any(os.path.exists(c) for c in candidates):
            missing.append(rel)
    return missing


# ─── Action: check-depends-on ────────────────────────────────────────
#
# Brief-014 fix 1 + 2: parse **Depends-on:** from pending-dispatch brief,
# compare against card Status==merged (card-is-truth, brief-108). Prints two lines to stdout:
#   line 1: verdict — "allowed" or "blocked:<first-unmet-dep>"
#   line 2: diagnostic — "brief=<id> depends_on=<list> merged_ids=<list> match=<allowed|blocked:…>"
# Diagnostic always emits regardless of outcome so grep-debugging is cheap.

def check_depends_on(paths):
    """Verify pending-dispatch brief's Depends-on against card Status==merged."""
    # Inline import avoids circular dep at module load (assess.py imports from
    # the same tree but isn't a package sibling).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from assess import DEPENDS_ON_LINE_RE, parse_depends_on_value

    brief_id = ""
    depends_on = []
    history_ids = []

    try:
        with open(paths["pending_dispatch"]) as f:
            spec = json.load(f)
        brief_id = spec.get("brief", "")
        brief_file = spec.get("brief_file", "")
        bf_path = os.path.join(paths["project_dir"], brief_file) if brief_file else ""

        if bf_path and os.path.exists(bf_path):
            with open(bf_path) as f:
                for line in f:
                    m = DEPENDS_ON_LINE_RE.match(line)
                    if m:
                        depends_on = parse_depends_on_value(m.group(1))
                        break

        if not depends_on:
            print("allowed")
            print(f"brief={brief_id} depends_on=[] merged_ids=<skipped> match=allowed")
            return True

        # Scan cards for merged status (card-is-truth: no history[] needed)
        cards_dir = os.path.join(paths["project_dir"], "wiki", "briefs", "cards")
        merged_ids = []
        if os.path.isdir(cards_dir):
            for card_id in os.listdir(cards_dir):
                card_file = os.path.join(cards_dir, card_id, "index.md")
                if not os.path.isfile(card_file):
                    continue
                try:
                    in_fm = False
                    with open(card_file) as cf:
                        for line in cf:
                            stripped = line.strip()
                            if stripped == "---":
                                if not in_fm:
                                    in_fm = True
                                else:
                                    break
                            elif in_fm and stripped.lower().startswith("status:"):
                                if stripped.split(":", 1)[1].strip().lower() == "merged":
                                    merged_ids.append(card_id)
                                break
                except Exception:
                    pass

        unmet = [d for d in depends_on if not any(_brief_id_matches(d, m) for m in merged_ids)]
        if unmet:
            verdict = f"blocked:{unmet[0]}"
            print(verdict)
            print(f"brief={brief_id} depends_on={depends_on} merged_ids={merged_ids} match={verdict}")
            return True
        print("allowed")
        print(f"brief={brief_id} depends_on={depends_on} merged_ids={merged_ids} match=allowed")
        return True
    except Exception as e:
        # Fail open: don't block dispatch on a parse error. Still emit
        # diagnostic so the failure is visible.
        print("allowed")
        print(f"brief={brief_id} depends_on={depends_on} history_ids=<error:{e}> match=allowed_on_error")
        return True


# ─── Action: check-depends-on-secrets ────────────────────────────────

def check_depends_on_secrets(paths):
    """Verify pending-dispatch brief's Depends-on-secrets env vars are set."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from assess import DEPENDS_ON_SECRETS_LINE_RE, parse_depends_on_value

    brief_id = ""
    secrets = []

    try:
        with open(paths["pending_dispatch"]) as f:
            spec = json.load(f)
        brief_id = spec.get("brief", "")
        brief_file = spec.get("brief_file", "")
        bf_path = os.path.join(paths["project_dir"], brief_file) if brief_file else ""

        if bf_path and os.path.exists(bf_path):
            with open(bf_path) as f:
                for line in f:
                    m = DEPENDS_ON_SECRETS_LINE_RE.match(line)
                    if m:
                        # Secrets are env-var names (FAKE_TOKEN_SL025), not
                        # brief ids — opt out of brief-082's id-shape validator.
                        secrets = parse_depends_on_value(
                            m.group(1), validate_brief_id=False
                        )
                        break

        if not secrets:
            print("allowed")
            return True

        for var in secrets:
            if not os.environ.get(var):
                print(f"blocked:{var}")
                return True

        print("allowed")
        return True
    except Exception as e:
        print("allowed")
        print(f"brief={brief_id} secrets=<error:{e}> match=allowed_on_error", file=sys.stderr)
        return True


# ─── Action: parse-cycle-wall-time-secs ──────────────────────────────

# Default matches MAX_CYCLE_WALL_TIME_SECS in daemon.sh
_DEFAULT_CYCLE_WALL_TIME_SECS = 5400


def parse_worker_model(brief_file_path):
    """Read the Model: frontmatter field from a brief card.

    Matches both YAML-frontmatter form (`Model: opus`) and legacy bold-markdown
    form (`**Model:** opus`), normalises to lowercase, validates against the
    allowed set {sonnet, opus, haiku}.

    Prints the resolved model name — callers use the printed value.
    Unrecognized value: prints 'sonnet' and emits a warning to stderr so a
    typo'd card never silently invokes the wrong model (issue #21 enum lesson).
    Prints nothing (empty) if no Model line is present; caller uses its default.

    Called by daemon.sh as:
        python3 actions.py parse-worker-model <absolute_brief_path>
    """
    if not brief_file_path or not os.path.exists(brief_file_path):
        return True
    try:
        with open(brief_file_path) as f:
            for line in f:
                m = MODEL_LINE_RE.match(line)
                if m:
                    raw = m.group(1).strip().lower()
                    # Strip trailing punctuation artefacts (parens, commas)
                    raw = raw.split("(")[0].split(",")[0].strip()
                    if raw in _ALLOWED_WORKER_MODELS:
                        print(raw)
                    else:
                        print(_DEFAULT_WORKER_MODEL)
                        print(
                            f"WARNING: unrecognized Model value '{raw}' in {brief_file_path}; "
                            f"allowed: {sorted(_ALLOWED_WORKER_MODELS)}; falling back to {_DEFAULT_WORKER_MODEL}",
                            file=sys.stderr,
                        )
                    return True
    except (IOError, OSError) as e:
        print(f"parse-worker-model error: {e}", file=sys.stderr)
    return True


def parse_cycle_wall_time_secs(paths):
    """Read Cycle-wall-time-secs frontmatter from pending-dispatch brief.

    Prints the integer value — brief override if present, daemon default otherwise.
    Used by tests to confirm the parser; daemon.sh uses its own bash grep/sed
    equivalent for the hot path.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from assess import CYCLE_WALL_TIME_SECS_LINE_RE

    try:
        with open(paths["pending_dispatch"]) as f:
            spec = json.load(f)
        brief_file = spec.get("brief_file", "")
        bf_path = os.path.join(paths["project_dir"], brief_file) if brief_file else ""

        if bf_path and os.path.exists(bf_path):
            with open(bf_path) as f:
                for line in f:
                    m = CYCLE_WALL_TIME_SECS_LINE_RE.match(line)
                    if m:
                        print(int(m.group(1)))
                        return True

        print(_DEFAULT_CYCLE_WALL_TIME_SECS)
        return True
    except Exception as e:
        print(_DEFAULT_CYCLE_WALL_TIME_SECS)
        print(f"parse-cycle-wall-time-secs error: {e}", file=sys.stderr)
        return True


# ─── Progress reset (brief-124) ──────────────────────────────────────

def ensure_progress_for_brief(progress_file: str, brief_id: str, brief_file: str) -> str:
    """Write a fresh progress.json if missing or `brief` field doesn't match brief_id.

    Called after each rebase in run_worker_iteration() to prevent inheriting a
    different brief's progress.json from main (brief-124 Bug 1).

    Returns:
        'unchanged'          — file exists with correct brief; nothing written
        'initialized'        — file was missing; written fresh
        'reset:<old_brief>'  — file existed with wrong brief; reset to fresh
    """
    existing_brief = None
    if os.path.exists(progress_file):
        try:
            with open(progress_file) as f:
                existing_brief = json.load(f).get("brief", "")
        except Exception:
            pass

    if existing_brief == brief_id:
        return "unchanged"

    os.makedirs(os.path.dirname(progress_file), exist_ok=True)
    with open(progress_file, "w") as f:
        json.dump({
            "brief": brief_id,
            "brief_file": brief_file,
            "iteration": 0,
            "status": "running",
            "tasks_completed": [],
            "tasks_remaining": [],
            "learnings": [],
        }, f, indent=2)
        f.write("\n")

    return "initialized" if existing_brief is None else f"reset:{existing_brief}"


# ─── Main ─────────────────────────────────────────────────────────────

def main():
    # parse-worker-model takes a single brief file path, not a project_dir.
    # Handle before the generic argc guard to avoid confusing error output.
    if len(sys.argv) >= 2 and sys.argv[1] == "parse-worker-model":
        brief_path = sys.argv[2] if len(sys.argv) >= 3 else ""
        parse_worker_model(brief_path)
        sys.exit(0)

    BRIEF_ACTIONS = ("move-to-eval", "move-to-pending-merges", "move-to-awaiting-review",
                     "approve-brief", "reject-brief", "close-brief", "ensure-progress-for-brief")

    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <action> <project_dir> [args]", file=sys.stderr)
        print("Actions: move-to-eval <brief_id> <project_dir>", file=sys.stderr)
        print("         move-to-pending-merges <brief_id> <project_dir>", file=sys.stderr)
        print("         move-to-awaiting-review <brief_id> <project_dir> <kind> [reason]", file=sys.stderr)
        print("         process-pending-merges <project_dir>", file=sys.stderr)
        print("         approve-brief <brief_id> <project_dir>", file=sys.stderr)
        print("         reject-brief <brief_id> <project_dir> [reason]", file=sys.stderr)
        print("         close-brief <brief_id> <project_dir> <delivered_via> [reason]", file=sys.stderr)
        print("         dispatch <project_dir>", file=sys.stderr)
        print("         merge <project_dir>", file=sys.stderr)
        print("         cleanup <project_dir>", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]

    # Actions that take <brief_id> <project_dir> [extra...]
    if action in BRIEF_ACTIONS:
        if len(sys.argv) < 4:
            print(f"{action} requires <brief_id> <project_dir>", file=sys.stderr)
            sys.exit(1)
        brief_id = sys.argv[2]
        project_dir = sys.argv[3]
        extra = sys.argv[4:]
    else:
        brief_id = ""
        project_dir = sys.argv[2]
        extra = sys.argv[3:]

    paths = init_paths(project_dir)

    try:
        if action == "move-to-eval":
            success = move_to_eval(paths, brief_id)
        elif action == "move-to-pending-merges":
            success = move_to_pending_merges(paths, brief_id)
        elif action == "move-to-awaiting-review":
            if not extra:
                print("move-to-awaiting-review requires <kind> [reason...]", file=sys.stderr)
                sys.exit(1)
            kind = extra[0]
            reason = " ".join(extra[1:]) if len(extra) > 1 else ""
            success = move_to_awaiting_review(paths, brief_id, kind, reason)
        elif action == "process-pending-merges":
            success = process_pending_merges(paths)
        elif action == "approve-brief":
            success = approve_brief(paths, brief_id)
        elif action == "reject-brief":
            reason = " ".join(extra) if extra else ""
            success = reject_brief(paths, brief_id, reason)
        elif action == "close-brief":
            if not extra:
                print("close-brief requires <delivered_via> [reason...]", file=sys.stderr)
                sys.exit(1)
            delivered_via = extra[0]
            reason = " ".join(extra[1:]) if len(extra) > 1 else ""
            success = close_as_delivered(paths, brief_id, delivered_via, reason)
        elif action == "dispatch":
            success = dispatch(paths)
        elif action == "merge":
            success = merge(paths)
        elif action == "cleanup":
            success = cleanup_worktrees(paths)
        elif action == "check-depends-on":
            success = check_depends_on(paths)
        elif action == "check-depends-on-secrets":
            success = check_depends_on_secrets(paths)
        elif action == "parse-cycle-wall-time-secs":
            success = parse_cycle_wall_time_secs(paths)
        elif action == "ensure-progress-for-brief":
            if len(extra) < 2:
                print("ensure-progress-for-brief requires <brief_file> <progress_file>", file=sys.stderr)
                sys.exit(1)
            brief_file_arg, progress_file_arg = extra[0], extra[1]
            result = ensure_progress_for_brief(progress_file_arg, brief_id, brief_file_arg)
            print(result)
            success = True
        else:
            print(f"Unknown action: {action}", file=sys.stderr)
            sys.exit(1)

        sys.exit(0 if success else 1)

    except subprocess.CalledProcessError as e:
        print(f"Git error in {action}: {e}", file=sys.stderr)
        if e.stderr:
            print(f"  stderr: {e.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error in {action}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
