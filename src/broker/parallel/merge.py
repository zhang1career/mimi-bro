"""
结果合并模块。

按依赖顺序 cherry-pick 各子任务的提交到主分支：
- 拓扑排序确定合并顺序
- 检测冲突并暂停
- 支持清理 worktree 和分支
- TTY 检测：非交互环境自动 fallback 保留冲突标记
"""

from __future__ import annotations

import os
import sys
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable


def is_interactive_tty() -> bool:
    """检测是否为交互式 TTY 环境"""
    return sys.stdin.isatty() and sys.stdout.isatty() and os.isatty(0)

from broker.parallel.analyzer import DependencyGraph
from broker.parallel.scheduler import (
    ParallelExecutionState,
    TaskStatus,
    get_topological_order,
)
from broker.parallel.worktree import GitWorktree


class MergeStatus(str, Enum):
    """合并状态"""

    PENDING = "pending"
    MERGED = "merged"
    CONFLICT = "conflict"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class MergeResult:
    """单个子任务的合并结果"""

    subtask_id: str
    branch: str
    status: MergeStatus
    commit_sha: str = ""
    conflict_files: list[str] = field(default_factory=list)
    error_message: str = ""


@dataclass
class MergeSummary:
    """合并摘要"""

    target_branch: str
    results: list[MergeResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    @property
    def all_merged_successfully(self) -> bool:
        """判断是否全部成功合并（无冲突、无失败）"""
        if not self.results:
            return False
        return all(
            r.status in (MergeStatus.MERGED, MergeStatus.SKIPPED)
            for r in self.results
        ) and not any(
            r.status in (MergeStatus.CONFLICT, MergeStatus.FAILED)
            for r in self.results
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_branch": self.target_branch,
            "results": [
                {
                    "subtask_id": r.subtask_id,
                    "branch": r.branch,
                    "status": r.status.value,
                    "commit_sha": r.commit_sha,
                    "conflict_files": r.conflict_files,
                    "error_message": r.error_message,
                }
                for r in self.results
            ],
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class ResultMerger:
    """结果合并器"""

    def __init__(
        self,
        workspace: Path,
        execution_state: ParallelExecutionState,
        dep_graph: DependencyGraph,
    ):
        self.workspace = Path(workspace).resolve()
        self.execution_state = execution_state
        self.dep_graph = dep_graph
        self.git = GitWorktree(workspace)
        self._conflict_callback: Callable[[MergeResult], bool] | None = None
        self._run_external_fn: Callable[[list[str], Path | None], int] | None = None

    def set_conflict_callback(
        self,
        callback: Callable[[MergeResult], bool],
    ) -> None:
        """
        设置冲突处理回调。

        回调返回 True 表示已解决冲突，继续合并；
        返回 False 表示暂停合并。
        """
        self._conflict_callback = callback

    def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """执行 git 命令"""
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd or self.workspace),
            capture_output=True,
            text=True,
            check=check,
        )

    def _get_current_branch(self) -> str:
        """获取当前分支"""
        result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        return result.stdout.strip()

    def _get_branch_commits(self, branch: str, base_branch: str) -> list[str]:
        """获取分支相对于 base 的提交"""
        try:
            result = self._run_git(
                ["log", f"{base_branch}..{branch}", "--format=%H", "--reverse"],
                check=False,
            )
            if result.returncode != 0:
                return []
            return [sha.strip() for sha in result.stdout.strip().split("\n") if sha.strip()]
        except Exception:
            return []

    def _cherry_pick_commit(self, commit_sha: str) -> tuple[bool, list[str]]:
        """
        Cherry-pick 单个提交。

        Returns:
            (success, conflict_files)
        """
        result = self._run_git(["cherry-pick", commit_sha], check=False)

        if result.returncode == 0:
            return True, []

        if "CONFLICT" in result.stdout or "conflict" in result.stderr.lower():
            status_result = self._run_git(["status", "--porcelain"], check=False)
            conflict_files = []
            for line in status_result.stdout.splitlines():
                if line.startswith("UU ") or line.startswith("AA ") or line.startswith("DD "):
                    conflict_files.append(line[3:].strip())
            return False, conflict_files

        return False, []

    def _abort_cherry_pick(self) -> None:
        """中止 cherry-pick"""
        self._run_git(["cherry-pick", "--abort"], check=False)

    def _continue_cherry_pick(self) -> bool:
        """继续 cherry-pick（冲突解决后）"""
        result = self._run_git(["cherry-pick", "--continue"], check=False)
        return result.returncode == 0

    def _get_conflict_files(self) -> list[str]:
        """获取当前冲突文件列表"""
        status_result = self._run_git(["status", "--porcelain"], check=False)
        conflict_files = []
        for line in status_result.stdout.splitlines():
            if line.startswith("UU ") or line.startswith("AA ") or line.startswith("DD "):
                conflict_files.append(line[3:].strip())
        return conflict_files

    def set_run_external_fn(
        self,
        fn: Callable[[list[str], Path | None], int],
    ) -> None:
        """
        设置外部命令运行函数。

        用于在 TUI 环境中暂停界面后运行外部命令（如 vimdiff）。
        """
        self._run_external_fn = fn

    def _run_mergetool(self) -> bool:
        """
        启动 git mergetool GUI。

        不捕获输出，让交互式工具（如 vimdiff）正常显示。
        在非 TTY 环境下不执行，直接返回 False。

        Returns:
            True 如果 mergetool 成功退出，False 否则
        """
        if not is_interactive_tty():
            return False

        args = ["git", "mergetool"]
        if self._run_external_fn is not None:
            exit_code = self._run_external_fn(args, self.workspace)
            return exit_code == 0
        else:
            result = subprocess.run(
                args,
                cwd=str(self.workspace),
                check=False,
            )
            return result.returncode == 0

    def _stage_resolved_files(self) -> bool:
        """
        暂存所有已解决的冲突文件。

        Returns:
            True 如果成功，False 否则
        """
        result = self._run_git(["add", "-u"], check=False)
        return result.returncode == 0

    def _resolve_conflicts_interactive(
        self,
        result: MergeResult,
        message_callback: Callable[[str], None] | None = None,
    ) -> bool:
        """
        交互式解决冲突：循环启动 mergetool 直到冲突解决或用户放弃。

        Args:
            result: 包含冲突信息的 MergeResult
            message_callback: 可选的消息回调函数

        Returns:
            True 如果冲突已解决，False 如果用户放弃
        """
        def msg(text: str) -> None:
            if message_callback:
                message_callback(text)

        max_attempts = 10
        for attempt in range(max_attempts):
            conflict_files = self._get_conflict_files()
            if not conflict_files:
                msg("All conflicts resolved.")
                return True

            msg(f"Conflict files ({len(conflict_files)}):")
            for f in conflict_files:
                msg(f"  - {f}")
            msg("")
            msg("Launching merge tool...")

            self._run_mergetool()
            self._stage_resolved_files()

            remaining = self._get_conflict_files()
            if not remaining:
                msg("All conflicts resolved.")
                return True

            msg(f"Still have {len(remaining)} unresolved conflicts.")

        msg(f"Exceeded max attempts ({max_attempts}), aborting.")
        return False

    def _get_head_sha(self) -> str:
        """获取 HEAD 的 SHA"""
        result = self._run_git(["rev-parse", "HEAD"])
        return result.stdout.strip()

    def merge(
        self,
        target_branch: str | None = None,
        auto_cleanup: bool = False,
        interactive: bool = False,
        message_callback: Callable[[str], None] | None = None,
    ) -> MergeSummary:
        """
        按拓扑顺序合并所有成功的子任务。

        Args:
            target_branch: 目标分支（默认当前分支）
            auto_cleanup: 合并后自动清理 worktree
            interactive: 是否启用交互式合并（遇到冲突时弹出 GUI mergetool）
            message_callback: 消息回调函数（用于向用户显示信息）

        Returns:
            MergeSummary 合并摘要
        """
        if target_branch is None:
            target_branch = self._get_current_branch()

        summary = MergeSummary(target_branch=target_branch)

        order = get_topological_order(self.dep_graph)

        self._run_git(["checkout", target_branch])

        def msg(text: str) -> None:
            if message_callback:
                message_callback(text)

        for subtask_id in order:
            subtask = self.execution_state.subtasks.get(subtask_id)
            if not subtask:
                continue

            if subtask.status != TaskStatus.SUCCESS:
                summary.results.append(MergeResult(
                    subtask_id=subtask_id,
                    branch=subtask.branch,
                    status=MergeStatus.SKIPPED,
                    error_message=f"Task status: {subtask.status.value}",
                ))
                continue

            if not subtask.branch:
                summary.results.append(MergeResult(
                    subtask_id=subtask_id,
                    branch="",
                    status=MergeStatus.SKIPPED,
                    error_message="No branch associated",
                ))
                continue

            result = self._merge_branch(subtask_id, subtask.branch, target_branch)
            summary.results.append(result)

            if result.status == MergeStatus.CONFLICT:
                if interactive:
                    if is_interactive_tty():
                        msg(f"Conflict in {subtask_id}:")
                        resolved = self._resolve_conflicts_interactive(result, message_callback)
                        if resolved:
                            self._stage_resolved_files()
                            if self._continue_cherry_pick():
                                result.status = MergeStatus.MERGED
                                result.commit_sha = self._get_head_sha()
                                result.conflict_files = []
                                msg(f"✓ {subtask_id} merged successfully")
                            else:
                                msg(f"✗ {subtask_id} cherry-pick continue failed")
                                self._abort_cherry_pick()
                                break
                        else:
                            msg(f"✗ {subtask_id} conflicts not resolved, aborting")
                            self._abort_cherry_pick()
                            break
                    else:
                        msg(f"⚠ Conflict in {subtask_id} (non-TTY, conflict markers preserved):")
                        for f in result.conflict_files:
                            msg(f"  - {f}")
                        msg("Run 'git mergetool' manually to resolve, then 'git cherry-pick --continue'")
                        break
                elif self._conflict_callback:
                    resolved = self._conflict_callback(result)
                    if resolved:
                        if self._continue_cherry_pick():
                            result.status = MergeStatus.MERGED
                            result.commit_sha = self._get_head_sha()
                            result.conflict_files = []
                        else:
                            self._abort_cherry_pick()
                            break
                    else:
                        self._abort_cherry_pick()
                        break
                else:
                    self._abort_cherry_pick()
                    break

            if result.status == MergeStatus.FAILED:
                break

        summary.finished_at = datetime.now()

        if auto_cleanup:
            self.cleanup_worktrees(force=True)

        return summary

    def _merge_branch(
        self,
        subtask_id: str,
        branch: str,
        target_branch: str,
    ) -> MergeResult:
        """合并单个分支"""
        commits = self._get_branch_commits(branch, target_branch)

        if not commits:
            return MergeResult(
                subtask_id=subtask_id,
                branch=branch,
                status=MergeStatus.SKIPPED,
                error_message="No new commits",
            )

        for commit in commits:
            success, conflict_files = self._cherry_pick_commit(commit)

            if not success:
                if conflict_files:
                    return MergeResult(
                        subtask_id=subtask_id,
                        branch=branch,
                        status=MergeStatus.CONFLICT,
                        conflict_files=conflict_files,
                    )
                else:
                    return MergeResult(
                        subtask_id=subtask_id,
                        branch=branch,
                        status=MergeStatus.FAILED,
                        error_message="Cherry-pick failed",
                    )

        return MergeResult(
            subtask_id=subtask_id,
            branch=branch,
            status=MergeStatus.MERGED,
            commit_sha=self._get_head_sha(),
        )

    def cleanup_worktrees(self, force: bool = False) -> tuple[list[str], list[str]]:
        """
        清理所有子任务的 worktree 和分支。

        Returns:
            (cleaned, errors) - 清理的路径列表和错误信息列表
        """
        cleaned = []
        errors = []

        for subtask in self.execution_state.subtasks.values():
            if not subtask.worktree_info:
                continue

            try:
                self.git.cleanup_worktree(
                    subtask.worktree_info,
                    delete_branch=True,
                    force=force,
                )
                cleaned.append(subtask.worktree_info.worktree_path)
            except Exception as e:
                errors.append(f"{subtask.id}: {e}")

        return cleaned, errors

    def get_merge_preview(self) -> list[dict[str, Any]]:
        """获取合并预览（不实际执行）"""
        order = get_topological_order(self.dep_graph)
        preview = []

        for subtask_id in order:
            subtask = self.execution_state.subtasks.get(subtask_id)
            if not subtask:
                continue

            item = {
                "subtask_id": subtask_id,
                "branch": subtask.branch,
                "status": subtask.status.value,
                "can_merge": subtask.status == TaskStatus.SUCCESS and bool(subtask.branch),
            }

            if item["can_merge"]:
                current_branch = self._get_current_branch()
                commits = self._get_branch_commits(subtask.branch, current_branch)
                item["commit_count"] = len(commits)

            preview.append(item)

        return preview


