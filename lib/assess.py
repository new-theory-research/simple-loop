#!/usr/bin/env python3
"""Assess daemon state — what should happen this tick?

Prints THREE lines:
  Line 1 (queen):     CONDUCTOR:<reason> or NONE
  Line 2 (worker):    WORKER:<brief>,<branch> or NONE
  Line 3 (validator): VALIDATOR:<brief>,<branch>,<commit> or NONE

Brief-003 Thread 1 added line 3: emits a VALIDATOR target when a builder
cycle has committed and no corresponding review exists. Queen trigger
CONDUCTOR:validator_blocked:<brief> preempts brief_complete when the latest
review's verdict is `block` (precedence 3 — ahead of brief_complete, behind
pending_eval and active_signal).

Usage:
    python3 lib/assess.py <project_dir>
"""

import json
import os
import re
import subprocess
import sys
import time


REVIEW_CYCLE_RE = re.compile(r"-cycle-(\d+)\.md$")
# Each key below matches both YAML-frontmatter form (`Key: value`) and legacy
# bold-markdown form (`**Key:** value`). Pre-fix these only matched the bold form,
# silently misparsing every YAML-frontmatter card (brief-108+ convention). Same
# dual-format fix applied to all `**Key:**` parsers in this module.
AUTO_MERGE_LINE_RE = re.compile(
    r"^\s*(?:\*\*Auto-merge:\*\*|Auto-merge:)\s*(\S+)", re.IGNORECASE
)
# Brief-014: capture everything after Depends-on: so comma-separated lists
# parse. `(.+)` (not `(\S+)`) picks up the full value; splitting happens below.
DEPENDS_ON_LINE_RE = re.compile(
    r"^\s*(?:\*\*Depends-on:\*\*|Depends-on:)\s*(.+?)\s*$", re.IGNORECASE
)
DEPENDS_ON_SECRETS_LINE_RE = re.compile(
    r"^\s*(?:\*\*Depends-on-secrets:\*\*|Depends-on-secrets:)\s*(.+?)\s*$", re.IGNORECASE
)
CYCLE_WALL_TIME_SECS_LINE_RE = re.compile(
    r"^\s*(?:\*\*Cycle-wall-time-secs:\*\*|Cycle-wall-time-secs:)\s*(\d+)\s*$", re.IGNORECASE
)
# Brief-id shape: `brief-NNN` or `brief-NNN-slug` (slug may itself contain hyphens),
# with an optional single letter-suffix on the number for sibling briefs
# (`brief-108a`, `brief-253a-nt0-rename-producer`) — an established portal
# convention the parser was silently dropping from Depends-on (2026-06-11).
# Shared by lib/lint.py — the linter's check_depends_on imports this so the daemon's
# parser and the author-time linter agree on what counts as a real brief id.
BRIEF_ID_RE = re.compile(r"^brief-\d+[a-z]?(-[\w-]+)?$")


def parse_depends_on_value(raw, validate_brief_id=True):
    """Split a raw Depends-on value into a list of tokens.

    Accepts:
        "brief-010-foo"                     → ["brief-010-foo"]
        "brief-010-foo, brief-011-bar"      → ["brief-010-foo", "brief-011-bar"]
        "brief-010-foo,brief-011-bar"       → ["brief-010-foo", "brief-011-bar"]
        "brief-010-foo,"                    → ["brief-010-foo"]  (trailing comma tolerated)

    Strips whitespace and trailing punctuation (commas, periods) from each token.
    Empty tokens filtered.

    Brief-082 hardening (validate_brief_id=True, default): tokens that don't
    match `brief-NNN(-slug)?` are dropped with a stderr warning. Two empirical
    wedges (brief-076 `none (daemon harness, simple-loop master)` and brief-082
    `_(intentionally empty — see Why)_`) had nonsense tokens survive cleaning
    and propagate to the deps history-check, where they never matched and
    produced a permanent `dispatch_blocked` loop. Author-time linter catches
    these shapes; the parser is the runtime backstop.

    `validate_brief_id=False` is for callers parsing Depends-on-SECRETS values,
    where tokens are env-var names (`FAKE_TOKEN_SL025`), not brief ids. Same
    splitting, no shape validation.

    Returns [] if no valid tokens survive (no deps → daemon dispatches normally).
    """
    if not raw:
        return []
    out = []
    for tok in raw.split(","):
        cleaned = tok.strip().strip(".,;")
        if not cleaned:
            continue
        if validate_brief_id:
            # Strip trailing parenthetical annotation before matching, e.g.
            # "brief-078 (hard)" → "brief-078". Allows the annotated dep form
            # at runtime while the linter still ERRORs on it at write time.
            stripped = cleaned.split("(")[0].strip().strip(".,;")
            if BRIEF_ID_RE.match(stripped):
                out.append(stripped)
            else:
                print(
                    f"parse_depends_on_value: dropping non-brief-id token: {cleaned!r}",
                    file=sys.stderr,
                )
        else:
            out.append(cleaned)
    return out


