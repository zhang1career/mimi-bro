"""
并行调度器模块。

按依赖图调度子任务并行执行：
- 拓扑排序确定执行批次
- 同一批次的任务可并行（各自在独立 worktree）
- 跟踪执行状态，等待前置任务完成
- 失败策略：停止依赖失败任务的子任务，继续执行不受影响的任务
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from broker.parallel.analyzer import DependencyGraph
from broker.parallel.worktree import GitWorktree, WorktreeInfo
from broker.model.plan_item import PlanItemType, get_plan_item_type


class TaskStatus(str, Enum):
    """任务状态"""

    PENDING = "pending"
    WAITING = "waiting"  # 等待依赖完成
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # 因依赖失败而跳过


@dataclass
class SubtaskState:
    """子任务状态"""

    id: str
    exec_type: PlanItemType = PlanItemType.SKILL
    skill: str = ""
    requirement: str = ""
    objective: str = ""
    mode: str = "agent"
    scope: str = ""
    role: str = ""
    status: TaskStatus = TaskStatus.PENDING
    worktree_info: WorktreeInfo | None = None
    branch: str = ""
    exit_code: int | None = None
    error_message: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class ParallelExecutionState:
    """并行执行状态"""

    run_id: str
    worker_id: str
    subtasks: dict[str, SubtaskState] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "worker_id": self.worker_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "subtasks": {
                k: {
                    "id": v.id,
                    "exec_type": v.exec_type.value,
                    "skill": v.skill,
                    "requirement": v.requirement,
                    "objective": v.objective,
                    "mode": v.mode,
                    "scope": v.scope,
                    "role": v.role,
                    "status": v.status.value,
                    "branch": v.branch,
                    "worktree_path": v.worktree_info.worktree_path if v.worktree_info else None,
                    "exit_code": v.exit_code,
                    "error_message": v.error_message,
                    "started_at": v.started_at.isoformat() if v.started_at else None,
                    "finished_at": v.finished_at.isoformat() if v.finished_at else None,
                    "depends_on": v.depends_on,
                }
                for k, v in self.subtasks.items()
            },
        }

    def save(self, path: Path) -> None:
        self.updated_at = datetime.now()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> ParallelExecutionState:
        data = json.loads(path.read_text())
        state = cls(
            run_id=data["run_id"],
            worker_id=data.get("worker_id") or data.get("task_id", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )
        for k, v in data.get("subtasks", {}).items():
            exec_type_str = v.get("exec_type", "skill")
            try:
                exec_type = PlanItemType(exec_type_str)
            except ValueError:
                exec_type = PlanItemType.SKILL
            subtask = SubtaskState(
                id=v["id"],
                exec_type=exec_type,
                skill=v.get("skill", ""),
                requirement=v.get("requirement", ""),
                objective=v.get("objective", ""),
                mode=v.get("mode", "agent"),
                scope=v.get("scope", ""),
                role=v.get("role", ""),
                status=TaskStatus(v["status"]),
                branch=v.get("branch", ""),
                exit_code=v.get("exit_code"),
                error_message=v.get("error_message", ""),
                depends_on=v.get("depends_on", []),
            )
            if v.get("started_at"):
                subtask.started_at = datetime.fromisoformat(v["started_at"])
            if v.get("finished_at"):
                subtask.finished_at = datetime.fromisoformat(v["finished_at"])
            state.subtasks[k] = subtask
        return state


class ParallelScheduler:
    """并行调度器"""

    def __init__(
            self,
            workspace: Path,
            worker_id: str,
            run_id: str,
            dep_graph: DependencyGraph,
            breakdown: list[dict[str, Any]],
            max_workers: int = 4,
            state_workspace: Path | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.state_workspace = Path(state_workspace).resolve() if state_workspace else self.workspace
        self.worker_id = worker_id
        self.run_id = run_id
        self.dep_graph = dep_graph
        self.max_workers = max_workers

        self.state = ParallelExecutionState(run_id=run_id, worker_id=worker_id)
        self._init_subtasks(breakdown)

        self._lock = threading.Lock()
        self._status_callback: Callable[[SubtaskState], None] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[str, Future] = {}

    def _init_subtasks(self, breakdown: list[dict[str, Any]]) -> None:
        """初始化子任务状态"""
        for item in breakdown:
            if not isinstance(item, dict):
                continue
            subtask_id = item.get("id", "")
            if not subtask_id:
                continue

            deps = self.dep_graph.get_dependencies(subtask_id)
            exec_type = get_plan_item_type(item)
            self.state.subtasks[subtask_id] = SubtaskState(
                id=subtask_id,
                exec_type=exec_type,
                skill=item.get("skill", ""),
                requirement=item.get("requirement", ""),
                objective=item.get("objective", ""),
                mode=item.get("mode", "agent"),
                scope=item.get("scope", ""),
                role=item.get("role", ""),
                depends_on=deps,
            )

    def set_status_callback(self, callback: Callable[[SubtaskState], None]) -> None:
        """设置状态变更回调"""
        self._status_callback = callback

    def _notify_status(self, subtask: SubtaskState) -> None:
        """通知状态变更"""
        if self._status_callback:
            self._status_callback(subtask)

    def _get_state_path(self) -> Path:
        """获取状态文件路径（存放在 state_workspace/works/{run_id}/ 下）"""
        return self.state_workspace / "works" / self.run_id / "status.json"

    def save_state(self) -> None:
        """保存状态"""
        self.state.save(self._get_state_path())

    def _can_start(self, subtask_id: str) -> bool:
        """检查子任务是否可以开始"""
        subtask = self.state.subtasks.get(subtask_id)
        if not subtask or subtask.status != TaskStatus.PENDING:
            return False

        for dep_id in subtask.depends_on:
            dep = self.state.subtasks.get(dep_id)
            if not dep:
                continue
            if dep.status == TaskStatus.FAILED:
                return False
            if dep.status != TaskStatus.SUCCESS:
                return False

        return True

    def _should_skip(self, subtask_id: str) -> bool:
        """检查子任务是否应该跳过（因依赖失败）"""
        subtask = self.state.subtasks.get(subtask_id)
        if not subtask:
            return True

        for dep_id in subtask.depends_on:
            dep = self.state.subtasks.get(dep_id)
            if dep and dep.status in (TaskStatus.FAILED, TaskStatus.SKIPPED):
                return True

        return False

    def _get_ready_tasks(self) -> list[str]:
        """获取可以开始执行的任务"""
        ready = []
        for subtask_id, subtask in self.state.subtasks.items():
            if subtask.status == TaskStatus.PENDING:
                if self._should_skip(subtask_id):
                    with self._lock:
                        subtask.status = TaskStatus.SKIPPED
                        subtask.error_message = "依赖的任务失败或被跳过"
                        self._notify_status(subtask)
                elif self._can_start(subtask_id):
                    ready.append(subtask_id)
        return ready

    def _setup_worktree(self, subtask: SubtaskState) -> WorktreeInfo:
        """为子任务创建 worktree"""
        git = GitWorktree(self.workspace)
        role = subtask.role or 'worker'
        branch = f"{self.run_id}-{role}"
        worktree_path = git.compute_worktree_path(branch, session_id=self.run_id[:8])

        existing = git.find_worktree_by_branch(branch)
        if existing:
            return WorktreeInfo(
                branch=branch,
                main_repo_path=str(self.workspace),
                worktree_path=str(existing.path),
                managed_by_broker=False,
            )

        return git.create_worktree(branch, worktree_path, create_branch=True)

    def _run_subtask(
            self,
            subtask_id: str,
            invoke_func: Callable[[SubtaskState, Path], int],
    ) -> None:
        """执行单个子任务"""
        with self._lock:
            subtask = self.state.subtasks.get(subtask_id)
            if not subtask:
                return
            subtask.status = TaskStatus.RUNNING
            subtask.started_at = datetime.now()
            self._notify_status(subtask)
            self.save_state()

        try:
            worktree_info = self._setup_worktree(subtask)
            with self._lock:
                subtask.worktree_info = worktree_info
                subtask.branch = worktree_info.branch

            worktree_path = Path(worktree_info.worktree_path)

            exit_code = invoke_func(subtask, worktree_path)

            with self._lock:
                subtask.exit_code = exit_code
                if exit_code == 0:
                    subtask.status = TaskStatus.SUCCESS
                else:
                    subtask.status = TaskStatus.FAILED
                    if not subtask.error_message:
                        subtask.error_message = f"Exit code: {exit_code}"
                subtask.finished_at = datetime.now()
                self._notify_status(subtask)
                self.save_state()

        except Exception as e:
            with self._lock:
                subtask.status = TaskStatus.FAILED
                subtask.error_message = str(e)
                subtask.finished_at = datetime.now()
                self._notify_status(subtask)
                self.save_state()

    def run(
            self,
            invoke_func: Callable[[SubtaskState, Path], int],
    ) -> ParallelExecutionState:
        """
        执行所有子任务。

        Args:
            invoke_func: 子任务执行函数，签名 (subtask_state, worktree_path) -> exit_code

        Returns:
            最终执行状态
        """
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.save_state()

        try:
            while True:
                ready_tasks = self._get_ready_tasks()

                if not ready_tasks:
                    running = [
                        s for s in self.state.subtasks.values()
                        if s.status == TaskStatus.RUNNING
                    ]
                    if not running:
                        break
                    time.sleep(0.5)
                    continue

                for i, subtask_id in enumerate(ready_tasks):
                    if len(self._futures) >= self.max_workers:
                        break
                    if i > 0:
                        time.sleep(5.0)
                    future = self._executor.submit(
                        self._run_subtask, subtask_id, invoke_func
                    )
                    self._futures[subtask_id] = future

                completed = []
                for subtask_id, future in list(self._futures.items()):
                    if future.done():
                        completed.append(subtask_id)
                        try:
                            future.result()
                        except Exception:
                            pass

                for subtask_id in completed:
                    del self._futures[subtask_id]

                time.sleep(0.1)

        finally:
            self._executor.shutdown(wait=True)
            self.save_state()

        return self.state

    def get_summary(self) -> dict[str, Any]:
        """获取执行摘要"""
        total = len(self.state.subtasks)
        success = sum(1 for s in self.state.subtasks.values() if s.status == TaskStatus.SUCCESS)
        failed = sum(1 for s in self.state.subtasks.values() if s.status == TaskStatus.FAILED)
        skipped = sum(1 for s in self.state.subtasks.values() if s.status == TaskStatus.SKIPPED)
        pending = sum(1 for s in self.state.subtasks.values() if s.status == TaskStatus.PENDING)
        running = sum(1 for s in self.state.subtasks.values() if s.status == TaskStatus.RUNNING)

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "pending": pending,
            "running": running,
            "completed": success + failed + skipped,
            "all_success": failed == 0 and skipped == 0 and pending == 0 and running == 0,
        }


def get_topological_order(graph: DependencyGraph) -> list[str]:
    """获取拓扑排序后的任务顺序"""
    from broker.parallel.confirm import compute_parallel_groups

    batches = compute_parallel_groups(graph)
    return [task for batch in batches for task in batch]
