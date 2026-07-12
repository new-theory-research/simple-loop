---
ID: brief-165-apiary-hum-presence-plane
Branch: brief-165-apiary-hum-presence-plane
Status: merged
Model: opus
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: new-theory-research/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: []
Edit-surface:
  - crates/hive/src/state.rs (apiary event source + schema fields)
  - lib/actions.py (append_event / runtime_event schema: box, lane, brief)
  - lib/daemon.sh (box tag on emitted events)
  - hum/ (new — per-box shipper sidecar + launchd plist template)
  - apiary/ (new — the cloud presence service, v0)
  - scripts/intent-journal.py, scripts/intent-hook-record.py, scripts/intent-hook-inject.py (vendored from portal)
  - .claude/settings.json (vendored intent hooks)
  - install.sh (carry vendored hooks + hum plist template)
Depends-on: none
Tags: [harness, remote-queens, presence-plane, apiary, hum, hive, observability, brick-1, portal-51]
---

# Brief: the presence plane — `hum` shipper + `apiary` bus, so a remote queen dances on the local floor

!!! abstract "Intent"
    The harness has two transports: **git** for the durable/shared planes (work —
    cards, code, merges; coordination — atomic claim refs, `brief-151`), and
    **local disk** for live state (presence — heartbeats, worker spawns, intents,
    escalations). The remote queen (daemon on `lady-titania`) is the first
    component that is **live AND not local**. Presence has no cross-box transport,
    so hive's dance floor is blind to remote workers — a queen on another machine
    is invisible (`crates/hive/src/main.rs:1843`, `current_dir` cwd-roots hive to one
    checkout). **Git must not become that transport** — the contention class was just
    eliminated in Wave-1b, and re-smearing presence across refs/working-tree/local-cache
    re-opens exactly the triad `~/new-theory/portal/wiki/specs/harness-coordination.md`
    §0 receipts (the spec lives in portal, not this repo). Build a third
    transport for presence only: a **loss-tolerant bus**. This is BRICK 1 —
    schema + `hum` + `apiary` v0 + hive apiary input + vendored intent hooks.

## Plain version

Writers do not change. Queens, workers, and validators keep appending single JSONL
lines to local files — `.loop/state/runtime-events.jsonl` (via `runtime_event` →
`append_event`, `lib/actions.py:294`), `.loop/state/intent-journal.jsonl` (the
vendored hook, below), `heartbeat.json` (`write_heartbeat`, `lib/daemon.sh:1614`) —
exactly as today. **The local file is the buffer and the offline fallback.** A
new per-box sidecar, `hum`, tails those journals with a per-file cursor and POSTs
batches to a dumb cloud bus, `apiary`. Hive gains one more event source
(`HIVE_APIARY_URL`) that feeds the **same merge machinery** it already uses for
multiple local journals (`HIVE_INTENT_JOURNALS`, `state.rs:2272`) — so a remote
box's workers render on the local floor, interleaved by timestamp, next to local
rows.

The whole plane is **fire-and-forget**. Box offline → catches up on reconnect from
its cursor. `apiary` down → work is completely unaffected; you lose the *view*,
never the *work*. The durable record is unchanged: git artifacts + local journals.

**This serves portal#51 (hive cross-box queen visibility) — and supersedes its
mechanism.** #51's drafted design proposed committing + pushing
`.loop/state/queens/<host>-<lane>.json` each tick — i.e. **git as the presence
transport**, the exact move Wave-1b closed. Serve the goal (#51: "a queen on
another machine is invisible to hive"), replace the mechanism (presence rides the
bus, not refs). Note this reconciliation on the issue when the comment step runs;
do not re-open the git-transport option.

## Architectural law (write this as a hard guard, not a preference)

> **The presence plane never carries coordination.** Not claims, not dispatch, not
> gates. Git decides who holds what; the apiary only *watches*. A worker's write
> to the intent log is a **hum**; the row it becomes on the floor is a **hum**.
> The bus is loss-tolerant by contract — total loss of apiary state is acceptable,
> because the durable truth is git + local journals. Any design pressure to route a
> claim, a dispatch, or a gate decision through the apiary is a **stop-and-escalate
> signal**, not an extension. And the reverse holds on the read side: **no code path
> may READ the apiary to make a dispatch, merge, claim, or gate decision — the apiary
> is written by the work and read only by eyes (hive, alerts, humans).** This line is
> the whole reason a third transport is safe to add.