def git_show(project_dir, ref, path):
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


def git_rev_parse(project_dir, ref):
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


def read_depends_on(brief_file_path):
    """Parse **Depends-on:** from a brief file on disk.

    Brief-014: returns a LIST of dep ids (was scalar str or None). Callers:
    the daemon's deps-check (now iterates), and scripts/test-flow-v2.sh.
    Empty list when no line present or value unparseable. Reads the file
    directly (not via git show) — caller should pass an absolute or
    project-relative path to the brief file on disk.
    """
    try:
        with open(brief_file_path) as f:
            for line in f:
                m = DEPENDS_ON_LINE_RE.match(line)
                if m:
                    return parse_depends_on_value(m.group(1))
    except (IOError, OSError):
        pass
    return []


def read_auto_merge_flag(project_dir, ref, brief_file_rel):
    """Thread 7: does this brief opt in to auto-merge?

    Parses `**Auto-merge:** true` from the brief frontmatter. Absent or any
    non-`true` value → False. Read via `git show <ref>:<path>` so the main
    worktree can inspect a brief-branch file without a checkout.
    """
    content = git_show(project_dir, ref, brief_file_rel)
    if not content:
        return False
    for line in content.splitlines():
        m = AUTO_MERGE_LINE_RE.match(line)
        if m:
            return m.group(1).strip().lower().strip('"').strip("'") == "true"
    return False


def max_review_cycle(project_dir, ref, brief_id):
    """Find max cycle N of review files for brief_id visible on ref."""
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "ls-tree", "-r", "--name-only", ref,
             ".loop/modules/validator/state/reviews/"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return 0
    except Exception:
        return 0
    best = 0
    for line in r.stdout.splitlines():
        name = os.path.basename(line)
        if not name.startswith(f"{brief_id}-cycle-"):
            continue
        m = REVIEW_CYCLE_RE.search(name)
        if m:
            best = max(best, int(m.group(1)))
    return best


