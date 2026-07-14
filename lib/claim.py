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
  2. Contention shows up as one of TWO non-zero rejections, depending on race
     timing, and BOTH mean "the ref already exists — someone else claimed it"
     (return ``False``):
       - "stale info": the lease check itself fails because this clone's
         remote-tracking ref already shows the claim ref present (the
         sequential / already-fetched case).
       - "reference already exists" / "failed to update ref": under TRUE
         concurrency both pushes pass the empty-lease check optimistically (the
         ref looked absent when each read it), then git's server-side ref lock
         serializes them — the winner creates the ref and the loser fails at
         LOCK time with this message. This is the COMMON path for two daemons
         racing in real time (proven by golden ii's interleaved-threads test),
         not an edge case. It is still atomically exactly-one-winner.
     ANY other failure (auth, network, bad refspec) is fail-loud: it raises, so
     dispatch aborts and never falls through to worktree creation on an
     unverified claim (engineering rule 10).
"""

import os
import re
import socket
import subprocess
import time


CLAIM_REF_PREFIX = "refs/claims/"


def _ref_for(brief_id):
    return CLAIM_REF_PREFIX + brief_id


def claim_box():
    """Stable identity of THIS box for claim ownership: the hostname.

    brief-160: a claim now records which BOX minted it so reconciliation can ask
    the load-bearing question — "is this brief active on the box the claim
    names?" — and reap ONLY own-box orphans, never a remote box's living claim
    (the "never reap on local ignorance" law). Hostname (not pid) is the right
    grain: it is stable across daemon restarts, which is exactly the horizon
    startup reconciliation spans.
    """
    return socket.gethostname()


def _parse_box_from_subject(subject):
    """Extract the claiming box from a claim commit subject.

    New (brief-160) mint format embeds an explicit ``box=<host>`` token. Legacy
    (brief-151) claims carry only the nonce ``host:pid:ns`` — its leading field
    IS the host, so we recover it as a fallback rather than treating every
    pre-160 claim as unknown. Returns "" when neither is parseable (genuine
    local ignorance — the caller must NOT reap on it)."""
    m = re.search(r"\bbox=(\S+)", subject)
    if m:
        return m.group(1)
    # Legacy: "claim <brief> <host>:<pid>:<ns>" — pull the host off the nonce.
    m = re.search(r"\bclaim\s+\S+\s+([^\s:]+):\d+:\d+", subject)
    return m.group(1) if m else ""


def _git(project_dir, *args, input_text=None, timeout=60):
    return subprocess.run(
        ["git", "-C", project_dir, *args],
        capture_output=True, text=True, timeout=timeout, input=input_text,
    )


def _mint_claim_object(project_dir, brief_id, box=None):
    """Create a UNIQUE commit object in ``project_dir``'s object store; return its
    sha. Uniqueness is essential: if the pushed value equalled the existing
    remote ref, git would short-circuit the lease and report a false-positive
    claim (see module docstring, point 1).

    brief-160: the commit subject also records ``box=<host>`` so a later
    reconciliation can read which box owns the claim (``claim_owner``)."""
    # `git mktree` over empty stdin writes (and returns the sha of) the empty
    # tree — a stable base for a contentless claim commit.
    mk = _git(project_dir, "mktree", input_text="")
    if mk.returncode != 0:
        raise RuntimeError(
            f"claim_brief: could not build empty tree: {mk.stderr.strip()}"
        )
    empty_tree = mk.stdout.strip()
    box = box or claim_box()
    # Nonce = host:pid:monotonic-ns — distinct per claimer and per attempt, so
    # the minted commit sha never collides with another daemon's claim object.
    nonce = f"{socket.gethostname()}:{os.getpid()}:{time.time_ns()}"
    # Explicit identity so commit-tree never fails with "empty ident" on a repo
    # lacking user.name/user.email config. The `box=` token is the machine-
    # readable owner (brief-160); the nonce stays for uniqueness + legacy parse.
    r = _git(
        project_dir,
        "-c", "user.name=loop-claim",
        "-c", "user.email=loop-claim@localhost",
        "commit-tree", empty_tree, "-m", f"claim {brief_id} box={box} {nonce}",
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"claim_brief: could not mint claim object: {r.stderr.strip()}"
        )
    return r.stdout.strip()


def claim_brief(project_dir, brief_id, remote, box=None):
    """Atomically claim ``brief_id`` by creating ``refs/claims/<brief_id>`` on
    ``remote``.

    Returns ``True`` iff THIS call created the ref. Returns ``False`` if another
    daemon already created it (lease rejection — "stale info"). Raises on any
    other push failure (auth/network); callers MUST NOT create a worktree in
    that case.

    brief-160: the minted ref records the claiming ``box`` (default: this
    host) so reconciliation can tell own-box orphans from foreign claims.
    """
    ref = _ref_for(brief_id)
    sha = _mint_claim_object(project_dir, brief_id, box=box)
    # Empty expected value after the colon == "the remote ref must not exist".
    # The server enforces this atomically, so concurrent claimers can't both win.
    lease = f"--force-with-lease={ref}:"
    result = _git(project_dir, "push", lease, remote, f"{sha}:{ref}")
    if result.returncode == 0:
        return True
    combined = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    # Both rejections below mean the ref already existed — another daemon claimed
    # it first. "stale info" is the lease check failing (sequential/fetched
    # case); "reference already exists" / "failed to update ref" is the lock-time
    # failure under true concurrency, where both pushes passed the empty lease
    # optimistically and the server's ref lock let exactly one win (see module
    # docstring point 2; golden ii interleaved-threads test).
    if (
        "stale info" in combined
        or "reference already exists" in combined
        or "failed to update ref" in combined
    ):
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


# ─── brief-160: claim reconciliation (read owner, verify against live world) ──

def claim_owner(project_dir, brief_id, remote):
    """Return the box (hostname) recorded in ``refs/claims/<brief_id>`` on
    ``remote``.

    Returns:
      - ``None`` if no claim ref exists on the remote.
      - ``""``   if the ref exists but no box is parseable (a genuinely unknown
                 owner — callers MUST treat this as local ignorance and NOT reap).
      - ``"<host>"`` the claiming box otherwise.

    Fetches the single claim ref into the throwaway ``FETCH_HEAD`` so the commit
    subject (which carries ``box=``) can be read without disturbing any tracked
    ref.
    """
    ref = _ref_for(brief_id)
    ls = _git(project_dir, "ls-remote", remote, ref)
    if ls.returncode != 0 or not ls.stdout.strip():
        return None
    fetched = _git(project_dir, "fetch", remote, ref)
    if fetched.returncode != 0:
        return ""  # ref exists but unreadable — unknown owner, never "no claim"
    subj = _git(project_dir, "log", "-1", "--format=%s", "FETCH_HEAD")
    if subj.returncode != 0:
        return ""
    return _parse_box_from_subject(subj.stdout.strip())


def list_remote_claims(project_dir, remote):
    """Return ``{brief_id: sha}`` for every ``refs/claims/*`` on ``remote``.

    Empty dict when there are none or the remote is unreachable (reconciliation
    is a best-effort janitor — an unreachable remote is a no-op, never a raise).
    """
    ls = _git(project_dir, "ls-remote", remote, CLAIM_REF_PREFIX + "*")
    if ls.returncode != 0:
        return {}
    out = {}
    for line in ls.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith(CLAIM_REF_PREFIX):
            out[parts[1][len(CLAIM_REF_PREFIX):]] = parts[0]
    return out


def reconcile_claims(project_dir, remote, working_brief_ids, this_box=None, log=None):
    """Verify ``refs/claims/*`` against the live working set on THIS box and
    release own-box orphans loudly.

    ``working_brief_ids``: brief ids this box is actively working — active[] ∪
    pending_merges[]. This is the box's OWN ground truth; for an own-box claim
    it is authoritative (unlike a remote box's process state, which this box
    cannot see and must never guess at).

    Policy (the "never reap on local ignorance" law, brief-160 guard):
      - claim's brief is in the working set  → invariant holds, leave it.
      - own-box claim, brief NOT working     → orphan: release LOUDLY.
      - foreign-box claim                    → observe only, never reap
        (brief-167's registry will own cross-box liveness/heartbeat policy;
        this function draws the boundary and does not cross it).
      - unknown-box claim (``""``)           → observe only — an unparseable
        owner is local ignorance, not permission to reap.

    ``log`` is an optional callable invoked with each action dict so the caller
    can route it to log.jsonl / stewardship (fail-loud: no silent deletes).
    Returns the list of action dicts.
    """
    this_box = this_box or claim_box()
    working = set(working_brief_ids)
    actions = []
    for brief_id in sorted(list_remote_claims(project_dir, remote)):
        if brief_id in working:
            continue
        owner = claim_owner(project_dir, brief_id, remote)
        if owner is None:
            continue  # raced away between listing and reading — nothing to do
        if owner and owner != this_box:
            action = {"reason": "foreign_claim_observed",
                      "brief": brief_id, "box": owner}
        elif not owner:
            action = {"reason": "unknown_box_claim_observed", "brief": brief_id}
        else:
            released = release_claim(project_dir, brief_id, remote)
            action = {"reason": ("orphan_claim_released" if released
                                 else "orphan_claim_release_failed"),
                      "brief": brief_id, "box": this_box}
        actions.append(action)
        if log is not None:
            log(action)
    return actions