def format_merge_summary(summary: MergeSummary) -> str:
    """格式化合并摘要"""
    lines = ["=" * 60, f"合并结果 (目标分支: {summary.target_branch})", "=" * 60, ""]

    merged = [r for r in summary.results if r.status == MergeStatus.MERGED]
    skipped = [r for r in summary.results if r.status == MergeStatus.SKIPPED]
    conflicts = [r for r in summary.results if r.status == MergeStatus.CONFLICT]
    failed = [r for r in summary.results if r.status == MergeStatus.FAILED]

    if merged:
        lines.append(f"已合并 ({len(merged)}):")
        for r in merged:
            lines.append(f"  - {r.subtask_id} ({r.branch}) → {r.commit_sha[:8]}")
        lines.append("")

    if skipped:
        lines.append(f"已跳过 ({len(skipped)}):")
        for r in skipped:
            lines.append(f"  - {r.subtask_id}: {r.error_message}")
        lines.append("")

    if conflicts:
        lines.append(f"冲突 ({len(conflicts)}):")
        for r in conflicts:
            lines.append(f"  - {r.subtask_id} ({r.branch}):")
            for f in r.conflict_files:
                lines.append(f"      {f}")
        lines.append("")

    if failed:
        lines.append(f"失败 ({len(failed)}):")
        for r in failed:
            lines.append(f"  - {r.subtask_id}: {r.error_message}")
        lines.append("")

    total = len(summary.results)
    lines.append(f"总计: {len(merged)}/{total} 成功合并")

    return "\n".join(lines)
