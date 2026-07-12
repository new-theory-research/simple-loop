#!/usr/bin/env python3
"""UserPromptSubmit hook wrapper — injects fresh peer intent into context.

Thin: parses Claude Code's UserPromptSubmit stdin JSON for session_id, then
shells out to intent-journal.py read-fresh for THIS session. Whatever the CLI
prints to stdout is injected as context (plain stdout on exit 0). The CLI emits
nothing when there are no fresh peer lines — so on quiet turns this hook costs
zero context, which is the firehose constraint (delta-cursor, §4 of the spec).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL_CLI = os.path.join(HERE, "intent-journal.py")


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    session = payload.get("session_id") or os.environ.get("INTENT_SESSION_TAG") or "unknown-session"
    # Pass through whatever read-fresh prints; empty stdout = zero context cost.
    subprocess.run(
        [sys.executable, JOURNAL_CLI, "read-fresh", "--session", session],
        check=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
