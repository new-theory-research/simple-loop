#!/usr/bin/env python3
"""Coverage diff: union of card Issues: frontmatter vs gh open-issue list."""
import json, re, subprocess, sys
from pathlib import Path
from collections import Counter

CARDS = Path("wiki/briefs/cards")
issue_re = re.compile(r'"#(\d+)"')

# Collect Issues: from every card, tracking which card each issue lands in.
placement = {}   # issue -> [cards]
for idx in sorted(CARDS.glob("*/index.md")):
    text = idx.read_text()
    m = re.search(r'^Issues:\s*\[(.*?)\]', text, re.MULTILINE)
    if not m:
        continue
    for num in issue_re.findall(m.group(1)):
        placement.setdefault(int(num), []).append(idx.parent.name)

carded = set(placement)
dupes = {i: c for i, c in placement.items() if len(c) > 1}

# Live open issues.
out = subprocess.check_output(
    ["gh", "issue", "list", "--repo", "ScavieFae/simple-loop",
     "--state", "open", "--limit", "200", "--json", "number"])
open_issues = {i["number"] for i in json.loads(out)}

uncovered = sorted(open_issues - carded)
extra = sorted(carded - open_issues)  # carded but not open (should be none)

print(f"open issues        : {len(open_issues)}")
print(f"issues carded      : {len(carded)}")
print(f"uncovered (open, no card) : {uncovered or 'NONE'}")
print(f"in >1 card                : {dupes or 'NONE'}")
print(f"carded but not open       : {extra or 'NONE'}")

ok = not uncovered and not dupes and not extra and open_issues == carded
print("\nCOVERAGE EXACT: " + ("YES — union of Issues: == gh open set, each once" if ok else "NO"))
sys.exit(0 if ok else 1)
