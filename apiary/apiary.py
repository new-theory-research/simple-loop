#!/usr/bin/env python3
"""apiary — the deliberately-dumb presence bus (brief-165, BRICK 1, v0).

Two endpoints, one shared per-box token, a bounded SQLite ring buffer. It is
boring on purpose: every feature it lacks is a feature. It stores presence
events written by `hum` and hands them back to eyes (hive, alerts, humans). It
NEVER decides anything — the coordination guard is the one place it says no.

    POST /v1/events   append a batch  (auth, guard, size caps, received_at, dedup)
    GET  /v1/events?since=<cursor>    poll for events after a rowid cursor

Storage bound (v0): 100,000 events OR 7 days, whichever trims first. Total loss
of this store is acceptable by contract — the durable truth is git + local
journals. Zero third-party deps: stdlib http.server + sqlite3.

Run locally (the only supported path pre-deploy — Railway deploy is Mattie's
gate, see the card):

    python3 apiary/apiary.py --db /tmp/apiary.db --port 8787 --token dev-token
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ── Bounds (loss-tolerant, never blocking) ────────────────────────────────────
MAX_EVENT_BYTES = 16 * 1024          # 16 KB per event
MAX_BATCH_BYTES = 1024 * 1024        # 1 MB per POST body
MAX_BATCH_EVENTS = 500               # 500 events per POST
RING_MAX_EVENTS = 100_000            # ring-buffer cap by count
RING_MAX_AGE_SECONDS = 7 * 24 * 3600  # ring-buffer cap by age (7 days)
GET_PAGE_LIMIT = 1000                # max events returned by one GET

TOKEN_HEADER = "X-Apiary-Token"

# ── The law, enforced (brief-165 piece 1) ─────────────────────────────────────
# Reserved coordination-COMMAND verbs. These are pure decisions with no presence
# meaning; the only reason one would ride the bus is to route a coordination
# decision — exactly what the presence-plane law forbids. Reject loud, drop, log.
#
# Deliberately NOT here: the intent hook's "dispatch" (an observation that a
# dispatch happened — the row it becomes on the floor IS a hum, per the law) and
# runtime "merged"/"dispatched" (past-tense facts on the `event` field, not
# `action`). See docs/architecture/presence-plane.md for the reconciliation.
COORDINATION_VERBS = {"claim", "gate", "merge-decide", "merge_decide"}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(path):
    """Open (creating if needed) the ring-buffer store. WAL for concurrent read."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
            id          TEXT UNIQUE,
            received_at TEXT NOT NULL,
            received_ts REAL NOT NULL,
            body        TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def is_coordination(event):
    """True when the event's `action` is a reserved coordination-command verb."""
    action = (event or {}).get("action")
    return isinstance(action, str) and action.strip().lower() in COORDINATION_VERBS


