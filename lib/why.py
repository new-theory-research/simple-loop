#!/usr/bin/env python3
"""Explain why a queued brief will — or won't — dispatch.

One pure function, two surfaces (this module's API + `loop why <brief-id>`).
Tonight's receipt: a queued brief sat undispatched for over an hour and the
operator hand-evaluated seven implicit predicates to find out why. This turns
that manual archaeology into a checklist.

`explain_dispatchability(project_dir, brief_id, running=None)` evaluates EVERY
predicate the daemon applies (not just the first failure) and returns a list of
Check(name, ok, receipt) so the surface can show the whole picture. It reuses
the REAL implementations — queue.py's enumerate/parse helpers, actions.py's
concurrency + dependency gates — so it reports what the daemon WILL do. If a
gate is wrong (see the Depends-on note below), this explainer is wrong the same
way, on purpose.

Predicates, in the operator's order:
  1. queued      — card present with Status: queued on the synced checkout.
  2. lane        — card's Program: lane is in the daemon's LOOP_LANE roster.
                   Fail-closed: an unlabeled card never matches a non-empty
                   roster; multi-lane comma lists supported; empty roster = no
                   filter (single-daemon).
  3. parallel_safe — Parallel-safe absent silently defaults false (single slot).
                   Evaluated against the live board: a solo brief dispatches on
                   an empty board but is blocked while anything is active.
  4. depends_on  — Depends-on satisfied per the enforcer, which recognizes ONLY
                   card Status: merged (daemon-merged). A dependency completed
                   via a director arc (card Status: complete) reads as UNMET.
                   This is a documented gap queued for design review — reported
                   here AS IMPLEMENTED, not fixed.
  5. claim_ref   — no stale refs/claims/<brief> claim ref on the remote.
  6. not_running — not already in running.json active/awaiting/pending/history.

Plus two gates dispatch() applies that the operator's folk list missed (the
folk list said seven; the code says ten — recorded as brief-166 activation
design-review evidence; the third missed gate, edit-surface overlap, is folded
into parallel_safe, which reuses the real concurrency gate):
  7. throttle    — in_flight (len active) vs THROTTLE, resolved via the same
                   config parse dispatch() uses (actions.config_int).
  8. solo_drain  — the drain-for-solo hold (SOLO_DRAIN_AFTER_SECS): when a
                   Parallel-safe:false brief sits at the queue head past the
                   threshold, ALL other dispatch is held until the board
                   empties. Reuses queue.head_solo_drain — dispatch()'s own
                   decision function.
 11. lane_mutex  — issue #74, Mattie's ruling 2026-07-11: Program: is the unit
                   of parallelism — a lane is a single thread. At most one
                   active brief per Program: value, independent of THROTTLE and
                   Parallel-safe/edit-surface. Reuses actions.lane_mutex_blocker
                   — dispatch()'s own gate. Unlabeled briefs (no Program:) are
                   N/A (surface concurrency governs). Numbered 11 in the ruling's
                   predicate ledger (the folded surface/edit gates fill 9–10).

The operator's predicate 7 — "daemon awake" — is not a property of
the brief and can't be answered by a pure function, so it is omitted here. A
true queue also needs a live daemon; the CLI footer points at `loop status`.

CLI:
    loop why <brief-id>   checklist for one brief, exit 0 if dispatchable else 1
    loop why              preflight sweep: run for every Status: queued card
"""

import importlib.util
import json
import os
import subprocess
import sys
from collections import namedtuple
from datetime import datetime

Check = namedtuple("Check", ["name", "ok", "receipt"])

# Same buckets enumerate_dispatchable excludes — a brief in any of these is not
# a fresh dispatch candidate.
_RUNNING_BUCKETS = ("active", "awaiting_review", "pending_merges",
                    "completed_pending_eval", "history")


