# Goals

simple-loop's own loop, bootstrapped 2026-05-05 per brief-143's ops execution. Briefs about simple-loop's source land here, not in portal. See portal's `wiki/operating-docs/cross-repo-loops.md` for the convention.

## Active program — finish brick 0 + close the gate bypass (2026-06-28)

The May hackathon-hardening queue (briefs 142–151) is **complete**: all merged, rejected, deferred, or abandoned — see portal `wiki/programs/harness-improvements/self-dev-backlog-disposition.md`. brief-151 (lane lock + atomic claim) landed the remote-queens keystone. Two follow-ups close the gaps that grounding the brick-1 launch surfaced:

1. **Finish brick 0's lane wiring** — brief-152: `--lane` reaches the queen's brief *selection*, not just dedup. Without it a lane-scoped daemon still dispatches any queue-head brief, so brick 1 (the lane-pinned Titania queen) can't land. Blocker for remote-queens brick 1. (portal#52.)
2. **Close the re-queue gate bypass** — brief-153: a re-queued `Auto-merge: false` / `Human-gate: review` brief merged with `approved_by: None`. The merge/approval girder failing — high severity. (portal#50.)

## Queued next

_Empty — the next queue is the Draft section below, pending Mattie's review-and-flip._

## Draft — awaiting human review

Emitted by brief-154's first triage run (2026-07-11) from the 32 issues open at run
time. **Not dispatchable** — each is `Status: draft`. A human reviews the cluster,
flips `draft → queued` (moving the entry up into `## Queued next`), and approves the
comment posting per `brief-154-issue-intake-triage/comment-plan.md`. Coverage is
exact: the union of these cards' `Issues:` equals the open-issue set, each issue in
one card (proof in `brief-154-issue-intake-triage/closeout.md`).

1. **brief-155 (daemon state out of the git working tree — day-one holistic fix)** — the dirty-tree mechanism (#2 #25 #33 #46 #54) carried whole; #2 is the isolation fix that supersedes the five piecemeal patches. `wiki/briefs/cards/brief-155-dirty-tree-daemon-state/index.md`.
2. **brief-156 (the gate/audit model — accountable, witnessed approvals)** — #16 #26 #48 #52; the self-merge bypass and waive-loop are facets of a gate model with no actor and no satisfied predicate. `wiki/briefs/cards/brief-156-gate-audit-model/index.md`.
3. **brief-157 (unbounded LLM subprocesses — budget/backoff/fill controller)** — #44 #47 #49 #51; generalizes the 2026-07-11 queen circuit breaker to worker/validator/budget. `wiki/briefs/cards/brief-157-unbounded-llm-subprocesses/index.md`.
4. **brief-158 (lane IDs — unlaned dispatch + brief-NNN-only regex)** — #30 #50; reconcile the lane/ID model. `wiki/briefs/cards/brief-158-lane-id-parsing/index.md`.
5. **brief-159 (runtime observability — status/sweep signals that cry wolf)** — #31 #38 #53; anchor observables to the running daemon, not local-clone/argv/dispatch-age proxies. `wiki/briefs/cards/brief-159-runtime-observability/index.md`.
6. **brief-160 (the blocked/parked brief lifecycle)** — #15 #27 #39 #58 #59; give "parked" a first-class state, surface, and unblock path. `wiki/briefs/cards/brief-160-blocked-brief-lifecycle/index.md`.
7. **brief-161 (cross-repo delivery — target-repo briefs strand their deliverable)** — #35 #36; build artifact resolution + PR hop for target-repo briefs. `wiki/briefs/cards/brief-161-cross-repo-delivery/index.md`.
9. **brief-163 (input parsing/validation robustness)** — #21 #23; validate-at-parse on LLM/free-form field boundaries. `wiki/briefs/cards/brief-163-input-validation-robustness/index.md`.
10. **brief-164 (roadmap/misc — field report + capability stubs)** — #1 #3 #4; holding pen with no shared bug mechanism, recommend fan-out at review. `wiki/briefs/cards/brief-164-roadmap-misc/index.md`.

## Disposition — prior queue (2026-07-11)

Resolved entries; kept for history:

- **brief-162 (harness-update propagation)** — **merged** (merge `6748c17`, 2026-07-11; card `Status: merged`; closes #20 #57). `loop update` is the propagation edge; first dogfood run propagated its own merge and surfaced #60 (installed-templates ghost files). Canonical at `wiki/briefs/cards/brief-162-harness-update-propagation/index.md`.

- **brief-154 (issue-intake triage skill)** — **merged** (merge `e630e9b`, 2026-07-11; card `Status: merged`). Director-iteration cycle: opus coder → opus reviewer (APPROVE, 8/8 criteria, live coverage diff exit 0) → merge. Its first run emitted the ten Draft cards above. Comment-plan posting still gated on Mattie. Canonical at `wiki/briefs/cards/brief-154-issue-intake-triage/index.md`.

- **brief-152 (queen lane wiring — finish brick 0)** — **merged** (merge `744eb06`, 2026-06-29; card `Status: merged`). `--lane` now reaches the queen's brief selection; single-daemon path byte-for-byte unchanged. Canonical at `wiki/briefs/cards/brief-152-queen-lane-wiring/index.md`.
- **brief-153 (re-queued human-gate brief must re-hold)** — **not-doing** (superseded). The re-queue gate concern folds into the gate/audit-model cluster (#16 #26 #48 #52) that brief-154's triage will card holistically rather than as a one-off. Canonical at `wiki/briefs/cards/brief-153-requeue-gate-hold/index.md`.
