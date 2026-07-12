#!/usr/bin/env python3
"""Repeat-failure fingerprint counter + escalation (issue #15).

Design rule (Mattie): the STATE CHANGE is the fix; the notification is one
line at the end. When a failure repeats identically N times with no human
watching, the system must stop silently retrying, park the work loudly, and
mark itself degraded so the next glance at loop status / hive is unmissably
red. notify() is a latency reducer for a human decision, never the detection
mechanism.

This module owns the detection state change: a per-fingerprint consecutive
counter persisted daemon-local in <state_dir>/failure-fingerprints.json. A
"fingerprint" is (site + brief + reason). On the Nth *identical* consecutive
failure it raises the EXISTING escalate.json signal with the receipt and
appends a runtime event. A DIFFERENT reason resets the counter (it's progress
of a kind). Success at a site clears the counter. Once raised, it does not
re-raise every tick — it stays escalated until escalate.json is resolved
(moved to escalate.json.resolved-*, mirroring how escalations clear today),
at which point the fingerprint re-arms.

The daemon (lib/daemon.sh) wires the notify() + brief-parking around this;
those are site-specific and stay in bash. This module is intentionally
decoupled from init_paths(): it takes a bare <state_dir> so it is trivially
testable and callable from any failure site.

Usage:
    python3 lib/failure_tracker.py record <state_dir> <site> <brief> <reason...>
        Record one failure. Exit code signals the daemon what to do:
          0  — below threshold (keep going; nothing raised)
          10 — threshold reached THIS call → escalation raised (park + notify)
          11 — already escalated for this fingerprint → suppress (no re-raise)
        Stdout: "<VERDICT> count=<n> brief=<brief>".

    python3 lib/failure_tracker.py clear <state_dir> <site> <brief>
        Success at <site> for <brief> → reset that fingerprint's counter.
        Stdout: "CLEARED" or "NOCHANGE".

Env:
    ESCALATE_AFTER_FAILURES — consecutive identical failures before escalating
                              (default 3).
"""

import json
import os
import re
import sys
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _threshold():
    try:
        n = int(os.environ.get("ESCALATE_AFTER_FAILURES", "3"))
        return n if n >= 1 else 3
    except (TypeError, ValueError):
        return 3


def _counter_path(state_dir):
    return os.path.join(state_dir, "failure-fingerprints.json")


def _escalate_path(state_dir):
    return os.path.join(state_dir, "signals", "escalate.json")


def _events_path(state_dir):
    # state_dir == <project>/.loop/state, so runtime-events.jsonl sits here.
    return os.path.join(state_dir, "runtime-events.jsonl")


def _key(site, brief):
    return f"{site}::{brief}"


def _normalize_reason(reason):
    """Collapse whitespace + bound length so the fingerprint is stable.

    The receipt line is kept verbatim elsewhere (failure_line in escalate.json);
    this normalized form is only the identity used for counter matching, so an
    identical refusal each tick lands on the same fingerprint.
    """
    reason = re.sub(r"\s+", " ", (reason or "").strip())
    return reason[:400]


def _load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _append_event(state_dir, **fields):
    path = _events_path(state_dir)
    payload = {"ts": _now()}
    payload.update(fields)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError:
        pass


def _write_escalation(state_dir, rec, failure_line):
    """Raise the EXISTING escalate.json with the receipt.

    Mirrors the schema current escalations use: the daemon machine fields
    (type/reason/timestamp) that Phase 4 notify reads, plus the human fields
    the queen's escalate.json.resolved-* carry (brief/raised_by/severity/
    human_action_required/one_liner). Follows sync_project_checkout's
    precedent of not clobbering an escalate.json that already holds the desk —
    a different escalation stays put; the state change (counter + park + event)
    still lands so this loop stops either way.
    """
    path = _escalate_path(state_dir)
    if os.path.exists(path):
        return False
    site = rec["site"]
    brief = rec["brief"]
    count = rec["count"]
    scope = brief if brief and brief != "-" else "(repo-level)"
    one_liner = (
        f"Repeat failure: {site} refused {count}x consecutively on {scope} "
        f"with no human watching — stopped retrying and parked. "
        f"Receipt: {failure_line}"
    )
    payload = {
        "type": "repeat_failure",
        "reason": one_liner,
        "site": site,
        "brief": brief,
        "failure_line": failure_line,
        "count": count,
        "first_ts": rec["first_ts"],
        "last_ts": rec["last_ts"],
        "timestamp": _now(),
        "raised_by": "daemon",
        "raised_at": _now(),
        "severity": "ops-recovery",
        "human_action_required": True,
        "director_clearable": True,
        "one_liner": one_liner,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return True


def record(state_dir, site, brief, reason):
    reason = _normalize_reason(reason)
    threshold = _threshold()
    cpath = _counter_path(state_dir)
    data = _load(cpath)
    key = _key(site, brief)
    now = _now()

    prior = data.get(key)

    # Re-arm: a fingerprint we already escalated whose escalate.json has since
    # been resolved (moved to escalate.json.resolved-*) starts fresh. Mirrors
    # the daemon's escalate-resolved dedup reset.
    if prior and prior.get("escalated") and not os.path.exists(_escalate_path(state_dir)):
        prior = None

    if prior and prior.get("reason") == reason:
        rec = prior
        rec["count"] += 1
        rec["last_ts"] = now
    else:
        # New fingerprint, or the reason changed (progress of a kind) → reset.
        rec = {
            "site": site,
            "brief": brief,
            "reason": reason,
            "count": 1,
            "first_ts": now,
            "last_ts": now,
            "escalated": False,
        }
    data[key] = rec

    if rec["count"] >= threshold and not rec.get("escalated"):
        _write_escalation(state_dir, rec, reason)
        _append_event(
            state_dir,
            event="repeat_failure_escalated",
            brief=brief,
            site=site,
            count=rec["count"],
            reason=reason,
        )
        rec["escalated"] = True
        _save(cpath, data)
        print(f"ESCALATE count={rec['count']} brief={brief}")
        return 10

    _save(cpath, data)
    if rec.get("escalated"):
        print(f"SUPPRESS count={rec['count']} brief={brief}")
        return 11
    print(f"COUNT count={rec['count']} brief={brief}")
    return 0


def clear(state_dir, site, brief):
    cpath = _counter_path(state_dir)
    data = _load(cpath)
    key = _key(site, brief)
    if key in data:
        del data[key]
        _save(cpath, data)
        print("CLEARED")
    else:
        print("NOCHANGE")
    return 0


def main(argv):
    if len(argv) < 2:
        print("usage: failure_tracker.py <record|clear> <state_dir> <site> <brief> [reason...]",
              file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "record":
        if len(argv) < 5:
            print("record requires <state_dir> <site> <brief> <reason...>", file=sys.stderr)
            return 2
        state_dir, site, brief = argv[2], argv[3], argv[4]
        reason = " ".join(argv[5:])
        return record(state_dir, site, brief, reason)
    if cmd == "clear":
        if len(argv) < 5:
            print("clear requires <state_dir> <site> <brief>", file=sys.stderr)
            return 2
        return clear(argv[2], argv[3], argv[4])
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
