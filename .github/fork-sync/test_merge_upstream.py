"""Exercise real Git merges in temporary repositories, without network access."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("merge_upstream.py")


class MergeUpstreamTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fork-sync-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.env = dict(
            os.environ,
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
        )
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Sync test")
        self.git("config", "user.email", "sync-test@example.invalid")
        self.git("config", "core.hooksPath", str(self.repo / ".git/no-hooks"))
        self.write("app.txt", "base\n")
        self.write(".github/workflows/ci.yml", "name: Original CI\n")
        self.commit("test: create upstream base")
        self.git("branch", "upstream")
        self.write(".github/workflows/sync-upstream.yml", "name: Sync upstream\n")
        self.write(".github/fork-sync/merge_upstream.py", SCRIPT.read_text())
        self.commit("test: configure fork automation")

    def git(self, *args, check=True):
        result = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env,
            capture_output=True, text=True,
        )
        if check and result.returncode:
            self.fail(result.stdout + result.stderr)
        return result.stdout.strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def sync(self, expect_success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "upstream"],
            cwd=self.repo, env=self.env, capture_output=True, text=True,
        )
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_repeated_sync_preserves_workflows_history_and_fork_changes(self):
        workflows = self.git("rev-parse", "HEAD:.github/workflows")
        self.write("fork-only.txt", "keep my changes\n")
        self.commit("test: keep a fork change")
        for version in (1, 2):
            self.git("switch", "upstream")
            self.write("app.txt", f"upstream version {version}\n")
            self.write(".github/workflows/ci.yml", f"name: Changed CI {version}\n")
            self.write(f".github/workflows/new-{version}.yml", "name: New workflow\n")
            upstream_sha = self.commit("test: update code and workflows")
            self.git("switch", "main")
            fork_sha = self.git("rev-parse", "HEAD")
            self.sync()
            self.assert_single_merge(fork_sha, upstream_sha)
            self.assertEqual((self.repo / "app.txt").read_text(), f"upstream version {version}\n")
            self.assertEqual((self.repo / "fork-only.txt").read_text(), "keep my changes\n")
            self.assertEqual(self.git("rev-parse", "HEAD:.github/workflows"), workflows)
            self.git("merge-base", "--is-ancestor", upstream_sha, "HEAD")
            self.assertEqual(self.git("status", "--porcelain"), "")
        synced_sha = self.git("rev-parse", "HEAD")
        self.sync()
        self.assertEqual(self.git("rev-parse", "HEAD"), synced_sha)

    def assert_single_merge(self, fork_sha, upstream_sha):
        self.assertEqual(
            self.git("rev-list", "--count", "HEAD", "--not", fork_sha, upstream_sha),
            "1",
            "Sync must add exactly one commit beyond the fork and upstream histories.",
        )
        self.assertEqual(
            self.git("show", "-s", "--format=%P", "HEAD"),
            f"{fork_sha} {upstream_sha}",
            "The merge must directly retain the original fork and upstream commits.",
        )

    def test_workflow_only_update_records_one_merge_without_tree_changes(self):
        fork_sha = self.git("rev-parse", "HEAD")
        fork_tree = self.git("rev-parse", "HEAD^{tree}")
        self.git("switch", "upstream")
        self.write(".github/workflows/ci.yml", "name: Changed upstream CI\n")
        self.write(".github/workflows/new.yml", "name: New upstream workflow\n")
        upstream_sha = self.commit("test: update only upstream workflows")
        self.git("switch", "main")
        self.sync()
        self.assert_single_merge(fork_sha, upstream_sha)
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), fork_tree)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_conflict_aborts_without_changing_fork(self):
        self.write("app.txt", "fork edit\n")
        fork_sha = self.commit("test: edit fork code")
        self.git("switch", "upstream")
        self.write("app.txt", "upstream edit\n")
        self.commit("test: edit upstream code")
        self.git("switch", "main")
        result = self.sync(expect_success=False)
        self.assertIn("app.txt", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), fork_sha)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.repo / ".git/MERGE_HEAD").exists())

    def test_dirty_tree_is_preserved(self):
        fork_sha = self.git("rev-parse", "HEAD")
        self.write("app.txt", "uncommitted work\n")
        self.write("untracked.txt", "untracked work\n")
        result = self.sync(expect_success=False)
        self.assertIn("uncommitted", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), fork_sha)
        self.assertEqual((self.repo / "app.txt").read_text(), "uncommitted work\n")
        self.assertEqual((self.repo / "untracked.txt").read_text(), "untracked work\n")

    def test_upstream_automation_namespace_collision_requires_review(self):
        fork_sha = self.git("rev-parse", "HEAD")
        self.git("switch", "upstream")
        self.write(".github/fork-sync/other.py", "# upstream-owned\n")
        self.commit("test: claim the automation namespace")
        self.git("switch", "main")
        result = self.sync(expect_success=False)
        self.assertIn("manual review", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), fork_sha)
        self.assertEqual(self.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
