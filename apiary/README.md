# apiary — the presence bus (v0)

The deliberately-dumb cloud service for the presence plane (brief-165, BRICK 1).
Stores presence events written by `hum`, hands them back to eyes (hive, alerts,
humans). It never decides anything. See `docs/architecture/presence-plane.md`.

## Run locally (the only supported path pre-deploy)

```
python3 apiary/apiary.py --db /tmp/apiary.db --port 8787 --token dev-token
```

Env equivalents: `APIARY_DB`, `APIARY_PORT`, `APIARY_HOST`, `APIARY_TOKEN`.

## API

- `POST /v1/events` — header `X-Apiary-Token: <token>`; body is a JSON array of
  event objects. Returns `{stored, deduped, skipped_poison, received_at}`.
  - Coordination-verb guard: any event whose `action` is a reserved coordination
    command (`claim`, `gate`, `merge-decide`) fails the whole POST with **422**,
    stores nothing, logs loud.
  - Size caps: body > 1 MB or > 500 events → **413**; a single event > 16 KB is
    dropped + logged server-side (never wedges the store).
  - Idempotent on `id` (unique index) — a byte-identical replay is deduped.
  - Stamps `received_at` (server clock) on ingest.
- `GET /v1/events?since=<cursor>` — header `X-Apiary-Token`. Returns
  `{events, cursor}`; each event carries its `received_at` and `cursor` (rowid).

## Storage

SQLite ring buffer, bounded to **100,000 events OR 7 days** (whichever trims
first). Total loss is acceptable by contract — the durable truth is git + local
journals.

## Deploy

**Do not deploy without Mattie's explicit on-card approval** (infra spend +
always-on service = always-Mattie). Everything above is verified locally.
