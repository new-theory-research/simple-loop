#!/usr/bin/env python3
"""Shared dispatch-queue enumerator (brief-108-cont-a).

Pure function: glob wiki/briefs/cards/*/index.md, filter Status: queued,
exclude already-dispatched briefs from running.json, order by goals.md
queue position. Returns a structured list of dispatchable brief dicts.

CLI:
    python3 lib/queue.py <project_dir>
    Prints JSON array to stdout.

    python3 lib/queue.py <project_dir> --fingerprint
    Prints a short queue-state fingerprint (issue #17 — queen dedup key).
"""

import hashlib
import json
import os
import re
import sys


_BRIEF_ID_RE = re.compile(r"brief-\d+(?:-[\w-]+)?")


def _parse_card_status(card_path):
    """Return lowercased Status: value from YAML frontmatter, or ''."""
    try:
        with open(card_path) as f:
            in_fm = False
            for line in f:
                stripped = line.strip()
                if stripped == "---":
                    if not in_fm:
                        in_fm = True
                        continue
                    else:
                        break
                if in_fm and stripped.lower().startswith("status:"):
                    return stripped.split(":", 1)[1].strip().lower()
    except (IOError, OSError):
        pass
    return ""


def _goals_order(goals_path):
    """Return brief IDs in first-appearance order from goals.md."""
    seen = set()
    order = []
    try:
        with open(goals_path) as f:
            for line in f:
                for m in _BRIEF_ID_RE.finditer(line):
                    bid = m.group(0)
                    if bid not in seen:
                        seen.add(bid)
                        order.append(bid)
    except (IOError, OSError):
        pass
    return order


def enumerate_dispatchable(project_dir, running=None):
    """Return dispatchable brief candidates ordered by goals.md queue position.

    Scans wiki/briefs/cards/*/index.md, keeps only cards with Status: queued
    (exact match, case-insensitive) that are not already in running.json under
    active / awaiting_review / pending_merges / completed_pending_eval / history.
    Orders results by first appearance in .loop/state/goals.md; cards not
    mentioned in goals.md sort after all mentioned ones.

    Args:
        project_dir: project root path (str).
        running: parsed running.json dict, or None to read from disk.

    Returns:
        List of dicts with keys: brief, branch, brief_file.
    """
    cards_dir = os.path.join(project_dir, "wiki", "briefs", "cards")

    if running is None:
        running_path = os.path.join(project_dir, ".loop", "state", "running.json")
        try:
            with open(running_path) as f:
                running = json.load(f)
        except (IOError, OSError, json.JSONDecodeError):
            running = {}

    excluded = set()
    for key in ("active", "awaiting_review", "pending_merges", "completed_pending_eval", "history"):
        for entry in running.get(key, []):
            bid = entry.get("brief", "")
            if bid:
                excluded.add(bid)

    candidates = []
    if os.path.isdir(cards_dir):
        for card_id in sorted(os.listdir(cards_dir)):
            if card_id.startswith("."):
                continue
            if card_id in excluded:
                continue
            card_path = os.path.join(cards_dir, card_id, "index.md")
            if not os.path.isfile(card_path):
                continue
            if _parse_card_status(card_path) != "queued":
                continue
            candidates.append({
                "brief": card_id,
                "branch": card_id,
                "brief_file": f"wiki/briefs/cards/{card_id}/index.md",
            })

    goals_path = os.path.join(project_dir, ".loop", "state", "goals.md")
    order = _goals_order(goals_path)
    rank = {bid: i for i, bid in enumerate(order)}
    candidates.sort(key=lambda c: rank.get(c["brief"], len(order)))

    return candidates


def queue_fingerprint(project_dir):
    """Cheap queue-state fingerprint for the daemon's queen dedup key (issue #17).

    The queen dedup key used to be the trigger name alone, so queue mutations
    during the TTL window were invisible — queued briefs sat undispatched for
    up to 30 min (portal daemon, 2026-06-11). Folding this fingerprint into
    the dedup comparison makes any queue change invalidate the dedup.

    Combines goals.md stat (mtime_ns + size — also makes `touch goals.md` a
    manual dedup-buster) with the ordered dispatchable brief ids from
    enumerate_dispatchable(), so a new card, a status flip, a running.json
    change, or a goals.md edit/reorder all change the fingerprint.
    O(N cards), frontmatter reads only — cheap per daemon tick.
    """
    goals_path = os.path.join(project_dir, ".loop", "state", "goals.md")
    try:
        st = os.stat(goals_path)
        goals_sig = "%d:%d" % (st.st_mtime_ns, st.st_size)
    except OSError:
        goals_sig = "missing"

    ids = ",".join(c["brief"] for c in enumerate_dispatchable(project_dir))
    raw = "%s|%s" % (goals_sig, ids)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def main():
    args = [a for a in sys.argv[1:] if a != "--fingerprint"]
    project_dir = args[0] if args else os.getcwd()
    if "--fingerprint" in sys.argv[1:]:
        print(queue_fingerprint(project_dir))
    else:
        print(json.dumps(enumerate_dispatchable(project_dir), indent=2))


if __name__ == "__main__":
    main()