class BatchError(Exception):
    """A batch-level rejection carrying the HTTP status to return."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def ingest(conn, events, now_iso=None, now_ts=None, log=print):
    """Validate + store a batch. Pure over an open connection so it unit-tests
    without HTTP. Returns a summary dict. Raises BatchError for batch-level
    rejections (guard 422, size 413) so nothing in the batch is stored.
    """
    if not isinstance(events, list):
        raise BatchError(400, "body must be a JSON array of events")
    if len(events) > MAX_BATCH_EVENTS:
        raise BatchError(413, f"batch has {len(events)} events, cap {MAX_BATCH_EVENTS}")

    # Coordination guard FIRST: any coordination write fails the whole POST loud,
    # storing nothing. Loss-tolerant means the bus may drop presence; it must
    # never silently accept a coordination write.
    for ev in events:
        if is_coordination(ev):
            log(f"apiary: REJECTED coordination write action={ev.get('action')!r} "
                f"id={ev.get('id')!r} — presence plane never carries coordination")
            raise BatchError(422, f"coordination verb rejected: {ev.get('action')!r}")

    now_iso = now_iso or _now_iso()
    now_ts = now_ts if now_ts is not None else time.time()
    stored, deduped, skipped_poison = 0, 0, 0
    for ev in events:
        blob = json.dumps(ev, ensure_ascii=False)
        if len(blob.encode("utf-8")) > MAX_EVENT_BYTES:
            # Poison event — drop + log server-side (hum should have pre-skipped).
            # Never wedge the store; the contract covers the loss.
            skipped_poison += 1
            log(f"apiary: dropped oversize event ({len(blob)}B > {MAX_EVENT_BYTES}) "
                f"id={ev.get('id')!r}")
            continue
        eid = ev.get("id")
        cur = conn.execute(
            "INSERT OR IGNORE INTO events (id, received_at, received_ts, body) "
            "VALUES (?, ?, ?, ?)",
            (eid, now_iso, now_ts, blob),
        )
        if cur.rowcount == 0:
            deduped += 1  # unique-index drop of a byte-identical replay (the belt)
        else:
            stored += 1
    conn.commit()
    _trim(conn, now_ts)
    return {"stored": stored, "deduped": deduped, "skipped_poison": skipped_poison,
            "received_at": now_iso}


def _trim(conn, now_ts):
    """Ring-buffer trim: drop rows older than 7 days, then any beyond 100k."""
    conn.execute("DELETE FROM events WHERE received_ts < ?", (now_ts - RING_MAX_AGE_SECONDS,))
    conn.execute(
        "DELETE FROM events WHERE rowid <= "
        "(SELECT MAX(rowid) FROM events) - ?",
        (RING_MAX_EVENTS,),
    )
    conn.commit()


def fetch_since(conn, cursor, limit=GET_PAGE_LIMIT):
    """Return events with rowid > cursor, oldest first. Each carries its server
    stamp (`received_at`) and its `cursor` (rowid) so the reader can advance.
    Ordering keys on rowid = insertion order = received_at order (skew-immune).
    """
    rows = conn.execute(
        "SELECT rowid, received_at, body FROM events WHERE rowid > ? "
        "ORDER BY rowid ASC LIMIT ?",
        (cursor, limit),
    ).fetchall()
    out = []
    for rowid, received_at, body in rows:
        try:
            ev = json.loads(body)
        except json.JSONDecodeError:
            continue
        ev["received_at"] = received_at
        ev["cursor"] = rowid
        out.append(ev)
    return out


def make_handler(conn, token):
    class Handler(BaseHTTPRequestHandler):
        # Quiet default logging; the ingest path logs what matters.
        def log_message(self, *args):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            return self.headers.get(TOKEN_HEADER) == token

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/v1/events":
                return self._send(404, {"error": "not found"})
            if not self._authed():
                return self._send(401, {"error": "bad or missing token"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BATCH_BYTES:
                # Drain and reject without buffering the whole oversize body.
                self._send(413, {"error": f"body {length}B > {MAX_BATCH_BYTES}"})
                return
            raw = self.rfile.read(length) if length else b""
            try:
                events = json.loads(raw or b"[]")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})
            try:
                summary = ingest(conn, events)
            except BatchError as e:
                return self._send(e.status, {"error": e.message})
            return self._send(200, summary)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/v1/health":
                return self._send(200, {"ok": True})
            if parsed.path != "/v1/events":
                return self._send(404, {"error": "not found"})
            if not self._authed():
                return self._send(401, {"error": "bad or missing token"})
            qs = parse_qs(parsed.query)
            try:
                since = int((qs.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            events = fetch_since(conn, since)
            next_cursor = events[-1]["cursor"] if events else since
            return self._send(200, {"events": events, "cursor": next_cursor})

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.environ.get("APIARY_DB", "apiary.db"))
    p.add_argument("--port", type=int, default=int(os.environ.get("APIARY_PORT", "8787")))
    p.add_argument("--host", default=os.environ.get("APIARY_HOST", "127.0.0.1"))
    p.add_argument("--token", default=os.environ.get("APIARY_TOKEN", "dev-token"))
    args = p.parse_args(argv)

    conn = init_db(args.db)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(conn, args.token))
    print(f"apiary v0 listening on http://{args.host}:{args.port} db={args.db}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
