#!/usr/bin/env python3
"""loop sweep — deterministic state validator.

Four predicates, each structural (no LLM, no inference):

  1. progress-parse    — worktree progress.json parses as valid JSON
  2. iteration-advance — active brief's iteration advanced since last sweep
  3. subprocess-exists — active brief has a live claude subprocess
  4. heartbeat-active  — heartbeat phase5_sleep_idle while active[] non-empty

Usage:
    python3 lib/sweep.py <project_dir> [--quick] [--auto-route] [--snapshot-dir DIR]

  --quick           Skip slow checks (subprocess scan); suitable for daemon pre-tick
  --auto-route      Move orphaned/stuck active[] entries to awaiting_review
  --snapshot-dir    Where to persist iteration snapshots (default: <project_dir>/.loop/state)

Exit codes:
  0  all predicates pass (clean)
  1  at least one predicate triggered
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone


# ── Thresholds ─────────────────────────────────────────────────────────────

STUCK_MIN = 30          # minutes in active[] before "stuck" flag
ORPHAN_MIN = 5          # minutes since dispatch before missing subprocess = orphan
SNAPSHOT_FILE = "sweep-iteration-snapshot.json"


# ── Helpers ────────────────────────────────────────────────────────────────

def utcnow_ts() -> float:
    return time.time()


def parse_iso_utc(ts_str: str) -> float:
    """Parse ISO-8601 UTC string to epoch float. Returns 0 on failure."""
    if not ts_str:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return 0


def age_minutes(ts_str: str) -> float:
    """How many minutes ago was ts_str? Returns 9999 if unparseable."""
    dispatched = parse_iso_utc(ts_str)
    if dispatched == 0:
        return 9999
    return (utcnow_ts() - dispatched) / 60


# ── Predicate 1 — progress.json parses ────────────────────────────────────

def check_progress_parse(brief_id: str, worktrees_dir: str) -> dict:
    """Attempt JSON parse of the worktree's .loop/state/progress.json."""
    wt = os.path.join(worktrees_dir, brief_id)
    progress_path = os.path.join(wt, ".loop", "state", "progress.json")

    if not os.path.isdir(wt):
        return {
            "predicate": "progress-parse",
            "brief": brief_id,
            "status": "skip",
            "evidence": f"worktree not found: {wt}",
            "suggested_action": "check if worktree was manually removed",
        }

    if not os.path.isfile(progress_path):
        return {
            "predicate": "progress-parse",
            "brief": brief_id,
            "status": "warn",
            "evidence": f"progress.json missing: {progress_path}",
            "suggested_action": "daemon may not have started the worker yet; check daemon.log",
        }

    try:
        with open(progress_path) as f:
            data = json.load(f)
        iteration = data.get("iteration", "?")
        status = data.get("status", "?")
        return {
            "predicate": "progress-parse",
            "brief": brief_id,
            "status": "ok",
            "evidence": f"iteration={iteration} status={status}",
            "suggested_action": None,
            "_parsed": data,
        }
    except Exception as e:
        return {
            "predicate": "progress-parse",
            "brief": brief_id,
            "status": "fail",
            "evidence": f"JSON parse error: {e}",
            "suggested_action": "hand-fix progress.json then restart daemon; see wiki/operating-docs/hand-merge-brief.md",
        }


# ── Predicate 2 — iteration advanced ──────────────────────────────────────

