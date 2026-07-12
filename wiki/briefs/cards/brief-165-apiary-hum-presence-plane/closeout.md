# closeout.md — brief-165 presence plane (BRICK 1)

Branch: `brief-165-apiary-hum` (6 commits, `[scav]`-prefixed). **Built + verified
entirely locally. NOT deployed** — Railway deploy is Mattie's explicit on-card
gate (see "Deploy gate state" below).

## What shipped — the six pieces

1. **Presence-plane contract — doc + code guard.**
   `docs/architecture/presence-plane.md` codifies the law (bus carries presence
   only; loss-tolerant; read only by eyes). Enforced in `apiary/apiary.py`
   (`COORDINATION_VERBS`): a POST whose `action` is a coordination command
   (`claim`, `gate`, `merge-decide`/`merge_decide`) is rejected **422**, dropped,
   logged — never stored, never on the floor.
2. **`hum` shipper — `hum/hum.py` + `hum/com.scaviefae.hum.plist`.** Tails
   `runtime-events.jsonl` + `intent-journal.jsonl` by per-file byte cursor
   (`.loop/state/hum-cursors/`), snapshots `heartbeat.json` on-change. Stamps
   `box` + stable `id = box:journal:offset`. At-least-once (POST → then persist
   cursor). Poison (>16 KB / unparseable) skipped loud, never wedges the tail.
   Apiary down → cursor frozen, local file is the buffer; bounded backoff.
3. **`apiary` v0 — `apiary/apiary.py` (+ README).** stdlib http.server + sqlite3,
   zero deps. `POST /v1/events` (token auth, guard, 16 KB/1 MB/500 caps → 413,
   `received_at` stamp, idempotent insert on `id`). `GET /v1/events?since=<cursor>`.
   SQLite ring buffer bounded 100k events / 7 days.
4. **hive apiary input — `crates/hive/src/state.rs` + `main.rs`.** `HIVE_APIARY_URL`
   set → hive pulls `GET /v1/events` (via `curl`, zero new crate deps) into the
   SAME `LogEvent` merge. `dedup_on_id` collapses replays; `sort_ts` interleaves
   remote rows on `received_at`, local on `ts`; `remote_liveness` is DEAD-not-green;
   remote rows render a `[box]` tag. Absent env = feature off, zero events/errors.
5. **Event schema — additive `{box, lane, brief, received_at, id}`.** Rust
   `RawIntentLine` + `LogEvent` (Option everywhere; `box`→`box_name` serde-rename);
   Python `append_event` env-fallback stamping for `box` (LOOP_BOX) / `lane`
   (LOOP_LANE). Absent env + absent kwargs = byte-identical pre-165 line.
6. **Vendored intent hooks + install.** `scripts/intent-journal.py` +
   `intent-hook-record.py` + `intent-hook-inject.py` (verbatim from portal;
   REPO_ROOT resolves to this repo). Two hook blocks in `.claude/settings.json`.
   `install.sh` carries all three + `hum.py` + `com.scaviefae.hum.plist` +
   `apiary.py`, each announced in the install output.

## Verification receipts (all local, pre-deploy)

- **Tests green.** Python `360 passed` (was 332: +4 schema, +12 apiary, +8 hum,
  +4 two-box). Hive `cargo test -p hive` `220 passed` (was 212: +8). `bash -n`
  clean on `install.sh`. **`lib/daemon.sh` untouched** (`git diff` empty) — hum is
  a sidecar the daemon does not know about (the loss-tolerance contract).
- **Two-box render.** `lib/tests/test_two_box_presence.py::test_two_boxes_render_on_one_floor`
  + the live transcript (in review.md): one local apiary, two hum instances, both
  boxes' runtime/intent/heartbeat rows on one floor via hive's exact
  `GET /v1/events?since=0` call, each tagged with its `box`.
- **Kill-apiary is a no-op for work.** `test_kill_apiary_is_a_noop_for_work`: with
  the apiary dead, the local journal keeps appending, hum exits 0 (never crashes),
  cursor stays where the live apiary acked. Only the view degrades.
- **Offline catch-up + exactly-once-across-crash.** `test_offline_box_catches_up`
  (buffer while down → ship on reconnect) and `test_exactly_once_across_a_crash`
  (drop cursor after ship → replay re-sends identical ids → each renders once,
  dedup on `id`).
- **Coordination guard holds.** `test_coordination_verb_rejected_and_not_stored`
  + live 422 in the transcript.
- **Install carries hooks + plist.** Verified from a temp-`SIMPLE_LOOP_HOME` run's
  output (review.md).

## Reconciliation — portal#51 (held for the issue comment)

BRICK 1 **serves** #51 ("a queen on another machine is invisible to hive") and
**supersedes its mechanism**. #51's drafted design pushed
`.loop/state/queens/<host>-<lane>.json` each tick — **git as the presence
transport**, the exact move Wave-1b closed. Presence now rides the bus, not refs.
Do not re-open the git-transport option.

## Deliberate reconciliation to flag for review

The card's piece-1 verb list names `dispatch`. The guard **excludes** the bare
word because the live intent hook emits `action: "dispatch"` as a presence
*observation* ("a peer director started parallel work"), and runtime emits
`merged`/`dispatched` as past-tense facts on the `event` field — including
`dispatch` verbatim would drop every real dispatch-presence row and break the
two-box render criterion. The guard keys on coordination *command* verbs
(`claim`, `gate`, `merge-decide`); the law's own "the row a dispatch becomes on
the floor is a hum" backs this. Full rationale in
`docs/architecture/presence-plane.md`. If the reviewer wants the literal list,
it is a one-line change to `COORDINATION_VERBS`.

## Deploy gate state

**Built + locally verified; deploy PENDING.** No `railway` CLI invoked, no cloud
writes, no Railway project touched. The apiary ran only as a localhost process
for every test. Mattie's infra-isolation clause (own Railway project, own
storage, no shared NT product infra/env/tokens) is carried verbatim in
`apiary/README.md` and satisfied by construction (SQLite in-service). Deploy
awaits Mattie's explicit on-card approval.

## Deferred (named, not scoped)

- **escalation-reach** — routing a remote escalation to a person (webhook out of
  apiary/hum). BRICK 1 renders remote presence; it does not page a human.
- **daemon push-retry hardening** — independent of the presence plane.
- **lane column on the floor** — `lane` is parsed/shipped but not yet rendered
  (v0 display-only limitation, noted in code).
