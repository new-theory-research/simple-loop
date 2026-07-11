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

    Both accept `--lane <spec>`, where <spec> is a comma-separated list of
    program lanes (`--lane "finetune,capture,fleets"`). A card matches when its
    Program: is in the lane set; an unlabeled card never matches a non-empty
    set (fail-closed). Empty/whitespace/absent lane = no filter (single-daemon
    behavior, byte-for-byte unchanged). Single lane is the one-element case.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time


# Card-id shape for goals.md ranking: `<lane>-<number><letter?>(-slug)?` with any
# lowercase lane prefix (issue #50 — `ft-011-fp3t5-base-serve` etc. never ranked
# because the regex hardcoded `brief-`). The `(?<![\w-])` lookbehind pins the lane
# to a word start so the scan can't grab the tail of a hyphenated prose token
# (e.g. the `brief-12` inside `sub-brief-12`) or a numeric slice of a date
# (`2026-07-11` has no leading letters, so `[a-z]+` never engages). A spurious
# prose `word-3` token that matches nothing on disk is harmless — it only occupies
# a rank slot, never reorders a real card (rank is relative, gaps don't matter).
_BRIEF_ID_RE = re.compile(r"(?<![\w-])[a-z]+-\d+[a-z]?(?:-[\w-]+)?")
# Match both YAML (`Parallel-safe: true`) and prose (`**Parallel-safe:** true`)
# forms — same dual-format the rest of the harness parses.
_PARALLEL_SAFE_RE = re.compile(
    r"^\s*(?:\*\*Parallel-safe:\*\*|Parallel-safe:)\s*(\S+)", re.IGNORECASE
)


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


def _lane_set(lane):
    """Normalize a lane spec into a set of lane keys, or None for "no filter".

    `lane` is a comma-separated list of program lanes: split on commas, strip,
    lowercase, drop empties. A one-element list is the degenerate single-lane
    case — membership against a one-element set is identical to the old exact
    `==` match, so single-lane results are byte-for-byte unchanged. Empty,
    whitespace-only, all-commas, or None → None (no filter — the single-daemon
    guarantee from brief-152). Multi-lane lets one daemon own several lanes
    (portal laptop: `--lane "finetune,capture,fleets"`; remote queen:
    `--lane remote-queens`).
    """
    if not lane:
        return None
    keys = {part.strip().lower() for part in lane.split(",")}
    keys.discard("")
    return keys or None


def _parse_card_program(card_path):
    """Return lowercased Program: value from YAML frontmatter, or ''.

    Sibling of _parse_card_status — reuses the same frontmatter reader rather
    than hand-rolling a second YAML parser (brief-151). The Program: field is
    the program-lane partition key. '' means the card declares no lane, which a
    lane-filtered enumeration treats fail-closed (excluded — an unlabeled brief
    is never silently grabbed by a lane queen).
    """
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
                if in_fm and stripped.lower().startswith("program:"):
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


def enumerate_dispatchable(project_dir, running=None, lane=None):
    """Return dispatchable brief candidates ordered by goals.md queue position.

    Scans wiki/briefs/cards/*/index.md, keeps only cards with Status: queued
    (exact match, case-insensitive) that are not already in running.json under
    active / awaiting_review / pending_merges / completed_pending_eval / history.
    Orders results by first appearance in .loop/state/goals.md; cards not
    mentioned in goals.md sort after all mentioned ones.

    Args:
        project_dir: project root path (str).
        running: parsed running.json dict, or None to read from disk.
        lane: optional program-lane partition, a comma-separated list of lanes
            (multi-lane-daemon). When None, empty, whitespace-only, or all-commas
            (brief-152), the card Program: field is never read and every queued
            card is a candidate — single-daemon behavior byte-for-byte unchanged.
            When it names one or more lanes, only cards whose Program: is IN the
            lane set (case- and whitespace-insensitive) are kept; a card with NO
            Program: field is EXCLUDED (fail-closed — an unlabeled brief never
            gets silently grabbed by a lane queen). Single-lane is the one-element
            degenerate case, byte-for-byte unchanged from the exact-match era.

    Returns:
        List of dicts with keys: brief, branch, brief_file.
    """
    # Normalize the lane spec to a set (or None = no filter). An empty /
    # whitespace / all-commas lane means "no filter" — NOT the literal "" key
    # (which would fail-closed against every unlabeled card). This is the
    # single-daemon default: the daemon exports an empty LOOP_LANE and the queen
    # passes `--lane "$LOOP_LANE"`, so enumerate must read that empty lane as
    # None. Non-empty lane semantics (151 fail-closed) are untouched.
    lane_set = _lane_set(lane)
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
            if lane_set is not None and _parse_card_program(card_path) not in lane_set:
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


