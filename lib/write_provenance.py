#!/usr/bin/env python3
"""Write PROVENANCE.json into the install dir at install time.

Called by install.sh. Records the source repo's HEAD SHA + install date so a
running daemon (daemon.sh startup log) and `loop status` can show which
commit is actually running, instead of assuming a merge to master means the
fix is live — the version-visibility primitive for issue #41. Prints
old-SHA -> new-SHA so an install is itself a receipt.

Usage: python3 write_provenance.py <source_repo_dir> <install_dir>
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def main():
    if len(sys.argv) != 3:
        print("Usage: write_provenance.py <source_repo_dir> <install_dir>", file=sys.stderr)
        sys.exit(1)
    source_repo, install_dir = sys.argv[1], sys.argv[2]

    try:
        sha = subprocess.run(
            ["git", "-C", source_repo, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except Exception:
        sha = "unknown"

    dirty = False
    try:
        status = subprocess.run(
            ["git", "-C", source_repo, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        dirty = bool(status.strip())
    except Exception:
        pass

    provenance_path = os.path.join(install_dir, "PROVENANCE.json")
    old_sha = None
    if os.path.exists(provenance_path):
        try:
            with open(provenance_path) as f:
                old_sha = json.load(f).get("source_commit")
        except Exception:
            old_sha = None

    os.makedirs(install_dir, exist_ok=True)
    data = {
        "source_repo": source_repo,
        "source_commit": sha,
        "source_dirty": dirty,
        "version": "0.2.0",
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp_path = provenance_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, provenance_path)

    if old_sha is None:
        print(f"  Version:   {sha} (first install)")
    elif old_sha != sha:
        print(f"  Version:   {old_sha} -> {sha}")
    else:
        print(f"  Version:   {sha} (unchanged)")


if __name__ == "__main__":
    main()