def check_iteration_advance(brief_id: str, dispatched_at: str, worktrees_dir: str,
                             snapshot: dict) -> dict:
    """Check if iteration in progress.json advanced since last sweep snapshot."""
    dispatch_age = age_minutes(dispatched_at)

    if dispatch_age < STUCK_MIN:
        return {
            "predicate": "iteration-advance",
            "brief": brief_id,
            "status": "ok",
            "evidence": f"dispatched {dispatch_age:.1f}m ago (< {STUCK_MIN}m threshold)",
            "suggested_action": None,
        }

    wt = os.path.join(worktrees_dir, brief_id)
    progress_path = os.path.join(wt, ".loop", "state", "progress.json")

    if not os.path.isfile(progress_path):
        return {
            "predicate": "iteration-advance",
            "brief": brief_id,
            "status": "skip",
            "evidence": "progress.json missing — skipping iteration check",
            "suggested_action": None,
        }

    try:
        with open(progress_path) as f:
            data = json.load(f)
        current_iter = data.get("iteration", 0)
    except Exception as e:
        return {
            "predicate": "iteration-advance",
            "brief": brief_id,
            "status": "skip",
            "evidence": f"progress.json unparseable ({e}) — covered by progress-parse predicate",
            "suggested_action": None,
        }

    prev_entry = snapshot.get(brief_id, {})
    prev_iter = prev_entry.get("iteration")
    if prev_iter is None:
        return {
            "predicate": "iteration-advance",
            "brief": brief_id,
            "status": "ok",
            "evidence": f"no previous snapshot — baseline: iteration={current_iter}",
            "suggested_action": None,
        }

    if current_iter > prev_iter:
        return {
            "predicate": "iteration-advance",
            "brief": brief_id,
            "status": "ok",
            "evidence": f"iteration advanced {prev_iter} → {current_iter}",
            "suggested_action": None,
        }

    # current_iter <= prev_iter: no advance since the last tick. Anchor the
    # freeze test to the last time the counter actually moved (last_advance_ts,
    # carried forward across snapshots), not to the brief's original dispatch —
    # a healthy multi-cycle brief older than STUCK_MIN will show current==prev
    # on most ticks between advances (issue #38: dispatch-age anchoring cried
    # wolf on every tick past minute 30). Falls back to dispatched_at when no
    # last_advance_ts has been recorded yet.
    anchor_ts = prev_entry.get("last_advance_ts") or dispatched_at
    frozen_age = age_minutes(anchor_ts)

    if frozen_age < STUCK_MIN:
        return {
            "predicate": "iteration-advance",
            "brief": brief_id,
            "status": "ok",
            "evidence": (
                f"iteration {current_iter} unchanged since last check, but last "
                f"advance was {frozen_age:.1f}m ago (< {STUCK_MIN}m threshold)"
            ),
            "suggested_action": None,
        }

    return {
        "predicate": "iteration-advance",
        "brief": brief_id,
        "status": "fail",
        "evidence": (
            f"iteration frozen at {current_iter} for {frozen_age:.0f}m since last advance "
            f"(dispatched {dispatch_age:.0f}m ago, prev snapshot={prev_iter})"
        ),
        "suggested_action": (
            "check daemon.log for dedup loops; inspect progress.json for status=blocked; "
            "if orphaned (no subprocess) move to awaiting_review"
        ),
    }


# ── Predicate 3 — subprocess exists ───────────────────────────────────────

def check_subprocess_exists(brief_id: str, dispatched_at: str) -> dict:
    """Check if a claude subprocess for this brief_id is running."""
    dispatch_age = age_minutes(dispatched_at)

    if dispatch_age < ORPHAN_MIN:
        return {
            "predicate": "subprocess-exists",
            "brief": brief_id,
            "status": "ok",
            "evidence": f"dispatched {dispatch_age:.1f}m ago (< {ORPHAN_MIN}m grace period)",
            "suggested_action": None,
        }

    # Search for a claude process referencing the brief_id
    # pgrep -f on macOS returns PIDs; on Linux also -l gives pid+cmd.
    # We search ps aux for portability.
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.splitlines()
        matches = [l for l in lines if "claude" in l.lower() and brief_id in l]
        if matches:
            pids = [l.split()[1] for l in matches if len(l.split()) > 1]
            return {
                "predicate": "subprocess-exists",
                "brief": brief_id,
                "status": "ok",
                "evidence": f"found subprocess(es): pid={','.join(pids)}",
                "suggested_action": None,
            }
        return {
            "predicate": "subprocess-exists",
            "brief": brief_id,
            "status": "fail",
            "evidence": f"no claude subprocess matching '{brief_id}' (age {dispatch_age:.0f}m)",
            "suggested_action": (
                "brief is orphaned — move to awaiting_review; "
                "use `loop sweep --auto-route` to do this automatically"
            ),
        }
    except Exception as e:
        return {
            "predicate": "subprocess-exists",
            "brief": brief_id,
            "status": "warn",
            "evidence": f"subprocess scan failed: {e}",
            "suggested_action": "manual check: ps aux | grep claude",
        }


