#!/usr/bin/env python3
"""brief-108-d migration: synthesize runtime-events.jsonl from running.json.

Idempotent. Reads .loop/state/running.json; for each entry across active /
awaiting_review / pending_merges / history, emits the equivalent runtime-event
lines so projecting from cards + events reproduces today's running.json state.

Skips brief ids already present in runtime-events.jsonl (idempotency guard).

CLI:
    python3 lib/migrate_runtime_events.py <project_dir>
"""

import json
import os
import sys
from datetime import datetime, timezone


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_brief_id(bid, card_ids):
    """Resolve a possibly-truncated brief id to the canonical card-dir slug.

    Legacy running.json history carries truncated ids ("brief-042") while the
    cards on disk use full slugs ("brief-042-camera-system-architecture").
    This function walks the set of card_ids and returns the longest unique
    prefix-match, or the original bid if no match exists.

    Symmetric with actions._brief_id_matches but here we resolve to a single
    canonical id (the cards are the truth, not the running.json history line).
    """
    if not bid or bid in card_ids:
        return bid
    candidates = [c for c in card_ids if c == bid or c.startswith(bid + "-") or bid.startswith(c + "-")]
    if len(candidates) == 1:
        return candidates[0]
    # Multiple matches → ambiguous. Don't guess; keep the original.
    return bid


def _list_card_ids(project_dir):
    cards_dir = os.path.join(project_dir, "wiki", "briefs", "cards")
    if not os.path.isdir(cards_dir):
        return set()
    return {
        name for name in os.listdir(cards_dir)
        if not name.startswith(".") and os.path.isfile(
            os.path.join(cards_dir, name, "index.md")
        )
    }


def _normalize_path(p):
    """Rewrite stale .loop/briefs/<brief>.md paths to canonical card paths.

    brief-108-cont-b retired .loop/briefs/. running.json history still carries
    the stale path string for pre-cont-a dispatches; this function rewrites
    them on the way into runtime-events.jsonl. Closes brief-108-cont-b
    worker surprise #3 (stale path residue).
    """
    if not p or not isinstance(p, str):
        return p
    prefix = ".loop/briefs/"
    if p.startswith(prefix) and p.endswith(".md"):
        # ".loop/briefs/brief-XYZ.md" → "wiki/briefs/cards/brief-XYZ/index.md"
        bid = p[len(prefix):-len(".md")]
        return f"wiki/briefs/cards/{bid}/index.md"
    return p


def _existing_briefs(events_path):
    """Read events file (if any) and return set of brief ids already present."""
    seen = set()
    if not os.path.exists(events_path):
        return seen
    with open(events_path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            bid = e.get("brief", "")
            if bid:
                seen.add(bid)
    return seen


def _emit(events, **fields):
    fields.setdefault("ts", _utc_now())
    events.append(fields)


def _from_active(entry, events):
    bid = entry.get("brief", "")
    if not bid:
        return
    _emit(events,
          ts=entry.get("dispatched_at") or _utc_now(),
          event="dispatched",
          brief=bid,
          branch=entry.get("branch", bid),
          brief_file=_normalize_path(entry.get("brief_file", "")),
          worker_slot=entry.get("worker_slot", 0),
          parallel_safe=bool(entry.get("parallel_safe", False)),
          edit_surface=list(entry.get("edit_surface", [])))


def _from_awaiting_review(entry, events):
    bid = entry.get("brief", "")
    if not bid:
        return
    _from_active(entry, events)
    _emit(events,
          ts=entry.get("completed_at") or _utc_now(),
          event="completed",
          brief=bid,
          kind=entry.get("kind", "complete"),
          auto_merge=bool(entry.get("auto_merge", False)),
          reason=entry.get("reason", ""))


def _from_pending_merges(entry, events):
    bid = entry.get("brief", "")
    if not bid:
        return
    _from_awaiting_review(entry, events)
    _emit(events,
          ts=entry.get("approved_at") or _utc_now(),
          event="approved",
          brief=bid)


def _from_history(entry, events):
    bid = entry.get("brief", "")
    if not bid:
        return
    # Full-lifecycle entries have dispatched_at; backfilled don't.
    if entry.get("dispatched_at"):
        _from_active(entry, events)
    if entry.get("completed_at"):
        _emit(events,
              ts=entry.get("completed_at"),
              event="completed",
              brief=bid,
              kind=entry.get("kind", "complete"),
              auto_merge=bool(entry.get("auto_merge", False)),
              reason=entry.get("reason", ""))
    if entry.get("approved_by_human_at") or entry.get("approved_at"):
        _emit(events,
              ts=entry.get("approved_by_human_at") or entry.get("approved_at"),
              event="approved",
              brief=bid)
    _emit(events,
          ts=entry.get("merged_at") or _utc_now(),
          event="merged",
          brief=bid,
          merge_sha=entry.get("merge_sha")
                    or entry.get("merge_sha_simple_loop")
                    or entry.get("merge_sha_portal")
                    or "",
          merged_at=entry.get("merged_at") or _utc_now(),
          evaluation=entry.get("evaluation", ""),
          reason=entry.get("reason", "backfilled_from_git"))


def migrate(project_dir):
    """Walk running.json and append synthesized events to runtime-events.jsonl.

    Idempotent: skips brief ids already in the events file. Returns the count
    of new events written.
    """
    state_dir = os.path.join(project_dir, ".loop", "state")
    running_path = os.path.join(state_dir, "running.json")
    events_path = os.path.join(state_dir, "runtime-events.jsonl")

    if not os.path.exists(running_path):
        print(f"migrate: no running.json at {running_path}", file=sys.stderr)
        return 0

    with open(running_path) as f:
        rc = json.load(f)

    seen = _existing_briefs(events_path)
    card_ids = _list_card_ids(project_dir)

    def _resolve_in_place(entry):
        bid = entry.get("brief", "")
        if bid:
            entry = dict(entry)
            entry["brief"] = _resolve_brief_id(bid, card_ids)
        return entry

    new_events = []
    for entry in rc.get("active", []):
        entry = _resolve_in_place(entry)
        if entry.get("brief", "") in seen:
            continue
        _from_active(entry, new_events)

    for entry in rc.get("awaiting_review", []):
        entry = _resolve_in_place(entry)
        if entry.get("brief", "") in seen:
            continue
        _from_awaiting_review(entry, new_events)

    for entry in rc.get("pending_merges", []):
        entry = _resolve_in_place(entry)
        if entry.get("brief", "") in seen:
            continue
        _from_pending_merges(entry, new_events)

    for entry in rc.get("completed_pending_eval", []):
        entry = _resolve_in_place(entry)
        if entry.get("brief", "") in seen:
            continue
        _from_awaiting_review(entry, new_events)

    for entry in rc.get("history", []):
        entry = _resolve_in_place(entry)
        if entry.get("brief", "") in seen:
            continue
        _from_history(entry, new_events)

    if not new_events:
        print("migrate: no new events to write (idempotent no-op)")
        return 0

    os.makedirs(state_dir, exist_ok=True)
    with open(events_path, "a") as f:
        for e in new_events:
            f.write(json.dumps(e) + "\n")

    print(f"migrate: wrote {len(new_events)} events to {events_path}")
    return len(new_events)


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    project_dir = sys.argv[1]
    migrate(project_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
