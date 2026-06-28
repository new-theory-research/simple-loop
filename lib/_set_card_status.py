#!/usr/bin/env python3
"""Set Status: field in YAML frontmatter of a brief card file.

Usage: python3 lib/_set_card_status.py <card-path> <status>

Preserves all other frontmatter fields and body verbatim.
Idempotent: if Status is already the target value, exits without writing.
No PyYAML dependency — parses frontmatter line-by-line.
"""

import os
import sys


def transform_card_status_content(content, status):
    """Update Status: field in YAML frontmatter. Pure string transform.

    Returns (new_content, changed). changed is False when content has no
    frontmatter, frontmatter is unclosed, or Status is already the target.
    """
    lines = content.splitlines(keepends=True)

    if not lines or lines[0].strip() != "---":
        return (content, False)

    fm_end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end = i
            break

    if fm_end == -1:
        return (content, False)

    new_lines = list(lines)
    status_found = False

    for i in range(1, fm_end):
        stripped = lines[i].lstrip()
        if stripped.lower().startswith("status:"):
            existing_val = stripped[len("status:"):].strip()
            if existing_val == status:
                return (content, False)  # already the target — no-op
            leading = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            key = "Status" if stripped.startswith("S") else "status"
            new_lines[i] = f"{leading}{key}: {status}\n"
            status_found = True
            break

    if not status_found:
        new_lines.insert(1, f"Status: {status}\n")

    return ("".join(new_lines), True)


def set_card_status(card_path, status):
    """Set the Status: field in YAML frontmatter.

    Returns True if the file was written (status changed), False for no-op
    (already the target value, no frontmatter, or read/write error).
    """
    try:
        with open(card_path) as f:
            content = f.read()
    except (IOError, OSError) as e:
        print(f"set_card_status: cannot read {card_path}: {e}", file=sys.stderr)
        return False

    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        print(f"set_card_status: no frontmatter in {card_path} — skipping", file=sys.stderr)
        return False

    fm_end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end = i
            break
    if fm_end == -1:
        print(f"set_card_status: unclosed frontmatter in {card_path} — skipping", file=sys.stderr)
        return False

    new_content, changed = transform_card_status_content(content, status)
    if not changed:
        return False

    try:
        with open(card_path, "w") as f:
            f.write(new_content)
        return True
    except (IOError, OSError) as e:
        print(f"set_card_status: cannot write {card_path}: {e}", file=sys.stderr)
        return False


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <card-path> <status>", file=sys.stderr)
        sys.exit(1)

    card_path = sys.argv[1]
    status = sys.argv[2]

    if not os.path.exists(card_path):
        print(f"set_card_status: {card_path} does not exist — skipping (no-op)")
        sys.exit(0)

    changed = set_card_status(card_path, status)
    if changed:
        print(f"set_card_status: {card_path} → Status: {status}")
    else:
        print(f"set_card_status: {card_path} already Status: {status} or skipped (no-op)")
    sys.exit(0)


if __name__ == "__main__":
    main()
