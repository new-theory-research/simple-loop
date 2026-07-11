---
ID: brief-163-input-validation-robustness
Branch: brief-163-input-validation-robustness
Status: draft
Model: sonnet
Auto-merge: false
Validator: core/agents/reviewer.md
Human-gate: review
Target repo: ScavieFae/simple-loop master
Parallel-safe: false
Program: harness-improvements
Issues: ["#21", "#23"]
Depends-on: none
Tags: [harness, validation, assess, presence-check, robustness]
---

# Brief: input parsing/validation robustness — the harness trusts free-form fields

!!! abstract "Intent"
    The harness reads LLM-written and free-form fields without validating them, then
    silently misbehaves: an unrecognized progress status idles `assess.py`, and a
    URL in completion criteria is mistaken for a required file artifact. One
    mechanism: parse-without-validate on untrusted input surfaces.

## The mechanism

- **#21 — `assess.py` silently idles on unrecognized progress status — LLM-written
  status values need enum validation.**
- **#23 — presence-check parses URLs in completion criteria as required file
  artifacts.**

Both are the same failure class: a field written loosely (an LLM status string, a
criterion line containing a URL) is consumed without a validating parse, so the
harness does the wrong thing quietly. Add validation at the parse boundary — an
enum for progress status (#21), URL-vs-path discrimination for criteria (#23).

## Holistic over symptom

The fix is validate-at-parse discipline on these input boundaries, not two
unrelated patches. Both live in the assess/presence-check reading path.

## Outputs

- `closeout.md` — the validation added and per-issue confirmation. Close #21 #23
  with the merge SHA.
- `review.md` — gate runbook.
