# review.md — brief-165 gate runbook (Human-gate: review)

Everything is reproducible on one laptop. **No deploy** — the apiary runs as a
localhost process. Run from the branch `brief-165-apiary-hum`.

## 1. Full suites green

```
cd lib && python3 -m pytest -q            # → 360 passed
cd crates/hive && cargo test -p hive      # → 220 passed
bash -n install.sh                        # → clean
git diff master --name-only | grep daemon.sh   # → empty (daemon.sh untouched)
```

## 2. Coordination-guard proof

```
cd lib && python3 -m pytest tests/test_apiary.py -k coordination -v
```
`test_coordination_verb_rejected_and_not_stored` fires `claim`/`gate`/
`merge-decide`, asserts each raises `BatchError(422)`, asserts
`fetch_since(conn, 0) == []` (never stored), and asserts the "REJECTED
coordination" log line. `test_coordination_422_over_http` proves it over real
HTTP. The reconciliation on the excluded `dispatch`/`merged` observational forms
is documented in `docs/architecture/presence-plane.md` and closeout.md — a
one-line `COORDINATION_VERBS` change if the reviewer wants the literal card list.

## 3. Additive-off proofs

**`HIVE_APIARY_URL` unset = feature off** (`crates/hive/src/state.rs`):
```
cargo test -p hive apiary_source_off_when_env_unset   # load_apiary_events() == []
```
Hive's merge, sort, and dedup all no-op on an empty apiary source; the local
floor is byte-identical to pre-165.

**New schema fields absent = byte-compatible** (`lib/tests/test_presence_schema.py`):
```
python3 -m pytest tests/test_presence_schema.py -v
```
`test_no_env_no_kwargs_is_byte_compatible` asserts the emitted line's keys are
exactly `{ts, event, brief}` — no `box`/`lane` when neither env nor kwarg is set.
Rust `intent_journal_old_line_parses_without_presence_fields` asserts a pre-165
`{ts,session,action,detail}` line still parses with the new fields `None`.

## 4. Two-box render — the money artifact (live transcript)

The transcript below is a real run: one local apiary, two hum instances, hive's
exact `GET /v1/events?since=0` call. Both boxes' runtime/intent/heartbeat events
land on one floor, each tagged with its `box`; the guard rejects `claim` 422.

```
1) apiary v0 listening on http://127.0.0.1:8791 (SQLite in-service, localhost)
2) box A (lady-titania): dispatched brief-165 + intent + heartbeat
   box B (scaviefae):    merged brief-201 + intent
3) hum --once per box → both ship
4) GET /v1/events?since=0  (hive's exact call):
   [ lady-titania  dispatched  brief-165    id=lady-titania:runtime-events.jsonl:0 ]
   [ lady-titania  dispatch    (intent)     id=lady-titania:intent-journal.jsonl:0 ]
   [ lady-titania  heartbeat   phase3_worker id=lady-titania:heartbeat.json:<ts>   ]
   [ scaviefae     merged      brief-201    id=scaviefae:runtime-events.jsonl:0    ]
   [ scaviefae     git push    (intent)     id=scaviefae:intent-journal.jsonl:0    ]
   every row carries box + server-stamped received_at + stable id
5) coordination guard: POST {action:"claim"} → HTTP 422
   body={"error":"coordination verb rejected: 'claim'"}
```

Regenerate:
```
cd lib && python3 -m pytest tests/test_two_box_presence.py -v   # 4 criteria, all pass
```
covering: two-box render, kill-apiary no-op, offline catch-up, exactly-once
across a crash.

## 5. Install carries the hooks + plist

```
git stash -u >/dev/null 2>&1 || true      # clean tree (install refuses dirty)
TMP=$(mktemp -d); SIMPLE_LOOP_HOME=$TMP bash install.sh | grep -iE 'Intent hook|Presence:'
#   Intent hook: intent-journal.py / intent-hook-record.py / intent-hook-inject.py
#   Presence: hum shipper + com.scaviefae.hum.plist template
#   Presence: apiary bus (local-run; deploy is Mattie's gate)
ls $TMP/scripts $TMP/hum $TMP/apiary $TMP/templates/com.scaviefae.hum.plist
```

## 6. portal#51 mechanism-supersession note (hold for the issue comment)

> BRICK 1 (brief-165) serves #51 — "a queen on another machine is invisible to
> hive" — and **supersedes its mechanism**. #51's drafted design committed +
> pushed `.loop/state/queens/<host>-<lane>.json` each tick, i.e. **git as the
> presence transport**, the exact contention class Wave-1b eliminated. Presence
> now rides a loss-tolerant bus (`hum` → `apiary` → hive), never refs. Serve the
> goal, replace the mechanism. Do not re-open the git-transport option.

## Deploy gate

**Do not deploy without Mattie's explicit on-card approval.** Nothing here
touched Railway; the apiary ran only on localhost. Isolation clause (own project,
own storage, no shared NT product infra/env/tokens) carried in
`apiary/README.md`, satisfied by construction (SQLite in-service).
