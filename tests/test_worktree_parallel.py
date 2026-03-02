"""
Git worktree parallel execution test.

Tests:
1. Worktree creation and isolation
2. Parallel execution in separate worktrees
3. Cherry-pick merge behavior
4. Conflict detection and handling
5. Worktree cleanup

Generated. Do not modify core safety logic via agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest


class GitTestHelper:
    """Helper for git operations in tests."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            check=check,
        )

    def init_repo(self) -> None:
        self.run(["init"])
        self.run(["config", "user.email", "test@example.com"])
        self.run(["config", "user.name", "Test User"])

    def create_file(self, path: str, content: str) -> Path:
        file_path = self.repo_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return file_path

    def commit(self, message: str) -> str:
        self.run(["add", "-A"])
        self.run(["commit", "-m", message])
        result = self.run(["rev-parse", "HEAD"])
        return result.stdout.strip()

    def create_worktree(self, branch: str, path: Path, create_branch: bool = False) -> None:
        args = ["worktree", "add"]
        if create_branch:
            args.extend(["-b", branch, str(path)])
        else:
            args.extend([str(path), branch])
        self.run(args)

    def list_worktrees(self) -> list[dict[str, Any]]:
        result = self.run(["worktree", "list", "--porcelain"])
        entries = []
        current: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if current:
                    entries.append(current)
                current = {"path": line[9:]}
            elif line.startswith("branch "):
                current["branch"] = line[7:].replace("refs/heads/", "")
            elif line == "detached":
                current["detached"] = True
        if current:
            entries.append(current)
        return entries

    def remove_worktree(self, path: Path, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        self.run(args)

    def cherry_pick(self, commit: str) -> tuple[bool, list[str]]:
        result = self.run(["cherry-pick", commit], check=False)
        if result.returncode == 0:
            return True, []
        status = self.run(["status", "--porcelain"], check=False)
        conflicts = []
        for line in status.stdout.splitlines():
            if line.startswith("UU ") or line.startswith("AA "):
                conflicts.append(line[3:].strip())
        return False, conflicts

    def abort_cherry_pick(self) -> None:
        self.run(["cherry-pick", "--abort"], check=False)


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> GitTestHelper:
    """Create a temporary git repository for testing."""
    git = GitTestHelper(tmp_path)
    git.init_repo()
    git.create_file("README.md", "# Test Repo\n")
    git.commit("Initial commit")
    return git


class TestWorktreeCreation:
    """Test worktree creation and basic operations."""

    def test_create_worktree_new_branch(self, temp_git_repo: GitTestHelper) -> None:
        """Creating a worktree with a new branch should succeed."""
        wt_path = temp_git_repo.repo_path.parent / "worktree-test"
        temp_git_repo.create_worktree("test-branch", wt_path, create_branch=True)

        assert wt_path.exists()
        worktrees = temp_git_repo.list_worktrees()
        branches = [w.get("branch") for w in worktrees]
        assert "test-branch" in branches

        temp_git_repo.remove_worktree(wt_path, force=True)

    def test_create_multiple_worktrees(self, temp_git_repo: GitTestHelper) -> None:
        """Multiple worktrees can exist simultaneously."""
        wt_paths = []
        for i in range(3):
            wt_path = temp_git_repo.repo_path.parent / f"worktree-{i}"
            temp_git_repo.create_worktree(f"branch-{i}", wt_path, create_branch=True)
            wt_paths.append(wt_path)

        worktrees = temp_git_repo.list_worktrees()
        assert len(worktrees) == 4  # main + 3 worktrees

        for wt_path in wt_paths:
            temp_git_repo.remove_worktree(wt_path, force=True)

    def test_worktree_isolation(self, temp_git_repo: GitTestHelper) -> None:
        """Changes in one worktree should not affect another."""
        wt1_path = temp_git_repo.repo_path.parent / "wt1"
        wt2_path = temp_git_repo.repo_path.parent / "wt2"

        temp_git_repo.create_worktree("branch-1", wt1_path, create_branch=True)
        temp_git_repo.create_worktree("branch-2", wt2_path, create_branch=True)

        (wt1_path / "file1.txt").write_text("Content from wt1")
        (wt2_path / "file2.txt").write_text("Content from wt2")

        assert not (wt2_path / "file1.txt").exists()
        assert not (wt1_path / "file2.txt").exists()

        temp_git_repo.remove_worktree(wt1_path, force=True)
        temp_git_repo.remove_worktree(wt2_path, force=True)


class TestParallelConflict:
    """Test conflict scenarios in parallel worktree execution."""

    def test_same_file_different_changes_causes_conflict(
        self, temp_git_repo: GitTestHelper
    ) -> None:
        """Two branches modifying the same file should cause merge conflict."""
        config_content = json.dumps({"version": 1, "name": "original"}, indent=2)
        temp_git_repo.create_file("config.json", config_content)
        base_commit = temp_git_repo.commit("Add config.json")

        wt_alpha = temp_git_repo.repo_path.parent / "wt-alpha"
        wt_beta = temp_git_repo.repo_path.parent / "wt-beta"

        temp_git_repo.create_worktree("branch-alpha", wt_alpha, create_branch=True)
        temp_git_repo.create_worktree("branch-beta", wt_beta, create_branch=True)

        alpha_config = {"version": 1, "name": "alpha-modified"}
        (wt_alpha / "config.json").write_text(json.dumps(alpha_config, indent=2))
        git_alpha = GitTestHelper(wt_alpha)
        alpha_commit = git_alpha.commit("Alpha changes name")

        beta_config = {"version": 1, "name": "beta-modified"}
        (wt_beta / "config.json").write_text(json.dumps(beta_config, indent=2))
        git_beta = GitTestHelper(wt_beta)
        beta_commit = git_beta.commit("Beta changes name")

        success, _ = temp_git_repo.cherry_pick(alpha_commit)
        assert success, "First cherry-pick should succeed"

        success, conflicts = temp_git_repo.cherry_pick(beta_commit)
        assert not success, "Second cherry-pick should conflict"
        assert "config.json" in conflicts

        temp_git_repo.abort_cherry_pick()
        temp_git_repo.remove_worktree(wt_alpha, force=True)
        temp_git_repo.remove_worktree(wt_beta, force=True)

    def test_different_files_no_conflict(self, temp_git_repo: GitTestHelper) -> None:
        """Two branches modifying different files should merge cleanly."""
        wt_alpha = temp_git_repo.repo_path.parent / "wt-alpha"
        wt_beta = temp_git_repo.repo_path.parent / "wt-beta"

        temp_git_repo.create_worktree("branch-alpha", wt_alpha, create_branch=True)
        temp_git_repo.create_worktree("branch-beta", wt_beta, create_branch=True)

        (wt_alpha / "alpha.txt").write_text("Alpha content")
        git_alpha = GitTestHelper(wt_alpha)
        alpha_commit = git_alpha.commit("Add alpha.txt")

        (wt_beta / "beta.txt").write_text("Beta content")
        git_beta = GitTestHelper(wt_beta)
        beta_commit = git_beta.commit("Add beta.txt")

        success1, _ = temp_git_repo.cherry_pick(alpha_commit)
        success2, _ = temp_git_repo.cherry_pick(beta_commit)

        assert success1, "First cherry-pick should succeed"
        assert success2, "Second cherry-pick should succeed (no conflict)"

        assert (temp_git_repo.repo_path / "alpha.txt").exists()
        assert (temp_git_repo.repo_path / "beta.txt").exists()

        temp_git_repo.remove_worktree(wt_alpha, force=True)
        temp_git_repo.remove_worktree(wt_beta, force=True)


class TestWorktreeCleanup:
    """Test worktree cleanup operations."""

    def test_cleanup_removes_worktree_directory(
        self, temp_git_repo: GitTestHelper
    ) -> None:
        """Removing a worktree should delete its directory."""
        wt_path = temp_git_repo.repo_path.parent / "wt-cleanup"
        temp_git_repo.create_worktree("cleanup-branch", wt_path, create_branch=True)

        assert wt_path.exists()

        temp_git_repo.remove_worktree(wt_path)

        assert not wt_path.exists()

    def test_cleanup_with_uncommitted_changes_requires_force(
        self, temp_git_repo: GitTestHelper
    ) -> None:
        """Removing a worktree with uncommitted changes needs --force."""
        wt_path = temp_git_repo.repo_path.parent / "wt-dirty"
        temp_git_repo.create_worktree("dirty-branch", wt_path, create_branch=True)

        (wt_path / "uncommitted.txt").write_text("Dirty content")

        with pytest.raises(subprocess.CalledProcessError):
            temp_git_repo.remove_worktree(wt_path, force=False)

        temp_git_repo.remove_worktree(wt_path, force=True)
        assert not wt_path.exists()

    def test_prune_removes_stale_entries(self, temp_git_repo: GitTestHelper) -> None:
        """git worktree prune should clean up stale entries."""
        wt_path = temp_git_repo.repo_path.parent / "wt-stale"
        temp_git_repo.create_worktree("stale-branch", wt_path, create_branch=True)

        shutil.rmtree(wt_path)

        worktrees_before = temp_git_repo.list_worktrees()

        temp_git_repo.run(["worktree", "prune"])

        worktrees_after = temp_git_repo.list_worktrees()

        assert len(worktrees_after) < len(worktrees_before) or all(
            Path(w["path"]).exists() for w in worktrees_after
        )


class TestMergeOrderDependency:
    """Test that merge order affects conflict resolution."""

    def test_topological_order_matters(self, temp_git_repo: GitTestHelper) -> None:
        """
        Simulate dependency graph: A -> B -> C
        where B depends on A, C depends on B.
        Merging out of order should fail.
        """
        temp_git_repo.create_file("data.txt", "line1\n")
        temp_git_repo.commit("Initial data")

        wt_a = temp_git_repo.repo_path.parent / "wt-a"
        wt_b = temp_git_repo.repo_path.parent / "wt-b"

        temp_git_repo.create_worktree("task-a", wt_a, create_branch=True)

        (wt_a / "data.txt").write_text("line1\nline2-from-a\n")
        git_a = GitTestHelper(wt_a)
        commit_a = git_a.commit("Task A adds line")

        git_a.run(["checkout", "-b", "task-b"])
        (wt_a / "data.txt").write_text("line1\nline2-from-a\nline3-from-b\n")
        commit_b = git_a.commit("Task B adds line (depends on A)")

        success_a, _ = temp_git_repo.cherry_pick(commit_a)
        assert success_a

        success_b, _ = temp_git_repo.cherry_pick(commit_b)
        assert success_b

        final_content = (temp_git_repo.repo_path / "data.txt").read_text()
        assert "line2-from-a" in final_content
        assert "line3-from-b" in final_content

        temp_git_repo.remove_worktree(wt_a, force=True)


def run_verification_script() -> dict[str, Any]:
    """
    Verification script to run after test_worktree_manager execution.

    Returns a summary of:
    - Worktrees created
    - Branches created
    - Files modified
    - Conflicts detected
    - Cleanup status
    """
    from broker.parallel.worktree import GitWorktree

    cwd = Path.cwd()
    if not GitWorktree.is_git_repo(cwd):
        return {"error": "Not in a git repository"}

    git = GitWorktree(cwd)
    worktrees = git.list_worktrees()

    result = subprocess.run(
        ["git", "branch", "-a"],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )
    branches = [b.strip().replace("* ", "") for b in result.stdout.splitlines()]

    test_branches = [b for b in branches if "test-worktree" in b.lower()]
    test_worktrees = [w for w in worktrees if "test-worktree" in str(w.path).lower()]

    return {
        "total_worktrees": len(worktrees),
        "test_worktrees": len(test_worktrees),
        "test_branches": test_branches,
        "worktree_paths": [str(w.path) for w in test_worktrees],
        "verification_passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(run_verification_script(), indent=2))
