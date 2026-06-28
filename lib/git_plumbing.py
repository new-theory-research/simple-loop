#!/usr/bin/env python3
"""Git plumbing operations for writing files directly to branches.

Implements commit_files_to_branch() — commits files to a named branch via
low-level git plumbing (hash-object, update-index, write-tree, commit-tree,
update-ref). The working tree is never touched.

This eliminates the bug class where git commit against the main worktree
lands on whatever branch the main worktree is checked out on.
"""

import os
import subprocess
import tempfile


class GitPlumbingError(Exception):
    """Raised when a git plumbing operation fails."""
    pass


def _git(project_dir, *args, env=None, check=True, timeout=30):
    """Run a git command in project_dir with optional extra env vars."""
    full_env = None
    if env:
        full_env = os.environ.copy()
        full_env.update(env)
    result = subprocess.run(
        ["git", "-C", project_dir] + list(args),
        capture_output=True, text=True, timeout=timeout,
        env=full_env,
    )
    if check and result.returncode != 0:
        raise GitPlumbingError(
            f"git {' '.join(str(a) for a in args)} failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    return result


def read_file_at_branch(project_dir, file_path, branch):
    """Return UTF-8 content of file_path at branch HEAD.

    Raises GitPlumbingError if the file or branch does not exist.
    """
    result = _git(project_dir, "show", f"{branch}:{file_path}", check=False)
    if result.returncode != 0:
        raise GitPlumbingError(
            f"read_file_at_branch: cannot read {file_path!r} at {branch!r}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _build_tree(project_dir, parent_tree_sha, files, index_env):
    """Seed a temp index from parent_tree_sha, apply file updates, return new tree sha."""
    _git(project_dir, "read-tree", parent_tree_sha, env=index_env)
    for disk_path, repo_path in files:
        blob_result = _git(project_dir, "hash-object", "-w", disk_path)
        blob_sha = blob_result.stdout.strip()
        _git(
            project_dir, "update-index", "--add", "--cacheinfo",
            f"100644,{blob_sha},{repo_path}",
            env=index_env,
        )
    return _git(project_dir, "write-tree", env=index_env).stdout.strip()


def commit_files_to_branch(project_dir, files, branch, message, remote=None, push=False):
    """Write files to branch via git plumbing. Working tree is never touched.

    Args:
        project_dir: absolute path to the git repository root.
        files: list of (disk_path, repo_path) tuples.
            disk_path — absolute path on disk; git hash-object reads from here.
            repo_path — path relative to repo root, used in the tree.
        branch: target branch name (must already exist as a local ref).
        message: commit message.
        remote: remote name (used only when push=True).
        push: if True, push branch to remote after committing.

    Returns:
        (commit_sha, did_commit) — did_commit is False when the resulting tree
        is identical to the parent (idempotent: no commit created, ref unchanged).

    Raises:
        GitPlumbingError on plumbing failures, including update-ref CAS misses
        after one retry.
    """
    if not files:
        raise GitPlumbingError("commit_files_to_branch: files list is empty")

    # ── 1. Resolve branch HEAD ────────────────────────────────────────
    ref_result = _git(
        project_dir, "rev-parse", f"refs/heads/{branch}", check=False
    )
    if ref_result.returncode != 0:
        raise GitPlumbingError(
            f"commit_files_to_branch: branch {branch!r} not found: "
            f"{ref_result.stderr.strip()}"
        )
    parent_sha = ref_result.stdout.strip()

    # ── 2. Get parent tree sha ────────────────────────────────────────
    parent_tree_sha = _git(
        project_dir, "rev-parse", f"{parent_sha}^{{tree}}"
    ).stdout.strip()

    # ── 3. Build new tree via temp index ─────────────────────────────
    tmp_fd, tmp_index = tempfile.mkstemp(prefix="git-plumbing-", suffix=".idx")
    os.close(tmp_fd)
    try:
        new_tree_sha = _build_tree(
            project_dir, parent_tree_sha, files,
            index_env={"GIT_INDEX_FILE": tmp_index},
        )
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass

    # ── 4. Idempotency check ──────────────────────────────────────────
    if new_tree_sha == parent_tree_sha:
        return (parent_sha, False)

    # ── 5. Create commit object ───────────────────────────────────────
    new_commit_sha = _git(
        project_dir, "commit-tree", new_tree_sha,
        "-p", parent_sha, "-m", message,
    ).stdout.strip()

    # ── 6. Advance branch ref (CAS) with one retry on race ───────────
    new_commit_sha = _advance_ref(
        project_dir, branch, new_commit_sha, parent_sha, files, message
    )

    # ── 7. Optional push ─────────────────────────────────────────────
    if push and remote:
        _git(project_dir, "push", remote, branch, timeout=60)

    return (new_commit_sha, True)


def _advance_ref(project_dir, branch, new_commit_sha, expected_parent_sha, files, message):
    """Update ref with compare-and-swap. Retries once on a ref-race.

    Returns the final commit sha (may differ from new_commit_sha if a
    concurrent write advanced the ref between our commit-tree and here).
    """
    result = _git(
        project_dir, "update-ref",
        f"refs/heads/{branch}", new_commit_sha, expected_parent_sha,
        check=False,
    )
    if result.returncode == 0:
        return new_commit_sha

    # Check whether the failure is a CAS miss (someone else advanced the ref)
    ref_result = _git(
        project_dir, "rev-parse", f"refs/heads/{branch}", check=False
    )
    if ref_result.returncode != 0:
        raise GitPlumbingError(
            f"update-ref failed for {branch!r}: {result.stderr.strip()}"
        )

    actual_parent = ref_result.stdout.strip()
    if actual_parent == expected_parent_sha:
        # Not a CAS miss — something else failed
        raise GitPlumbingError(
            f"update-ref failed for {branch!r} (not a race): {result.stderr.strip()}"
        )

    # Ref-race detected — retry once with fresh parent
    print(f"loop: dispatch state-write race — retrying (branch={branch})")

    actual_tree = _git(
        project_dir, "rev-parse", f"{actual_parent}^{{tree}}"
    ).stdout.strip()

    tmp_fd, tmp_index = tempfile.mkstemp(prefix="git-plumbing-retry-", suffix=".idx")
    os.close(tmp_fd)
    try:
        retry_tree = _build_tree(
            project_dir, actual_tree, files,
            index_env={"GIT_INDEX_FILE": tmp_index},
        )
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass

    if retry_tree == actual_tree:
        # After retry, content already present — truly idempotent
        return actual_parent

    retry_commit = _git(
        project_dir, "commit-tree", retry_tree,
        "-p", actual_parent, "-m", message,
    ).stdout.strip()

    retry_result = _git(
        project_dir, "update-ref",
        f"refs/heads/{branch}", retry_commit, actual_parent,
        check=False,
    )
    if retry_result.returncode != 0:
        raise GitPlumbingError(
            f"update-ref race retry failed for {branch!r}: "
            f"{retry_result.stderr.strip()}"
        )
    return retry_commit


def commit_file_to_branch(project_dir, disk_path, repo_path, branch, message,
                           remote=None, push=False):
    """Single-file convenience wrapper around commit_files_to_branch."""
    return commit_files_to_branch(
        project_dir, [(disk_path, repo_path)], branch, message, remote, push,
    )
