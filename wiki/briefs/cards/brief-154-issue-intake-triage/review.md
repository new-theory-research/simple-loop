# Review gate — brief-154 issue-intake triage

The ask, the recommendation, and what to check before you flip anything. For the
forensic record of what shipped, see [closeout.md](./closeout.md).

## The ask

Two decisions are yours, and only yours — triage deliberately stopped short of both:

1. **Which draft cards become work.** Ten `Status: draft` cluster cards sit in
   `wiki/briefs/cards/` (brief-155 … brief-164). Flip the ones you accept
   `draft → queued` and move their line from `## Draft — awaiting human review` into
   `## Queued next` in `.loop/state/goals.md`. Leave the rest as draft.
2. **Whether to post the back-link comments.** `comment-plan.md` holds one "tracked
   as brief-NNN" comment per open issue, unposted. After you accept a card, run the
   gated posting step for its issues (the `gh issue comment` lines in the plan) —
   this is the only step that writes to the tracker.

## Recommendation

- **Flip brief-155 first.** It carries the dirty-tree mechanism whole and names #2
  as the day-one holistic fix — the highest-leverage card, and the one that
  supersedes the five piecemeal patches this repo has been shipping for six weeks.
- **Flip brief-156 (gate/audit) next** — #26 (a brief that merges itself past a
  human gate) is the sharpest live severity.
- **Hold brief-164 (misc) as a holding pen** — #1 #3 #4 share no mechanism; split
  them into real briefs (or a program) rather than queueing the card as one unit.
- The remaining cards (157–163) are sound clusters; queue them as bandwidth allows.

## What to check before flipping

- **Coverage is exact and self-verifying.** Run
  `python3 wiki/briefs/cards/brief-154-issue-intake-triage/coverage_diff.py` — exit 0
  means the union of all cards' `Issues:` equals the live open-issue set, each issue
  once. If you close/open issues before flipping, re-run it.
- **No tracker writes happened.** Confirm the tracker has no new "tracked as
  brief-NNN" comments — triage was read-only. They appear only when you run the
  gated step.
- **Each card is `draft`, `Human-gate: review`, `Auto-merge: false`.** Nothing is
  dispatchable until you flip it.
- **Mechanism fit.** Skim each card's prose — the value is the *clustering* judgment
  (holistic over symptom). If a cluster looks wrong, re-home the issue and re-run the
  diff; the frontmatter `Issues:` list is the single source the diff checks.

## What you should feel

Confidence that the inbound edge now exists and is honest — every open issue is
accounted for exactly once, and the two outward-facing actions (queueing work,
posting comments) are still yours. Skepticism is welcome on the *clustering*: that's
the one judgment call triage made for you, and it's the cheapest thing to revise.