def read_config_value(loop_dir, key, default=""):
    """Read a single `KEY=value` from config.sh (and config.local.sh overlay).

    Mirrors the bash sourcing precedence used by daemon.sh: config.local.sh
    wins over config.sh. Strips inline `# comment`, surrounding quotes, and
    whitespace. Returns `default` if the key is absent in both files.

    Used for WORKER_PARALLEL / THROTTLE so assess.py's worker-target emission
    matches the daemon's flag state without re-sourcing bash.
    """
    value = default
    for fname in ("config.sh", "config.local.sh"):
        path = os.path.join(loop_dir, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    s = line.strip()
                    if s.startswith(key + "="):
                        raw = s.split("=", 1)[1]
                        raw = raw.split("#", 1)[0].strip()
                        value = raw.strip('"').strip("'")
        except (IOError, OSError):
            pass
    return value


def _worker_parallel_enabled(loop_dir):
    return read_config_value(loop_dir, "WORKER_PARALLEL", "false").strip().lower() == "true"


def _throttle(loop_dir):
    try:
        t = int(read_config_value(loop_dir, "THROTTLE", "1") or "1")
    except (ValueError, TypeError):
        t = 1
    return t if t >= 1 else 1


def last_iteration_ts(project_dir, brief_id):
    """Most-recent worker-log mtime for a brief, or 0.0 if none.

    Worker iterations write `.loop/logs/worker_<brief>_<YYYYmmdd_HHMMSS>.log`
    in the daemon's main checkout (run_worker_iteration). The newest such
    file's mtime is a cheap, already-present proxy for "when did this brief
    last iterate" — no new event emission, readable from the main checkout
    where assess.py runs. Briefs that never iterated (no log) return 0.0 and
    therefore sort FIRST under least-recently-iterated ordering (#24: a brief
    that has never run must not starve behind one that just ran).
    """
    log_dir = os.path.join(project_dir, ".loop", "logs")
    if not os.path.isdir(log_dir):
        return 0.0
    prefix = "worker_%s_" % brief_id
    best = 0.0
    try:
        for name in os.listdir(log_dir):
            if name.startswith(prefix) and name.endswith(".log"):
                try:
                    m = os.path.getmtime(os.path.join(log_dir, name))
                except OSError:
                    continue
                if m > best:
                    best = m
    except OSError:
        return 0.0
    return best


def order_worker_targets(primary, candidates, throttle):
    """Return the extra worker lines to emit (lines 4+), capped by THROTTLE.

    `primary` is the line already printed on line 2 (walk-order-first; pinned
    so the flag-off / byte-identical contract holds). `candidates` is a list
    of (last_iter_ts, "WORKER:brief,branch") for every running brief. Extras
    are ordered least-recently-iterated first (oldest mtime; never-iterated =
    0.0 sorts first — #24), deduped against `primary`, and limited so the
    TOTAL number of worker targets (primary + extras) never exceeds throttle.

    Pure function → unit-testable without git/running.json fixtures.
    """
    if throttle < 1:
        throttle = 1
    ordered = sorted(candidates, key=lambda c: (c[0], c[1]))
    extras = []
    for _ts, line in ordered:
        if line == primary:
            continue
        if 1 + len(extras) + 1 > throttle:
            break
        extras.append(line)
    return extras


def latest_review_verdict(project_dir, ref, brief_id, cycle):
    """Read the most recent review's `verdict:` frontmatter field from ref."""
    path = f".loop/modules/validator/state/reviews/{brief_id}-cycle-{cycle}.md"
    content = git_show(project_dir, ref, path)
    if not content:
        return None
    in_front = False
    for line in content.splitlines():
        s = line.strip()
        if s == "---":
            if not in_front:
                in_front = True
                continue
            else:
                break
        if in_front and s.lower().startswith("verdict:"):
            v = s.split(":", 1)[1].strip()
            v = v.split("#", 1)[0].strip().strip('"').strip("'")
            return v.lower() or None
    return None


def main():
    project_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    loop_dir = os.path.join(project_dir, ".loop")
    state_dir = os.path.join(loop_dir, "state")
    signals_dir = os.path.join(state_dir, "signals")
    running_file = os.path.join(state_dir, "running.json")

    conductor = "NONE"
    worker = "NONE"
    validator = "NONE"

    # WORKER_PARALLEL (default off): when on, emit up to THROTTLE worker
    # targets (least-recently-iterated first) instead of just the first
    # running brief in walk order (#24 starvation). The output stays
    # line-compatible: flag-off emits exactly one WORKER line as before;
    # flag-on may emit multiple, and the daemon reads all of them.
    worker_parallel = _worker_parallel_enabled(loop_dir)
    throttle = _throttle(loop_dir)
    worker_candidates = []  # list of (last_iter_ts, "WORKER:brief,branch")

    if not os.path.exists(running_file):
        print("CONDUCTOR:no_state")
        print("NONE")
        print("NONE")
        return

    # If queue files exist (and not stale), let daemon process them first
    for qf in ["pending-dispatch.json", "pending-merge.json"]:
        qpath = os.path.join(state_dir, qf)
        if os.path.exists(qpath):
            age_min = (time.time() - os.path.getmtime(qpath)) / 60
            if age_min < 30:
                print("NONE")
                print("NONE")
                print("NONE")
                return

    with open(running_file) as f:
        rc = json.load(f)

    # Read config for git remote
    config_file = os.path.join(loop_dir, "config.sh")
    remote = "origin"
    if os.path.exists(config_file):
        with open(config_file) as f:
            for line in f:
                if line.strip().startswith("GIT_REMOTE="):
                    remote = line.strip().split("=", 1)[1].strip('"').strip("'")
                    break

    # --- Queen triggers ---

    # Pending evaluation
    pending = rc.get("completed_pending_eval", [])
    if pending:
        conductor = "CONDUCTOR:pending_eval"

    # Active escalation signal
    if conductor == "NONE":
        esc_file = os.path.join(signals_dir, "escalate.json")
        if os.path.exists(esc_file):
            try:
                with open(esc_file) as f:
                    esc = json.load(f)
                if esc.get("type", "none") != "none":
                    conductor = "CONDUCTOR:active_signal"
            except (json.JSONDecodeError, KeyError):
                pass

    # High-priority queen triggers (pending_eval, active_signal) cannot be
    # preempted by validator_blocked. Track so the later block check knows.
    high_priority = conductor in ("CONDUCTOR:pending_eval", "CONDUCTOR:active_signal")

    # No active briefs
    active = rc.get("active", [])
    if not active and conductor == "NONE":
        conductor = "CONDUCTOR:no_active"

    blocked_brief = ""  # populated below if any active brief has verdict: block

    # --- Check active briefs for queen triggers, worker targets, validator targets ---
    for brief_entry in active:
        brief_id = brief_entry.get("brief", "")
        branch = brief_entry.get("branch", "")
        if not branch:
            continue

        status = "running"
        branch_exists = False
        iteration = 0
        used_ref = None
        for ref in [branch, f"{remote}/{branch}"]:
            prog_raw = git_show(project_dir, ref, ".loop/state/progress.json")
            if prog_raw is None:
                continue
            try:
                prog = json.loads(prog_raw)
            except (json.JSONDecodeError, ValueError):
                continue
            status = prog.get("status", "running")
            iteration = int(prog.get("iteration", 0) or 0)
            branch_exists = True
            used_ref = ref
            break

        if status == "complete":
            pass  # Phase 2.5 handles completion: moves to pending_merges or awaiting_review
        elif status == "blocked":
            if conductor == "NONE":
                conductor = f"CONDUCTOR:brief_blocked:{brief_id}"
        elif status == "running" and branch_exists:
            # Flag-off: first running brief in walk order wins (legacy).
            # Flag-on: collect all, order/emit below.
            if worker == "NONE":
                worker = f"WORKER:{brief_id},{branch}"
            if worker_parallel:
                worker_candidates.append(
                    (last_iteration_ts(project_dir, brief_id),
                     f"WORKER:{brief_id},{branch}")
                )
        elif not branch_exists:
            if conductor == "NONE":
                conductor = f"CONDUCTOR:stale_brief:{brief_id}"

        # Validator logic — only meaningful if we actually found the branch.
        if not branch_exists or not used_ref:
            continue

        max_cycle = max_review_cycle(project_dir, used_ref, brief_id)

        # Review owed when builder has advanced past last reviewed cycle.
        if iteration > max_cycle and validator == "NONE":
            tip_sha = git_rev_parse(project_dir, used_ref)
            if tip_sha:
                validator = f"VALIDATOR:{brief_id},{branch},{tip_sha}"

        # Block verdict on the most recent review preempts other queen triggers.
        if max_cycle > 0 and not blocked_brief:
            verdict = latest_review_verdict(project_dir, used_ref, brief_id, max_cycle)
            if verdict == "block":
                blocked_brief = brief_id

    # Precedence 3: validator_blocked preempts brief_complete / brief_blocked /
    # stale_brief / no_active, but not pending_eval or active_signal.
    if blocked_brief and not high_priority:
        conductor = f"CONDUCTOR:validator_blocked:{blocked_brief}"

    print(conductor)
    print(worker)
    print(validator)

    # WORKER_PARALLEL: emit any ADDITIONAL worker targets as lines 4+ (the
    # primary worker already occupies line 2; the 3-line conductor/worker/
    # validator contract is preserved so existing `sed -n 2p`/`sed -n 3p`
    # daemon reads are byte-unchanged). Ordering: least-recently-iterated
    # first (oldest log mtime; never-iterated = 0.0 sorts first). Cap the
    # TOTAL number of worker targets (line 2 + extras) at THROTTLE so the
    # daemon never spawns more concurrent workers than the configured cap.
    if worker_parallel and len(worker_candidates) > 1:
        for line in order_worker_targets(worker, worker_candidates, throttle):
            print(line)


if __name__ == "__main__":
    main()