def _load_by_path(mod_name, filename):
    """Load a lib module by explicit path (queue.py collides with stdlib)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_queue = _load_by_path("loop_queue", "queue.py")
_actions = _load_by_path("loop_actions", "actions.py")


def _resolve_lane(project_dir, lane):
    """Daemon LOOP_LANE roster: explicit arg > env LOOP_LANE > config.sh.

    Mirrors daemon.sh precedence (CLI --lane, then env/config). Empty/whitespace
    resolves to None (no filter — single-daemon global dispatch).
    """
    if lane is not None:
        return lane
    env = os.environ.get("LOOP_LANE")
    if env is not None:
        return env
    cfg = _actions.read_config(os.path.join(project_dir, ".loop"))
    return cfg.get("LOOP_LANE", "")


def _load_running(project_dir, running):
    if running is not None:
        return running
    path = os.path.join(project_dir, ".loop", "state", "running.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, OSError, json.JSONDecodeError):
        return {}


def _card_status_lookup(project_dir, card_id):
    """Status: value of another brief's card, or '' if absent — for receipts."""
    path = os.path.join(project_dir, "wiki", "briefs", "cards", card_id, "index.md")
    return _queue._parse_card_status(path)


def explain_dispatchability(project_dir, brief_id, running=None, lane=None):
    """Return a list[Check] — every dispatch predicate for brief_id, evaluated.

    Not first-failure-only: every predicate is evaluated so the surface shows
    the full picture. Reuses the real queue/enforcer implementations.
    """
    checks = []
    card_path = os.path.join(project_dir, "wiki", "briefs", "cards", brief_id, "index.md")
    card_rel = f"wiki/briefs/cards/{brief_id}/index.md"
    running = _load_running(project_dir, running)
    config = _actions.read_config(os.path.join(project_dir, ".loop"))
    remote = config.get("GIT_REMOTE", "origin")

    # ── 1. queued ────────────────────────────────────────────────────
    if not os.path.isfile(card_path):
        status = None
        checks.append(Check("queued", False,
                            f"no card at {card_rel}"))
    else:
        status = _queue._parse_card_status(card_path)
        if status == "queued":
            checks.append(Check("queued", True, "card Status: queued"))
        else:
            checks.append(Check("queued", False,
                                f"card Status: {status or '(none)'!r} — not queued"))

    # ── 2. lane ──────────────────────────────────────────────────────
    lane_spec = _resolve_lane(project_dir, lane)
    lane_set = _queue._lane_set(lane_spec)
    if lane_set is None:
        checks.append(Check("lane", True,
                            "no lane filter — single-daemon, every lane dispatches"))
    else:
        roster = ",".join(sorted(lane_set))
        program = _queue._parse_card_program(card_path) if os.path.isfile(card_path) else ""
        if program in lane_set:
            checks.append(Check("lane", True,
                                f"lane {program!r} in daemon roster {roster!r}"))
        elif not program:
            checks.append(Check("lane", False,
                                f"card declares no Program: lane — fail-closed against roster {roster!r}"))
        else:
            checks.append(Check("lane", False,
                                f"lane {program!r} not in daemon roster {roster!r}"))

    # ── 3. parallel_safe (evaluated against the live board) ──────────
    parallel_safe, edit_surface = _actions.parse_concurrency_frontmatter(card_path)
    active = running.get("active", [])
    if not active:
        if parallel_safe:
            checks.append(Check("parallel_safe", True,
                                "Parallel-safe: true — board empty, dispatchable"))
        else:
            checks.append(Check("parallel_safe", True,
                                "Parallel-safe absent — defaults false (single slot); board empty, dispatchable"))
    elif not parallel_safe:
        checks.append(Check("parallel_safe", False,
                            f"Parallel-safe absent — defaults false (single slot); {len(active)} brief(s) active → blocked by {active[0].get('brief','?')}"))
    else:
        block = None
        for entry in active:
            if not entry.get("parallel_safe", False):
                block = ("active brief not parallel-safe", entry.get("brief", "?"))
                break
            if _actions.edit_surfaces_overlap(edit_surface, entry.get("edit_surface", [])):
                block = ("edit-surface overlap", entry.get("brief", "?"))
                break
        if block:
            checks.append(Check("parallel_safe", False,
                                f"Parallel-safe: true but {block[0]} with active {block[1]}"))
        else:
            checks.append(Check("parallel_safe", True,
                                "Parallel-safe: true — no edit-surface overlap with active board"))

    # ── 4. depends_on (the enforcer's exact verdict) ─────────────────
    depends_on, merged_ids, unmet = _actions.depends_on_verdict(project_dir, card_rel)
    if not depends_on:
        checks.append(Check("depends_on", True, "no Depends-on"))
    elif not unmet:
        checks.append(Check("depends_on", True,
                            f"Depends-on {depends_on} all merged"))
    else:
        # Concrete receipt: show the first unmet dep's ACTUAL card status so the
        # daemon-merged-only gap is legible.
        d = unmet[0]
        dep_status = _card_status_lookup(project_dir, d) or "(no card)"
        if dep_status == "merged":
            # Would only happen on an id-match miss — still honest.
            extra = "id did not match any merged card"
        else:
            extra = f"card status {dep_status!r} not recognized by enforcer (recognizes daemon-merged only)"
        more = "" if len(unmet) == 1 else f" (+{len(unmet)-1} more unmet)"
        checks.append(Check("depends_on", False,
                            f"Depends-on {d}: {extra}{more}"))

    # ── 5. claim_ref (git ls-remote) ─────────────────────────────────
    ref = f"refs/claims/{brief_id}"
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "ls-remote", remote, ref],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            checks.append(Check("claim_ref", True,
                                f"remote {remote!r} unreachable — claim ref not verified (assuming unclaimed)"))
        elif r.stdout.strip():
            checks.append(Check("claim_ref", False,
                                f"{ref} present on remote {remote!r} — already claimed by a daemon"))
        else:
            checks.append(Check("claim_ref", True,
                                f"no claim ref on remote {remote!r}"))
    except (subprocess.SubprocessError, OSError) as e:
        checks.append(Check("claim_ref", True,
                            f"could not run ls-remote on {remote!r} ({e}) — claim ref not verified"))

    # ── 6. not_running ───────────────────────────────────────────────
    found_bucket = None
    for bucket in _RUNNING_BUCKETS:
        for entry in running.get(bucket, []):
            if _actions._brief_id_matches(brief_id, entry.get("brief", "")):
                found_bucket = bucket
                break
        if found_bucket:
            break
    if found_bucket:
        checks.append(Check("not_running", False,
                            f"already in running.json {found_bucket}[] — not a fresh candidate"))
    else:
        checks.append(Check("not_running", True,
                            "not in running.json active/awaiting/pending/history"))

    # ── 7. throttle (dispatch()'s own THROTTLE resolution) ───────────
    throttle = _actions.config_int(config, "THROTTLE", 1)
    if throttle < 1:
        throttle = 1
    in_flight = len(active)
    if in_flight >= throttle:
        names = ", ".join(e.get("brief", "?") for e in active)
        checks.append(Check("throttle", False,
                            f"board at THROTTLE cap {in_flight}/{throttle} — blocked until a slot frees (active: {names})"))
    else:
        checks.append(Check("throttle", True,
                            f"board {in_flight}/{throttle} — capacity for this brief"))

    # ── 8. solo_drain (dispatch()'s drain-for-solo hold) ─────────────
    # Reuses queue.head_solo_drain — the exact decision function dispatch()
    # calls. Gate semantics mirrored: consulted only when the feature is on
    # (SOLO_DRAIN_AFTER_SECS > 0) AND the board is non-empty; the draining
    # head itself is allowed through.
    drain_secs = _actions.config_int(config, "SOLO_DRAIN_AFTER_SECS", 0)
    if drain_secs <= 0:
        checks.append(Check("solo_drain", True,
                            "SOLO_DRAIN_AFTER_SECS off — no drain gate"))
    elif not active:
        checks.append(Check("solo_drain", True,
                            "board empty — no solo brief draining the board"))
    else:
        decision = _queue.head_solo_drain(
            project_dir, drain_secs, running=running, lane=lane_spec)
        if decision["drain"] and decision["brief"] != brief_id:
            checks.append(Check("solo_drain", False,
                                f"held: Parallel-safe:false brief {decision['brief']} at queue head "
                                f"past SOLO_DRAIN_AFTER_SECS (draining {int(decision['waited'])}s) — "
                                f"all other dispatch held until board empties"))
        elif decision["drain"]:
            checks.append(Check("solo_drain", True,
                                f"this brief IS the draining solo head (waited {int(decision['waited'])}s) — "
                                f"allowed through once the board empties"))
        else:
            checks.append(Check("solo_drain", True,
                                "no solo brief draining the board"))

    # ── 11. lane_mutex (issue #74 — Program: is the unit of parallelism) ─
    # Mattie's ruling 2026-07-11: a program is a single thread — at most one
    # active brief per Program: value, independent of THROTTLE and
    # Parallel-safe/edit-surface. Reuses actions.lane_mutex_blocker — the exact
    # function dispatch() gates on — so this green matches the daemon's.
    program = _queue._parse_card_program(card_path) if os.path.isfile(card_path) else ""
    if not program:
        checks.append(Check("lane_mutex", True,
                            "card declares no Program: — lane mutex N/A "
                            "(surface concurrency governs)"))
    else:
        holder = _actions.lane_mutex_blocker(project_dir, program, active)
        if holder is None:
            checks.append(Check("lane_mutex", True,
                                f"no same-lane brief active — lane {program!r} free"))
        else:
            checks.append(Check("lane_mutex", False,
                                f"lane {program!r} single-threaded — held behind "
                                f"{holder.get('brief', '?')} "
                                f"(active, slot {holder.get('worker_slot', '?')})"))

    return checks