## The build (six pieces)

1. **The presence-plane contract — codified, not just asserted.** Ship the law
   above as a doc (`docs/architecture/presence-plane.md` or the harness-coordination
   spec's §-of-record) *and* as a guard in code: the apiary's POST handler rejects
   any event whose `action` is a coordination verb (claim/dispatch/gate/merge-decide)
   — fail loud, drop the event, log it. Loss-tolerant means the bus may drop
   presence; it must **never silently accept** a coordination write.

2. **`hum` — the per-box shipper sidecar (~30–100 lines + a launchd plist
   template).** A tiny long-running process (one per box) that tails the local
   journals with a **per-file byte/line cursor** (persisted under
   `.loop/state/hum-cursors/`, gitignored, same disposable-cursor shape as
   `intent-cursors/`) and POSTs post-cursor lines in batches to the apiary. Writers
   are untouched — hum is a *reader*. On reconnect it resumes from the cursor
   (offline catch-up is free). If the apiary is unreachable, hum retries with
   bounded backoff and the local file keeps growing as the buffer. Ships with a
   `launchd` plist template so a box brings its own shipper up on boot;
   `install.sh` carries the template (see piece 6).
   - **Event identity — a stable `id` = `(box, journal-file, byte-offset)`,**
     computed for free by hum as it tails (it already holds all three). The
     delivery contract is explicitly **at-least-once**: hum POSTs a batch, then
     persists its cursor — so a crash between POST and cursor-persist re-sends the
     batch on restart. That is *correct and expected*; identity is what makes the
     redelivery harmless. Every event carries its `id` end to end.

3. **`apiary` — one deliberately-dumb cloud service (v0).** Deployed on Railway
   (account exists; `use-railway` skill available — but see the **hard deploy gate**
   below; do not deploy without sign-off). Two endpoints, one shared per-box token:
     - `POST /v1/events` — append a batch. Validated against piece 1's coordination
       guard, then **stamped `received_at`** (server clock, on ingest) and written
       to storage. The apiary **MAY** be idempotent on insert — a unique index on
       the event `id` (piece 2) that drops a duplicate re-send. That is still dumb
       storage, not coordination: it dedups a byte-identical replay, it never
       decides anything. (Belt to hive's braces below — either side alone suffices,
       both is deliberate.)
     - `GET /v1/events?since=<cursor>` — poll (or SSE) for events after a cursor.
   - **Storage: a bounded ring buffer** (SQLite or a capped file) — total loss is
     acceptable by contract. **v0 bound: 100,000 events or 7 days, whichever trims
     first.** **No queries, no aggregation, no writes back into any repo, no
     coordination.** The service is boring on purpose; every feature it lacks is a
     feature. Keep it small enough to reason about in one read.
   - **Size bounds (loss-tolerant, never blocking).** Max event **16 KB**, max batch
     **1 MB or 500 events**. Oversize → **HTTP 413**; hum logs loudly and **skips**
     the offending event (the contract covers the loss). The tail loop must **never
     block on a poison event** — one 17 KB line can't wedge the shipper.

4. **hive apiary input — `HIVE_APIARY_URL`, feeding the existing merge.** Hive
   already merges N local journals: `intent_journal_paths()` (`state.rs:2323`) unions
   `DEFAULT_INTENT_JOURNAL` (`state.rs:2266`) with the colon list in
   `HIVE_INTENT_JOURNALS` (`INTENT_JOURNALS_ENV`, `state.rs:2272`), and
   `load_intent_journal_events` (`state.rs:2345`) parses each into `LogEvent`s
   interleaved by timestamp. Add an **apiary source**: a new `HIVE_APIARY_URL` env
   that, when set, pulls `GET /v1/events?since=<cursor>` and feeds those events into
   the **same** `LogEvent` merge — remote boxes' rows land on the local floor with
   zero new render path. Absent env = feature off, zero events, zero errors (mirror
   the existing "absent journal = feature off" posture, `state.rs:2348`).
   - **Hive MUST collapse duplicates on `id`,** regardless of whether the apiary
     dedups. At-least-once delivery means a replayed event can arrive twice; hive
     keys on the event `id` (piece 2) and renders each once. This is the braces to
     the apiary's optional belt — hive dedups even against a naive apiary that
     stored both copies.
   - **Ordering & staleness key on `received_at`, not box-local `ts`.**
     Cross-box interleaving on the dance floor orders by the apiary's server-stamped
     `received_at` (skew-immune); within a single box, ordering uses that box's local
     `ts`. The remote-staleness rule from portal#51 keys on `received_at` too: for a
     remote row the server-clock arrival is the signal, so **silence past cadence =
     DEAD (coral), missing = DEAD, never a green row.** Do **not** port hive's local
     "silence = busy" inversion (`parse_last_event_ts` busy signal, `state.rs:332-334`;
     busy-cycling override, `state.rs:503-513`) across boxes. Box-local `ts` clock
     skew is a **known v0 limitation, display-only** — a remote row's shown timestamp
     may drift from wall-clock, but ordering and liveness never rely on it.

5. **Event schema — `{ts, session, action, detail}` gains `{box, lane, brief}`.**
   The live line today is `{ts, session, action, detail}` (`RawIntentLine`,
   `state.rs:2278-2283`; and `append_event` in `lib/actions.py`). The ratified
   `harness-coordination.md` §4 (spec, ratified 2026-07-05) specified and **deferred**
   the richer shape — line 154 names `{ts, director_id, lane, action, refs}`.
   Reconcile, don't invent:
     - `session` **is** the spec's `director_id` (keep the field name `session` —
       it's what's live and what `short_session_tag` keys on, `state.rs:2289`).
     - `lane` — new, matches the spec's `lane` exactly.
     - `brief` — new, the concrete form of the spec's `refs` (the brief a hum is
       about).
     - `box` — **genuinely new**, the field the single-box spec never needed. It
       names the machine (`lady-titania`, the local host, …) and is what lets the
       floor say *which* box a remote worker is on.
   Make the three new fields **optional** everywhere (`RawIntentLine`'s `Option<…>`
   pattern; `append_event`'s `**fields`) so old lines and local single-box runs are
   byte-compatible and the feature is purely additive.

6. **Vendor the intent hooks into the harness (the deferred Wave-2
   intent-log-first-class item).** The intent journal is currently **portal-owned**:
   `~/new-theory/portal/scripts/intent-journal.py` (the append/read-fresh engine),
   plus the two hook shims `scripts/intent-hook-record.py` (PostToolUse, matcher
   `Bash|Task|Agent`) and `scripts/intent-hook-inject.py` (UserPromptSubmit), wired
   in portal's `.claude/settings.json:16-40`. The harness cannot own its own
   presence if the *writer* of intents lives in another repo. Vendor the three
   scripts into `scripts/` here and the two hook entries into the harness's project
   settings, and have `install.sh` carry them (the `core/skills/*/` copy loop at
   `install.sh:170-176` is the model — extend the install to place the hooks +
   the `hum` plist template; verify the exact install path, don't guess). This is
   the deferred **Wave-2 "intent-log first-class in the harness"** item, in scope
   here because BRICK 1's presence plane is meaningless if the harness can't emit
   the hums it ships.

## HARD GATE — deploy is always-Mattie

Deploying the apiary is **infra spend + a new always-on service** = the
**always-Mattie** class under portal standing-delegation. The worker **builds
everything and verifies locally**, then deploys to a Railway environment **only
after Mattie's explicit deploy approval, recorded on this card**. There is no
"deploy to test it" — the pre-approval verification path is fully local:

- Run the apiary locally (SQLite/file ring buffer, localhost).
- Simulate **two boxes**: two clones each running a `hum` against its own journals,
  both POSTing to the local apiary.
- Point one hive at `HIVE_APIARY_URL=localhost` and confirm both boxes' events
  render on the one floor.

Everything below is provable on one laptop. Railway deploy is the *last* step and
only Mattie flips it.

**Infrastructure isolation (Mattie, 2026-07-12, hard requirement):** the apiary
deploys to its OWN Railway project with its OWN storage — never a service inside
a product project, never a shared database/Redis with NT products, no product
env vars or tokens in its environment. Blast-radius isolation is the point: the
apiary is loss-tolerant by contract, so it must be *deletable* without a product
thought; sharing infrastructure would silently revoke that property. v0's
SQLite-inside-the-service satisfies this by construction — keep it that way.

## Success criteria (concrete, testable — all local, pre-deploy)

- **Two-box render.** Local apiary + two simulated boxes (two clones, two `hum`s):
  both boxes' worker/intent/heartbeat events render on **one** hive dance floor,
  interleaved by timestamp, each row tagged with its `box`. Remote rows follow the
  DEAD-not-green staleness rule (piece 4).
- **Kill-apiary is a no-op for work.** With a queen/worker mid-run, kill the apiary
  process. Prove the daemon/worker flow is **provably unaffected** — the run
  completes, cards/merges land, local journals keep appending. Only the *view*
  degrades. (Capture the before/after: work output identical, floor loses remote
  rows.)
- **Offline box catches up, exactly once across a crash.** Start a box with the
  apiary down (or network-partitioned); it buffers to its local journal. Bring the
  apiary up; `hum` resumes from its cursor and the buffered events appear on the
  floor. The sharp test of the at-least-once contract: **kill `hum` after a POST but
  before it persists its cursor, restart it, let it replay** — the dance floor
  renders each event **exactly once** (hive collapses the redelivered batch on
  `id`). No loss, no visible dupes.
- **Schema fields present.** Emitted events carry `{ts, session, action, detail,
  box, lane, brief}`; the three new fields are optional and old/local-single-box
  lines still parse (byte-compatible). `RawIntentLine` and `append_event` both
  round-trip the new fields.
- **Coordination guard holds.** A POST whose `action` is a coordination verb is
  rejected (fail loud, dropped, logged) — proven by a test that fires one and
  asserts it never reaches storage and never reaches the floor.
- **Hooks vendored + install carries them.** The three intent scripts live under
  `scripts/` in this repo, the two hooks live in the harness project settings, and
  a fresh `./install.sh` places them (and the `hum` plist template) — verified from
  the install output, not by hand-copy.
- **`HIVE_APIARY_URL` unset = feature off.** No env → hive behaves exactly as
  today (local floor untouched, zero events from the apiary path, zero errors).

## Guards

- **Presence plane never carries coordination.** (Piece 1's law.) If a criterion
  seems to need the apiary to route a claim/dispatch/gate, **stop and escalate** —
  it doesn't. Git decides; the apiary watches.
- **Writers do not change.** Queens/workers/validators keep appending single JSONL
  lines to local files exactly as today. `hum` is a *reader* of those files. If the
  build starts editing the emit sites to POST directly, it's off-design — the local
  file must stay the buffer and the offline fallback.
- **Loss-tolerant by contract.** No durability guarantees on the apiary, no
  retries that block work, no back-pressure onto writers. A dropped presence event
  is fine; a blocked worker is not.
- **Additive only.** `HIVE_APIARY_URL` unset and the three new schema fields absent
  must both leave the local single-box path byte-for-byte unchanged.
- **No deploy without the recorded gate.** Local verification is the full
  pre-approval path; Railway deploy waits for Mattie's explicit on-card approval.
- **No `conductor` naming.**

## Out of scope / next cards

Named here so they're tracked, **not** scoped into BRICK 1:

- **escalation-reach** — remote escalations pushing to a human (webhook /
  notification out of the apiary or hum). BRICK 1 renders remote presence on the
  floor; it does **not** route a remote escalation to a person. Future card.
- **daemon push-retry hardening** — bounded pull-rebase-push retry on the daemon's
  plumbing pushes. Adjacent to the git-transport surface but independent of the
  presence plane. Future card.

## Outputs

- `plan.md` — the worker's build order (schema → hum → apiary → hive input →
  vendored hooks), with the local two-box harness described before any Railway
  touch.
- `closeout.md` — what shipped, the six pieces, the local two-box verification
  receipts (render, kill-apiary no-op, offline catch-up), and an explicit note on
  the deploy gate's state (deployed only if Mattie approved on-card; else built +
  locally-verified, deploy pending).
- `review.md` — gate runbook (Human-gate: review): the coordination-guard proof,
  the additive-off proofs (`HIVE_APIARY_URL` unset, new fields absent), and the
  portal#51 mechanism-supersession note held for the issue comment.

## Follow-up (2026-07-12, from the rq-001 smoke): #85
hum cannot ship overwrite-style files — heartbeat.json never flows, so a live
remote box reads dead between events (presence contract violated at its most
basic). Also first-run cursors must init at EOF (backlog flood). Both surfaced
during Titania's spin-up; carried here as the brick-2 head items.
