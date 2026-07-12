# The presence plane

The harness has three transports. Two existed before BRICK 1:

- **git** — durable/shared planes: work (cards, code, merges) and coordination
  (atomic claim refs, `brief-151`). Contended, consistent, authoritative.
- **local disk** — live state on one box: heartbeats, worker spawns, intents,
  escalations. Fast, ephemeral, single-machine.

BRICK 1 adds the third:

- **the presence plane** — a *loss-tolerant bus* (`hum` shipper → `apiary`
  service → hive) that carries **presence only** across boxes, so a remote
  queen's workers render on the local dance floor.

## The law (this is a hard guard, not a preference)

> **The presence plane never carries coordination.** Not claims, not dispatch
> decisions, not gates. Git decides who holds what; the apiary only *watches*.
> A worker's write to the intent log is a **hum**; the row it becomes on the
> floor is a **hum**. The bus is loss-tolerant by contract — total loss of
> apiary state is acceptable, because the durable truth is git + local journals.
>
> Any design pressure to route a claim, a dispatch **decision**, or a gate
> decision *through* the apiary is a **stop-and-escalate signal**, not an
> extension. And the reverse holds on the read side: **no code path may READ the
> apiary to make a dispatch, merge, claim, or gate decision** — the apiary is
> written by the work and read only by eyes (hive, alerts, humans).

This line is the whole reason a third transport is safe to add. If a feature
seems to need presence to carry a coordination decision, it doesn't — stop and
escalate.

## How the law is enforced in code

The apiary's `POST /v1/events` handler runs a **coordination-verb guard**
(`apiary/apiary.py`, `COORDINATION_VERBS`). An event whose `action` is a
coordination *command* verb is rejected loud (HTTP 422), dropped, and logged —
never stored, never rendered. Loss-tolerant means the bus **may drop** a
presence event; it must **never silently accept** a coordination write.

### What counts as a coordination command

Reserved, rejected: **`claim`, `gate`, `merge-decide`** (and the underscore
spelling `merge_decide`). These are pure decisions — they have no presence
meaning; their only reason to appear on the bus would be to route a coordination
decision, which is exactly what the law forbids.

**Deliberately allowed** (they are the live hums, not decisions): the intent
hook's `dispatch` action ("a peer director started parallel work" — an
*observation* of an already-taken action, `scripts/intent-hook-record.py`) and
the runtime `merged`/`dispatched`/`completed`/`approved` events (past-tense
facts on the `event` field, not `action`). The law itself says "the row a
dispatch becomes on the floor is a hum" — rendering that a remote worker
dispatched brief-X is the *point* of the presence plane, so those flow. The
guard rejects the *decision to dispatch*, never the *observation that a dispatch
happened*. (Reconciliation note for the reviewer: piece 1 of the card lists
`dispatch` among the verbs; including the bare word verbatim would drop every
real dispatch-presence row and break the two-box render criterion, so the guard
keys on the coordination-command verbs and excludes the observational forms.
This matches the card's own "reconcile, don't invent" posture and the law's
"the row it becomes is a hum.")

## Event identity and delivery

Every event carries a stable `id = box:journal-basename:byte-offset`, computed by
`hum` as it tails (it holds all three for free). Delivery is **at-least-once**:
`hum` POSTs a batch, *then* persists its cursor, so a crash between the two
re-sends the batch on restart. That is correct and expected — identity makes the
redelivery harmless:

- the apiary **may** dedup on a unique index over `id` (belt), and
- hive **must** collapse duplicates on `id` regardless (braces).

Either alone suffices; both is deliberate.

## Ordering and staleness

Cross-box interleaving on the floor orders by the apiary's server-stamped
`received_at` (skew-immune). Within one box, ordering uses that box's local `ts`.
Remote-row liveness keys on `received_at`: **silence past cadence = DEAD (coral),
missing = DEAD, never green.** Hive's local "silence = busy" inversion is
**not** ported across boxes — on another machine, server-clock arrival is the
only trustworthy liveness signal. Box-local `ts` skew is a known v0 limitation,
display-only.
