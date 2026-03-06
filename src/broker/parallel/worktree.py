"""
Git worktree 管理模块。

参考 agent-of-empires 的 src/git/mod.rs 实现，提供：
- 创建独立的 worktree 环境
- 列出现有 worktree
- 清理 worktree 和分支
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class GitError(Exception):
    """Git 操作异常基类"""


class NotAGitRepoError(GitError):
    """不是 git 仓库"""


class WorktreeExistsError(GitError):
    """Worktree 已存在"""


class WorktreeNotFoundError(GitError):
    """Worktree 不存在"""


class BranchNotFoundError(GitError):
    """分支不存在"""


class GitCommandError(GitError):
    """Git 命令执行失败"""


@dataclass
class WorktreeInfo:
    """Worktree 信息"""

    branch: str
    main_repo_path: str
    worktree_path: str
    managed_by_broker: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    cleanup_on_delete: bool = True


@dataclass
class WorktreeEntry:
    """Worktree 列表条目"""

    path: Path
    branch: str | None
    is_detached: bool = False


class GitWorktree:
    """Git worktree 操作封装"""

    DEFAULT_PATH_TEMPLATE = "../{repo_name}-worktrees/{branch}"

    def __init__(self, repo_path: Path | str):
        self.repo_path = Path(repo_path).resolve()
        if not self.is_git_repo(self.repo_path):
            raise NotAGitRepoError(f"Not a git repository: {self.repo_path}")

    @staticmethod
    def is_git_repo(path: Path) -> bool:
        """检查路径是否在 git 仓库中"""
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=str(path),
                capture_output=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return GitWorktree._find_main_repo_from_gitfile(path) is not None

    @staticmethod
    def _find_main_repo_from_gitfile(path: Path) -> Path | None:
        """从 .git 文件（linked worktree）找到主仓库"""
        current = path if path.is_dir() else path.parent
        while current != current.parent:
            git_entry = current / ".git"
            if git_entry.is_file():
                content = git_entry.read_text().strip()
                if content.startswith("gitdir:"):
                    gitdir = content[7:].strip()
                    gitdir_path = Path(gitdir)
                    if not gitdir_path.is_absolute():
                        gitdir_path = (current / gitdir_path).resolve()
                    if "worktrees" in str(gitdir_path):
                        worktrees_dir = gitdir_path.parent
                        if worktrees_dir.name == "worktrees":
                            git_dir = worktrees_dir.parent
                            if git_dir.name == ".git":
                                return git_dir.parent
                            return git_dir
                return None
            if git_entry.is_dir():
                return None
            current = current.parent
        return None

    @classmethod
    def find_main_repo(cls, path: Path) -> Path:
        """找到主仓库路径（处理 worktree 情况）"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(path),
                capture_output=True,
                text=True,
                check=True,
            )
            toplevel = Path(result.stdout.strip())
            git_common_result = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=str(path),
                capture_output=True,
                text=True,
                check=True,
            )
            git_common = git_common_result.stdout.strip()
            if git_common != ".git" and "worktrees" not in git_common:
                common_path = Path(git_common)
                if not common_path.is_absolute():
                    common_path = (toplevel / common_path).resolve()
                if common_path.name == ".git":
                    return common_path.parent
                return common_path
            return toplevel
        except subprocess.CalledProcessError:
            main_repo = cls._find_main_repo_from_gitfile(path)
            if main_repo:
                return main_repo
            raise NotAGitRepoError(f"Cannot find main repo from: {path}")

    def _run_git(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """执行 git 命令"""
        try:
            return subprocess.run(
                ["git"] + args,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                check=check,
            )
        except subprocess.CalledProcessError as e:
            raise GitCommandError(f"Git command failed: {e.stderr.strip()}") from e

    def prune_worktrees(self) -> None:
        """清理过期的 worktree 条目"""
        self._run_git(["worktree", "prune"])

    def get_current_branch(self) -> str | None:
        """获取当前分支名"""
        try:
            result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
            branch = result.stdout.strip()
            return None if branch == "HEAD" else branch
        except GitCommandError:
            return None

    def branch_exists(self, branch: str, include_remote: bool = True) -> bool:
        """检查分支是否存在"""
        result = self._run_git(["branch", "--list", branch], check=False)
        if result.stdout.strip():
            return True
        if include_remote:
            result = self._run_git(["branch", "-r", "--list", f"*/{branch}"], check=False)
            return bool(result.stdout.strip())
        return False

    def create_worktree(
            self,
            branch: str,
            path: Path | str,
            create_branch: bool = False,
    ) -> WorktreeInfo:
        """
        创建新的 worktree。

        Args:
            branch: 分支名
            path: worktree 路径
            create_branch: 是否创建新分支（基于当前 HEAD）

        Returns:
            WorktreeInfo 对象
        """
        path = Path(path).resolve()
        if path.exists():
            raise WorktreeExistsError(f"Worktree path already exists: {path}")

        self.prune_worktrees()

        if create_branch:
            self._run_git(["branch", branch])
        elif not self.branch_exists(branch):
            raise BranchNotFoundError(f"Branch not found: {branch}")

        try:
            self._run_git(["worktree", "add", str(path), branch])
        except GitCommandError as e:
            if create_branch:
                self._run_git(["branch", "-d", branch], check=False)
            raise e

        _convert_gitfile_to_relative(path)

        return WorktreeInfo(
            branch=branch,
            main_repo_path=str(self.repo_path),
            worktree_path=str(path),
            managed_by_broker=True,
            created_at=datetime.now(),
            cleanup_on_delete=True,
        )

    def list_worktrees(self) -> list[WorktreeEntry]:
        """列出所有 worktree"""
        result = self._run_git(["worktree", "list", "--porcelain"])
        entries = []
        current_path = None
        current_branch = None
        is_detached = False

        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if current_path:
                    entries.append(WorktreeEntry(
                        path=Path(current_path),
                        branch=current_branch,
                        is_detached=is_detached,
                    ))
                current_path = line[9:]
                current_branch = None
                is_detached = False
            elif line.startswith("branch "):
                ref = line[7:]
                if ref.startswith("refs/heads/"):
                    current_branch = ref[11:]
                else:
                    current_branch = ref
            elif line == "detached":
                is_detached = True

        if current_path:
            entries.append(WorktreeEntry(
                path=Path(current_path),
                branch=current_branch,
                is_detached=is_detached,
            ))

        return entries

    def find_worktree_by_branch(self, branch: str) -> WorktreeEntry | None:
        """按分支名查找 worktree"""
        for entry in self.list_worktrees():
            if entry.branch == branch:
                return entry
        return None

    def remove_worktree(self, path: Path | str, force: bool = False) -> None:
        """删除 worktree"""
        path = Path(path).resolve()
        if not path.exists():
            raise WorktreeNotFoundError(f"Worktree not found: {path}")

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))

        self._run_git(args)

    def delete_branch(self, branch: str, force: bool = False) -> None:
        """删除分支"""
        flag = "-D" if force else "-d"
        try:
            self._run_git(["branch", flag, branch])
        except GitCommandError:
            if not force:
                self._run_git(["branch", "-D", branch])
            else:
                raise BranchNotFoundError(f"Branch not found: {branch}")

    def compute_worktree_path(
            self,
            branch: str,
            template: str | None = None,
            session_id: str = "",
    ) -> Path:
        """
        根据模板计算 worktree 路径。

        模板变量：
        - {repo_name}: 仓库名称
        - {branch}: 分支名（/ 替换为 -）
        - {session_id}: 会话 ID
        """
        template = template or self.DEFAULT_PATH_TEMPLATE
        repo_name = self.repo_path.name
        safe_branch = re.sub(r"[/\\]", "-", branch)

        path_str = template.format(
            repo_name=repo_name,
            branch=safe_branch,
            session_id=session_id,
        )

        path = Path(path_str)
        if not path.is_absolute():
            path = (self.repo_path / path).resolve()

        return path

    def cleanup_worktree(
            self,
            info: WorktreeInfo,
            delete_branch: bool = True,
            force: bool = False,
    ) -> None:
        """清理 worktree 及其分支"""
        worktree_path = Path(info.worktree_path)
        if worktree_path.exists():
            self.remove_worktree(worktree_path, force=force)

        if delete_branch and info.branch:
            try:
                self.delete_branch(info.branch, force=force)
            except (GitCommandError, BranchNotFoundError):
                pass