# ── Predicate 4 — heartbeat↔active consistency ────────────────────────────

def check_heartbeat_active(brief_id: str, dispatched_at: str, heartbeat: dict) -> dict:
    """Flag mode-B stuck: heartbeat=phase5_sleep_idle while brief is active >N min."""
    last_event = heartbeat.get("last_event", "")
    dispatch_age = age_minutes(dispatched_at)

    if not last_event.startswith("phase5_sleep_idle"):
        return {
            "predicate": "heartbeat-active",
            "brief": brief_id,
            "status": "ok",
            "evidence": f"heartbeat.last_event={last_event!r} (not idle)",
            "suggested_action": None,
        }

    if dispatch_age < STUCK_MIN:
        return {
            "predicate": "heartbeat-active",
            "brief": brief_id,
            "status": "ok",
            "evidence": f"daemon idle but brief only dispatched {dispatch_age:.1f}m ago",
            "suggested_action": None,
        }

    hb_ts = heartbeat.get("ts", "")
    hb_age = age_minutes(hb_ts)
    return {
        "predicate": "heartbeat-active",
        "brief": brief_id,
        "status": "fail",
        "evidence": (
            f"heartbeat.last_event={last_event!r} (idle) while {brief_id} "
            f"active {dispatch_age:.0f}m (heartbeat {hb_age:.1f}m ago) — "
            "mode-B stuck state (brief-067 pattern)"
        ),
        "suggested_action": (
            "daemon is stuck in dedup loop; "
            "fix or move progress.json then restart daemon: loop stop && loop start"
        ),
    }


# ── Auto-route ────────────────────────────────────────────────────────────

