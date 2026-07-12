#!/usr/bin/env python3
"""running.json card-derived projector (brief-108-d).

Pure function: walk wiki/briefs/cards/*/index.md + .loop/state/runtime-events.jsonl
and produce running.json's dict shape. Card frontmatter is the truth for *what
status a brief is in*; runtime-events.jsonl is the truth for *runtime facts*
(dispatched_at, merge_sha, worker_slot, kind, reason). running.json itself
becomes derived state — projected from those two sources.

This retires the multi-writer drift class where ad-hoc scripts and reconciliation
helpers could each splice running.json buckets independently.

# runtime-events.jsonl schema
#
# Append-only JSONL at .loop/state/runtime-events.jsonl. Each line is one event:
#
#   {"ts": "2026-05-04T18:30:00Z", "event": "dispatched", "brief": "<id>",
#    "branch": "<branch>", "brief_file": "wiki/briefs/cards/<id>/index.md",
#    "worker_slot": 0, "throttle": 2, "parallel_safe": false,
#    "edit_surface": ["..."]}
#
#   {"ts": "...", "event": "completed", "brief": "<id>",
#    "kind": "complete" | "watchdog-timed-out" | "rebase-blocked" |
#            "manual-recovery" | "merge-conflict",
#    "reason": "...", "auto_merge": false}
#
#   {"ts": "...", "event": "approved", "brief": "<id>"}
#       — emitted when a human approves an awaiting_review brief; flips runtime
#         fact so the next projection puts it in pending_merges instead of
#         awaiting_review.
#
#   {"ts": "...", "event": "merged", "brief": "<id>", "merge_sha": "abc123",
#    "merged_at": "..."}
#
# Bucketing
#
#   card Status: queued  → not in any bucket (queue.py enumerates these)
#   card Status: active  + no `completed` event              → active[]
#   card Status: active  + `completed` event, no `approved`  → awaiting_review[]
#   card Status: active  + `approved` event                  → pending_merges[]
#   card Status: merged  → history[]
#
# Schema-parity goal: emitted shape mirrors today's running.json byte-for-byte
# on the frozen fixture. Hive reads running.json; the projector cannot change
# its shape in this brief. Schema evolution is a follow-up.

CLI:
    python3 lib/state.py project-running-json <project_dir>
        Prints projected running.json to stdout.

    python3 lib/state.py append-event <project_dir> <event_type> <brief> [k=v ...]
        Append one event line to runtime-events.jsonl.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone


# ── Card frontmatter parsing ──────────────────────────────────────────

# Existing card-as-truth contract: queued / active / merged / rejected /
# not-doing / draft. brief-108-d adds NO new statuses; the bucketing logic
# joins card status with runtime-events.jsonl.
def _parse_card_frontmatter(card_path):
    """Read a card's YAML frontmatter into a dict (lowercased keys).

    Returns {} on read errors. Handles only YAML-style cards (post-brief-108
    canonical shape) — the prose form (**Field:** value) was retired by
    brief-108-cont-c.
    """
    out = {}
    try:
        with open(card_path) as f:
            in_fm = False
            current_key = None
            for raw in f:
                line = raw.rstrip("\n")
                stripped = line.strip()
                if stripped == "---":
                    if not in_fm:
                        in_fm = True
                        continue
                    else:
                        break
                if not in_fm:
                    continue
                # List continuation: "  - foo" extends the current_key
                m_list = re.match(r"^\s+-\s*(.*\S)\s*$", line)
                if m_list and current_key and isinstance(out.get(current_key), list):
                    item = m_list.group(1).strip()
                    if item and not (item.startswith("[") and item.endswith("]")):
                        out[current_key].append(item)
                    continue
                # New key: "Key: value" or "Key:" (list opener)
                m_kv = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$", line)
                if m_kv:
                    key = m_kv.group(1).lower()
                    val = m_kv.group(2).strip()
                    current_key = key
                    if val == "":
                        out[key] = []
                    else:
                        out[key] = val
    except (IOError, OSError):
        pass
    return out


def _normalize_bool(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().strip('"').strip("'").lower() == "true"
    return False


def _card_status(fm):
    s = fm.get("status", "")
    if isinstance(s, list):
        return ""
    return str(s).strip().lower()


# ── Runtime-events log ────────────────────────────────────────────────

def _read_runtime_events(events_path):
    """Read the runtime-events.jsonl log into a list. Tolerates missing file."""
    events = []
    if not os.path.exists(events_path):
        return events
    try:
        with open(events_path) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    # Parser-permissive: bad lines drop silently. Lint surface
                    # forces write-time cleanup.
                    continue
    except (IOError, OSError):
        pass
    return events


def _index_events_by_brief(events):
    """Group events by brief id, preserving append order."""
    out = {}
    for e in events:
        brief = e.get("brief", "")
        if not brief:
            continue
        out.setdefault(brief, []).append(e)
    return out


# ── Card walk ─────────────────────────────────────────────────────────

def _walk_cards(project_dir):
    """Yield (brief_id, card_path, frontmatter_dict) for each card."""
    cards_dir = os.path.join(project_dir, "wiki", "briefs", "cards")
    if not os.path.isdir(cards_dir):
        return
    for name in sorted(os.listdir(cards_dir)):
        if name.startswith("."):
            continue
        card_path = os.path.join(cards_dir, name, "index.md")
        if not os.path.isfile(card_path):
            continue
        fm = _parse_card_frontmatter(card_path)
        yield name, card_path, fm


# ── Entry shape builders ──────────────────────────────────────────────

def _canonical_brief_file(brief_id):
    return f"wiki/briefs/cards/{brief_id}/index.md"


def _current_generation(events_for_brief):
    """Suffix of events starting at the LAST `dispatched` event — the current
    generation. A re-queued brief gets a fresh `dispatched` event; completed/
    approved events from before it belong to a previous generation and must not
    bucket the new run (brief-249 re-queue bounce, 2026-06-11: stale completed
    event projected a freshly re-dispatched brief into awaiting_review, so the
    worker never spawned). No `dispatched` event → full history (backfill).
    """
    last = None
    for i, e in enumerate(events_for_brief):
        if e.get("event") == "dispatched":
            last = i
    if last is None:
        return events_for_brief
    return events_for_brief[last:]


def _dispatched_event(events_for_brief):
    # Last dispatched event = the current generation's dispatch (re-queued
    # briefs are dispatched more than once).
    last = None
    for e in events_for_brief:
        if e.get("event") == "dispatched":
            last = e
    return last


def _completed_event(events_for_brief):
    last = None
    for e in _current_generation(events_for_brief):
        if e.get("event") == "completed":
            last = e
    return last


def _approved_event(events_for_brief):
    for e in _current_generation(events_for_brief):
        if e.get("event") == "approved":
            return e
    return None


def _merged_event(events_for_brief):
    last = None
    for e in events_for_brief:
        if e.get("event") == "merged":
            last = e
    return last


def _superseded_event(events_for_brief):
    last = None
    for e in events_for_brief:
        if e.get("event") == "superseded":
            last = e
    return last


def _build_active_entry(brief_id, fm, events_for_brief):
    """Shape of an active[] entry — mirrors actions.dispatch()'s splice."""
    disp = _dispatched_event(events_for_brief) or {}
    edit_surface = fm.get("edit-surface")
    if not isinstance(edit_surface, list):
        edit_surface = []
    parallel_safe = _normalize_bool(fm.get("parallel-safe", False))
    entry = {
        "brief": brief_id,
        "branch": disp.get("branch") or fm.get("branch", brief_id),
        "brief_file": disp.get("brief_file") or _canonical_brief_file(brief_id),
        "dispatched_at": disp.get("ts", ""),
        "parallel_safe": parallel_safe,
        "edit_surface": list(edit_surface),
        "worker_slot": disp.get("worker_slot", 0),
    }
    return entry