@dataclass
class AutoCommitResult:
    """自动提交结果"""

    success: bool
    commit_sha: str = ""
    message: str = ""
    files_changed: int = 0
    skipped: bool = False


def auto_commit_changes(
    worktree_path: Path,
    run_id: str,
    plan_id: str,
    objective: str = "",
    requirement: str = "",
    max_message_length: int = 72,
    main_repo_path: Path | str | None = None,
) -> AutoCommitResult:
    """
    自动提交 worktree 中的所有变更。

    当 main_repo_path 存在时，使用主仓库的 git-dir 和 work-tree 显式指定上下文，
    避免 agent 在容器内破坏 worktree 的 .git（如 git init）导致提交到错误仓库。

    Args:
        worktree_path: worktree 路径
        run_id: 运行 ID
        plan_id: 计划 ID
        objective: 任务目标（用于 commit message）
        requirement: 任务需求（用于 commit message）
        max_message_length: commit message 第一行最大长度
        main_repo_path: 主仓库路径；若提供则强制使用主仓库上下文

    Returns:
        AutoCommitResult 对象
    """
    worktree_path = Path(worktree_path).resolve()
    main = Path(main_repo_path).resolve() if main_repo_path else None

    def run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        if main is not None:
            base = ["git", f"--git-dir={main / '.git'}", f"--work-tree={worktree_path}"]
        else:
            base = ["git"]
        return subprocess.run(
            base + args,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=check,
        )

    try:
        diff_result = run_git(["diff", "--quiet"], check=False)
        diff_staged_result = run_git(["diff", "--cached", "--quiet"], check=False)
        status_result = run_git(["status", "--porcelain"], check=False)

        has_unstaged = diff_result.returncode != 0
        has_staged = diff_staged_result.returncode != 0
        has_untracked = any(
            line.startswith("??") for line in status_result.stdout.splitlines()
        )

        if not has_unstaged and not has_staged and not has_untracked:
            return AutoCommitResult(
                success=True,
                skipped=True,
                message="No changes to commit",
            )

        run_git(["add", "-A"])

        desc = objective or requirement or "auto commit"
        desc = desc.replace("\n", " ").strip()
        prefix = f"[{run_id[:8]}][{plan_id}] "
        available_len = max_message_length - len(prefix)
        if len(desc) > available_len:
            desc = desc[: available_len - 3] + "..."
        commit_msg = f"{prefix}{desc}"

        run_git(["commit", "-m", commit_msg])

        sha_result = run_git(["rev-parse", "HEAD"])
        commit_sha = sha_result.stdout.strip()

        shortstat = run_git(["diff", "--shortstat", "HEAD~1", "HEAD"], check=False)
        files_changed = 0
        if shortstat.stdout:
            import re
            match = re.search(r"(\d+) file", shortstat.stdout)
            if match:
                files_changed = int(match.group(1))

        return AutoCommitResult(
            success=True,
            commit_sha=commit_sha,
            message=commit_msg,
            files_changed=files_changed,
        )

    except subprocess.CalledProcessError as e:
        return AutoCommitResult(
            success=False,
            message=f"Git error: {e.stderr.strip() if e.stderr else str(e)}",
        )
    except Exception as e:
        return AutoCommitResult(
            success=False,
            message=f"Error: {str(e)}",
        )


def _convert_gitfile_to_relative(worktree_path: Path) -> None:
    """将 worktree 的 .git 文件从绝对路径转为相对路径（便于 Docker 挂载）"""
    git_file = worktree_path / ".git"
    if not git_file.is_file():
        return

    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return

    gitdir = content[7:].strip()
    gitdir_path = Path(gitdir)
    if gitdir_path.is_relative_to(worktree_path):
        return

    try:
        relative = Path(gitdir).resolve().relative_to(worktree_path.resolve())
    except ValueError:
        import os
        relative = Path(os.path.relpath(gitdir, worktree_path))

    git_file.write_text(f"gitdir: {relative}\n")
