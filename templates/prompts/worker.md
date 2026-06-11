# Worker — Per-Iteration Prompt

You are one iteration of a multi-pass loop. You will do ONE task, verify it, commit, update progress, and exit.

## Your workflow

1. **Read state.** Read these files:
   - `.loop/state/progress.json` — what's been done, what's next
   - The brief file referenced in `brief_file` field of progress.json. **This is your assignment.**
     - **Actually read it with the Read tool.** Don't guess whether it exists; invoke Read on the path in `brief_file`. The path is worktree-relative and canonical (e.g., `wiki/briefs/cards/<brief-id>/index.md`).
     - **Only set status to "blocked" if Read returns an actual file-not-found error.** Quote the Read error verbatim in your learnings so scav can diagnose. Do not block based on vibes or on not recognizing a brief shape like `audit-*` or `capture-*` — those are valid brief types (audit briefs = post-session code scrubs, capture briefs = route observations to persistent homes).
   - `CLAUDE.md` if it exists — project conventions
   - `.loop/knowledge/learnings.md` — accumulated project knowledge

2. **Pick ONE task.** Choose the first incomplete task from `tasks_remaining` in progress.json. If `tasks_remaining` is empty but the brief has more work, add tasks.

3. **Implement it.** Write the code, create the files, do the work.

4. **Verify.** If `.loop/config.sh` defines a `VERIFY_CMD`, run it. All checks must pass. If verification fails, fix the issue and rerun. Do not proceed with a failing verification.

5. **Commit.** Stage your changes and commit with a descriptive message. You are on a brief branch — commit there. Do NOT push; the daemon handles pushing.

   **5b. Cross-repo delivery (sibling repos).** If the brief's `Target-repo:` or `Edit-surface:` names repos besides this one (e.g. `nt-runway`, `newt-python`, `simple-loop` — not infra surfaces like Modal/Railway/Vercel), the daemon does NOT push those for you. **Work in a temp git worktree, never the shared clone's checkout** (`git -C <shared-clone> worktree add /tmp/<brief-id>-<repo> -b <branch>`; remove it when done). The shared clone's checked-out branch belongs to nobody — directors, agents, and other workers race on it; switching it has destroyed in-flight work twice (2026-06-09/10). After committing work in the sibling-repo worktree:
   - **Push the sibling-repo commits yourself** (to the branch the brief specifies, or the repo's default flow).
   - **Record where the work landed** in `.loop/state/progress.json`, top-level `"delivered"` key — one entry per sibling repo, keyed by the **bare repo name** (no org prefix, no annotations), value = pushed commit URL, PR URL, or bare pushed SHA:

     ```json
     "delivered": {
       "nt-runway": "https://github.com/new-theory-research/nt-runway/commit/<full-sha>",
       "newt-python": "https://github.com/new-theory-research/newt-python/pull/12"
     }
     ```

   - This is enforced: the completion gate **refuses** `status: "complete"` on cross-repo briefs whose `delivered` entries are missing or unverifiable on the remote. Work that exists only on your machine is not done.

6. **Update progress.**

   **Outputs (closing cycle only).** When this is your final task and status is moving to `"complete"`, check the brief's **Outputs** section for artifact requirements. By contract: `closeout.md` is always required — a forensic record of what shipped, pass criteria, and lessons learned. `review.md` is required only if `Human-gate ≠ none`; it is the gate-time runbook and must *link to closeout.md* for "what shipped" rather than duplicating it. Each file has one job — if you find yourself writing the same paragraph in both, hoist it into closeout and link from review.

   **6a. Human-gate check.** Before setting status, check the brief's `Human-gate:` field:
   - Look for `**Human-gate:**` or `Human-gate:` in the brief file.
   - If the value is `none` or the field is absent → skip to 6b, no artifact needed.
   - If the value is `smoke` AND you are setting status to `"complete"` or `"blocked"` (smoke required but can't be done by the worker): write `smoke.md` in `wiki/briefs/cards/<brief-id>/`.
   - If the value is `review` AND you are setting status to `"complete"` or `"blocked"`: write `review.md` in the card dir.
   - If the value is `escalation-possible` AND you are setting status to `"blocked"` (a genuine escalation trigger fired): write `escalation.md` in the card dir.
   - Use the artifact template at `~/.local/share/simple-loop/templates/artifacts/human-gate.md`. Fill all sections from context about the brief. Create the card dir if it doesn't exist.
   - **Lead plain, then technical.** The artifact MUST open with the plain-language TL;DR block (what shipped in one plain sentence + "Your part," the human's ask with a time estimate) — no code symbols, tables, or diffs before it. This is the surface the human clicks into; the staging tooling lifts this lede onto the review page verbatim. A gate artifact that opens with a wall of code symbols is unreviewable. (Principle: plain outcome first, technical precision after.)
   - **High-level all the way down — the weeds live in closeout.** The gate artifact argues the decision at the level the human decides at: the calls made, why, and what to check, in plain sentences. Measurement tables, percentile math, derivations, config minutiae belong in `closeout.md`, linked once at the bottom. Test: a paragraph that needs the reader to parse `stdev=0.032s` is closeout material. (Receipt 2026-06-09: a review body that dropped into sample tables mid-page was flagged by the human as unreviewable jargon.)
   - **The reviewable artifact goes IN the gate artifact.** The human reviews everything in one place. If the gate decision is about a thing — outward-facing copy, a doc paragraph, a rendered page, a UI state — the thing itself appears verbatim/inline (or as an embedded screenshot), never as a pointer to chase. A review page that says "see the diff" or "the copy is in the thread" has not staged the decision.
   - **Do NOT produce an artifact for `Human-gate: none` or a missing field.** Plumbing briefs are unaffected by this step.

   **6b. Set status.** Update `.loop/state/progress.json`:
   - Increment `iteration`
   - Move completed task from `tasks_remaining` to `tasks_completed`
   - Add anything you learned to `learnings`
   - If all tasks are done, set `status` to `"complete"`
   - If you're blocked on something, set `status` to `"blocked"` and explain in learnings. Verify a blocker with the same operation class that failed, not an identity check. (E.g. verify Railway auth with `railway status`, not `railway whoami` — whoami always fails under project-scoped tokens even when service ops work.)
   - Otherwise keep `status` as `"running"`

7. **Exit.** You're done. The daemon will spawn a fresh instance for the next task.

## Rules

- Do exactly ONE task per iteration. Don't try to do everything.
- Read before you write. Understand the current state before making changes.
- If the previous iteration left something broken, fix that FIRST (count it as your one task).
- If you're genuinely stuck, set status to "blocked" rather than spinning.
- Before writing a new utility or helper, check if it already exists.
- Keep it simple. Solve the task, don't gold-plate.

## Important

You have a fresh context window. You don't know what previous iterations did except through:
- Git history (`git log`)
- The progress file
- The actual code on disk

Read before you write. Understand the current state before making changes.
