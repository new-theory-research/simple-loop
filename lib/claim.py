"""brief-151: atomic cross-box brief claim via a pushed git ref.

A claim lives in committed/pushed git state — ``refs/claims/<brief_id>`` on the
shared remote — NOT in running.json (which is projected per-box from each box's
local runtime-events.jsonl and is therefore invisible across checkouts and
boxes; see the brief card "Motivation / receipts"). Two daemons sharing one
repo+lane race to create that ref; exactly one wins, and the loser creates no
branch/worktree.

The atomic primitive is ``git push`` with ``--force-with-lease=<ref>:`` — an
EMPTY expected value, which git enforces server-side as "the ref must not
already exist". Two subtleties make or break it (both proven under simulated
contention in lib/tests/test_lane_and_claim.py, golden ii):

  1. You MUST push a UNIQUE object, never the shared HEAD. Pushing a commit the
     remote ref already points at makes git short-circuit to "Everything
     up-to-date" (exit 0) WITHOUT evaluating the lease — so a second claimer
     reading exit 0 would wrongly believe it won (two ``True`` winners, the
     brief's top escalation trigger). ``_mint_claim_object`` fabricates a
     per-call commit so the pushed value always differs and the lease always
     fires.
  2. A lease rejection prints "stale info" and exits non-zero — that is the
     ONLY non-zero outcome we read as "already claimed" (return ``False``). ANY
     other failure (auth, network, bad refspec) is fail-loud: it raises, so
     dispatch aborts and never falls through to worktree creation on an
     unverified claim (engineering rule 10).
"""

import os
import socket
import subprocess
import time


CLAIM_REF_PREFIX = "refs/claims/"


def _ref_for(brief_id):
    return CLAIM_REF_PREFIX + brief_id


def _git(project_dir, *args, input_text=None, timeout=60):
    return subprocess.run(
        ["git", "-C", project_dir, *args],
        capture_output=True, text=True, timeout=timeout, input=input_text,
    )


def _mint_claim_object(project_dir, brief_id):
    """Create a UNIQUE commit object in ``project_dir``'s object store; return its
    sha. Uniqueness is essential: if the pushed value equalled the existing
    remote ref, git would short-circuit the lease and report a false-positive
    claim (see module docstring, point 1)."""
    # `git mktree` over empty stdin writes (and returns the sha of) the empty
    # tree — a stable base for a contentless claim commit.
    mk = _git(project_dir, "mktree", input_text="")
    if mk.returncode != 0:
        raise RuntimeError(
            f"claim_brief: could not build empty tree: {mk.stderr.strip()}"
        )
    empty_tree = mk.stdout.strip()
    # Nonce = host:pid:monotonic-ns — distinct per claimer and per attempt, so
    # the minted commit sha never collides with another daemon's claim object.
    nonce = f"{socket.gethostname()}:{os.getpid()}:{time.time_ns()}"
    # Explicit identity so commit-tree never fails with "empty ident" on a repo
    # lacking user.name/user.email config.
    r = _git(
        project_dir,
        "-c", "user.name=loop-claim",
        "-c", "user.email=loop-claim@localhost",
        "commit-tree", empty_tree, "-m", f"claim {brief_id} {nonce}",
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"claim_brief: could not mint claim object: {r.stderr.strip()}"
        )
    return r.stdout.strip()


def claim_brief(project_dir, brief_id, remote):
    """Atomically claim ``brief_id`` by creating ``refs/claims/<brief_id>`` on
    ``remote``.

    Returns ``True`` iff THIS call created the ref. Returns ``False`` if another
    daemon already created it (lease rejection — "stale info"). Raises on any
    other push failure (auth/network); callers MUST NOT create a worktree in
    that case.
    """
    ref = _ref_for(brief_id)
    sha = _mint_claim_object(project_dir, brief_id)
    # Empty expected value after the colon == "the remote ref must not exist".
    # The server enforces this atomically, so concurrent claimers can't both win.
    lease = f"--force-with-lease={ref}:"
    result = _git(project_dir, "push", lease, remote, f"{sha}:{ref}")
    if result.returncode == 0:
        return True
    combined = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    # Lease rejection: the ref already existed — another daemon claimed it first.
    if "stale info" in combined:
        return False
    # Anything else (auth, network, bad refspec) is fail-loud — never silently
    # treat an unverified push as "someone else has it" (engineering rule 10).
    raise RuntimeError(
        f"claim_brief: pushing {ref} to {remote} failed for a reason other than "
        f"contention (rc={result.returncode}): "
        f"{(result.stderr or result.stdout or '').strip()}"
    )


def release_claim(project_dir, brief_id, remote):
    """Delete ``refs/claims/<brief_id>`` on ``remote`` so a re-queued brief is
    re-claimable.

    Best-effort and non-fatal: a failed release leaks a ref (see the brief's
    Residue — a stale-claim reaper is a follow-up) but must never break a
    terminal-state transition. Returns ``True`` iff the delete push succeeded.
    """
    ref = _ref_for(brief_id)
    try:
        result = _git(project_dir, "push", remote, "--delete", ref)
    except Exception:
        return False
    return result.returncode == 0