def _build_awaiting_review_entry(brief_id, fm, events_for_brief):
    """active[] shape + completed_at + auto_merge=False + kind/reason from
    completed event."""
    entry = _build_active_entry(brief_id, fm, events_for_brief)
    comp = _completed_event(events_for_brief) or {}
    entry["completed_at"] = comp.get("ts", "")
    entry["auto_merge"] = _normalize_bool(comp.get("auto_merge", False))
    if "kind" in comp:
        entry["kind"] = comp["kind"]
    if comp.get("reason"):
        entry["reason"] = comp["reason"]
        entry["conflict_note"] = comp["reason"]
    return entry


def _build_pending_merges_entry(brief_id, fm, events_for_brief):
    """awaiting_review shape + approved_at + auto_merge=True (the approve flips
    auto_merge per actions.approve_brief)."""
    entry = _build_awaiting_review_entry(brief_id, fm, events_for_brief)
    appr = _approved_event(events_for_brief) or {}
    if appr.get("ts"):
        entry["approved_at"] = appr["ts"]
    entry["auto_merge"] = True
    return entry


def _build_history_entry(brief_id, fm, events_for_brief):
    """history[] entry — minimal merge record. Today's running.json carries two
    shapes: backfilled (brief/branch/merged_at/merge_sha/evaluation/reason) and
    full-lifecycle (active fields + status=merged + merge_sha/merged_at). The
    projector emits the full-lifecycle shape when runtime-events have a
    `dispatched` event for this brief, else the backfilled shape.
    """
    merged = _merged_event(events_for_brief) or {}
    disp = _dispatched_event(events_for_brief)
    if disp is None:
        # Backfilled shape: minimal.
        return {
            "brief": brief_id,
            "branch": fm.get("branch", brief_id),
            "merged_at": merged.get("merged_at", merged.get("ts", "")),
            "merge_sha": merged.get("merge_sha", ""),
            "evaluation": merged.get("evaluation", ""),
            "reason": merged.get("reason", "backfilled_from_git"),
        }
    # Full-lifecycle shape: mirror what actions.merge() leaves behind.
    entry = _build_active_entry(brief_id, fm, events_for_brief)
    comp = _completed_event(events_for_brief)
    if comp is not None:
        entry["completed_at"] = comp.get("ts", "")
        entry["auto_merge"] = _normalize_bool(comp.get("auto_merge", False))
        if "kind" in comp:
            entry["kind"] = comp["kind"]
        if comp.get("reason"):
            entry["reason"] = comp["reason"]
            entry["conflict_note"] = comp["reason"]
    appr = _approved_event(events_for_brief)
    if appr and appr.get("ts"):
        entry["approved_by_human_at"] = appr["ts"]
    entry["status"] = "merged"
    entry["merge_sha"] = merged.get("merge_sha", "")
    entry["merged_at"] = merged.get("merged_at", merged.get("ts", ""))
    return entry


