"""
结果合并模块。

按依赖顺序 merge 各子任务分支到主分支（3-way merge，便于合并并行修改）：
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
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """执行 git 命令"""
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd or self.workspace),
            capture_output=True,
            text=True,
            check=check,
            env=run_env,
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

    def _merge_branch_git(self, branch: str) -> tuple[bool, list[str], str]:
        """
        将分支 merge 到当前 HEAD（3-way merge）。

        Returns:
            (success, conflict_files, error_detail)
        """
        result = self._run_git(["merge", branch, "--no-edit"], check=False)

        if result.returncode == 0:
            return True, [], ""

        if "CONFLICT" in result.stdout or "conflict" in (result.stderr or "").lower():
            conflict_files = self._get_conflict_files()
            return False, conflict_files, ""

        err = (result.stderr or "").strip() or (result.stdout or "").strip()
        return False, [], err

    def _abort_merge(self) -> None:
        """中止 merge"""
        self._run_git(["merge", "--abort"], check=False)

    def _continue_merge(self) -> bool:
        """继续 merge（冲突解决后）。git merge --continue 不接受 --no-edit，需用 GIT_EDITOR=true 禁用编辑器。"""
        no_editor = {"GIT_EDITOR": "true", "EDITOR": "true", "VISUAL": "true"}
        result = self._run_git(
            ["merge", "--continue"],
            check=False,
            env=no_editor,
        )
        return result.returncode == 0

    def _get_conflict_files(self) -> list[str]:
        """获取当前冲突（unmerged）文件列表。覆盖 porcelain 所有 unmerged 状态码：UU,AA,DD,AU,UA,UD,DU。"""
        status_result = self._run_git(["status", "--porcelain"], check=False)
        conflict_files = []
        for line in status_result.stdout.splitlines():
            if len(line) >= 3 and line[2] == " ":
                # 第一列或第二列为 A/D/U 表示 unmerged
                if line[0] in "ADU" or line[1] in "ADU":
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

        不捕获输出，让交互式工具（如 vimdiff、Beyond Compare）正常显示。
        在非 TTY 且无 _run_external_fn 时不执行。
        当 _run_external_fn 已设置（如 TUI），由 driver 挂起界面后在真实终端运行，可弹出 Beyond Compare 等 GUI。

        Returns:
            True 如果 mergetool 成功退出，False 否则
        """
        # TUI 模式下 _run_external_fn 会挂起界面并赋予真实终端，无需 isatty
        if not is_interactive_tty() and self._run_external_fn is None:
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
        暂存 mergetool 解决后的文件（采用用户选择的结果）。
        先对当前 unmerged 文件执行 git add，再 git add -u 兜底。
        """
        conflict_files = self._get_conflict_files()
        if conflict_files:
            # 显式 add 冲突文件，确保主仓库工作区中 mergetool 写回的内容被暂存
            result = self._run_git(["add", "--"] + conflict_files, check=False)
            if result.returncode != 0:
                return False
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

        def msg(text: str) -> None:
            if message_callback:
                message_callback(text)

        msg(f"[merge] workspace={self.workspace} target_branch={target_branch}")

        order = get_topological_order(self.dep_graph)

        self._run_git(["checkout", target_branch])

        # 主仓库 worktree 不干净会导致 merge 失败（如 "Your local changes would be overwritten"）
        # 自动 stash，合并完成后 pop；若 pop 有冲突则调用 mergetool 解决
        stashed = False
        status = self._run_git(["status", "--porcelain"], check=False)
        if status.returncode == 0 and status.stdout.strip():
            msg("Working tree has uncommitted changes; stashing before merge...")
            result = self._run_git(
                ["stash", "push", "-m", "bro: pre-merge stash"],
                check=False,
            )
            if result.returncode == 0:
                stashed = True
                msg("Stash created. Merge will run on clean tree.")
            else:
                msg("⚠ git stash failed; merge may fail. Consider: git stash")
                msg("  " + (result.stderr or result.stdout or "").strip()[:100])

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

            result = self._merge_branch(
                subtask_id, subtask.branch, target_branch, message_callback=message_callback
            )
            summary.results.append(result)

            if result.status == MergeStatus.CONFLICT:
                if interactive:
                    # TUI 模式下 _run_external_fn 已设置，driver 会挂起界面后运行 mergetool
                    can_interactive = is_interactive_tty() or self._run_external_fn is not None
                    if can_interactive:
                        msg(f"Conflict in {subtask_id}:")
                        resolved = self._resolve_conflicts_interactive(result, message_callback)
                        if resolved:
                            self._stage_resolved_files()
                            if self._continue_merge():
                                result.status = MergeStatus.MERGED
                                result.commit_sha = self._get_head_sha()
                                result.conflict_files = []
                                msg(f"✓ {subtask_id} merged successfully")
                            else:
                                msg(f"✗ {subtask_id} merge continue failed")
                                self._abort_merge()
                                break
                        else:
                            msg(f"✗ {subtask_id} conflicts not resolved, aborting")
                            self._abort_merge()
                            break
                    else:
                        msg(f"⚠ Conflict in {subtask_id} (no interactive terminal, conflict markers preserved):")
                        for f in result.conflict_files:
                            msg(f"  - {f}")
                        msg("Run 'git mergetool' manually to resolve, then 'git merge --continue'")
                        break
                elif self._conflict_callback:
                    resolved = self._conflict_callback(result)
                    if resolved:
                        if self._continue_merge():
                            result.status = MergeStatus.MERGED
                            result.commit_sha = self._get_head_sha()
                            result.conflict_files = []
                        else:
                            self._abort_merge()
                            break
                    else:
                        self._abort_merge()
                        break
                else:
                    self._abort_merge()
                    break

            if result.status == MergeStatus.FAILED:
                break

        summary.finished_at = datetime.now()

        for r in summary.results:
            msg(f"[merge] result {r.subtask_id}: {r.status.value}" + (f" ({r.error_message})" if r.error_message else ""))

        # 合并前若执行了 stash，此处恢复
        if stashed:
            msg("[merge] Restoring stashed changes...")
            pop_result = self._run_git(["stash", "pop"], check=False)
            if pop_result.returncode != 0:
                # stash pop 可能因冲突失败，尝试 mergetool 解决后 drop
                conflict_files = self._get_conflict_files()
                if conflict_files and (is_interactive_tty() or self._run_external_fn is not None):
                    msg("Stash pop had conflicts; launching mergetool...")
                    self._run_mergetool()
                    self._stage_resolved_files()
                    self._run_git(["stash", "drop"], check=False)
                    msg("Stash conflicts resolved.")
                else:
                    msg("⚠ Stash pop failed (conflicts). Run 'git mergetool' and 'git stash drop' manually.")

        if auto_cleanup:
            self.cleanup_worktrees(force=True)

        return summary

    def _merge_branch(
        self,
        subtask_id: str,
        branch: str,
        target_branch: str,
        message_callback: Callable[[str], None] | None = None,
    ) -> MergeResult:
        """merge 单个分支到 target_branch（3-way merge，合并并行修改）"""
        commits = self._get_branch_commits(branch, target_branch)

        def _msg(text: str) -> None:
            if message_callback:
                message_callback(text)

        _msg(f"[merge] {subtask_id} branch={branch} commits={len(commits)} (base={target_branch})")

        if not commits:
            return MergeResult(
                subtask_id=subtask_id,
                branch=branch,
                status=MergeStatus.SKIPPED,
                error_message="No new commits",
            )

        success, conflict_files, err_detail = self._merge_branch_git(branch)

        if not success:
            if conflict_files:
                return MergeResult(
                    subtask_id=subtask_id,
                    branch=branch,
                    status=MergeStatus.CONFLICT,
                    conflict_files=conflict_files,
                )
            err_msg = "Merge failed"
            if err_detail:
                err_msg += f": {err_detail[:200]}"
            return MergeResult(
                subtask_id=subtask_id,
                branch=branch,
                status=MergeStatus.FAILED,
                error_message=err_msg,
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

    # 分母为“实际参与合并”的任务数（排除 SKIPPED），与用户预期一致
    attempted = [r for r in summary.results if r.status != MergeStatus.SKIPPED]
    total_attempted = len(attempted)
    lines.append(f"总计: {len(merged)}/{total_attempted} 成功合并")

    return "\n".join(lines)
