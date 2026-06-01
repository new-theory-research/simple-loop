#!/usr/bin/env python3
"""Daemon startup state repair — runs once per process start before the tick loop.

Three repair operations (added per brief-019 tasks 2-4):
  A. dedup_active: remove duplicate entries from active[]
  B. backfill_history: add merge-committed briefs to history[] (task 3)
  C. clean_stale_queues: remove queue files for already-merged briefs (task 4)

All functions are pure: take a running dict, return (new_dict, action_list).
The orchestrator run_startup_repair() loads state, calls repairs, saves if changed,
logs all actions to log.jsonl.
"""

import glob as _glob
import json
import os
import re
import subprocess
from datetime import datetime, timezone

# Matches brief IDs in git commit subjects: brief-*, audit-*, capture-*
_BRIEF_ID_RE = re.compile(r'\b((?:brief|audit|capture)-[a-zA-Z0-9][a-zA-Z0-9-]*)', re.IGNORECASE)


def dedup_active(running: dict) -> tuple:
    """Keep first occurrence of each brief ID in active[]. Return (repaired, actions).

    Duplicate active entries cause the queen to re-dispatch already-running
    briefs. This trims the array to first-seen per brief ID.
    """
    seen: set = set()
    new_active = []
    removed: dict = {}

    for entry in running.get("active", []):
        brief_id = entry.get("brief")
        if brief_id not in seen:
            seen.add(brief_id)
            new_active.append(entry)
        else:
            removed[brief_id] = removed.get(brief_id, 0) + 1

    if not removed:
        return running, []

    result = dict(running)
    result["active"] = new_active
    actions = [
        {"reason": "duplicate_active_entry", "brief": brief_id, "count_removed": count}
        for brief_id, count in removed.items()
    ]
    return result, actions


