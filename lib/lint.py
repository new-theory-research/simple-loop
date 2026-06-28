#!/usr/bin/env python3
"""
loop lint — deterministic brief-format linter.

Checks brief files for format drift that the daemon can't parse.
No LLM calls. Subsecond per file. Read-only.

Exit codes:
  0 — clean
  1 — drift detected
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# ── Severity ─────────────────────────────────────────────────

ERROR = "error"
WARNING = "warning"
INFO = "info"

SEVERITY_ICON = {
    ERROR: "❌",
    WARNING: "⚠️",
    INFO: "ℹ️",
}


@dataclass
class Issue:
    severity: str
    message: str
    expected: str = ""
    fix: str = ""


# ── Required field regexes ───────────────────────────────────

REQUIRED_FIELDS = {
    "ID": re.compile(r"^\*\*ID:\*\*\s*\S+", re.MULTILINE),
    "Branch": re.compile(r"^\*\*Branch:\*\*\s*\S+", re.MULTILINE),
    "Status": re.compile(r"^\*\*Status:\*\*\s*\S+", re.MULTILINE),
    "Model": re.compile(r"^\*\*Model:\*\*\s*\S+", re.MULTILINE),
    "Auto-merge": re.compile(r"^\*\*Auto-merge:\*\*\s*\S+", re.MULTILINE),
    "Validator": re.compile(r"^\*\*Validator:\*\*\s*\S+", re.MULTILINE),
    "Human-gate": re.compile(r"^\*\*Human-gate:\*\*\s*\S+", re.MULTILINE),
}

BUDGET_SECTION_RE = re.compile(r"^## Budget\s*$", re.MULTILINE)
BUDGET_OPENER_RE = re.compile(r"^\*\*\d+\s+cycles?\s+\w+\.\*\*", re.MULTILINE)

DEPENDS_ON_RE = re.compile(r"^\*\*Depends-on:\*\*\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
# Single source of truth for brief-id shape. assess.py owns the canonical regex
# (the daemon's parser is the runtime path that wedges); lint.py imports it so
# author-time and dispatch-time agree on what a brief id looks like.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assess import BRIEF_ID_RE  # noqa: E402

ADR_FIELD_RE = re.compile(r"^\*\*ADRs?:\*\*\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
ADR_NUMBER_RE = re.compile(r"\b(\d{3})\b")

MANDATORY_LINK_RE = re.compile(r"\[.*?\]\(((?!https?://)[^)]+\.md[^)]*)\)", re.MULTILINE)

YAML_FRONTMATTER_RE = re.compile(r"^---\s*$", re.MULTILINE)
MD_FIELD_RE = re.compile(r"^\*\*\w", re.MULTILINE)

# ── Sibling-field regexes (for check_sibling_fields) ─────────────────────────
_AUTO_MERGE_RE = re.compile(r"^\*\*Auto-merge:\*\*\s*(.+?)\s*$", re.MULTILINE)
_HUMAN_GATE_RE = re.compile(r"^\*\*Human-gate:\*\*\s*(.+?)\s*$", re.MULTILINE)
_BRANCH_FIELD_RE = re.compile(r"^\*\*Branch:\*\*\s*(.+?)\s*$", re.MULTILINE)
_VALIDATOR_FIELD_RE = re.compile(r"^\*\*Validator:\*\*\s*(.+?)\s*$", re.MULTILINE)
_STATUS_FIELD_RE = re.compile(r"^\*\*Status:\*\*\s*(.+?)\s*$", re.MULTILINE)
_MODEL_FIELD_RE = re.compile(r"^\*\*Model:\*\*\s*(.+?)\s*$", re.MULTILINE)
_TARGET_REPO_RE = re.compile(r"^\*\*Target repo:\*\*\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_FIELD_PAREN_RE = re.compile(r"\(")
_FIELD_ITALIC_RE = re.compile(r"^[_*]")
_ILLEGAL_PLACEHOLDERS = frozenset(("none", "empty", "n/a", "tbd"))


# ── Check 1: Frontmatter style ───────────────────────────────

def check_frontmatter_style(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """Frontmatter must be markdown-emphasis lines, not YAML --- blocks."""
    if YAML_FRONTMATTER_RE.search(content):
        return [Issue(
            severity=ERROR,
            message="YAML frontmatter (`---` block) detected.",
            expected="Frontmatter uses markdown-emphasis lines: `**Field:** value`",
            fix="Remove the `---` delimiters. Each field should be a standalone bold-label line.",
        )]
    return []


# ── Check 2: Required fields ─────────────────────────────────

def check_required_fields(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """All required frontmatter fields must be present."""
    issues = []
    for field_name, pattern in REQUIRED_FIELDS.items():
        if not pattern.search(content):
            issues.append(Issue(
                severity=ERROR,
                message=f"Missing required field `**{field_name}:**`.",
                expected=f"`**{field_name}:** <value>` on its own line near the top of the brief.",
                fix=f"Add `**{field_name}:** <value>` after the title.",
            ))
    return issues


# ── Check 3: Budget section ──────────────────────────────────

def check_budget_section(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """## Budget section must exist with a parseable opener."""
    if not BUDGET_SECTION_RE.search(content):
        return [Issue(
            severity=ERROR,
            message="Missing `## Budget` section. Hive cycle-X/Y display will fall back to event count.",
            expected="A `## Budget` header followed by a line like `**N cycles sonnet.**`",
            fix="Insert `## Budget\\n\\n**N cycles sonnet.** [rationale]\\n` before `## Completion criteria`.",
        )]

    # Section exists — check for parseable opener
    # Find the content after ## Budget
    budget_match = BUDGET_SECTION_RE.search(content)
    after_budget = content[budget_match.end():]
    # Take up to the next ## section
    next_section = re.search(r"^##\s", after_budget, re.MULTILINE)
    budget_body = after_budget[:next_section.start()] if next_section else after_budget

    if not BUDGET_OPENER_RE.search(budget_body):
        return [Issue(
            severity=ERROR,
            message="`## Budget` section exists but has no parseable opener.",
            expected="First non-empty line after `## Budget` should be `**N cycles MODEL.**`",
            fix="Add e.g. `**3 cycles sonnet.**` as the first line of the Budget section.",
        )]
    return []