def _card_is_solo(card_path):
    """True if the card declares Parallel-safe:false (or omits it → default).

    Mirrors actions.parse_concurrency_frontmatter's solo semantics without the
    import: a card with no Parallel-safe line, or any value that isn't `true`,
    runs alone. Returns True (solo) on read error — fail-safe.
    """
    try:
        with open(card_path) as f:
            for line in f:
                m = _PARALLEL_SAFE_RE.match(line)
                if m:
                    val = m.group(1).strip().strip('"').strip("'").lower()
                    return val != "true"
    except (IOError, OSError):
        return True
    return True  # no Parallel-safe line → default solo


def _card_queued_age_secs(project_dir, card_rel_path, now=None):
    """Seconds since the card's most recent git commit, or 0.0 if unknown.

    The card's last commit time is a deterministic, already-present proxy for
    "how long has this been sitting at queue head" — the Status: queued flip is
    itself a commit. Uncommitted/untracked cards (no git time) return 0.0 so
    they never trip the drain. `now` is injectable for tests.
    """
    if now is None:
        now = time.time()
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "log", "-1", "--format=%ct", "--", card_rel_path],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return 0.0
        committed = float(r.stdout.strip())
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0
    age = now - committed
    return age if age > 0 else 0.0


def head_solo_drain(project_dir, threshold_secs, running=None, now=None, lane=None):
    """Drain-for-solo decision (SOLO_DRAIN_AFTER_SECS).

    Returns a dict {"drain": bool, "brief": <head id or "">, "waited": <secs>}.

    `drain` is True when ALL hold:
      - threshold_secs > 0 (feature on),
      - the dispatch queue HEAD is a solo (parallel-safe:false) brief,
      - that head has been queued longer than threshold_secs.

    When True, the daemon/dispatch should stop feeding parallel briefs past the
    head and let the board drain so the solo brief runs next (closes the
    live brief-253a starvation: a solo head sat at position 1 for hours while
    parallel briefs dispatched past it). Pure read — no state mutation.
    """
    if not threshold_secs or threshold_secs <= 0:
        return {"drain": False, "brief": "", "waited": 0.0}

    candidates = enumerate_dispatchable(project_dir, running=running, lane=lane)
    if not candidates:
        return {"drain": False, "brief": "", "waited": 0.0}

    head = candidates[0]
    card_path = os.path.join(project_dir, head["brief_file"])
    if not _card_is_solo(card_path):
        return {"drain": False, "brief": head["brief"], "waited": 0.0}

    waited = _card_queued_age_secs(project_dir, head["brief_file"], now=now)
    return {
        "drain": waited > threshold_secs,
        "brief": head["brief"],
        "waited": waited,
    }


def queue_fingerprint(project_dir, lane=None):
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

    `lane` accepts the same comma-separated lane list as enumerate_dispatchable.
    The lane namespace folded into the fingerprint is the sorted set join, so a
    reordered lane list ("a,b" vs "b,a") maps to one stable fingerprint and never
    spuriously busts the queen dedup (multi-lane-daemon).
    """
    goals_path = os.path.join(project_dir, ".loop", "state", "goals.md")
    try:
        st = os.stat(goals_path)
        goals_sig = "%d:%d" % (st.st_mtime_ns, st.st_size)
    except OSError:
        goals_sig = "missing"

    ids = ",".join(c["brief"] for c in enumerate_dispatchable(project_dir, lane=lane))
    # lane=None (or empty/whitespace/all-commas → no filter) keeps the legacy
    # fingerprint string byte-for-byte; a lane-scoped daemon gets a distinct
    # namespace so two lanes can't collide on identical id lists (brief-151).
    # The namespace is the SORTED set join, so "a,b" and "b,a" (and a single
    # "alpha") produce the same fingerprint — reorder-stable (multi-lane-daemon).
    lane_set = _lane_set(lane)
    if lane_set is None:
        raw = "%s|%s" % (goals_sig, ids)
    else:
        lane_norm = ",".join(sorted(lane_set))
        raw = "%s|lane=%s|%s" % (goals_sig, lane_norm, ids)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def main():
    argv = sys.argv[1:]
    fingerprint = False
    lane = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--fingerprint":
            fingerprint = True
        elif a == "--lane":
            i += 1
            lane = argv[i] if i < len(argv) else None
        elif a.startswith("--lane="):
            lane = a[len("--lane="):]
        else:
            positional.append(a)
        i += 1
    # An explicit empty/whitespace --lane "" is degenerate; treat it as no lane
    # so it can't fail-closed against unlabeled cards (which report Program: "").
    # This keeps the single-daemon path — queen runs `--lane "$LOOP_LANE"` with
    # LOOP_LANE empty — byte-for-byte identical to no --lane, including the
    # queue_fingerprint string (which branches on lane is None).
    if lane is not None and not lane.strip():
        lane = None
    project_dir = positional[0] if positional else os.getcwd()
    if fingerprint:
        print(queue_fingerprint(project_dir, lane=lane))
    else:
        print(json.dumps(enumerate_dispatchable(project_dir, lane=lane), indent=2))


if __name__ == "__main__":
    main()