def backfill_history(running: dict, project_dir: str, card_dirs: list,
                     main_branch: str = "main") -> tuple:
    """For each brief with a merge commit on main, move/add it to history[].

    Scans git log for merge commits. Brief IDs are extracted from commit subjects
    using a pattern that handles both actions.py format ("Merge brief-NNN: title")
    and hand-merge format ("Merge branch 'brief-NNN' into main").

    - If brief is already in history[]: skip.
    - If brief is in active[] or completed_pending_eval[]: move it to history[].
    - If brief is absent from all arrays but has a merge commit: add to history[].
    """
    try:
        result = subprocess.run(
            ["git", "log", "--merges", f"--format=%H|%cI|%s", main_branch],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        raw = result.stdout.strip()
        merge_lines = raw.split("\n") if raw else []
    except subprocess.CalledProcessError:
        return running, []

    # Build merged map: brief_id -> (sha, iso_timestamp)
    merged: dict = {}
    for line in merge_lines:
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, ts, subject = parts
        match = _BRIEF_ID_RE.search(subject)
        if match:
            brief_id = match.group(1)
            if brief_id not in merged:
                merged[brief_id] = (sha, ts)

    if not merged:
        return running, []

    history = list(running.get("history", []))
    active = list(running.get("active", []))
    completed = list(running.get("completed_pending_eval", []))
    awaiting_review = list(running.get("awaiting_review", []))
    actions = []

    history_briefs = {e.get("brief") for e in history}

    for brief_id, (sha, ts) in merged.items():
        if brief_id in history_briefs:
            continue

        active = [e for e in active if e.get("brief") != brief_id]
        completed = [e for e in completed if e.get("brief") != brief_id]
        awaiting_review = [e for e in awaiting_review if e.get("brief") != brief_id]

        history.insert(0, {
            "brief": brief_id,
            "branch": brief_id,
            "merged_at": ts,
            "merge_sha": sha,
            "evaluation": "",
            "reason": "backfilled_from_git",
        })
        history_briefs.add(brief_id)
        actions.append({"reason": "backfilled_from_git", "brief": brief_id, "merge_sha": sha})

    if not actions:
        return running, []

    result_running = dict(running)
    result_running["history"] = history
    result_running["active"] = active
    result_running["completed_pending_eval"] = completed
    result_running["awaiting_review"] = awaiting_review
    return result_running, actions


def clean_stale_queues(running: dict, state_dir: str, project_dir: str,
                       main_branch: str = "main") -> tuple:
    """Remove pending-merge.json / pending-dispatch.json if their brief is already merged.

    Reads each queue file, looks up the brief ID in git log --merges. If a merge
    commit exists for that brief, the queue file is stale — remove it and log with
    reason: queue_file_stale_post_merge. Does NOT touch signals/*.json.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--merges", "--format=%s", main_branch],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        raw = result.stdout.strip()
        merge_subjects = raw.split("\n") if raw else []
    except subprocess.CalledProcessError:
        return running, []

    merged_ids: set = set()
    for subject in merge_subjects:
        match = _BRIEF_ID_RE.search(subject)
        if match:
            merged_ids.add(match.group(1))

    if not merged_ids:
        return running, []

    queue_files = [
        os.path.join(state_dir, "pending-merge.json"),
        os.path.join(state_dir, "pending-dispatch.json"),
    ]

    actions = []
    for qf in queue_files:
        if not os.path.exists(qf):
            continue
        try:
            with open(qf) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        brief_id = data.get("brief")
        if brief_id and brief_id in merged_ids:
            try:
                os.remove(qf)
                actions.append({
                    "reason": "queue_file_stale_post_merge",
                    "brief": brief_id,
                    "file": os.path.basename(qf),
                })
            except OSError:
                pass

    return running, actions


def run_startup_repair(paths: dict, project_dir: str) -> list:
    """Orchestrator: run all repair steps, save if any changes, log all actions.

    Returns the list of repair action dicts (empty if nothing was repaired).
    Set NT_DAEMON_STARTUP_REPAIR=false to skip all repair (escape hatch).
    """
    from actions import load_running, save_running, log_action, read_config

    if os.environ.get("NT_DAEMON_STARTUP_REPAIR", "true").lower() == "false":
        with open(paths["log_file"], "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "action": "daemon:startup_repair_disabled",
                "reason": "NT_DAEMON_STARTUP_REPAIR=false",
            }) + "\n")
        return []

    t_start = datetime.now(timezone.utc)
    running = load_running(paths)
    all_actions: list = []

    # A. Dedup active[]
    running, actions = dedup_active(running)
    all_actions.extend(actions)

    # B. Backfill history from git log
    config = read_config(paths["loop_dir"])
    main_branch = config.get("GIT_MAIN_BRANCH", "main")
    cards_dir = os.path.join(project_dir, "wiki", "briefs", "cards")
    card_dirs = (
        [os.path.basename(p) for p in _glob.glob(os.path.join(cards_dir, "*")) if os.path.isdir(p)]
        if os.path.isdir(cards_dir)
        else []
    )
    running, actions = backfill_history(running, project_dir, card_dirs, main_branch)
    all_actions.extend(actions)

    # C. Clean stale queue files
    running, actions = clean_stale_queues(running, paths["state_dir"], project_dir, main_branch)
    all_actions.extend(actions)

    # Per-action log entries used to be written here, but they stamped current
    # `datetime.now()` on every backfilled brief — making old briefs appear in
    # the Dance Floor as "Xm ago" on every daemon restart. The summary entry
    # below (startup_repair_complete) carries the same `actions` payload, so
    # audit information is preserved without polluting the recent-activity feed.
    if all_actions:
        save_running(paths, running)

    # brief-108-d: seed runtime-events.jsonl from the (potentially repaired)
    # running.json so the projector has the runtime facts. Idempotent — second
    # daemon start does nothing because brief ids already in the log are skipped.
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from migrate_runtime_events import migrate as _migrate
        _events_added = _migrate(project_dir)
        if _events_added:
            log_action(paths, "startup_repair", {
                "reason": "migrate_runtime_events",
                "events_added": _events_added,
            })
            # Re-project running.json from cards + events now that the events
            # log is seeded — single-writer ownership.
            from state import write_running_json as _write_running
            _write_running(project_dir)
    except Exception as _e:
        log_action(paths, "startup_repair", {
            "reason": "migrate_runtime_events_failed",
            "error": str(_e),
        })

    duration_ms = int((datetime.now(timezone.utc) - t_start).total_seconds() * 1000)
    summary = {
        "timestamp": t_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": "daemon:startup_repair_complete",
        "actions": all_actions,
        "duration_ms": duration_ms,
    }
    with open(paths["log_file"], "a") as f:
        f.write(json.dumps(summary) + "\n")

    return all_actions