def _build_superseded_history_entry(brief_id, fm, events_for_brief):
    """history[] entry for superseded briefs (work shipped through another door).

    Mirrors _build_history_entry shape but carries delivered_via + superseded_at
    instead of merge_sha + merged_at. Uses full-lifecycle shape when a dispatched
    event exists, else minimal backfill shape.
    """
    sup = _superseded_event(events_for_brief) or {}
    disp = _dispatched_event(events_for_brief)
    if disp is None:
        return {
            "brief": brief_id,
            "branch": fm.get("branch", brief_id),
            "status": "superseded",
            "delivered_via": sup.get("delivered_via", ""),
            "reason": sup.get("reason", ""),
            "superseded_at": sup.get("ts", ""),
        }
    entry = _build_active_entry(brief_id, fm, events_for_brief)
    entry["status"] = "superseded"
    entry["delivered_via"] = sup.get("delivered_via", "")
    if sup.get("reason"):
        entry["reason"] = sup["reason"]
    entry["superseded_at"] = sup.get("ts", "")
    return entry


# ── Projector ─────────────────────────────────────────────────────────

def _card_program(fm):
    """Return lowercased program lane from frontmatter, or ''."""
    p = fm.get("program", "")
    if isinstance(p, list):
        return ""
    return str(p).strip().lower()


def _lane_set(lane):
    """Normalize a lane spec into a set of lane keys, or None for "no filter".

    Mirrors queue.py's `_lane_set` so a lane-scoped daemon's running.json
    projection partitions on the SAME comma-separated lane list the queue
    enumerator uses (multi-lane-daemon). A one-element list is the degenerate
    single-lane case, byte-for-byte unchanged from the exact-match era; empty /
    whitespace / all-commas / None → None (global projection, single-daemon).
    """
    if not lane:
        return None
    keys = {part.strip().lower() for part in lane.split(",")}
    keys.discard("")
    return keys or None