def slots_available_candidate(project_dir, running=None, lane=None, cap=3):
    """Pure wake-check for the daemon's slots_available trigger (issue #51).

    Return the brief_id of the first cross-lane dispatchable candidate, or ""
    if none. The daemon only WAKES the queen on a non-empty return; the queen's
    assess remains the decider (Mattie's doctrine: the wake check is pure
    functions the repo already ships, not inference).

    Everything the trigger needs is already a deterministic gate:
      - "board has capacity" (active < THROTTLE) is the `throttle` check;
      - "cross-lane work exists" falls out of `lane_mutex` — a queued brief
        whose Program: is held by an active brief FAILS lane_mutex, so a
        same-lane candidate can never satisfy `all(ok)`;
      - "a draining solo head suppresses slot-filling" is the `solo_drain`
        check (and parallel_safe for the solo head itself).
    A brief counts only when EVERY explain_dispatchability check passes, so all
    of these hold by construction — no separate re-derivation.

    Cost is bounded: enumerate is lane-scoped and cheap, and at most `cap`
    queue-head candidates are evaluated (short-circuits on the first green).
    """
    running = _load_running(project_dir, running)
    lane_spec = _resolve_lane(project_dir, lane)
    candidates = _queue.enumerate_dispatchable(
        project_dir, running=running, lane=lane_spec)
    for cand in candidates[:cap]:
        brief_id = cand["brief"]
        checks = explain_dispatchability(
            project_dir, brief_id, running=running, lane=lane_spec)
        if all(c.ok for c in checks):
            return brief_id
    return ""


