#!/usr/bin/env python3
"""Merge upstream code, keeping this fork's automation unchanged.

A temporary commit replaces upstream's workflow directory to calculate the
merged tree. The published merge uses the original upstream commit as its second
parent, so only one new commit enters the fork's history. New workflows never
reach the fork's active tree, and GITHUB_TOKEN does not need permission to edit
workflow files. Existing workflows are disabled separately by sync-upstream.yml.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


PRESERVED_PATHS = (".github/workflows", ".github/fork-sync")


def git(*args, env=None, check=True):
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, env=env
    )
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def merge_upstream(ref):
    if git("rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        raise RuntimeError("An existing merge must be completed or aborted first.")
    if git("status", "--porcelain", "--untracked-files=all").stdout:
        raise RuntimeError("Refusing to sync a working tree with uncommitted files.")

    fork_sha = git("rev-parse", "HEAD").stdout.strip()
    upstream_sha = git(
        "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
    ).stdout.strip()
    ancestry = git("merge-base", "--is-ancestor", upstream_sha, fork_sha, check=False)
    if ancestry.returncode == 0:
        print(f"Already synced through upstream {upstream_sha}.")
        return
    if ancestry.returncode != 1:
        raise RuntimeError(ancestry.stderr)
    git("merge-base", fork_sha, upstream_sha)

    # This namespace belongs to the fork. A future upstream collision requires
    # an explicit decision instead of silently replacing either side's scripts.
    if git("ls-tree", upstream_sha, "--", ".github/fork-sync").stdout:
        raise RuntimeError("Upstream now uses .github/fork-sync; manual review is required.")
    for path in PRESERVED_PATHS:
        if git("cat-file", "-t", f"{fork_sha}:{path}").stdout.strip() != "tree":
            raise RuntimeError(f"Expected a tracked directory: {path}")

    with tempfile.TemporaryDirectory(prefix="fork-sync-index-") as scratch:
        index_env = dict(os.environ, GIT_INDEX_FILE=str(Path(scratch) / "index"))
        git("read-tree", upstream_sha, env=index_env)
        # Only the temporary index changes; the real index and files stay intact.
        git("rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", *PRESERVED_PATHS, env=index_env)
        for path in PRESERVED_PATHS:
            git("read-tree", f"--prefix={path}/", f"{fork_sha}:{path}", env=index_env)
        tree = git("write-tree", env=index_env).stdout.strip()
        prepared_sha = git(
            "commit-tree", tree, "-p", upstream_sha,
            "-m", "ci: preserve fork automation during upstream sync",
        ).stdout.strip()

    try:
        result = git("merge", "--no-ff", "--no-commit", prepared_sha, check=False)
        if result.returncode:
            conflicts = git("diff", "--name-only", "--diff-filter=U").stdout
            raise RuntimeError(
                "Upstream merge failed; manual review is required.\n"
                + result.stdout + result.stderr + conflicts
            )
        git("diff", "--cached", "--exit-code", fork_sha, "--", *PRESERVED_PATHS)
        # Save the merged tree, then discard the temporary merge state. Only
        # the original fork and upstream commits become published parents.
        merged_tree = git("write-tree").stdout.strip()
        git("merge", "--abort")
        merged_sha = git(
            "commit-tree", merged_tree, "-p", fork_sha, "-p", upstream_sha,
            "-m", "chore: sync upstream",
            "-m", f"Upstream-Commit: {upstream_sha}",
        ).stdout.strip()
        git("merge", "--ff-only", merged_sha)
    except BaseException:
        if git("rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git("merge", "--abort")
        raise
    print(f"Synced upstream {upstream_sha}; fork workflows were preserved.")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise RuntimeError("Usage: merge_upstream.py <upstream-commit>")
        merge_upstream(sys.argv[1])
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