def project_running_json(project_dir, events=None, lane=None):
    """Project running.json from card frontmatter + runtime-events.jsonl.

    Args:
        project_dir: absolute project root path.
        events: parsed runtime-events list (for tests). None → read from disk.
        lane: optional program-lane partition — a comma-separated lane list
            (harness-001/003, multi-lane-daemon). When None, empty, whitespace-
            only, or all-commas the projection is global — all briefs, no lane
            filter. Single-daemon behavior is byte-for-byte unchanged (single
            lane is the one-element degenerate case). When it names one or more
            lanes, only briefs whose card Program: is in the lane set appear in
            active[], awaiting_review[], and pending_merges[]. history[] is
            always global (read-only, cosmetic). A brief with NO Program: field
            is excluded from a lane-scoped projection (fail-closed — same
            semantics as queue.py's --lane).

    Returns:
        dict with keys: active, completed_pending_eval, pending_merges,
        awaiting_review, history. Bucket population follows the rules in the
        module docstring.

    Notes:
        - completed_pending_eval is intentionally always empty in the projector.
          The legacy bucket existed for the validator-eval flow that's been
          retired; today's daemon goes active → awaiting_review (human-gate)
          or active → pending_merges (auto-merge).
        - History order matches first-merged-first (events file is append-only
          and reflects merge order). Backfill order matches the events file.
    """
    # Normalise lane the same way queue.py does: a comma-separated lane list →
    # a set, empty/whitespace/all-commas → None (no filter). Multi-lane lets one
    # daemon's projection span several lanes (multi-lane-daemon).
    lane_set = _lane_set(lane)

    if events is None:
        events_path = os.path.join(project_dir, ".loop", "state", "runtime-events.jsonl")
        events = _read_runtime_events(events_path)

    by_brief = _index_events_by_brief(events)

    # Build a merged-id-order index from events to preserve history ordering.
    merged_order = []
    seen_in_order = set()
    for e in events:
        if e.get("event") == "merged":
            bid = e.get("brief", "")
            if bid and bid not in seen_in_order:
                seen_in_order.add(bid)
                merged_order.append(bid)

    out = {
        "active": [],
        "completed_pending_eval": [],
        "pending_merges": [],
        "awaiting_review": [],
        "history": [],
    }

    # Walk cards once; route by status + completion/approval state.
    history_entries = {}
    for brief_id, card_path, fm in _walk_cards(project_dir):
        status = _card_status(fm)
        evs = by_brief.get(brief_id, [])

        if status == "queued":
            # Queue surface — queue.py enumerates these.
            continue

        if status == "active":
            # Lane filter: when lane_key is set, exclude briefs not in this lane.
            # A brief with no Program: field is excluded (fail-closed — a lane
            # daemon must never inherit another lane's active state).
            if lane_set is not None and _card_program(fm) not in lane_set:
                continue
            comp = _completed_event(evs)
            appr = _approved_event(evs)
            if appr is not None:
                out["pending_merges"].append(_build_pending_merges_entry(brief_id, fm, evs))
            elif comp is not None:
                out["awaiting_review"].append(_build_awaiting_review_entry(brief_id, fm, evs))
            else:
                out["active"].append(_build_active_entry(brief_id, fm, evs))
            continue

        if status == "merged":
            history_entries[brief_id] = _build_history_entry(brief_id, fm, evs)
            continue

        if status == "superseded":
            # Work shipped through another door (loop close --delivered-via).
            history_entries[brief_id] = _build_superseded_history_entry(brief_id, fm, evs)
            continue

        # rejected / not-doing / draft / unknown → no bucket; not user-visible
        # in running.json today (cards are the surface for those).

    # History ordering: merged-event order, then any merged-cards without
    # merged events (shouldn't happen in a clean state but defensive).
    for bid in merged_order:
        if bid in history_entries:
            out["history"].append(history_entries.pop(bid))
    # Append any leftover merged cards without a merged event (e.g., backfilled
    # via card status flip but no events log entry). Sort by brief id for
    # determinism.
    for bid in sorted(history_entries.keys()):
        out["history"].append(history_entries[bid])

    return out


# ── Append helper ─────────────────────────────────────────────────────