# ─── Papercuts ledger (Scav, 2026-07-11) ─────────────────────────────

_PAPERCUT_NOTE_SHOWN = [False]


def _append_papercut(project_dir, brief_id, checks, preflight=False):
    """Append a stop-rate entry to wiki/harness-operations/papercuts.md.

    Scav's append-only incidence ledger: two clauses per entry — what happened,
    what we expected — no fixes, no editorializing, no dedup (duplicates ARE the
    rate). loop why's ideal steady-state usage is ZERO (Mattie's doctrine: it's
    an instrument for improving the harness, not for operating it), so every
    explicit run is itself a papercut — auto-appending records it in the same
    ledger as every other stop, which is what makes "usage should trend to zero"
    measurable. A project without the ledger file is not an error.
    """
    path = os.path.join(project_dir, "wiki", "harness-operations", "papercuts.md")
    if not os.path.isfile(path):
        if not _PAPERCUT_NOTE_SHOWN[0]:
            print("papercuts.md not found — stop not recorded")
            _PAPERCUT_NOTE_SHOWN[0] = True
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    tag = " (preflight)" if preflight else ""
    failing = [c for c in checks if not c.ok]
    if failing:
        first = failing[0]
        happened = f"loop why {brief_id}{tag}: blocked — {first.name}: {first.receipt}"
        expected = "queued brief dispatches without manual diagnosis"
    else:
        happened = (f"loop why {brief_id}{tag}: dispatchable — ran the explainer "
                    "on an already-dispatchable brief")
        expected = "not to need to check; loop why steady-state usage is zero"
    with open(path, "a") as f:
        f.write(f"- **{ts}** — {happened}. Expected: {expected}.\n")


# ─── CLI ─────────────────────────────────────────────────────────────

_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _color(s, code):
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return s
    return f"{code}{s}{_RESET}"


