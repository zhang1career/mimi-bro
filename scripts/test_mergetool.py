#!/usr/bin/env python3
"""
测试 bro submit 的 mergetool 功能。

在 --source 指定的 git 仓库中：
1. 创建 test.json 并初始提交
2. 复用 GitWorktree 创建 worktree，分支 a 和 b 分别在各自 worktree 中修改 test.json（制造合并冲突）
3. 复用 auto_commit_changes（含 main_repo_path + branch）提交各 worktree 变更
4. 通过 ResultMerger 按拓扑顺序 merge 合并到原始分支
5. 发现冲突后弹出 git mergetool 窗口

覆盖：main_repo_path/branch、HEAD 切换、merge（非 cherry-pick）、原始分支作为目标。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Ensure broker package is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from broker.parallel.analyzer import DependencyEdge, DependencyGraph
from broker.parallel.merge import ResultMerger, format_merge_summary
from broker.parallel.scheduler import ParallelExecutionState, TaskStatus
from broker.parallel.worktree import GitWorktree, WorktreeInfo, auto_commit_changes
from broker.ui.driver import CLIDriver
from broker.ui.themes import DEFAULT_THEME


def setup_initial_commit(repo: Path) -> None:
    """在主仓库创建 test.json 并初始提交（有变更才提交）。"""
    test_file = repo / "test.json"
    content = json.dumps({"version": "main", "line": 1}, indent=2)
    test_file.write_text(content)
    subprocess.run(["git", "add", "test.json"], cwd=str(repo), capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "Add test.json"], cwd=str(repo), capture_output=True, text=True, check=False)


def setup_with_worktrees(repo: Path, run_id: str) -> tuple[WorktreeInfo, WorktreeInfo]:
    """
    复用 GitWorktree 创建 worktree，在各 worktree 中修改 test.json 并提交。
    使用 main_repo_path + branch 覆盖 broker 的提交逻辑（含 HEAD 切换）。
    返回 (WorktreeInfo_a, WorktreeInfo_b)。
    """
    git = GitWorktree(repo)
    # 路径与 broker 一致：../{repo_name}-worktrees/{branch}
    worktree_path_a = git.compute_worktree_path("a", session_id=run_id[:8])
    worktree_path_b = git.compute_worktree_path("b", session_id=run_id[:8])

    # 创建 worktree a（从 main 新建分支 a）
    info_a = git.create_worktree("a", worktree_path_a, create_branch=True)
    wt_a = Path(info_a.worktree_path)
    (wt_a / "test.json").write_text(json.dumps({"version": "main", "line": 2, "a_field": "value_a"}, indent=2))
    res_a = auto_commit_changes(
        wt_a,
        run_id=run_id,
        plan_id="a",
        objective="Branch a: modify line and add a_field",
        main_repo_path=repo,
        branch="a",
    )
    if not res_a.success:
        raise RuntimeError(f"auto_commit a failed: {res_a.message}")

    # 创建 worktree b（从 main 新建分支 b）
    info_b = git.create_worktree("b", worktree_path_b, create_branch=True)
    wt_b = Path(info_b.worktree_path)
    (wt_b / "test.json").write_text(json.dumps({"version": "main", "line": 3, "b_field": "value_b"}, indent=2))
    res_b = auto_commit_changes(
        wt_b,
        run_id=run_id,
        plan_id="b",
        objective="Branch b: modify line and add b_field",
        main_repo_path=repo,
        branch="b",
    )
    if not res_b.success:
        raise RuntimeError(f"auto_commit b failed: {res_b.message}")

    return info_a, info_b


def _get_current_branch(repo: Path) -> str:
    """获取主仓库当前分支。"""
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else "main"


def _ensure_on_main(repo: Path) -> None:
    """确保主仓库在 main 分支。"""
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, text=True, check=True)


def _cleanup_existing(repo: Path) -> None:
    """若 worktree/分支 a、b 已存在则清理（便于重复测试）。"""
    git = GitWorktree(repo)
    for branch in ("a", "b"):
        entry = git.find_worktree_by_branch(branch)
        if entry:
            git.remove_worktree(entry.path, force=True)
        if git.branch_exists(branch, include_remote=False):
            git.delete_branch(branch, force=True)
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, text=True, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test bro submit mergetool: TUI + git worktree + merge conflict + mergetool",
    )
    parser.add_argument(
        "-s", "--source",
        required=True,
        type=Path,
        help="Source path (must contain .git)",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Do not clean existing worktrees/branches a,b (fail if they exist)",
    )
    args = parser.parse_args()
    repo = args.source.resolve()

    if not (repo / ".git").exists():
        print(f"Error: {repo} is not a git repository (no .git)", file=sys.stderr)
        return 1

    run_id = "test-mergetool"

    if args.no_cleanup:
        # 存在则报错
        git = GitWorktree(repo)
        for branch in ("a", "b"):
            if git.find_worktree_by_branch(branch) or git.branch_exists(branch, include_remote=False):
                print(f"Error: branch or worktree '{branch}' exists. Use default (with cleanup) or remove manually.", file=sys.stderr)
                return 1
    else:
        _cleanup_existing(repo)

    _ensure_on_main(repo)
    # 确保 main 有 test.json 的初始提交
    setup_initial_commit(repo)
    # 合并目标为当前（原始）分支
    target_branch = _get_current_branch(repo)

    info_a, info_b = setup_with_worktrees(repo, run_id)

    # 构造 DependencyGraph: a -> b，先合并 a 再合并 b
    graph = DependencyGraph()
    graph.add_node("a")
    graph.add_node("b")
    graph.add_edge(DependencyEdge(from_task="a", to_task="b", reason="test"))

    from broker.model.plan_item import PlanItemType
    from broker.parallel.scheduler import SubtaskState

    state = ParallelExecutionState(run_id=run_id, worker_id="test")
    state.subtasks["a"] = SubtaskState(
        id="a",
        exec_type=PlanItemType.SKILL,
        status=TaskStatus.SUCCESS,
        branch="a",
        worktree_info=info_a,
    )
    state.subtasks["b"] = SubtaskState(
        id="b",
        exec_type=PlanItemType.SKILL,
        status=TaskStatus.SUCCESS,
        branch="b",
        worktree_info=info_b,
    )

    driver = CLIDriver(verbose=True, theme_name=DEFAULT_THEME)
    merger = ResultMerger(repo, state, graph)
    merger.set_run_external_fn(driver.run_external_command)

    def msg_cb(text: str) -> None:
        driver.on_console_message(text)

    def run_merge() -> None:
        # 合并前确保主仓库 worktree 干净（避免之前的 index 污染或残留）
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=False)
        subprocess.run(["git", "clean", "-fd"], cwd=str(repo), capture_output=True, text=True, check=False)
        summary = merger.merge(
            target_branch=target_branch,
            auto_cleanup=False,
            interactive=True,
            message_callback=msg_cb,
        )
        driver.on_console_message(format_merge_summary(summary))

    driver.run_with(run_merge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
