#!/usr/bin/env python3
"""Auto-merge decision layer (brief-003 Thread 7).

When a brief ships with `**Auto-merge:** true` in its frontmatter AND:
  - the latest validator review's verdict is `pass`, AND
  - the global kill-switch `.loop/state/pause-auto-merge` is absent, AND
  - the queen's escalation reason is `human_approval_required_for_merge`
      (a proxy for "programmatic criteria met" — the queen only escalates
      with that reason when it decided the brief is otherwise merge-ready)

…then swap the queen's escalate.json for a pending-merge.json so the
daemon merges the branch on its next tick. Log a distinct
`auto_merge_approved` event to log.jsonl.

Default stays human-gated. Auto-merge is strictly opt-in per brief.

CLI:
    python3 lib/auto_merge.py check-escalate <project_dir>
        Exit 0 → swap performed (or no-op because no pending escalate).
        Exit 2 → preconditions not met / swap skipped (non-error).
        Exit 1 → error.

    python3 lib/auto_merge.py decide <project_dir> <brief_id> <branch>
        Prints a JSON decision {approved, reason, details} to stdout.
        Exit 0 → decision printed. Used by the dry-run harness.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


# ─── Reused helpers ──────────────────────────────────────────────────

def _git_show(project_dir, ref, path):
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "show", f"{ref}:{path}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return None


def _git_rev_parse(project_dir, ref):
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "rev-parse", ref],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _config_remote(project_dir):
    config_file = os.path.join(project_dir, ".loop", "config.sh")
    remote = "origin"
    if os.path.exists(config_file):
        with open(config_file) as f:
            for line in f:
                if line.strip().startswith("GIT_REMOTE="):
                    remote = line.strip().split("=", 1)[1].strip('"').strip("'")
                    break
    return remote


# ─── Auto-merge primitives ───────────────────────────────────────────

AUTO_MERGE_LINE_RE = re.compile(r"^\s*\*\*Auto-merge:\*\*\s*(\S+)", re.IGNORECASE)


def parse_auto_merge_flag(brief_content):
    """Return True iff brief text has `**Auto-merge:** true` in frontmatter.

    Absent or any non-`true` value → False. Case-insensitive on the key and value.
    """
    if not brief_content:
        return False
    for line in brief_content.splitlines():
        m = AUTO_MERGE_LINE_RE.match(line)
        if m:
            return m.group(1).strip().lower().strip('"').strip("'") == "true"
    return False


def kill_switch_active(project_dir):
    """Global kill-switch: if .loop/state/pause-auto-merge exists, disable."""
    return os.path.exists(os.path.join(project_dir, ".loop", "state", "pause-auto-merge"))


REVIEW_CYCLE_RE = re.compile(r"-cycle-(\d+)\.md$")


def latest_review(project_dir, ref, brief_id):
    """Return (cycle, verdict) for the highest-numbered review of brief_id on ref.

    Returns (0, None) if no review exists.
    """
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "ls-tree", "-r", "--name-only", ref,
             ".loop/modules/validator/state/reviews/"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return 0, None
    except Exception:
        return 0, None

    best = 0
    for line in r.stdout.splitlines():
        name = os.path.basename(line)
        if not name.startswith(f"{brief_id}-cycle-"):
            continue
        m = REVIEW_CYCLE_RE.search(name)
        if m:
            best = max(best, int(m.group(1)))

    if best == 0:
        return 0, None

    path = f".loop/modules/validator/state/reviews/{brief_id}-cycle-{best}.md"
    content = _git_show(project_dir, ref, path)
    if not content:
        return best, None

    in_front = False
    for line in content.splitlines():
        s = line.strip()
        if s == "---":
            if not in_front:
                in_front = True
                continue
            break
        if in_front and s.lower().startswith("verdict:"):
            v = s.split(":", 1)[1].strip()
            v = v.split("#", 1)[0].strip().strip('"').strip("'")
            return best, (v.lower() or None)
    return best, None


def progress_iteration(project_dir, ref):
    """Read iteration from progress.json on ref. Returns int or None."""
    raw = _git_show(project_dir, ref, ".loop/state/progress.json")
    if not raw:
        return None
    try:
        return int(json.loads(raw).get("iteration", 0))
    except (ValueError, json.JSONDecodeError):
        return None


# ─── Decision ────────────────────────────────────────────────────────

def decide(project_dir, brief_id, branch, brief_file_rel=None):
    """Evaluate the three auto-merge preconditions + kill-switch.

    Returns dict: {approved: bool, reason: str, details: {...}}.
    `reason` is a machine-readable tag; `details` carries evidence for logs.
    """
    details = {
        "brief": brief_id,
        "branch": branch,
    }

    if kill_switch_active(project_dir):
        return {
            "approved": False,
            "reason": "kill_switch_active",
            "details": details,
        }

    remote = _config_remote(project_dir)
    ref = None
    for candidate in [branch, f"{remote}/{branch}"]:
        if _git_rev_parse(project_dir, candidate):
            ref = candidate
            break
    if ref is None:
        return {
            "approved": False,
            "reason": "branch_not_found",
            "details": details,
        }
    details["ref"] = ref

    # Brief file: explicit path wins; fall back to progress.json's brief_file.
    if not brief_file_rel:
        raw = _git_show(project_dir, ref, ".loop/state/progress.json")
        if raw:
            try:
                brief_file_rel = json.loads(raw).get("brief_file", "")
            except (ValueError, json.JSONDecodeError):
                brief_file_rel = ""
    if not brief_file_rel:
        return {
            "approved": False,
            "reason": "brief_file_unknown",
            "details": details,
        }
    details["brief_file"] = brief_file_rel

    brief_content = _git_show(project_dir, ref, brief_file_rel)
    flag = parse_auto_merge_flag(brief_content or "")
    details["auto_merge_flag"] = flag
    if not flag:
        return {
            "approved": False,
            "reason": "flag_off",
            "details": details,
        }

    iteration = progress_iteration(project_dir, ref)
    review_cycle, verdict = latest_review(project_dir, ref, brief_id)
    details["iteration"] = iteration
    details["review_cycle"] = review_cycle
    details["verdict"] = verdict

    if verdict != "pass":
        return {
            "approved": False,
            "reason": "validator_not_pass",
            "details": details,
        }

    if iteration is not None and review_cycle < iteration:
        # Validator hasn't caught up with the latest builder commit — do NOT
        # auto-merge on a stale pass.
        return {
            "approved": False,
            "reason": "validator_behind",
            "details": details,
        }

    return {
        "approved": True,
        "reason": "auto_merge_approved",
        "details": details,
    }


# ─── Swap + log ──────────────────────────────────────────────────────

def log_event(project_dir, event, payload):
    """Append a structured event to .loop/state/log.jsonl."""
    log_file = os.path.join(project_dir, ".loop", "state", "log.jsonl")
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        **payload,
    }
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def check_escalate(project_dir):
    """Inspect escalate.json; if it's a merge-approval escalation and
    auto-merge preconditions hold, swap it for a pending-merge.json.

    Returns exit code:
      0 — swap performed, OR no escalate.json to act on (no-op).
      2 — escalate.json present but preconditions not met.
      1 — error.
    """
    state_dir = os.path.join(project_dir, ".loop", "state")
    escalate_path = os.path.join(state_dir, "signals", "escalate.json")
    pending_merge_path = os.path.join(state_dir, "pending-merge.json")

    if not os.path.exists(escalate_path):
        return 0

    try:
        with open(escalate_path) as f:
            esc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"auto_merge: failed to read escalate.json: {e}", file=sys.stderr)
        return 1

    reason = str(esc.get("reason", ""))
    # Only act on the human_approval_required_for_merge class; everything else
    # (infra failure, contested architecture, validator_block) still pages human.
    if reason != "human_approval_required_for_merge":
        return 0

    brief_id = esc.get("brief") or esc.get("brief_id") or ""
    branch = esc.get("branch") or brief_id
    title = esc.get("title", brief_id)
    brief_file_rel = esc.get("brief_file") or None

    if not brief_id or not branch:
        print("auto_merge: escalate.json missing brief/branch fields", file=sys.stderr)
        return 2

    decision = decide(project_dir, brief_id, branch, brief_file_rel)
    if not decision["approved"]:
        # Precondition failure: leave escalate.json in place; log a skip event
        # so `loop logs -f` shows why we didn't auto-merge this completion.
        log_event(project_dir, "auto_merge_skipped", decision)
        return 2

    payload = {
        "brief": brief_id,
        "branch": branch,
        "title": title,
        "auto_merged": True,
        "auto_merge_reason": "validator pass + programmatic criteria green + opt-in flag set",
    }
    tmp = pending_merge_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, pending_merge_path)
    try:
        os.remove(escalate_path)
    except OSError:
        pass

    log_event(project_dir, "auto_merge_approved", {
        **decision,
        "payload": payload,
    })
    print(f"auto_merge_approved: {brief_id} → pending-merge.json")
    return 0


# ─── CLI ─────────────────────────────────────────────────────────────

def _usage():
    print(__doc__, file=sys.stderr)
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        _usage()
    cmd = sys.argv[1]

    if cmd == "check-escalate":
        if len(sys.argv) < 3:
            _usage()
        sys.exit(check_escalate(sys.argv[2]))

    if cmd == "decide":
        if len(sys.argv) < 5:
            _usage()
        project_dir, brief_id, branch = sys.argv[2], sys.argv[3], sys.argv[4]
        brief_file_rel = sys.argv[5] if len(sys.argv) > 5 else None
        decision = decide(project_dir, brief_id, branch, brief_file_rel)
        print(json.dumps(decision, indent=2))
        sys.exit(0 if decision["approved"] else 2)

    _usage()


if __name__ == "__main__":
    main()