def auto_route_brief(brief_id: str, branch: str, running_path: str, reason: str) -> None:
    """Move a brief from active[] to awaiting_review[] in running.json."""
    with open(running_path) as f:
        state = json.load(f)

    active = state.get("active", [])
    entry = next((e for e in active if e.get("brief") == brief_id), None)
    if not entry:
        return

    active.remove(entry)
    entry["conflict_note"] = f"sweep: {reason}"
    entry["awaiting_since"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if "awaiting_review" not in state:
        state["awaiting_review"] = []
    state["awaiting_review"].append(entry)
    state["active"] = active

    with open(running_path, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def release_claim_on_route(project_dir: str, brief_id: str) -> None:
    """brief-160: releasing the claim is PART of the auto-route move.

    A brief pulled out of active[] must not leave its claim ref standing — that
    is the serve-009 leak (the queen saw 'already claimed', skipped it, and the
    stranded brief never re-entered the queue). Loud, never silent: a structured
    action on stdout AND a log.jsonl entry (never a bare `git update-ref -d`).
    Best-effort — a release failure is logged, never raised (it must not break
    the sweep)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from claim import release_claim
        from actions import read_config, init_paths, log_action
        remote = read_config(os.path.join(project_dir, ".loop")).get("GIT_REMOTE", "origin")
        released = release_claim(project_dir, brief_id, remote)
        print(json.dumps({
            "action": "claim-released" if released else "claim-release-failed",
            "brief": brief_id,
            "context": "sweep-auto-route",
        }))
        if released:
            log_action(init_paths(project_dir), "claim_released",
                       {"brief": brief_id, "context": "sweep_auto_route"})
    except Exception as e:
        print(json.dumps({"action": "claim-release-error",
                          "brief": brief_id, "error": str(e)}))


def reconcile_claims_in_sweep(project_dir: str, running: dict) -> list:
    """brief-160: verify refs/claims/* against the live working set (active[] ∪
    pending_merges[]) and release own-box ORPHANS loudly — foreign/unknown-owner
    claims are observed only, never reaped (the never-reap-on-local-ignorance
    law). Best-effort: never raises into the sweep; an unreachable remote is a
    no-op."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from claim import reconcile_claims
        from actions import read_config, init_paths, log_action
        remote = read_config(os.path.join(project_dir, ".loop")).get("GIT_REMOTE", "origin")
        working = (
            [e.get("brief") for e in running.get("active", [])]
            + [e.get("brief") for e in running.get("pending_merges", [])]
        )
        paths = init_paths(project_dir)

        def _log(action):
            print(json.dumps({"action": "claim-reconcile", **action}))
            log_action(paths, "claim_reconcile", action)

        return reconcile_claims(project_dir, remote, working, log=_log)
    except Exception as e:
        print(json.dumps({"action": "claim-reconcile-error", "error": str(e)}))
        return []


# ── Snapshot I/O ──────────────────────────────────────────────────────────

def load_snapshot(snapshot_dir: str) -> dict:
    path = os.path.join(snapshot_dir, SNAPSHOT_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_snapshot(snapshot_dir: str, active_entries: list, worktrees_dir: str,
                   prev_snapshot: dict = None) -> None:
    """Persist this tick's iteration snapshot, carrying forward last_advance_ts.

    last_advance_ts only moves to "now" when the iteration counter actually
    increased since prev_snapshot; otherwise it's carried forward unchanged so
    check_iteration_advance can anchor its freeze test to the last real advance.
    """
    prev_snapshot = prev_snapshot or {}
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = {}
    for entry in active_entries:
        brief_id = entry.get("brief", "")
        if not brief_id:
            continue
        wt = os.path.join(worktrees_dir, brief_id)
        progress_path = os.path.join(wt, ".loop", "state", "progress.json")
        if os.path.isfile(progress_path):
            try:
                with open(progress_path) as f:
                    data = json.load(f)
                current_iter = data.get("iteration", 0)
                prev_entry = prev_snapshot.get(brief_id, {})
                prev_iter = prev_entry.get("iteration")
                if prev_iter is None or current_iter > prev_iter:
                    last_advance_ts = now_ts
                else:
                    last_advance_ts = prev_entry.get("last_advance_ts", now_ts)
                snapshot[brief_id] = {
                    "iteration": current_iter,
                    "snapshot_ts": now_ts,
                    "last_advance_ts": last_advance_ts,
                }
            except Exception:
                pass

    path = os.path.join(snapshot_dir, SNAPSHOT_FILE)
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
        f.write("\n")


# ── Report output ─────────────────────────────────────────────────────────

def format_markdown_report(results: list, timestamp: str) -> str:
    lines = [f"## {timestamp} — loop-sweep", ""]
    fails = [r for r in results if r["status"] == "fail"]
    warns = [r for r in results if r["status"] == "warn"]
    oks = [r for r in results if r["status"] == "ok"]
    skips = [r for r in results if r["status"] == "skip"]

    if not fails and not warns:
        lines.append("**Classification:** clean")
        lines.append("")
        lines.append(f"All {len(oks)} checks passed.")
        if skips:
            lines.append(f"{len(skips)} skipped (no worktree or insufficient data).")
    else:
        classification = "STUCK" if fails else "WARN"
        lines.append(f"**Classification:** {classification}")
        lines.append("")
        if fails:
            lines.append(f"**For Mattie:** {len(fails)} predicate(s) failed — see below.")
            lines.append("")
        for r in fails + warns:
            lines.append(f"- **{r['predicate']}** `{r['brief']}` [{r['status'].upper()}]")
            lines.append(f"  - Evidence: {r['evidence']}")
            if r.get("suggested_action"):
                lines.append(f"  - Action: {r['suggested_action']}")
    lines.append("")
    return "\n".join(lines)


def append_stewardship_log(report_md: str, state_dir: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = os.path.join(state_dir, f"stewardship-log-{today}.md")
    with open(log_path, "a") as f:
        f.write("\n")
        f.write(report_md)


# ── Main ──────────────────────────────────────────────────────────────────

def run_sweep(project_dir: str, quick: bool = False, auto_route: bool = False,
              snapshot_dir=None) -> int:
    """Run all predicates. Returns exit code (0=clean, 1=triggered)."""
    loop_dir = os.path.join(project_dir, ".loop")
    state_dir = os.path.join(loop_dir, "state")
    worktrees_dir = os.path.join(loop_dir, "worktrees")

    if snapshot_dir is None:
        snapshot_dir = state_dir

    running_path = os.path.join(state_dir, "running.json")
    heartbeat_path = os.path.join(state_dir, "heartbeat.json")

    # Load state
    try:
        with open(running_path) as f:
            running = json.load(f)
    except Exception as e:
        print(json.dumps({
            "predicate": "running-parse",
            "brief": None,
            "status": "fail",
            "evidence": f"running.json unparseable: {e}",
            "suggested_action": "fix running.json before the daemon can operate",
        }))
        return 1

    active = running.get("active", [])

    heartbeat = {}
    if os.path.isfile(heartbeat_path):
        try:
            with open(heartbeat_path) as f:
                heartbeat = json.load(f)
        except Exception:
            pass

    snapshot = load_snapshot(snapshot_dir)

    all_results = []
    for entry in active:
        brief_id = entry.get("brief", "")
        branch = entry.get("branch", brief_id)
        dispatched_at = entry.get("dispatched_at", "")

        r1 = check_progress_parse(brief_id, worktrees_dir)
        r2 = check_iteration_advance(brief_id, dispatched_at, worktrees_dir, snapshot)
        r3 = check_subprocess_exists(brief_id, dispatched_at) if not quick else {
            "predicate": "subprocess-exists", "brief": brief_id, "status": "skip",
            "evidence": "--quick mode", "suggested_action": None,
        }
        r4 = check_heartbeat_active(brief_id, dispatched_at, heartbeat)

        all_results.extend([r1, r2, r3, r4])

        # Auto-route: move orphaned brief to awaiting_review
        if auto_route:
            is_orphaned = r3["status"] == "fail"
            is_corrupt = r1["status"] == "fail"
            is_frozen = r2["status"] == "fail"
            if is_orphaned or (is_corrupt and is_frozen):
                reason = "orphaned subprocess" if is_orphaned else "corrupt+frozen"
                auto_route_brief(brief_id, branch, running_path, reason)
                print(json.dumps({
                    "action": "auto-route",
                    "brief": brief_id,
                    "reason": reason,
                    "moved_to": "awaiting_review",
                }))
                # brief-160: release the claim in the SAME operation as the move.
                release_claim_on_route(project_dir, brief_id)

    # brief-160: claims pass — release own-box orphan claim refs whose brief is
    # no longer in the live working set. Shares the invariant with startup
    # repair; here it runs every sweep so a leak is caught within a tick.
    reconcile_claims_in_sweep(project_dir, running)

    # If no active briefs, report clean
    if not active:
        all_results.append({
            "predicate": "active-briefs",
            "brief": None,
            "status": "ok",
            "evidence": "no active briefs",
            "suggested_action": None,
        })

    # Print structured results
    for r in all_results:
        print(json.dumps({k: v for k, v in r.items() if not k.startswith("_")}))

    # Markdown report
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_md = format_markdown_report(all_results, timestamp)
    print("---")
    print(report_md, end="")

    # Append to stewardship log if any failures
    triggered = any(r["status"] == "fail" for r in all_results)
    if triggered:
        try:
            append_stewardship_log(report_md, state_dir)
        except Exception:
            pass

    # Update snapshot
    try:
        save_snapshot(snapshot_dir, active, worktrees_dir, prev_snapshot=snapshot)
    except Exception:
        pass

    return 1 if triggered else 0


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    project_dir = None
    quick = False
    auto_route = False
    snapshot_dir = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--quick":
            quick = True
        elif arg == "--auto-route":
            auto_route = True
        elif arg == "--snapshot-dir":
            i += 1
            snapshot_dir = args[i] if i < len(args) else None
        elif not arg.startswith("--"):
            project_dir = arg
        i += 1

    if not project_dir:
        # Try to find .loop/ from cwd
        d = os.getcwd()
        while d != "/":
            if os.path.isdir(os.path.join(d, ".loop")):
                project_dir = d
                break
            d = os.path.dirname(d)

    if not project_dir or not os.path.isdir(os.path.join(project_dir, ".loop")):
        print("Error: no .loop/ found. Pass project_dir or run from project root.", file=sys.stderr)
        sys.exit(2)

    sys.exit(run_sweep(project_dir, quick=quick, auto_route=auto_route, snapshot_dir=snapshot_dir))


if __name__ == "__main__":
    main()
