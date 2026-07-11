# Comment plan — brief-154 first triage run (2026-07-11)

The "tracked as brief-NNN" comment to post on each open issue, one per covered
issue. **Held for the gated posting step (brief-154 piece 5).** Nothing here has
been posted — the triage run performed zero `gh` writes. These post only after a
human reviews the draft cards + this plan and approves; that step is the only one
requiring `gh` write auth and never runs unattended.

## How to run the gated step (human-approved only)

For each row below, once the corresponding draft card is flipped `draft → queued`
by a human:

```
gh issue comment <ISSUE> --repo ScavieFae/simple-loop --body "<COMMENT>"
```

Do **not** post for a card still in `Status: draft` — the back-link should point at
a card a human has accepted into the queue.

## Plan (32 issues → 10 cards)

| Issue | Card | Comment to post |
|-------|------|-----------------|
| #2  | brief-155 | Tracked as brief-155-dirty-tree-daemon-state (day-one holistic fix for the dirty-working-tree mechanism). |
| #25 | brief-155 | Tracked as brief-155-dirty-tree-daemon-state (dirty-working-tree mechanism). |
| #33 | brief-155 | Tracked as brief-155-dirty-tree-daemon-state (dirty-working-tree mechanism). |
| #46 | brief-155 | Tracked as brief-155-dirty-tree-daemon-state (dirty-working-tree mechanism). |
| #54 | brief-155 | Tracked as brief-155-dirty-tree-daemon-state (dirty-working-tree mechanism). |
| #16 | brief-156 | Tracked as brief-156-gate-audit-model (gate/audit model). |
| #26 | brief-156 | Tracked as brief-156-gate-audit-model (gate/audit model — the self-merge bypass). |
| #48 | brief-156 | Tracked as brief-156-gate-audit-model (gate/audit model). |
| #52 | brief-156 | Tracked as brief-156-gate-audit-model (gate/audit model). |
| #44 | brief-157 | Tracked as brief-157-unbounded-llm-subprocesses (budget/backoff/fill controller). |
| #47 | brief-157 | Tracked as brief-157-unbounded-llm-subprocesses (budget/backoff/fill controller). |
| #49 | brief-157 | Tracked as brief-157-unbounded-llm-subprocesses (budget/backoff/fill controller). |
| #51 | brief-157 | Tracked as brief-157-unbounded-llm-subprocesses (budget/backoff/fill controller). |
| #30 | brief-158 | Tracked as brief-158-lane-id-parsing (lane/ID reconciliation). |
| #50 | brief-158 | Tracked as brief-158-lane-id-parsing (lane/ID reconciliation). |
| #31 | brief-159 | Tracked as brief-159-runtime-observability (status/sweep signal source-of-truth). |
| #38 | brief-159 | Tracked as brief-159-runtime-observability (status/sweep signal source-of-truth). |
| #53 | brief-159 | Tracked as brief-159-runtime-observability (status/sweep signal source-of-truth). |
| #15 | brief-160 | Tracked as brief-160-blocked-brief-lifecycle (parked/blocked brief lifecycle). |
| #27 | brief-160 | Tracked as brief-160-blocked-brief-lifecycle (parked/blocked brief lifecycle). |
| #39 | brief-160 | Tracked as brief-160-blocked-brief-lifecycle (parked/blocked brief lifecycle). |
| #58 | brief-160 | Tracked as brief-160-blocked-brief-lifecycle (parked/blocked brief lifecycle). |
| #59 | brief-160 | Tracked as brief-160-blocked-brief-lifecycle (multi-commit progress.json auto-resolve, follow-up to #55). |
| #35 | brief-161 | Tracked as brief-161-cross-repo-delivery (target-repo delivery path). |
| #36 | brief-161 | Tracked as brief-161-cross-repo-delivery (target-repo delivery path). |
| #20 | brief-162 | Tracked as brief-162-harness-update-propagation (`loop update` propagation). |
| #57 | brief-162 | Tracked as brief-162-harness-update-propagation (`loop update` propagation). |
| #21 | brief-163 | Tracked as brief-163-input-validation-robustness (validate-at-parse boundary). |
| #23 | brief-163 | Tracked as brief-163-input-validation-robustness (validate-at-parse boundary). |
| #1  | brief-164 | Tracked as brief-164-roadmap-misc (roadmap holding pen — likely to be split at review). |
| #3  | brief-164 | Tracked as brief-164-roadmap-misc (roadmap holding pen — likely to be split at review). |
| #4  | brief-164 | Tracked as brief-164-roadmap-misc (roadmap holding pen — likely to be split at review). |

## Not posted here

Closed issues never get a "tracked as" comment from this run — they are named in
their cluster card's prose as mechanism history, not carried in `Issues:`
frontmatter. Closed members referenced in prose: #5 #28 #29 (dirty-tree), #32
(subprocesses), #41 (observability), #55 (parked lifecycle — parent of #59).
