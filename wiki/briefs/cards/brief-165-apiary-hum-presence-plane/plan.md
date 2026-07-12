# plan.md — brief-165 presence plane (BRICK 1)

Build order (schema → apiary → hum → hive input → vendored hooks), each a logical
commit on `brief-165-apiary-hum`. Everything is verified locally; **no Railway
deploy** (HARD GATE — Mattie only, later, on-card).

## 1. Schema (additive, byte-compatible)
- Rust `RawIntentLine` + `LogEvent` (`crates/hive/src/state.rs`): add optional
  `box`, `lane`, `brief`, `received_at`, `id`. `box` is a Rust keyword → serde
  rename. Old lines (fields absent) still parse.
- Python `append_event` (`lib/state.py`): env-fallback stamping for `lane`
  (`LOOP_LANE`) and `box` (`LOOP_BOX`) — absent env = byte-identical output.
- `docs/architecture/presence-plane.md` — the law, codified.

## 2. apiary (`apiary/apiary.py`) — deliberately dumb, stdlib only
- `POST /v1/events` — shared-token auth; coordination-verb guard (reject
  claim/gate/merge-decide → drop+log, never store); size caps (16 KB event,
  1 MB/500 batch → 413); stamp `received_at`; idempotent insert on `id`
  (unique index). SQLite ring buffer (100k events / 7 days trim).
- `GET /v1/events?since=<rowid>` — events after cursor, each carries `cursor`.

## 3. hum (`hum/hum.py`) — per-box shipper sidecar, stdlib only
- Tails journals with per-file byte cursors under `.loop/state/hum-cursors/`.
- `id = box:journal-basename:byte-offset`. At-least-once: POST batch, THEN
  persist cursor (crash between = harmless replay, dedup covers it).
- Poison event (>16 KB line) → skip loudly, never wedge the tail.
- Bounded backoff when apiary unreachable; local file is the buffer.
- `hum/com.scaviefae.hum.plist` launchd template.

## 4. hive apiary input — `HIVE_APIARY_URL`
- When set, `GET /v1/events` (curl, zero new deps) → feed same `LogEvent` merge.
- Collapse duplicates on `id` (braces to apiary's optional belt).
- Remote rows order/stale on `received_at`; DEAD-not-green (no silence=busy).
- Absent env = feature off, zero events, zero errors.

## 5. Vendor intent hooks + install
- `scripts/intent-journal.py`, `intent-hook-record.py`, `intent-hook-inject.py`.
- `.claude/settings.json` (harness project settings: the two hook blocks).
- `install.sh` carries `scripts/*.py` + the hum plist template.
- `.gitignore` for cursors + journals.

## 6. Local two-box verification (the money artifact)
- `lib/tests/test_two_box_presence.py` + a transcript script: one local apiary,
  two temp project dirs, two hum instances, one hive apiary pull. Assert both
  boxes render, kill-apiary no-op, dedup-across-crash exactly-once, offline
  catch-up.

Tests: `cd lib && python3 -m pytest` (332 baseline), `cargo test -p hive` (212
baseline). `bash -n` on shell. daemon.sh tick/failure logic untouched.