def append_event(project_dir, event_type, brief, **fields):
    """Append one event to runtime-events.jsonl. Creates the file if absent.

    Args:
        project_dir: absolute project root.
        event_type: one of dispatched, completed, approved, merged.
        brief: brief id.
        **fields: extra fields written into the event line. `ts` is supplied
            automatically if absent.

    Returns the event dict that was written.
    """
    events_path = os.path.join(project_dir, ".loop", "state", "runtime-events.jsonl")
    os.makedirs(os.path.dirname(events_path), exist_ok=True)
    payload = {
        "ts": fields.pop("ts", None) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event_type,
        "brief": brief,
    }
    payload.update(fields)
    # brief-165 presence plane: stamp the additive {box, lane} fields from the
    # environment when a caller did not pass them and the env names a value.
    # LOOP_LANE is exported by lib/daemon.sh; LOOP_BOX names the machine. Both
    # absent → byte-identical to the pre-165 single-box line (additive only).
    if payload.get("lane") is None:
        _lane = os.environ.get("LOOP_LANE") or None
        if _lane:
            payload["lane"] = _lane
        else:
            payload.pop("lane", None)
    if payload.get("box") is None:
        _box = os.environ.get("LOOP_BOX") or None
        if _box:
            payload["box"] = _box
        else:
            payload.pop("box", None)
    with open(events_path, "a") as f:
        f.write(json.dumps(payload) + "\n")
    return payload


# ── Atomic write of running.json from projection ──────────────────────

def write_running_json(project_dir, data=None, lane=None):
    """Project (or accept pre-projected data) and write running.json atomically.

    THIS IS THE ONLY FUNCTION ALLOWED TO WRITE running.json. Lint enforces.

    Args:
        project_dir: absolute project root path.
        data: pre-projected dict, or None to project from cards+events.
        lane: optional program-lane partition key (harness-001/003). Passed
            through to project_running_json; see its docstring. Empty/None →
            global projection (single-daemon, backward-compat).
    """
    if data is None:
        data = project_running_json(project_dir, lane=lane)
    running_path = os.path.join(project_dir, ".loop", "state", "running.json")
    os.makedirs(os.path.dirname(running_path), exist_ok=True)
    tmp = running_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, running_path)
    return data


# ── CLI ───────────────────────────────────────────────────────────────

def _usage():
    print(__doc__, file=sys.stderr)
    sys.exit(1)


def _parse_cli_lane(args, start_idx):
    """Extract --lane <value> from args starting at start_idx.

    Returns (lane_value_or_None, remaining_positional_args).
    Empty/whitespace lane → None (same normalisation as queue.py).
    """
    lane = None
    positional = []
    i = start_idx
    while i < len(args):
        a = args[i]
        if a == "--lane":
            i += 1
            if i < len(args):
                lane = args[i]
        elif a.startswith("--lane="):
            lane = a[len("--lane="):]
        else:
            positional.append(a)
        i += 1
    if lane is not None and not lane.strip():
        lane = None
    return lane, positional


def main():
    if len(sys.argv) < 2:
        _usage()
    cmd = sys.argv[1]

    if cmd == "project-running-json":
        if len(sys.argv) < 3:
            _usage()
        lane, positional = _parse_cli_lane(sys.argv, 2)
        project_dir = positional[0] if positional else sys.argv[2]
        result = project_running_json(project_dir, lane=lane)
        print(json.dumps(result, indent=2))
        return 0

    if cmd == "write-running-json":
        if len(sys.argv) < 3:
            _usage()
        lane, positional = _parse_cli_lane(sys.argv, 2)
        project_dir = positional[0] if positional else sys.argv[2]
        write_running_json(project_dir, lane=lane)
        return 0

    if cmd == "append-event":
        if len(sys.argv) < 5:
            _usage()
        project_dir = sys.argv[2]
        event_type = sys.argv[3]
        brief = sys.argv[4]
        fields = {}
        # Int coercion is allowlisted: SHAs and other identifiers can be
        # all-digit strings (e.g. short-SHA `92329478`); auto-int-coercing
        # them turns merge_sha into a JSON number, which downstream consumers
        # (notably hive's typed RunningJson parser) can't deserialize.
        INT_FIELDS = {"worker_slot", "throttle", "iteration", "duration_ms", "in_flight_count"}
        for kv in sys.argv[5:]:
            if "=" in kv:
                k, _, v = kv.partition("=")
                if v.lower() in ("true", "false"):
                    fields[k] = v.lower() == "true"
                elif k in INT_FIELDS:
                    try:
                        fields[k] = int(v)
                    except ValueError:
                        fields[k] = v
                else:
                    fields[k] = v
        append_event(project_dir, event_type, brief, **fields)
        return 0

    _usage()


if __name__ == "__main__":
    sys.exit(main() or 0)