# ── Check 4: Depends-on validity ────────────────────────────

def check_depends_on(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """Depends-on must be absent, or a real brief-id — not 'none' or empty.

    Brief-082: extended to catch the empirical wedge shapes:
      - `none (annotation, more)` — author wrote 'none' with explanatory parens.
      - `_(intentionally empty)_` — italics-wrapped placeholder.
      - `none (...)` or any value with `(` before the first `,` — annotation-as-value.

    Each of these previously survived the parser intact and produced a phantom
    dep that never appeared in history → permanent `dispatch_blocked`. Both the
    parser (assess.py) and the linter now reject them; the parser drops with a
    warning at dispatch-time, the linter ERRORs at write-time.
    """
    m = DEPENDS_ON_RE.search(content)
    if not m:
        return []  # absent is fine

    raw = m.group(1).strip()
    if not raw:
        return [Issue(
            severity=ERROR,
            message="`**Depends-on:**` field is present but empty.",
            expected="Either omit the field or list real brief IDs: `**Depends-on:** brief-042-slug`",
            fix="Remove `**Depends-on:**` entirely if there are no dependencies.",
        )]

    issues = []

    # Annotation-as-value: a `(` appearing before the first `,` means the author
    # wrote prose-with-parenthetical instead of a comma-separated id list. The
    # daemon parser splits on the comma and treats both halves as brief ids,
    # producing a permanent dispatch block. Ref: brief-076 (2026-04-26).
    first_comma = raw.find(",")
    first_paren = raw.find("(")
    if first_paren >= 0 and (first_comma < 0 or first_paren < first_comma):
        issues.append(Issue(
            severity=ERROR,
            message="`**Depends-on:**` value contains a parenthetical annotation. The daemon's parser splits on commas inside the parens and treats each half as a brief ID, causing a permanent dispatch block until human edits frontmatter.",
            expected="List ONLY real brief IDs (e.g. `brief-042-slug`). Omit the field entirely when there are no dependencies.",
            fix="Remove the parenthetical. If there are no real deps, delete the `**Depends-on:**` line.",
        ))

    # Split on commas (mirroring parse_depends_on_value's pre-validation cleaning)
    tokens = [t.strip().strip(".,;") for t in raw.split(",") if t.strip().strip(".,;")]

    for tok in tokens:
        low = tok.lower()
        # Strip a parenthetical so `none (foo)` and `none(foo)` both surface.
        low_no_paren = re.sub(r"\s*\(.*$", "", low).strip()
        if low_no_paren == "none" or low.startswith("none "):
            issues.append(Issue(
                severity=ERROR,
                message="`**Depends-on:** none` — literal 'none' is treated as a brief ID by the daemon, causing a permanent dispatch block until human edits frontmatter.",
                expected="Omit the `**Depends-on:**` field entirely when there are no dependencies.",
                fix="Remove the `**Depends-on:**` line.",
            ))
            continue
        # Markdown italics-wrapped placeholders (`_..._`, `*...*`). Brief-082
        # used `_(intentionally empty — see Why)_`; parser kept it as one token.
        if tok.startswith(("_", "*")):
            issues.append(Issue(
                severity=ERROR,
                message=f"`**Depends-on:**` value `{tok}` is a markdown-italics placeholder, not a brief ID. The daemon's parser keeps it intact and the deps history-check never matches, causing a permanent dispatch block until human edits frontmatter.",
                expected="Either list real brief IDs or omit the `**Depends-on:**` field entirely.",
                fix="Remove the `**Depends-on:**` line if there are no dependencies.",
            ))
            continue
        if not BRIEF_ID_RE.match(tok):
            issues.append(Issue(
                severity=WARNING,
                message=f"`**Depends-on:**` value `{tok}` doesn't match `brief-NNN` or `brief-NNN-slug` format.",
                expected="Each dep should match `brief-\\d+(-\\w+)*` e.g. `brief-042` or `brief-042-camera-system`",
                fix=f"Check the spelling of `{tok}` against the actual brief ID.",
            ))
    return issues


# ── Check 5: Dep ID format consistency ──────────────────────

def check_dep_id_format(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """Each Depends-on value must be a well-formed brief ID."""
    # This overlaps check 4 — specifically the format regex check is already
    # in check 4 as a WARNING. Check 5 focuses on ID format *consistency* —
    # whether the ID is short-form vs full-form and whether that matters.
    # The daemon's history regex captures either form, so this is just a
    # formatting consistency warning, not an error.
    m = DEPENDS_ON_RE.search(content)
    if not m:
        return []

    raw = m.group(1).strip()
    tokens = [t.strip().strip(".,;") for t in raw.split(",") if t.strip().strip(".,;")]

    issues = []
    for tok in tokens:
        if tok.lower() == "none":
            continue  # already caught by check 4
        # Check for "brief-NNN" short form (no slug) — valid but note it
        # Short form is fine; this check passes. Only flag truly malformed IDs.
        if re.match(r"^brief-\d+$", tok):
            # Short form like brief-042 — valid
            pass
        elif re.match(r"^brief-\d+-", tok):
            # Full form like brief-042-slug — valid
            pass
        # Other formats already caught as WARNING in check 4
    return []


# ── Check 6: ADR resolution ──────────────────────────────────

def check_adr_resolution(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """ADRs field references must resolve to wiki/decisions/NNN-*.md."""
    m = ADR_FIELD_RE.search(content)
    if not m:
        return []

    raw = m.group(1).strip()
    if raw.lower() == "none":
        return []

    decisions_dir = project_root / "wiki" / "decisions"
    if not decisions_dir.exists():
        return []  # can't resolve without the decisions dir

    issues = []
    for num_match in ADR_NUMBER_RE.finditer(raw):
        adr_num = num_match.group(1)
        # Look for wiki/decisions/NNN-*.md
        matches = list(decisions_dir.glob(f"{adr_num}-*.md"))
        if not matches:
            issues.append(Issue(
                severity=WARNING,
                message=f"`**ADRs:** {adr_num}` — no file found at `wiki/decisions/{adr_num}-*.md`.",
                expected=f"A decision file at `wiki/decisions/{adr_num}-<slug>.md`",
                fix=f"Create `wiki/decisions/{adr_num}-slug.md` or fix the ADR number.",
            ))
    return issues


# ── Check 7: MANDATORY reading link resolution ───────────────

def check_mandatory_reading_links(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """MANDATORY reading section link paths must resolve."""
    # Find the MANDATORY reading section
    mandatory_match = re.search(r"^##\s+MANDATORY reading", content, re.MULTILINE | re.IGNORECASE)
    if not mandatory_match:
        return []

    # Extract section content up to next ##
    after = content[mandatory_match.end():]
    next_section = re.search(r"^##\s", after, re.MULTILINE)
    section = after[:next_section.start()] if next_section else after

    issues = []
    brief_dir = brief_path.parent

    for link_match in MANDATORY_LINK_RE.finditer(section):
        link_path_raw = link_match.group(1).split("#")[0]  # strip anchors
        if not link_path_raw:
            continue
        # Resolve relative to brief_path
        resolved = (brief_dir / link_path_raw).resolve()
        if not resolved.exists():
            issues.append(Issue(
                severity=WARNING,
                message=f"MANDATORY reading link `{link_path_raw}` does not resolve.",
                expected="All MANDATORY reading links must point to existing files.",
                fix=f"Check the path relative to `{brief_path}` or update the link.",
            ))
    return issues


# ── Check 8: Status consistency with running.json ────────────

def check_status_consistency(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """Brief Status field should be consistent with running.json's view."""
    status_m = re.search(r"^\*\*Status:\*\*\s*(\S+)", content, re.MULTILINE)
    if not status_m:
        return []  # missing field caught by check 2

    brief_status = status_m.group(1).lower()

    id_m = re.search(r"^\*\*ID:\*\*\s*(\S+)", content, re.MULTILINE)
    if not id_m:
        return []  # missing field caught by check 2
    brief_id = id_m.group(1).lower()

    running_file = project_root / ".loop" / "state" / "running.json"
    if not running_file.exists():
        return []

    try:
        with running_file.open() as f:
            running = json.load(f)
    except Exception:
        return []

    # Determine what running.json thinks about this brief
    active_ids = {e.get("brief", "").lower() for e in running.get("active", [])}
    pending_eval_ids = {e.get("brief", "").lower() for e in running.get("completed_pending_eval", [])}
    awaiting_review_ids = {e.get("brief", "").lower() for e in running.get("awaiting_review", [])}
    history_ids = {e.get("brief", "").lower() for e in running.get("history", [])}

    issues = []

    if brief_id in active_ids and brief_status == "queued":
        issues.append(Issue(
            severity=INFO,
            message=f"Brief `{brief_id}` says Status: queued but running.json has it active.",
            expected="Status field should be updated to `active` once dispatch happens.",
            fix="Update `**Status:** active` in the brief (or accept the drift as cosmetic).",
        ))
    elif brief_id in history_ids and brief_status == "queued":
        issues.append(Issue(
            severity=INFO,
            message=f"Brief `{brief_id}` says Status: queued but running.json shows it in history (merged).",
            expected="Status field should be `merged` or `complete` for finished briefs.",
            fix="Update `**Status:** merged` in the brief.",
        ))

    return issues


# ── Check 9: Sibling-field format (parser-permissive, linter-strict) ─────────

def _check_sibling_field(
    content: str,
    field_name: str,
    pattern: "re.Pattern[str]",
    none_is_valid: bool = False,
) -> List[Issue]:
    """Enforce the parser-permissive + linter-strict discipline on a single-token field.

    Three pollution shapes caught:
      1. Parenthetical annotation  — `opus (comment)`, `true (rationale)`
      2. Italic-wrapped placeholder — `_intentionally empty_`
      3. Illegal literal            — `none`/`empty`/`n/a`/`tbd` where not valid

    `none_is_valid=True` exempts Human-gate from check #3 (`none` = explicit opt-out).
    Missing fields are caught upstream by check_required_fields — not repeated here.
    """
    m = pattern.search(content)
    if not m:
        return []

    raw = m.group(1).strip()
    if not raw:
        return []

    issues: List[Issue] = []

    if _FIELD_PAREN_RE.search(raw):
        bare = raw.split("(")[0].strip().rstrip(",;")
        issues.append(Issue(
            severity=ERROR,
            message=f"`**{field_name}:**` value contains a parenthetical annotation: `{raw[:80]}`. The daemon extracts the first token; parens-as-prose make the field unparseable by tools.",
            expected=f"A bare value with no parenthetical, e.g. `**{field_name}:** {bare}`. Move explanatory notes into a prose section of the brief.",
            fix=f"Remove the parenthetical: `**{field_name}:** {bare}`",
        ))

    if _FIELD_ITALIC_RE.match(raw):
        issues.append(Issue(
            severity=ERROR,
            message=f"`**{field_name}:**` value `{raw[:80]}` is an italic-wrapped placeholder, not a real value.",
            expected=f"A concrete value, or remove the `**{field_name}:**` line if the field is optional.",
            fix=f"Replace `{raw}` with the actual value, or remove the `**{field_name}:**` line.",
        ))

    if not none_is_valid:
        first_word = raw.lower().split()[0].rstrip(".,;") if raw.split() else ""
        if first_word in _ILLEGAL_PLACEHOLDERS:
            issues.append(Issue(
                severity=ERROR,
                message=f"`**{field_name}:** {raw}` — `{first_word}` is not a valid value for this field.",
                expected=f"A real value for `**{field_name}:**`, or omit the line entirely if the field is optional.",
                fix=f"Replace `{first_word}` with the actual value, or remove the `**{field_name}:**` line.",
            ))

    return issues


def check_sibling_fields(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """Sibling frontmatter fields: parens-as-annotation, italic placeholders, illegal literals.

    Mirrors the graduated discipline from check_depends_on across the seven fields
    most likely to carry prose-pollution next: Auto-merge, Human-gate, Branch,
    Validator, Status, Model, Target-repo.

    Human-gate is the one exception: `none` IS a legitimate value (explicit opt-out).
    All other fields treat `none`/`empty`/`n/a`/`tbd` as illegal placeholders —
    canonical empty form is to omit the field entirely.
    """
    issues: List[Issue] = []
    issues.extend(_check_sibling_field(content, "Auto-merge",  _AUTO_MERGE_RE,    none_is_valid=False))
    issues.extend(_check_sibling_field(content, "Human-gate",  _HUMAN_GATE_RE,    none_is_valid=True))
    issues.extend(_check_sibling_field(content, "Branch",      _BRANCH_FIELD_RE,  none_is_valid=False))
    issues.extend(_check_sibling_field(content, "Validator",   _VALIDATOR_FIELD_RE, none_is_valid=False))
    issues.extend(_check_sibling_field(content, "Status",      _STATUS_FIELD_RE,  none_is_valid=False))
    issues.extend(_check_sibling_field(content, "Model",       _MODEL_FIELD_RE,   none_is_valid=False))
    issues.extend(_check_sibling_field(content, "Target repo", _TARGET_REPO_RE,   none_is_valid=False))
    return issues


# ── Check 10: Output artifact contract ───────────────────────

_JACCARD_OVERLAP_THRESHOLD = 0.60  # tunable; conservative default

def _jaccard(tokens_a: set, tokens_b: set) -> float:
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


def _tokenize_body(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _extract_h1(text: str) -> str:
    m = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    return m.group(1).strip().lower() if m else ""


def _first_paragraph(text: str) -> str:
    """Return first non-empty, non-heading line (up to 200 chars, lowercased)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:200].lower()
    return ""


def _check_artifact_overlap(review_path: Path, closeout_path: Path) -> Optional[Issue]:
    """Detect substantial content overlap between review.md and closeout.md.

    Three deterministic heuristics (any one triggers ERROR):
      1. Identical H1 titles (after stripping `#` and whitespace).
      2. Identical first non-empty, non-heading paragraph (first 200 chars).
      3. Jaccard token overlap >= 60% on full body text.

    Deterministic only — no LLM calls. Threshold is conservative to avoid
    false positives from shared boilerplate tokens (e.g. the brief ID appearing
    in both files). The Jaccard check at 60% means the majority of unique tokens
    are shared, which signals duplicated prose rather than shared references.
    """
    try:
        r_text = review_path.read_text(encoding="utf-8")
        c_text = closeout_path.read_text(encoding="utf-8")
    except Exception:
        return None

    r_h1 = _extract_h1(r_text)
    c_h1 = _extract_h1(c_text)
    if r_h1 and c_h1 and r_h1 == c_h1:
        return Issue(
            severity=ERROR,
            message="Substantial overlap between `review.md` and `closeout.md`: identical H1 titles.",
            expected="Each artifact has one job. review.md is the gate-time runbook; closeout.md is the forensic record. They must have distinct titles.",
            fix="Rename the H1 in review.md (e.g. `# Review gate — <brief-slug>`) and replace any duplicated 'what shipped' content with a link to closeout.md.",
        )

    r_para = _first_paragraph(r_text)
    c_para = _first_paragraph(c_text)
    if r_para and c_para and r_para == c_para:
        return Issue(
            severity=ERROR,
            message="Substantial overlap between `review.md` and `closeout.md`: identical opening paragraphs.",
            expected="review.md opens with the gate-time ask; closeout.md opens with the forensic TL;DR. They must differ.",
            fix="Rewrite the review.md opener as the gate-time runbook (ask + recommendation + what-you-should-feel). Link to closeout.md for 'what shipped.'",
        )

    r_tokens = _tokenize_body(r_text)
    c_tokens = _tokenize_body(c_text)
    if r_tokens and c_tokens:
        j = _jaccard(r_tokens, c_tokens)
        if j >= _JACCARD_OVERLAP_THRESHOLD:
            return Issue(
                severity=ERROR,
                message=f"Substantial overlap between `review.md` and `closeout.md`: {j:.0%} Jaccard token overlap (threshold: {_JACCARD_OVERLAP_THRESHOLD:.0%}).",
                expected="review.md links to closeout.md for 'what shipped' rather than duplicating it. Overlap this high indicates copied prose.",
                fix="Remove the duplicated 'what shipped' sections from review.md and replace with: `See [closeout.md](./closeout.md) for the forensic record.`",
            )

    return None


def check_outputs(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """Enforce the closeout/review artifact contract at brief write time.

    Contract:
      - closeout.md: always required at brief close. Forensic record. (Presence
        not enforced by this check — absence is a writer error, not pre-detectable
        without knowing the brief is truly finished.)
      - review.md: required IFF Human-gate ≠ none. Gate-time runbook for Mattie.
      - No content overlap: review.md links to closeout.md for 'what shipped';
        it does not duplicate it.

    Rules:
      ERROR — brief Status is `awaiting_review` AND Human-gate ≠ none AND
              review.md absent from the card directory.
      ERROR — both review.md and closeout.md exist AND substantial overlap
              detected (identical H1, identical first paragraph, or ≥60% Jaccard).
      WARN  — Human-gate = none AND review.md exists (unnecessary artifact).

    Briefs live at canonical card paths (`wiki/briefs/cards/<id>/index.md`)
    post-brief-108-cont-b; the card directory is just `brief_path.parent`.
    """
    gate_m = _HUMAN_GATE_RE.search(content)
    gate_val = gate_m.group(1).strip().lower() if gate_m else "none"
    gate_none = (gate_val == "none" or not gate_m)

    status_m = _STATUS_FIELD_RE.search(content)
    brief_status = status_m.group(1).strip().lower() if status_m else ""

    card_dir = brief_path.parent
    if not card_dir.is_dir():
        return []

    review_path = card_dir / "review.md"
    closeout_path = card_dir / "closeout.md"
    review_exists = review_path.exists()
    closeout_exists = closeout_path.exists()

    issues: List[Issue] = []

    if brief_status == "awaiting_review" and not gate_none and not review_exists:
        issues.append(Issue(
            severity=ERROR,
            message=f"Brief is `awaiting_review` with `Human-gate: {gate_val}` but `review.md` is missing from the card directory.",
            expected="A `review.md` in the card directory: gate-time runbook + scav recommendation + what-you-should-feel. Links to closeout.md for 'what shipped.'",
            fix=f"Create `{card_dir.name}/review.md` using the human-gate artifact template.",
        ))

    if gate_none and review_exists:
        issues.append(Issue(
            severity=WARNING,
            message="`Human-gate: none` but `review.md` exists in the card directory — unnecessary artifact.",
            expected="No `review.md` when Human-gate is none (brief closes without a human gate).",
            fix=f"Remove `{card_dir.name}/review.md` or change `**Human-gate:**` to a non-none value if a gate was intended.",
        ))

    if review_exists and closeout_exists:
        overlap = _check_artifact_overlap(review_path, closeout_path)
        if overlap:
            issues.append(overlap)

    return issues


# ── Check 11: Code-change review.md outcome shape ─────────────

_EDIT_SURFACE_FIELD_RE = re.compile(
    r"^\*\*Edit-surface:\*\*\s*(.*?)$", re.MULTILINE | re.IGNORECASE
)
_CODE_CHANGE_PREFIXES = frozenset(("apps/", "web/", "tools/", "crates/", "packages/"))

_REVIEW_WHAT_BROKEN_RE = re.compile(
    r"^#+\s+What was broken", re.MULTILINE | re.IGNORECASE
)
_REVIEW_HOW_FIXED_RE = re.compile(
    r"^#+\s+How we know it.s fixed", re.MULTILINE | re.IGNORECASE
)
_REVIEW_RECURRENCE_RE = re.compile(
    r"^#+\s+How we.d know if it recurred", re.MULTILINE | re.IGNORECASE
)


def _classify_edit_surface(content: str) -> str:
    """Return "code-change", "no-field", or "not-code-change".

    Code-change: Edit-surface has paths under apps/, web/, tools/, crates/, packages/.
    Fail-permissive: field absent → "no-field" (caller warns, does not error).
    """
    field_m = _EDIT_SURFACE_FIELD_RE.search(content)
    if not field_m:
        return "no-field"

    # Collect the field's same-line value + any following indented list items,
    # stopping at the next **Bold:** field or ## section header.
    block = field_m.group(0)
    for line in content[field_m.end():].split("\n"):
        if re.match(r"^\*\*\w", line) or re.match(r"^##", line):
            break
        block += "\n" + line

    for prefix in _CODE_CHANGE_PREFIXES:
        if prefix in block:
            return "code-change"

    return "not-code-change"


def check_review_md_shape(content: str, brief_path: Path, project_root: Path) -> List[Issue]:
    """Code-change briefs: review.md must have all three outcome sections.

    Contract (brief-101): outcome-surface, not diff-skim.
    Code-change classified by Edit-surface paths under apps/, web/, tools/, crates/, packages/.
    Fail-permissive on missing Edit-surface field — warns, does not error.
    Deterministic: section-header presence via regex only; no LLM calls.
    """
    classification = _classify_edit_surface(content)
    card_dir = brief_path.resolve().parent
    review_path = card_dir / "review.md"

    if classification == "no-field":
        return []  # Fail-permissive: can't classify without field; skip silently.

    if classification != "code-change":
        return []  # Non-code-change brief — this rule does not apply.

    if not review_path.exists():
        return []  # review.md absent — check_outputs handles the presence gate.

    try:
        review_text = review_path.read_text(encoding="utf-8")
    except Exception as e:
        return [Issue(severity=ERROR, message=f"Cannot read review.md: {e}")]

    issues: List[Issue] = []

    if not _REVIEW_WHAT_BROKEN_RE.search(review_text):
        issues.append(Issue(
            severity=ERROR,
            message="Code-change `review.md` missing `## What was broken` section.",
            expected="Plain-words failure mode description — no diff references.",
            fix="Add `## What was broken\\n\\n[Describe the failure in plain words, no diff references.]`",
        ))

    if not _REVIEW_HOW_FIXED_RE.search(review_text):
        issues.append(Issue(
            severity=ERROR,
            message="Code-change `review.md` missing `## How we know it's fixed (live, observable now)` section.",
            expected="Table of observables: log lines, healthz fields, behavior changes — each with a 'where to verify' citation.",
            fix="Add `## How we know it's fixed (live, observable now)\\n\\n| Observable | Status | Where to verify |\\n|---|---|---|`",
        ))

    if not _REVIEW_RECURRENCE_RE.search(review_text):
        issues.append(Issue(
            severity=ERROR,
            message="Code-change `review.md` missing `## How we'd know if it recurred` section.",
            expected="Regression detector: name the same observables as 'stops firing → regression'.",
            fix="Add `## How we'd know if it recurred\\n\\n[Name the observables that stop firing if this regresses.]`",
        ))

    return issues


# ── Check 12: goals.md state-prose ───────────────────────────

# Brief-104 + Brief-108: goals.md is "priority intent only" — state prose
# ("merged", "rejected", "killed", etc.) bleeds display surfaces (hive Queued
# shows merged briefs). Severity: ERROR (strict; goals.md sweep completed 2026-04-29).
_GOALS_STATE_SEVERITY = ERROR

_GOALS_STATE_PATTERNS: List[Tuple["re.Pattern[str]", str]] = [
    # ~~strikethrough~~ — visual "this is done" that belongs in cards
    (re.compile(r"~~.+~~"), "strikethrough"),
    # [MERGED sha/date] — bracketed merge receipt
    (re.compile(r"\[MERGED\b", re.IGNORECASE), "bracketed MERGED"),
    # [REJECTED] — bracketed rejection receipt
    (re.compile(r"\[REJECTED\b", re.IGNORECASE), "bracketed REJECTED"),
    # MERGED + INSTALLED + VERIFIED — post-install state marker
    (re.compile(r"\bMERGED\s*\+\s*INSTALLED", re.IGNORECASE), "MERGED+INSTALLED+VERIFIED"),
    # "merged <sha>" or "merged YYYY-MM-DD" — inline merge receipt
    (re.compile(r"\bmerged\s+(?:`?[0-9a-f]{5,}`?|\d{4}-\d{2}-\d{2})", re.IGNORECASE), "merged+sha/date"),
    # REJECTED — or REJECTED YYYY-MM-DD (all-caps only; lowercase "rejected" in prose is common)
    (re.compile(r"\bREJECTED\s*(?:—|–|--|2\d{3}-\d{2}-\d{2})"), "REJECTED state"),
    # KILL as a bare disposition (all-caps to avoid "skill", etc.)
    (re.compile(r"\bKILL\b"), "KILL disposition"),
    # ABSORBED into — explicit absorption language
    (re.compile(r"\bABSORBED\s+into\b", re.IGNORECASE), "ABSORBED into"),
    # "completed" or "shipped" as terminal state words
    (re.compile(r"\bcompleted\b", re.IGNORECASE), "completed state"),
    (re.compile(r"\bshipped\b", re.IGNORECASE), "shipped state"),
    # "Recently merged" — historical list masquerading as queue
    (re.compile(r"\brecently\s+merged\b", re.IGNORECASE), "recently merged section"),
    # "Killed YYYY-MM-DD" — dead-brief state label
    (re.compile(r"\bKilled\s+\d{4}-\d{2}-\d{2}"), "Killed+date"),
    # "Rejected YYYY-MM-DD" — rejected-brief state label (title case)
    (re.compile(r"\bRejected\s+\d{4}-\d{2}-\d{2}"), "Rejected+date"),
    # "(forensic)" — archaeic forensic-section label
    (re.compile(r"\(forensic\)"), "forensic label"),
]


def check_goals_md_state_prose(content: str, goals_path: Path, project_root: Path) -> List[Issue]:
    """goals.md must not carry state prose — state lives in card Status: frontmatter.

    Flags patterns that indicate state bleeding into goals.md entries:
    strikethrough, bracketed state keywords, merged+sha/date, REJECTED,
    KILL, ABSORBED into, completed, shipped, recently merged, Killed+date,
    Rejected+date, forensic labels.

    Severity: ERROR (strict; goals.md sweep completed 2026-04-29 per brief-108).
    Deterministic regex only — no LLM calls.
    """
    issues: List[Issue] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for pattern, label in _GOALS_STATE_PATTERNS:
            if pattern.search(line):
                snippet = line.strip()[:120]
                issues.append(Issue(
                    severity=_GOALS_STATE_SEVERITY,
                    message=f"Line {lineno}: goals.md state-y prose ({label}): `{snippet}`",
                    expected="goals.md carries priority intent only. State (merged, rejected, etc.) lives in running.json. Shipped entries get DELETED on next sweep, not struck-through.",
                    fix="Remove state prose from this line. If the entry has shipped, delete the line entirely.",
                ))
                break  # one issue per line, even if multiple patterns match
    return issues


GOALS_CHECKS: List[Tuple[str, Callable]] = [
    ("goals-state-prose", check_goals_md_state_prose),
]


# ── Source-code check: forbid direct running.json writes (brief-108-d) ────

# Matches `open(... running.json ..., "w")` and `open(... running.json ..., 'w')`
# in any combination, with optional whitespace and parens. Bytewise match;
# we don't AST-parse — keeps lint fast and matches str-template patterns too.
_RUNNING_JSON_WRITE_RE = re.compile(
    r"open\s*\([^)]*running\.json[^)]*['\"]w['\"]",
    re.IGNORECASE,
)
# Allowed call sites — anything in lib/state.py (the projector module) or the
# migration script (which writes running.json once at migration time, indirect
# via state.write_running_json).
_RUNNING_JSON_WRITE_ALLOWED_FILES = frozenset({
    "state.py",
    "state_test.py",  # tests construct fixtures
    "lint.py",        # this file (the regex itself)
    "migrate_runtime_events.py",
})


def check_running_json_writes(content: str, source_path: Path,
                              project_root: Path) -> List[Issue]:
    """Forbid direct writes to running.json outside lib/state.py.

    Pattern: `open(... running.json ..., "w")` matches in any python file
    under lib/ except the allow-list. Ensures running.json stays a projected
    file (brief-108-d) — single-writer ownership in
    lib/state.py:write_running_json.
    """
    issues: List[Issue] = []
    if source_path.name in _RUNNING_JSON_WRITE_ALLOWED_FILES:
        return issues
    for lineno, line in enumerate(content.splitlines(), start=1):
        if _RUNNING_JSON_WRITE_RE.search(line):
            issues.append(Issue(
                severity=ERROR,
                message=(
                    f"{source_path.name}:{lineno}: direct write to running.json. "
                    f"running.json is a projected file (brief-108-d). "
                    f"Use state.write_running_json(project_dir) instead."
                ),
                expected=(
                    "running.json is derived from cards + runtime-events.jsonl. "
                    "Mutate the source (card status, runtime event), then call "
                    "state.write_running_json() to project."
                ),
                fix=(
                    "Replace with `from state import write_running_json; "
                    "write_running_json(project_dir)`."
                ),
            ))
    return issues


def lint_lib_dir(lib_dir: Path, project_root: Path) -> List[Tuple[Path, List[Issue]]]:
    """Scan lib/*.py for the running.json write check.

    Returns list of (file_path, issues) for files with at least one issue.
    """
    out: List[Tuple[Path, List[Issue]]] = []
    if not lib_dir.is_dir():
        return out
    for py in sorted(lib_dir.glob("*.py")):
        try:
            content = py.read_text(encoding="utf-8")
        except OSError:
            continue
        issues = check_running_json_writes(content, py, project_root)
        if issues:
            out.append((py, issues))
    return out


def lint_goals_md(goals_path: Path, project_root: Path) -> List[Issue]:
    """Run goals.md-specific lint checks."""
    try:
        content = goals_path.read_text(encoding="utf-8")
    except Exception as e:
        return [Issue(severity=ERROR, message=f"Cannot read file: {e}")]

    issues: List[Issue] = []
    for _name, check_fn in GOALS_CHECKS:
        issues.extend(check_fn(content, goals_path, project_root))
    return issues


# ── Check registry ────────────────────────────────────────────

CHECKS: List[Tuple[str, Callable]] = [
    ("frontmatter-style", check_frontmatter_style),
    ("required-fields", check_required_fields),
    ("budget-section", check_budget_section),
    ("depends-on", check_depends_on),
    ("dep-id-format", check_dep_id_format),
    ("sibling-fields", check_sibling_fields),
    ("adr-resolution", check_adr_resolution),
    ("mandatory-reading-links", check_mandatory_reading_links),
    ("status-consistency", check_status_consistency),
    ("outputs", check_outputs),
    ("review-md-shape", check_review_md_shape),
]


# ── Lint a single file ────────────────────────────────────────

def lint_file(brief_path: Path, project_root: Path) -> List[Issue]:
    try:
        content = brief_path.read_text(encoding="utf-8")
    except Exception as e:
        return [Issue(severity=ERROR, message=f"Cannot read file: {e}")]

    issues: List[Issue] = []
    for _name, check_fn in CHECKS:
        issues.extend(check_fn(content, brief_path, project_root))
    return issues


# ── Find project root ─────────────────────────────────────────

def find_project_root(start: Path) -> Optional[Path]:
    """Walk up from start to find the dir containing .loop/."""
    p = start.resolve()
    while p != p.parent:
        if (p / ".loop").exists():
            return p
        p = p.parent
    return None


# ── Output formatting ─────────────────────────────────────────

def format_issues(rel_path: str, issues: List[Issue]) -> str:
    lines = [rel_path]
    for issue in issues:
        icon = SEVERITY_ICON.get(issue.severity, "  ")
        lines.append(f"  {icon} {issue.message}")
        if issue.expected:
            lines.append(f"     Expected: {issue.expected}")
        if issue.fix:
            lines.append(f"     Fix: {issue.fix}")
    return "\n".join(lines)


def count_by_severity(all_issues: List[Issue]) -> dict:
    counts = {ERROR: 0, WARNING: 0, INFO: 0}
    for issue in all_issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
    return counts


# ── Brief status reader ───────────────────────────────────────

def read_brief_status(path: Path) -> Optional[str]:
    """Read just the Status field from a brief file."""
    try:
        content = path.read_text(encoding="utf-8")
        m = re.search(r"^\*\*Status:\*\*\s*(\S+)", content, re.MULTILINE)
        return m.group(1).lower() if m else None
    except Exception:
        return None


def _brief_meta(path: Path) -> tuple:
    """Return (status, brief_id) from a brief file. Both may be None."""
    try:
        content = path.read_text(encoding="utf-8")
        status_m = re.search(r"^\*\*Status:\*\*\s*(\S+)", content, re.MULTILINE)
        id_m = re.search(r"^\*\*ID:\*\*\s*(\S+)", content, re.MULTILINE)
        status = status_m.group(1).lower() if status_m else None
        brief_id = id_m.group(1).lower() if id_m else None
        return status, brief_id
    except Exception:
        return None, None


def _load_history_ids(project_root: Path) -> set:
    """Load the set of brief IDs in running.json history (already merged)."""
    running_file = project_root / ".loop" / "state" / "running.json"
    if not running_file.exists():
        return set()
    try:
        with running_file.open() as f:
            data = json.load(f)
        return {e.get("brief", "").lower() for e in data.get("history", [])}
    except Exception:
        return set()


# ── Main entry point ──────────────────────────────────────────

def main(argv: List[str] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Parse flags before positional args
    all_statuses = "--all" in argv
    argv = [a for a in argv if a != "--all"]
    summary_mode = "--summary" in argv
    argv = [a for a in argv if a != "--summary"]

    # ── --summary mode: one-line drift check for loop info ───────────────────
    if summary_mode:
        if argv:
            target = Path(argv[0]).resolve()
            project_root = find_project_root(target) or find_project_root(Path.cwd()) or Path.cwd()
        else:
            project_root = find_project_root(Path.cwd()) or Path.cwd()
            target = project_root / "wiki" / "briefs" / "cards"

        if not target.is_dir():
            print("none")
            return 0

        candidates = sorted(target.rglob("index.md"))
        if not all_statuses:
            history_ids = _load_history_ids(project_root)
            filtered = []
            for bf in candidates:
                status, brief_id = _brief_meta(bf)
                if status != "queued":
                    continue
                if brief_id and brief_id in history_ids:
                    continue
                filtered.append(bf)
            candidates = filtered

        for bf in candidates:
            issues = lint_file(bf, project_root)
            error_count = sum(1 for i in issues if i.severity == ERROR)
            if error_count:
                brief_label = bf.parent.name
                noun = "error" if error_count == 1 else "errors"
                print(f"{brief_label}: {error_count} {noun}")
                return 1

        print("none")
        return 0

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("Usage: loop lint [--all] <brief-path-or-dir>")
        print("       loop lint --lib <lib-dir>      # source-code checks")
        print()
        print("  loop lint wiki/briefs/cards/brief-049-scene-reset-on-run/index.md")
        print("  loop lint wiki/briefs/cards/              # queued briefs only")
        print("  loop lint --all wiki/briefs/cards/        # all briefs regardless of status")
        print("  loop lint --lib lib/                      # forbid direct running.json writes")
        print()
        print("Checks:")
        for name, _ in CHECKS:
            print(f"  {name}")
        return 0

    # ── --lib mode: scan source files for the running.json-write check ──
    if argv[0] == "--lib":
        if len(argv) < 2:
            print("Error: --lib requires a directory argument", file=sys.stderr)
            return 1
        lib_dir = Path(argv[1]).resolve()
        project_root = find_project_root(lib_dir) or Path.cwd()
        results = lint_lib_dir(lib_dir, project_root)
        if not results:
            print(f"✓ Clean ({lib_dir}).")
            return 0
        total = 0
        for py, issues in results:
            try:
                rel = py.relative_to(project_root)
            except ValueError:
                rel = py
            print(format_issues(str(rel), issues))
            total += len(issues)
            print()
        word = "issue" if total == 1 else "issues"
        print(f"{total} {word} across {len(results)} file(s).")
        return 1

    target_arg = argv[0]
    target = Path(target_arg).resolve()

    if not target.exists():
        print(f"Error: {target_arg} does not exist", file=sys.stderr)
        return 1

    project_root = find_project_root(target)
    if not project_root:
        # Try from cwd
        project_root = find_project_root(Path.cwd())
    if not project_root:
        # Fall back: use target's root or cwd
        project_root = Path.cwd()

    # goals.md lint mode — different check set than brief files
    if target.is_file() and target.name == "goals.md":
        issues = lint_goals_md(target, project_root)
        try:
            rel = target.relative_to(project_root)
        except ValueError:
            rel = target
        if issues:
            print(format_issues(str(rel), issues))
            print()
            issue_word = "issue" if len(issues) == 1 else "issues"
            print(f"{len(issues)} {issue_word} in goals.md.")
            return 1
        else:
            print("✓ Clean (goals.md).")
            return 0

    # Collect brief files to lint
    brief_files: List[Path] = []
    if target.is_file():
        brief_files = [target]
    elif target.is_dir():
        # Find all index.md files inside brief-* subdirs
        candidates = sorted(target.rglob("index.md"))
        if not candidates:
            candidates = sorted(target.glob("*.md"))

        if all_statuses:
            brief_files = candidates
        else:
            # Default: queued briefs that haven't been merged yet.
            # Many old briefs have a stale `Status: queued` field even after merge —
            # cross-check against running.json history to exclude them.
            history_ids = _load_history_ids(project_root)
            filtered = []
            for bf in candidates:
                status, brief_id = _brief_meta(bf)
                if status != "queued":
                    continue
                if brief_id and brief_id in history_ids:
                    continue
                filtered.append(bf)
            brief_files = filtered

    if not brief_files:
        if target.is_dir() and not all_statuses:
            print(f"No queued briefs found at {target_arg} (use --all to scan all statuses).", file=sys.stderr)
        else:
            print(f"No brief files found at {target_arg}", file=sys.stderr)
        return 0

    total_issues = 0
    files_with_issues = 0
    output_blocks: List[str] = []

    for bf in brief_files:
        issues = lint_file(bf, project_root)
        if issues:
            files_with_issues += 1
            total_issues += len(issues)
            try:
                rel = bf.relative_to(project_root)
            except ValueError:
                rel = bf
            output_blocks.append(format_issues(str(rel), issues))

    if output_blocks:
        print("\n\n".join(output_blocks))
        print()
        files_scanned = len(brief_files)
        issue_word = "issue" if total_issues == 1 else "issues"
        file_word = "file" if files_with_issues == 1 else "files"
        scanned_word = "file" if files_scanned == 1 else "files"
        print(f"{total_issues} {issue_word} across {files_with_issues} {file_word} ({files_scanned} {scanned_word} scanned).")
        return 1
    else:
        files_scanned = len(brief_files)
        scanned_word = "file" if files_scanned == 1 else "files"
        print(f"✓ Clean ({files_scanned} {scanned_word} scanned).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
