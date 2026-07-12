#!/usr/bin/env python3
"""Intent-declaration journal — the watchable-layer coordination primitive.

Design source: wiki/specs/harness-coordination.md §4 (the intent-declaration
primitive) + the delta-cursor requirement (Mattie, glyph review 2026-07-05).

Two modes:

  append      One JSONL line — {ts, session, action, detail} — atomically
              appended (O_APPEND) to the shared journal. Two sessions never
              clobber; a torn write loses at most the last line.

  read-fresh  Given a per-session cursor, emit ONLY the lines written AFTER
              the cursor by OTHER sessions, capped at --cap (default 12) with
              an explicit "N older lines skipped" marker when over. Advances
              the cursor. Empty stdout when nothing new — the near-zero-cost
              quiet-turn path the firehose constraint demands.

This is the watchable layer: ephemeral, gitignored, disposable. It is NEVER a
durable record. A decision that comes out of an avoided collision gets written
to the auditable layer (the decisions page) like any other decision.

Session-tag resolution (documented, deliberate — not an invented default):
  1. --session argument (hook wrappers pass Claude Code's session_id here)
  2. INTENT_SESSION_TAG environment variable
  3. controlling tty name, if one is attached
  4. otherwise: fail loud. We do NOT invent a session identity — a wrong tag
     silently corrupts the "other sessions only" filter (Rule 10).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_JOURNAL = os.path.join(REPO_ROOT, ".loop", "state", "intent-journal.jsonl")
DEFAULT_CURSOR_DIR = os.path.join(REPO_ROOT, ".loop", "state", "intent-cursors")
DEFAULT_CAP = 12


def resolve_session(explicit):
    """Resolve the session tag; fail loud if no real identity is available."""
    if explicit:
        return explicit
    env = os.environ.get("INTENT_SESSION_TAG")
    if env:
        return env
    # A controlling tty is a real per-terminal signal, not an invented value.
    try:
        return os.path.basename(os.ttyname(2))
    except OSError:
        pass
    sys.exit(
        "intent-journal: cannot resolve a session tag. Pass --session, set "
        "INTENT_SESSION_TAG, or run attached to a tty. Refusing to invent one."
    )


def cursor_path_for(session, explicit_cursor, cursor_dir):
    if explicit_cursor:
        return explicit_cursor
    return os.path.join(cursor_dir, f"{session}.cursor")


def do_append(args):
    session = resolve_session(args.session)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "action": args.action,
        "detail": args.detail,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(args.journal), exist_ok=True)
    # O_APPEND makes each write() land atomically at end-of-file, so concurrent
    # sessions never interleave or clobber. One write() per line.
    fd = os.open(args.journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return 0


def _read_cursor(path):
    try:
        with open(path, "r") as f:
            return int(f.read().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def _write_cursor(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(str(value))
    os.replace(tmp, path)  # atomic — a torn cursor would replay or skip lines


def _format(record):
    ts = record.get("ts", "")
    # Trim to HH:MM for the injected line; the full ts stays in the journal.
    short = ts
    if "T" in ts:
        short = ts.split("T", 1)[1][:5]
    return f"[{short} {record.get('session', '?')}] {record.get('action', '')} — {record.get('detail', '')}"


def do_read_fresh(args):
    session = resolve_session(args.session)
    cursor_file = cursor_path_for(session, args.cursor, args.cursor_dir)
    cursor = _read_cursor(cursor_file)

    try:
        with open(args.journal, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    total = len(lines)
    # Everything up to `total` is now considered seen — advance regardless of
    # what we surface, so quiet turns cost nothing next time.
    fresh_from_others = []
    for raw in lines[cursor:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue  # torn/partial trailing line — skip, don't crash
        if record.get("session") == session:
            continue  # own lines never echo back
        fresh_from_others.append(record)

    _write_cursor(cursor_file, total)

    if not fresh_from_others:
        return 0  # empty stdout — the quiet-turn zero-cost path

    cap = args.cap
    skipped = 0
    shown = fresh_from_others
    if cap is not None and cap > 0 and len(fresh_from_others) > cap:
        skipped = len(fresh_from_others) - cap
        shown = fresh_from_others[-cap:]

    out = ["Peer-director intent since your last turn (watchable layer — ephemeral, not knowledge):"]
    if skipped:
        out.append(f"  … {skipped} older line{'s' if skipped != 1 else ''} skipped")
    for record in shown:
        out.append("  " + _format(record))
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    a = sub.add_parser("append", help="append one intent line to the journal")
    a.add_argument("--action", required=True, help="short verb phrase — what I'm about to do")
    a.add_argument("--detail", required=True, help="one-line specifics (lane, ref, surface)")
    a.add_argument("--session", default=None, help="session tag (else INTENT_SESSION_TAG / tty)")
    a.add_argument("--journal", default=DEFAULT_JOURNAL)
    a.set_defaults(func=do_append)

    r = sub.add_parser("read-fresh", help="emit post-cursor lines from OTHER sessions, capped")
    r.add_argument("--session", default=None, help="session tag (else INTENT_SESSION_TAG / tty)")
    r.add_argument("--cursor", default=None, help="explicit cursor file path (else derived from session)")
    r.add_argument("--cursor-dir", default=DEFAULT_CURSOR_DIR)
    r.add_argument("--cap", type=int, default=DEFAULT_CAP, help="max lines surfaced (default 12)")
    r.add_argument("--journal", default=DEFAULT_JOURNAL)
    r.set_defaults(func=do_read_fresh)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
