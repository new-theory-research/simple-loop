#!/usr/bin/env python3
"""hum — the per-box presence shipper sidecar (brief-165, BRICK 1).

One tiny long-running process per box. It TAILS the local journals with a
per-file byte cursor and POSTs post-cursor lines in batches to the apiary.
Writers are untouched — hum is a *reader*. The local file is the buffer and the
offline fallback: if the apiary is unreachable, hum simply does not advance its
cursor and the file keeps growing.

Event identity is `id = box:journal-basename:byte-offset`, computed for free as
hum tails (it holds all three). Delivery is **at-least-once**: hum POSTs a batch,
THEN persists its cursor — a crash between the two re-sends the batch on restart.
That is correct and expected; identity (dedup at the apiary and at hive) makes
the redelivery harmless.

The daemon does not know hum exists — that is the loss-tolerance contract. Losing
hum loses the *view*, never the *work*.

Run (launchd brings it up on boot; see hum/com.scaviefae.hum.plist):

    python3 hum/hum.py --box lady-titania --project-dir /path/to/project \\
        --apiary-url http://127.0.0.1:8787 --token dev-token
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

MAX_EVENT_BYTES = 16 * 1024        # matches apiary bound — poison skip threshold
MAX_BATCH_EVENTS = 500             # matches apiary batch cap
POLL_SECONDS = 3.0                 # tail cadence
BACKOFF_MAX_SECONDS = 60.0         # bounded backoff when the apiary is unreachable

# Append-only JSONL journals hum tails by byte cursor. heartbeat.json is a
# rewritten single-object file, so it is shipped by snapshot-on-change instead.
DEFAULT_JOURNALS = ("runtime-events.jsonl", "intent-journal.jsonl")


def _state_dir(project_dir):
    return os.path.join(project_dir, ".loop", "state")


def _cursor_path(project_dir, key):
    d = os.path.join(_state_dir(project_dir), "hum-cursors")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{key}.cursor")


def read_cursor(project_dir, key):
    try:
        with open(_cursor_path(project_dir, key)) as f:
            return int(f.read().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def write_cursor(project_dir, key, value):
    path = _cursor_path(project_dir, key)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(str(value))
    os.replace(tmp, path)  # atomic — a torn cursor would replay or skip lines


def tail_journal(path, cursor, box, journal_key, log=print):
    """Read complete post-cursor lines from an append-only JSONL journal.

    Returns (events, new_cursor). Each event is the parsed line dict with `box`
    and a stable `id = box:journal:byte-offset` stamped on. A trailing partial
    line (no newline yet) is left unconsumed — the cursor stops before it, so the
    next pass re-reads it once it is complete. Poison (oversize or unparseable)
    lines are skipped LOUD but still consumed so they never wedge the tail.
    """
    try:
        with open(path, "rb") as f:
            f.seek(cursor)
            buf = f.read()
    except FileNotFoundError:
        return [], cursor

    events = []
    consumed = 0  # bytes past `cursor` that are safe to commit to the cursor
    while True:
        nl = buf.find(b"\n", consumed)
        if nl == -1:
            break  # only a partial trailing line remains — leave it unconsumed
        line_start = cursor + consumed
        line_bytes = buf[consumed:nl]  # excludes the newline
        consumed = nl + 1

        if len(line_bytes) > MAX_EVENT_BYTES:
            log(f"hum: SKIP poison oversize line ({len(line_bytes)}B > {MAX_EVENT_BYTES}) "
                f"in {journal_key} at offset {line_start}")
            continue
        stripped = line_bytes.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            log(f"hum: SKIP poison unparseable line in {journal_key} at offset {line_start}")
            continue
        if not isinstance(ev, dict):
            log(f"hum: SKIP non-object line in {journal_key} at offset {line_start}")
            continue
        ev.setdefault("box", box)
        ev["id"] = f"{box}:{journal_key}:{line_start}"
        events.append(ev)

    return events, cursor + consumed


def snapshot_heartbeat(path, box, last_ts, log=print):
    """heartbeat.json is rewritten in place, so tail it by snapshot-on-change.

    Returns (event_or_None, new_last_ts). Ships one event only when the `ts`
    changed since the last ship; its id keys on that ts so a replay dedups.
    """
    try:
        with open(path) as f:
            hb = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None, last_ts
    ts = hb.get("ts")
    if not ts or ts == last_ts:
        return None, last_ts
    ev = dict(hb)
    ev.setdefault("box", box)
    ev.setdefault("action", "heartbeat")
    ev["id"] = f"{box}:heartbeat.json:{ts}"
    return ev, ts


def post_batch(apiary_url, token, events, timeout=5):
    """POST a batch. Returns True on 2xx, False on any failure (buffer stays)."""
    if not events:
        return True
    url = apiary_url.rstrip("/") + "/v1/events"
    data = json.dumps(events).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Apiary-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # 413 (oversize batch) is a permanent reject — do NOT wedge on it; the
        # per-line 16KB skip above already keeps single poison out, so a 413 here
        # means the batch itself was too big. Caller shrinks and retries.
        print(f"hum: apiary returned HTTP {e.code} — batch not delivered", file=sys.stderr)
        return False
    except (urllib.error.URLError, OSError) as e:
        print(f"hum: apiary unreachable ({e}) — buffering, cursor not advanced",
              file=sys.stderr)
        return False


def run_once(cfg, state, log=print):
    """One tail+ship pass. Collects post-cursor events from every source, POSTs
    them, and only THEN persists each source's cursor (at-least-once). Returns
    the number of events delivered (0 if nothing new or delivery failed).

    `state` is a mutable dict carrying `heartbeat_ts` across passes.
    """
    project_dir = cfg["project_dir"]
    box = cfg["box"]
    pending = []            # (source_key, kind, new_value, events)

    for jname in cfg["journals"]:
        path = os.path.join(_state_dir(project_dir), jname)
        cursor = read_cursor(project_dir, jname)
        events, new_cursor = tail_journal(path, cursor, box, jname, log=log)
        if new_cursor != cursor or events:
            pending.append((jname, "cursor", new_cursor, events))

    if cfg.get("heartbeat"):
        hb_path = os.path.join(_state_dir(project_dir), "heartbeat.json")
        ev, new_ts = snapshot_heartbeat(hb_path, box, state.get("heartbeat_ts"), log=log)
        if ev is not None:
            pending.append(("heartbeat.json", "hb_ts", new_ts, [ev]))

    all_events = [e for (_, _, _, evs) in pending for e in evs]

    if all_events:
        # Ship in id-stable batches; cursors advance only after the POST succeeds.
        for i in range(0, len(all_events), MAX_BATCH_EVENTS):
            if not post_batch(cfg["apiary_url"], cfg["token"], all_events[i:i + MAX_BATCH_EVENTS]):
                return 0  # delivery failed — advance NOTHING, buffer stays intact

    # Delivery succeeded (or there was only cursor movement past poison) — persist.
    for key, kind, value, _ in pending:
        if kind == "cursor":
            write_cursor(project_dir, key, value)
        elif kind == "hb_ts":
            state["heartbeat_ts"] = value
    return len(all_events)


def run_loop(cfg, log=print):
    state = {"heartbeat_ts": None}
    backoff = POLL_SECONDS
    while True:
        delivered = run_once(cfg, state, log=log)
        # Simple bounded backoff: on a failed/empty pass, ease off up to the cap;
        # on a productive pass, snap back to the base cadence.
        if delivered:
            backoff = POLL_SECONDS
        else:
            backoff = min(backoff * 1.5, BACKOFF_MAX_SECONDS) if backoff else POLL_SECONDS
        time.sleep(POLL_SECONDS if delivered else min(backoff, BACKOFF_MAX_SECONDS))


def build_config(args):
    return {
        "box": args.box,
        "project_dir": os.path.abspath(args.project_dir),
        "apiary_url": args.apiary_url,
        "token": args.token,
        "journals": list(DEFAULT_JOURNALS),
        "heartbeat": not args.no_heartbeat,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--box", default=os.environ.get("LOOP_BOX") or _default_box(),
                   help="this box's name (default: LOOP_BOX or hostname)")
    p.add_argument("--project-dir", default=os.environ.get("LOOP_PROJECT_DIR", "."))
    p.add_argument("--apiary-url", default=os.environ.get("HUM_APIARY_URL", "http://127.0.0.1:8787"))
    p.add_argument("--token", default=os.environ.get("APIARY_TOKEN", "dev-token"))
    p.add_argument("--no-heartbeat", action="store_true", help="do not ship heartbeat.json")
    p.add_argument("--once", action="store_true", help="one pass then exit (for tests/cron)")
    args = p.parse_args(argv)

    cfg = build_config(args)
    print(f"hum: box={cfg['box']} project={cfg['project_dir']} → {cfg['apiary_url']}",
          file=sys.stderr, flush=True)
    if args.once:
        run_once(cfg, {"heartbeat_ts": None})
        return 0
    try:
        run_loop(cfg)
    except KeyboardInterrupt:
        pass
    return 0


def _default_box():
    import socket
    return socket.gethostname().split(".")[0]


if __name__ == "__main__":
    sys.exit(main())