def _print_checklist(brief_id, checks):
    dispatchable = all(c.ok for c in checks)
    head = _color("✓", _GREEN) if dispatchable else _color("✗", _RED)
    # Honesty marker (review rec): a green built on a fail-open check must say
    # so — the rollup names any passing check whose receipt admits it could not
    # be verified (e.g. claim_ref with the remote unreachable).
    unverified = [c.name for c in checks if c.ok and "not verified" in c.receipt]
    verdict = "DISPATCHABLE" if dispatchable else "BLOCKED"
    if dispatchable and unverified:
        verdict += f" (unverified: {', '.join(unverified)})"
    print(f"{head} {brief_id} — {verdict}")
    for c in checks:
        mark = _color("✓", _GREEN) if c.ok else _color("✗", _RED)
        name = c.name.ljust(14)
        print(f"  {mark} {name} {c.receipt}")
    return dispatchable


def _queued_card_ids(project_dir):
    cards_dir = os.path.join(project_dir, "wiki", "briefs", "cards")
    out = []
    if os.path.isdir(cards_dir):
        for card_id in sorted(os.listdir(cards_dir)):
            if card_id.startswith("."):
                continue
            card_path = os.path.join(cards_dir, card_id, "index.md")
            if os.path.isfile(card_path) and _queue._parse_card_status(card_path) == "queued":
                out.append(card_id)
    return out


def _lane_from_argv(argv):
    lane = None
    for i, a in enumerate(argv):
        if a == "--lane" and i + 1 < len(argv):
            lane = argv[i + 1]
        elif a.startswith("--lane="):
            lane = a[len("--lane="):]
    return lane


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    project_dir = os.getcwd()
    brief_id = None

    # --slots-available: pure wake-check for the daemon's slots_available
    # trigger (issue #51). Distinct code path — NO papercut side-effect, NO
    # checklist print: it runs per daemon tick and must stay cheap and quiet.
    # Emits three lines when a cross-lane dispatchable candidate exists (exit 0):
    #   <candidate brief id>
    #   <queue fingerprint>   (dedup key part 1 — queue changed → re-fire)
    #   <active-set fingerprint>  (dedup key part 2 — board changed → re-fire)
    # and prints nothing / exits 1 when there is no candidate. Failure-safe by
    # the daemon's `|| echo`/`2>/dev/null` wrappers.
    if "--slots-available" in argv:
        positional = [a for a in argv if not a.startswith("-")]
        pdir = positional[0] if positional and os.path.isdir(positional[0]) else project_dir
        lane = _lane_from_argv(argv)
        running = _load_running(pdir, None)
        bid = slots_available_candidate(pdir, running=running, lane=lane)
        if not bid:
            return 1
        qfp = _queue.queue_fingerprint(pdir, lane=_resolve_lane(pdir, lane))
        afp = ",".join(sorted(e.get("brief", "") for e in running.get("active", [])))
        print(bid)
        print(qfp)
        print(afp)
        return 0
    # Positional args: [project_dir?] [brief_id?]. `loop why` wraps this passing
    # PROJECT_DIR first, then the optional brief id.
    positional = [a for a in argv if not a.startswith("-")]
    if len(positional) == 1:
        # Ambiguous single arg: treat an existing dir as project_dir, else brief.
        if os.path.isdir(positional[0]):
            project_dir = positional[0]
        else:
            brief_id = positional[0]
    elif len(positional) >= 2:
        project_dir = positional[0]
        brief_id = positional[1]

    footer = _color(
        "note: a true queue also needs a live daemon — check `loop status`.",
        _DIM)

    if brief_id:
        checks = explain_dispatchability(project_dir, brief_id)
        ok = _print_checklist(brief_id, checks)
        # Every explicit run is itself a papercut — record it (green included).
        _append_papercut(project_dir, brief_id, checks, preflight=False)
        print()
        print(footer)
        return 0 if ok else 1

    # Preflight sweep: every queued card.
    queued = _queued_card_ids(project_dir)
    if not queued:
        print("No Status: queued cards found.")
        print()
        print(footer)
        return 0
    all_ok = True
    for i, bid in enumerate(queued):
        if i:
            print()
        checks = explain_dispatchability(project_dir, bid)
        if not _print_checklist(bid, checks):
            all_ok = False
            # Sweep records only BLOCKED briefs, marked (preflight).
            _append_papercut(project_dir, bid, checks, preflight=True)
    print()
    print(footer)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
