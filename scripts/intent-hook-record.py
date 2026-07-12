#!/usr/bin/env python3
"""PostToolUse hook wrapper — appends an intent line for collision-prone acts.

Thin: parses Claude Code's PostToolUse stdin JSON, decides whether the tool
call is collision-prone (a `git push`, or a subagent dispatch), builds a
one-line summary, and shells out to intent-journal.py append. All real logic
lives in intent-journal.py; this only extracts + routes.

Fires the append for:
  - Bash commands containing "git push"  (a push changes shared refs)
  - Task / Agent dispatches               (a peer director starts parallel work)

Anything else: silent exit 0. Never fails the tool call.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL_CLI = os.path.join(HERE, "intent-journal.py")


def one_line(text, limit=140):
    text = " ".join((text or "").split())
    return text[:limit]


def classify(tool_name, tool_input):
    """Return (action, detail) if collision-prone, else None."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if "git push" in cmd:
            return ("git push", one_line(cmd))
        return None
    if tool_name in ("Task", "Agent"):
        subtype = tool_input.get("subagent_type", "subagent")
        desc = tool_input.get("description") or tool_input.get("prompt") or ""
        return ("dispatch", one_line(f"{subtype}: {desc}"))
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never break the tool call over a parse hiccup

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    result = classify(tool_name, tool_input)
    if result is None:
        return 0

    action, detail = result
    session = payload.get("session_id") or os.environ.get("INTENT_SESSION_TAG") or "unknown-session"
    subprocess.run(
        [sys.executable, JOURNAL_CLI, "append", "--session", session,
         "--action", action, "--detail", detail],
        check=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
