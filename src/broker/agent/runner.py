from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from broker.agent.docker import run_container
from broker.agent.local import run_local
from broker.audit.skeleton import run_audit
from broker.audit.store import get_audit_summary_for_boost, save_audit_record
from broker.context import current_work_dir
from broker.decision.propose import propose
from broker.parallel.worktree import GitWorktree
from broker.model.plan_item import PlanItemType, get_plan_item_type
from broker.planner import plan_task
from broker.skill import get_invocation, load_skill_registry
from broker.skill.registry import set_console_callback
from broker.state.progress import load_progress, save_progress
from broker.task import _find_project_root, load_task, substitute_task
from broker.ui import NullDriver
from broker.ui.driver import DisplayDriver
from broker.utils.id_client import gen_run_id
from broker.utils.path_util import BRO_PROJECT_ROOT_ENV, PROJECT_ROOT
from broker.utils.prompt_util import CONFIRM_TIMEOUT, prompt_with_timeout
from broker.utils.file_lock import locked_append
from broker.utils.work_util import (
    BREAKDOWN_JSON,
    build_task_payload,
    build_work_dir,
    get_work_dir,
    write_run_meta,
    write_task_json,
)

BRO_PARENT_TASK_ID = "BRO_PARENT_TASK_ID"


def _check_parallel_conditions(
        source: Path,
        items: list[dict],
        display_driver: DisplayDriver | None = None,
) -> tuple[bool, str]:
    """
    检查是否满足 Git Worktree + Cherry-pick 并行执行的条件。

    条件：
    1. 源路径是 git 仓库（存在 .git）
    2. 有 2 个或更多可执行元素

    Args:
        source: 源路径（-s 参数的值）
        items: plans 或 breakdown.json 的元素列表
        display_driver: 显示驱动（用于输出警告）

    Returns:
        (can_parallel, reason) - 是否可以并行执行，以及原因
    """
    drv = display_driver or NullDriver()

    if len(items) < 2:
        return False, "只有 1 个元素，无需并行"

    src = Path(source).resolve()
    if not GitWorktree.is_git_repo(src):
        drv.on_console_message(f"警告: 源路径 {src} 不是 git 仓库，降级为串行执行")
        return False, "源路径不是 git 仓库"

    return True, f"满足条件: {len(items)} 个元素，源路径是 git 仓库"


def _running_file(parent_worker_id: str) -> Path:
    """Path to .state/running/{parent_worker_id}.jsonl for subtask work_dir discovery."""
    d = PROJECT_ROOT / ".state" / "running"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{parent_worker_id}.jsonl"


def _resolve_validation_path(validate_with: str, workspace: Path, local: bool = False) -> Path:
    """Resolve validate_with path. When local: use project root's workers/ first."""
    if not validate_with or not isinstance(validate_with, str):
        raise ValueError("validate_with must be a non-empty string")
    ws = Path(workspace).resolve()
    candidates = [ws / validate_with]
    if local or validate_with.startswith("workers/"):
        candidates.insert(0, _find_project_root(ws) / validate_with)
    candidates.append(ws.parent / validate_with)
    for base_path in candidates:
        p = base_path.resolve()
        if not p.suffix:
            p = p.with_suffix(".json")
        if p.exists():
            return p
    return (ws / validate_with).resolve()


def _run_sub_task(
        validate_with: str,
        workspace: Path,
        source: Path,
        local: bool,
        params: dict | None = None,
        verbose: bool = True,
        display_driver: DisplayDriver | None = None,
) -> int:
    """
    Run a sub-task (e.g. validation) directly by broker. No agent shell - avoids run_terminal_cmd timeout.
    Returns exit code (0 = success).
    """
    drv = display_driver or NullDriver()
    if verbose:
        drv.verbose(f"[broker] running validation sub-task: {validate_with}")
    ws = Path(workspace).resolve()
    src = Path(source).resolve() if source else ws
    path = _resolve_validation_path(validate_with, ws, local=local)
    try:
        task = load_task(str(path), workspace=ws)
    except (FileNotFoundError, ValueError) as e:
        if verbose:
            drv.verbose(f"[broker] validation task load failed: {e}")
        return 1
    if not isinstance(task, dict):
        if verbose:
            drv.verbose("[broker] validation task file did not contain a valid task object")
        return 1
    sub_params = dict(params) if params else {}
    sub_params.update(task.get("params") or {})
    task = substitute_task(task, sub_params)
    if "params" in task:
        del task["params"]

    dag = plan_task(task)
    result = propose(dag)
    plans = result["plans"]
    if not plans:
        return 1
    agents = plans[0]["agents"]

    if local:
        return _run_agents_local_internal(
            agents, ws, task, src, raise_on_failure=False, verbose=verbose, display_driver=drv
        )
    try:
        _run_agents_internal(agents, ws, task, src, verbose=verbose, display_driver=drv)
        return 0
    except (RuntimeError, SystemExit):
        return 1


def worker_id_from_task(task: dict) -> str:
    """Worker ID from task JSON (worker.id field). Used for progress key, work_dir naming, etc."""
    task_block = task.get("worker") or task.get("task") or task
    return task_block.get("id") or task.get("id") or "demo"


def _generate_subtask_id(skill_id: str, index: int) -> str:
    """Generate a unique subtask ID when not provided in breakdown.json."""
    return f"{skill_id}-{index}"


def _ensure_subtask_ids(items: list[dict]) -> list[dict]:
    """Ensure each item in breakdown has a unique 'id' field. Generate if missing."""
    result = []
    for i, item in enumerate(items):
        item_copy = dict(item)
        if not item_copy.get("id"):
            exec_type = get_plan_item_type(item_copy)
            if exec_type == PlanItemType.SKILL:
                base_id = item_copy.get("skill") or item_copy.get("skill_id") or "subtask"
            else:  # INLINE
                base_id = item_copy.get("role") or "inline"
            item_copy["id"] = _generate_subtask_id(base_id, i)
        result.append(item_copy)
    return result


def _build_subtask_command(
        item: dict,
        src_path: str,
        fresh_level: int = -1,
        local: bool = False,
        workspace_path: str | None = None,
        default_objective: str | None = None,
        worker_id: str | None = None,
        run_id: str | None = None,
        cursor_api_key: str | None = None,
        parent_run_id: str | None = None,
) -> str | None:
    """
    Build the command to execute a subtask based on its type.
    Returns command string or None if cannot execute.
    
    For breakdown subtasks: parent_run_id is passed (not run_id) to record parent-child mapping.
    For plans subtasks: run_id is passed to inherit parent's run_id.
    """
    exec_type = get_plan_item_type(item)
    fresh_flag = ""
    if fresh_level == 0:
        fresh_flag = " --fresh 0"
    elif fresh_level > 0:
        fresh_flag = f" --fresh {fresh_level - 1}"

    api_key_flag = ""
    if cursor_api_key:
        api_key_flag = f' --api-key "{cursor_api_key}"'

    if exec_type == PlanItemType.SKILL:
        skill_id = item.get("skill") or item.get("skill_id")
        if not skill_id:
            return None
        params = {k: str(v) for k, v in item.items() if k not in ("skill", "skill_id", "id", "deps")}
        inv = get_invocation(skill_id, src_path=src_path, **params)
        if inv is None or isinstance(inv, dict):
            return None
        if fresh_flag and "bro submit" in inv:
            inv = inv.rstrip() + fresh_flag
        if local and "--local" not in inv:
            inv = inv.rstrip() + " --local"
        if api_key_flag and "cursor" in inv.lower():
            inv = inv.rstrip() + api_key_flag
        if workspace_path and "bro submit" in inv:
            import re
            if re.search(r'\s-w\s', inv):
                inv = re.sub(r'\s-w\s+\S+', f' -w "{workspace_path}"', inv)
            else:
                inv = inv.replace("bro submit", f'bro submit -w "{workspace_path}"', 1)
        if run_id and "bro submit" in inv:
            inv = inv.rstrip() + f' --run-id "{run_id}"'
        if parent_run_id and "bro submit" in inv:
            inv = inv.rstrip() + f' --parent-run-id "{parent_run_id}"'
        return inv

    else:  # INLINE
        objective = item.get("objective") or item.get("requirement") or default_objective or ""
        if not objective:
            return None
        role = item.get("role", "worker")
        ws = workspace_path or src_path
        task_id = worker_id or item.get("id", role)
        cmd = f'bro run {role} --workspace "{ws}" --source "{src_path}" --objective "{objective}" --task-id "{task_id}"'
        if run_id:
            cmd += f' --run-id "{run_id}"'
        if parent_run_id:
            cmd += f' --parent-run-id "{parent_run_id}"'
        if local:
            cmd += " --local"
        if api_key_flag:
            cmd += api_key_flag
        return cmd


def _invoke_skill_refs(
        workspace: Path,
        work_dir: Path,
        task: dict,
        source: Path,
        verbose: bool = True,
        fresh_level: int = -1,
        local: bool = False,
        display_driver: DisplayDriver | None = None,
        auto: bool = False,
        parent_run_id: str | None = None,
        child_run_id: str | None = None,
) -> int:
    """
    After prep work, invoke subtasks from breakdown.json.
    Supports two execution types:
    - skill: call skill invocation (worker is a type of skill with invocation.type="bro_submit")
    - inline: directly execute with objective

    breakdown.json format:
    [{"id": "<subtask-id>", "skill": "...", "requirement": "..."}, ...]
    or [{"id": "<subtask-id>", "objective": "...", "mode": "agent"}, ...]

    Each item MUST have 'id' (auto-generated if missing for backward compatibility).
    If breakdown.json missing, for single-shot workers invoke each skill_ref once with task params.
    Returns last exit code.

    fresh_level: controls which levels to re-execute:
      -1: continue all (default)
       0: re-execute all
       n (n>0): continue levels <= n, re-execute levels > n
    auto: skip skill selection confirmation (default False)
    parent_run_id: parent task's run_id for tracing.
    child_run_id: unified run_id for all breakdown subtasks (passed as --run-id).
    """
    project_root, src, skill_refs_list, drv = _prepare_skill_execution_context(workspace, source, task, display_driver)
    src_path = str(src)
    breakdown_file, items = _load_breakdown_items(work_dir, drv, verbose)
    if not items and not skill_refs_list:
        return 0
    items = _prepare_breakdown_items(
        breakdown_file, items, skill_refs_list, task,
        display_driver=drv, auto=auto, verbose=verbose,
    )

    parent_worker_id = worker_id_from_task(task)
    last_code = 0

    task_block = task.get("worker") or task.get("task") or task
    default_objective = task_block.get("objective", "")

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        subtask_id = item.get("id")
        exec_type = get_plan_item_type(item)

        cmd = _build_subtask_command(
            item,
            src_path=src_path,
            fresh_level=fresh_level,
            local=local,
            workspace_path=str(workspace),
            default_objective=default_objective,
            run_id=child_run_id,
            parent_run_id=parent_run_id,
        )
        if cmd is None:
            if verbose:
                drv.verbose(f"[broker] subtask {subtask_id} has no valid command, skip")
            continue

        type_label = exec_type.value
        target = item.get("skill") or item.get("objective", "")[:30]
        drv.on_console_message(f"Type: {type_label}")
        drv.on_console_message(f"Assigned: {subtask_id} → {target}")
        scope_val = item.get("scope", "")
        drv.on_console_message(f"Scope: {scope_val if scope_val else '(not set)'}")

        if verbose:
            drv.verbose(f"[broker] invoking subtask {subtask_id} ({type_label}) ({i + 1}/{len(items)}): {cmd[:80]}...")
        drv.on_status(f"Invoking subtask {i + 1}/{len(items)}: {subtask_id} ({type_label})")

        try:
            env = os.environ.copy()
            env[BRO_PARENT_TASK_ID] = parent_worker_id
            env[BRO_PROJECT_ROOT_ENV] = str(PROJECT_ROOT)
            run_file = _running_file(parent_worker_id)
            if run_file.exists():
                run_file.unlink()
            proc = subprocess.Popen(cmd, shell=True, cwd=str(project_root), env=env, stdin=subprocess.DEVNULL)
            child_tasks = [{"subtask_id": subtask_id, "current": 1, "total": 1}]
            drv.on_progress(i + 1, len(items), child_tasks=child_tasks)
            seen_paths: set[str] = set()
            start = time.time()
            last_elapsed_emit = -1
            skill_timeout = int(os.environ.get("BROKER_SKILL_TIMEOUT", "7200"))  # 2h default
            while proc.poll() is None:
                if run_file.exists():
                    for line in run_file.read_text().splitlines():
                        if line.strip() and line not in seen_paths:
                            seen_paths.add(line)
                            try:
                                data = json.loads(line)
                                drv.on_log_paths([{
                                    "path": data.get("path", ""),
                                    "worker_id": data.get("worker_id") or data.get("task_id"),
                                    "role": data.get("role"),
                                    "parent_id": parent_worker_id,
                                }])
                            except json.JSONDecodeError:
                                pass
                elapsed = time.time() - start
                if elapsed >= skill_timeout:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    if verbose:
                        drv.verbose(f"[broker] subtask {subtask_id} timed out after {skill_timeout}s")
                    drv.on_console_message(f"Result: {subtask_id}: timeout ({skill_timeout}s)")
                    last_code = 124
                    break
                if int(elapsed) >= last_elapsed_emit + 2:
                    last_elapsed_emit = int(elapsed)
                    drv.on_progress(i + 1, len(items), child_tasks=child_tasks)
                    drv.on_status(f"Running subtask {subtask_id}...", elapsed_seconds=elapsed)
                time.sleep(0.3)
            else:
                last_code = proc.returncode
                drv.on_console_message(
                    f"Result: {subtask_id}: {'ok' if last_code == 0 else 'failed'} (exit {last_code})")
            drv.on_status("")
        except Exception as e:
            if verbose:
                drv.verbose(f"[broker] subtask invoke failed: {e}")
            last_code = 1
    return last_code


def _build_items_from_skill_refs(task, skill_refs_list):
    items = []
    params = task.get("params") or {}
    params.update((task.get("worker") or task.get("task") or task).get("params") or {})
    for skill_id in skill_refs_list:
        items.append({"skill": skill_id, **params})
    return items


def _load_breakdown_items(work_dir, driver, verbose):
    breakdown_file = work_dir / BREAKDOWN_JSON
    items: list[dict] = []
    if breakdown_file.exists():
        try:
            data = json.loads(breakdown_file.read_text())
            if isinstance(data, list):
                items = [x for x in data if isinstance(x, dict)]
        except (json.JSONDecodeError, TypeError, OSError):
            if verbose:
                driver.verbose(f"[broker] {BREAKDOWN_JSON} invalid or empty")
    return breakdown_file, items


def _prepare_skill_execution_context(workspace, source, task, display_driver):
    skill_refs = task.get("skill_refs")
    skill_refs_list: list[str] = []
    if skill_refs:
        if isinstance(skill_refs, list):
            skill_refs_list = skill_refs
        elif isinstance(skill_refs, str):
            skill_refs_list = [skill_refs]
    drv = display_driver or NullDriver()
    if skill_refs_list:
        try:
            set_console_callback(drv.on_console_message if display_driver else None)
            load_skill_registry(skill_refs=skill_refs_list)
        finally:
            set_console_callback(None)
    project_root = _find_project_root(workspace)
    src = Path(source).resolve()
    return project_root, src, skill_refs_list, drv


def _execute_parallel_items(
        items: list[dict],
        workspace: Path,
        source: Path,
        task: dict,
        project_root: Path,
        scheduler_workspace: Path,
        drv: DisplayDriver,
        verbose: bool = True,
        fresh_level: int = -1,
        max_workers: int = 4,
        local: bool = False,
        auto: bool = False,
        parent_run_id: str | None = None,
        child_run_id: str | None = None,
        interactive_merge: bool = True,
) -> int:
    """
    Core parallel execution logic using git worktree isolation.

    Args:
        items: subtask items to execute
        workspace: work directory for task.json and logs
        source: source code path
        task: task definition
        project_root: project root for env lookup
        scheduler_workspace: workspace path for scheduler and merger (may differ from workspace)
        drv: display driver
        verbose: verbose logging
        fresh_level: refresh level
        max_workers: max parallel workers
        local: local execution mode
        auto: skip confirmation
        parent_run_id: parent task's run_id (for breakdown subtasks).
        child_run_id: unified run_id for all breakdown subtasks. If set, passed as --run-id to subtasks.
        interactive_merge: whether to use interactive merge with GUI mergetool on conflicts

    Returns:
        exit code (0=success)
    """
    from broker.parallel.analyzer import DependencyAnalyzer
    from broker.parallel.confirm import format_dependency_graph
    from broker.parallel.scheduler import ParallelScheduler, SubtaskState
    from broker.parallel.merge import ResultMerger

    worker_id = worker_id_from_task(task)
    run_id = gen_run_id()
    src = Path(source).resolve()
    workspace = Path(workspace).resolve()

    if verbose:
        drv.verbose(f"[broker] Parallel execution: analyzing {len(items)} items...")

    analyzer = DependencyAnalyzer(workspace)

    run_state_dir = workspace / "works" / run_id
    run_state_dir.mkdir(parents=True, exist_ok=True)

    temp_breakdown = run_state_dir / "breakdown_temp.json"
    temp_breakdown.write_text(json.dumps(items, indent=2, ensure_ascii=False))

    graph = analyzer.analyze_breakdown(temp_breakdown, use_explicit_deps=True, analyze_code=False)

    deps_path = run_state_dir / "deps.json"
    graph.save(deps_path)

    graph_text = format_dependency_graph(graph)
    if verbose:
        drv.verbose(f"[broker] Found {len(graph.edges)} dependency relationships")
        drv.verbose(graph_text)

    confirmed_deps_path = run_state_dir / "confirmed_deps.json"
    if auto:
        confirmed_graph = graph
    else:
        confirmed_graph = drv.confirm_dependencies(graph, graph_text, confirmed_deps_path)

    confirmed_graph.save(confirmed_deps_path)

    scheduler = ParallelScheduler(
        workspace=scheduler_workspace,
        worker_id=worker_id,
        run_id=run_id,
        dep_graph=confirmed_graph,
        breakdown=items,
        max_workers=max_workers,
        state_workspace=workspace,
    )

    total_items = len(items)
    completed_count = [0]

    def status_callback(subtask: SubtaskState) -> None:
        status_str = subtask.status.value
        drv.on_console_message(f"Subtask {subtask.id}: {status_str}")
        if status_str in ("success", "failed", "skipped"):
            completed_count[0] += 1
            drv.on_progress(completed_count[0], total_items)

    scheduler.set_status_callback(status_callback)

    log_paths = []
    for item in items:
        subtask_id = item.get("id", "unknown")
        role = item.get("role") or subtask_id
        wd = build_work_dir(workspace, run_id, role)
        log_path = wd / "agent.log"
        log_paths.append({
            "path": str(log_path),
            "worker_id": worker_id,
            "role": role,
        })
    drv.on_log_paths(log_paths)
    drv.on_progress(0, total_items)

    task_block = task.get("worker") or task.get("task") or task
    default_objective = task_block.get("objective", "")

    from broker.utils.env_util import get_env_value
    cursor_api_key = (
            os.environ.get("CURSOR_API_KEY")
            or get_env_value(project_root, "CURSOR_API_KEY")
            or get_env_value(workspace.parent, "CURSOR_API_KEY")
            or get_env_value(src, "CURSOR_API_KEY")
    )
    if verbose:
        if cursor_api_key:
            drv.verbose(f"[broker] CURSOR_API_KEY loaded (length={len(cursor_api_key)})")
        else:
            drv.verbose("[broker] WARNING: CURSOR_API_KEY not found in env or .env files")

    def invoke_subtask(subtask: SubtaskState, worktree_path: Path) -> int:
        """Execute a single subtask based on its type."""
        _item = {
            "id": subtask.id,
            "skill": subtask.skill,
            "requirement": subtask.requirement,
            "objective": subtask.objective,
            "mode": subtask.mode,
            "scope": subtask.scope,
            "role": subtask.role,
        }
        effective_run_id = child_run_id if child_run_id else run_id
        effective_parent_run_id = parent_run_id if child_run_id else None
        cmd = _build_subtask_command(
            _item,
            src_path=str(worktree_path),
            fresh_level=fresh_level,
            local=local,
            workspace_path=str(workspace),
            default_objective=default_objective,
            worker_id=worker_id,
            run_id=effective_run_id,
            cursor_api_key=cursor_api_key,
            parent_run_id=effective_parent_run_id,
        )
        if cmd is None:
            drv.on_console_message(f"[ERROR] Cannot build command for subtask {subtask.id}: no objective or skill")
            return 1

        if verbose:
            drv.on_console_message(f"[CMD] {subtask.id}: {cmd[:100]}...")

        env = os.environ.copy()
        env[BRO_PARENT_TASK_ID] = worker_id
        env["BRO_PARALLEL_RUN_ID"] = run_id
        env[BRO_PROJECT_ROOT_ENV] = str(PROJECT_ROOT)

        if cursor_api_key:
            env["CURSOR_API_KEY"] = cursor_api_key

        try:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=str(worktree_path), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = proc.communicate()
            if proc.returncode != 0:
                stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
                stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
                error_details = f"Exit code: {proc.returncode}"
                if stderr_text:
                    error_details += f"\n\nstderr:\n{stderr_text[:1000]}"
                if stdout_text:
                    error_details += f"\n\nstdout (last 500 chars):\n{stdout_text[-500:]}"
                subtask.error_message = error_details
                drv.on_console_message(f"[ERROR] Subtask {subtask.id} failed (exit {proc.returncode})")
                if stderr_text:
                    drv.on_console_message(f"  stderr: {stderr_text[:200]}")
            return proc.returncode
        except (OSError, subprocess.SubprocessError) as e:
            import traceback
            subtask.error_message = f"Exception: {e}\n\n{traceback.format_exc()}"
            drv.on_console_message(f"[ERROR] Subtask {subtask.id} exception: {e}")
            return 1

    if verbose:
        drv.verbose(f"[broker] Starting parallel execution with {max_workers} workers...")

    state = scheduler.run(invoke_subtask)

    summary = scheduler.get_summary()
    if verbose:
        drv.verbose(f"[broker] Parallel execution complete: {summary['success']}/{summary['total']} succeeded")

    if summary["all_success"]:
        if verbose:
            drv.verbose("[broker] All subtasks succeeded, merging results...")

        merger = ResultMerger(scheduler_workspace, state, confirmed_graph)

        def message_callback(msg: str) -> None:
            drv.on_console_message(msg)

        merge_summary = merger.merge(
            interactive=interactive_merge,
            message_callback=message_callback,
        )

        merged_count = sum(1 for r in merge_summary.results if r.status.value == "merged")
        if verbose:
            drv.verbose(f"[broker] Merged {merged_count} subtasks")

        val_workspace = get_env_value(workspace, "BROKER_AUTO_CLEANUP_WORKTREE")
        val_project_root = get_env_value(project_root, "BROKER_AUTO_CLEANUP_WORKTREE")
        val_environ = os.environ.get("BROKER_AUTO_CLEANUP_WORKTREE")
        cleanup_env = val_workspace or val_project_root or val_environ or "1"
        auto_cleanup = cleanup_env.lower() not in ("0", "false", "no")

        if verbose:
            drv.verbose(f"[broker] BROKER_AUTO_CLEANUP_WORKTREE={cleanup_env}, auto_cleanup={auto_cleanup}")

        if auto_cleanup and merge_summary.all_merged_successfully:
            cleaned, errors = merger.cleanup_worktrees(force=True)
            if cleaned:
                drv.on_console_message(f"[cleanup] Removed {len(cleaned)} worktrees")
            for err in errors:
                drv.on_console_message(f"[cleanup] {err}")

        return 0
    else:
        if verbose:
            drv.verbose(f"[broker] {summary['failed']} subtasks failed, {summary['skipped']} skipped")
        return 1


def _prepare_breakdown_items(
        breakdown_file,
        items,
        skill_refs_list,
        task,
        display_driver: DisplayDriver | None = None,
        auto: bool = False,
        verbose: bool = True,
):
    """
    准备 breakdown items，包含三层 skill 选择机制：
    1. 规则匹配（方案 B）
    2. Agent 选择（方案 A）- 已在 items 中
    3. 用户确认（方案 C）- 60 秒限时

    Args:
        breakdown_file: breakdown.json 文件路径
        items: 原始 breakdown items
        skill_refs_list: skill ID 列表
        task: 任务定义（可能包含扩展的 skill_refs）
        display_driver: 显示驱动
        auto: 是否跳过确认
        verbose: 是否详细输出
    """
    drv = display_driver or NullDriver()

    if not items:
        items = _build_items_from_skill_refs(task, skill_refs_list)
    items = _ensure_subtask_ids(items)

    if len(skill_refs_list) <= 1:
        if breakdown_file.exists():
            breakdown_file.write_text(json.dumps(items, indent=2, ensure_ascii=False))
        return items

    from broker.skill.selector import (
        SkillInfo,
        apply_rule_selection,
        prepare_confirmation_items,
        apply_confirmation_result,
        validate_skill_selection,
    )
    from broker.skill.registry import get_skill_info

    skill_infos: list[SkillInfo] = []
    for sid in skill_refs_list:
        try:
            info = get_skill_info(sid)
            skill_infos.append(SkillInfo.from_dict(info))
        except Exception:
            skill_infos.append(SkillInfo(id=sid))

    def on_rule_match(item_id: str, skill_id: str, reason: str) -> None:
        if verbose:
            drv.on_console_message(f"Skill 规则匹配: [{item_id}] → {skill_id} ({reason})")

    rule_matched, need_confirm = apply_rule_selection(items, skill_infos, on_rule_match)

    if verbose:
        drv.on_console_message(
            f"Skill 选择: {len(rule_matched)} 规则匹配, {len(need_confirm)} 需确认"
        )

    if need_confirm and not auto:
        need_confirm = validate_skill_selection(need_confirm, skill_infos)
        confirm_items = prepare_confirmation_items(need_confirm, skill_infos)

        confirm_data = [
            {
                "item_id": ci.item_id,
                "requirement": ci.requirement,
                "current_skill": ci.current_skill,
                "available_skills": ci.available_skills,
                "source": ci.source,
            }
            for ci in confirm_items
        ]

        confirmed_skills = drv.confirm_skill_selection(confirm_data, timeout_seconds=60)
        need_confirm = apply_confirmation_result(need_confirm, confirmed_skills)

    final_items = rule_matched + need_confirm

    id_order = {item.get("id"): i for i, item in enumerate(items)}
    final_items.sort(key=lambda x: id_order.get(x.get("id"), 999))

    if breakdown_file.exists():
        breakdown_file.write_text(json.dumps(final_items, indent=2, ensure_ascii=False))

    return final_items


def _run_parallel_with_worktree(
        items: list[dict],
        workspace: Path,
        source: Path,
        task: dict,
        verbose: bool = True,
        fresh_level: int = -1,
        max_workers: int = 4,
        local: bool = False,
        auto: bool = False,
        display_driver: DisplayDriver | None = None,
        parent_run_id: str | None = None,
        child_run_id: str | None = None,
        interactive_merge: bool = True,
) -> int:
    """
    统一的 Git Worktree + Cherry-pick 并行执行函数。

    适用于：
    - plans 字段的多个元素（child_run_id=None，继承父 run_id）
    - breakdown.json 的多个元素（child_run_id 为统一生成的新 run_id）

    Args:
        interactive_merge: 是否启用交互式合并（遇到冲突时弹出 GUI mergetool）
    """
    drv = display_driver or NullDriver()

    skill_refs = task.get("skill_refs")
    skill_refs_list: list[str] = []
    if skill_refs:
        if isinstance(skill_refs, list):
            skill_refs_list = skill_refs
        elif isinstance(skill_refs, str):
            skill_refs_list = [skill_refs]

    if skill_refs_list:
        try:
            set_console_callback(drv.on_console_message if display_driver else None)
            load_skill_registry(skill_refs=skill_refs_list)
        finally:
            set_console_callback(None)

    items = _ensure_subtask_ids(items)
    project_root = _find_project_root(workspace)
    src = Path(source).resolve()

    return _execute_parallel_items(
        items=items,
        workspace=workspace,
        source=src,
        task=task,
        project_root=project_root,
        scheduler_workspace=src,
        drv=drv,
        verbose=verbose,
        fresh_level=fresh_level,
        max_workers=max_workers,
        local=local,
        auto=auto,
        parent_run_id=parent_run_id,
        child_run_id=child_run_id,
        interactive_merge=interactive_merge,
    )


def _emit_aggregation(
        workspace: Path,
        worker_id: str,
        run_id: str,
        agents: list[dict],
        display_driver: DisplayDriver,
) -> None:
    """Emit results aggregation via display driver: on_result per agent."""
    for agent in agents:
        role = agent.get("role", "?")
        work_dir = build_work_dir(workspace, run_id, role)
        result_file = work_dir / "result.json"
        status = "no result.json"
        exit_code: int | None = None
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
                status = data.get("status", "?")
                exit_code = data.get("code")
            except (json.JSONDecodeError, OSError, TypeError):
                status = "read error"
        display_driver.on_result(worker_id, role, status, work_dir, exit_code=exit_code)


def _read_previous_round_summary(workspace: Path, work_dir: Path | None = None) -> str:
    """Read result.json to summarize previous round for next round context."""
    if work_dir is None:
        work_dir = get_work_dir(workspace, check_conflict=False)
    result_file = work_dir / "result.json"
    if not result_file.exists():
        return ""
    try:
        data = json.loads(result_file.read_text())
        status = data.get("status", "unknown")
        code = data.get("code", "")
        return f"Previous round: status={status}, exit_code={code}, log=agent.log (same dir). Continue from here."
    except (json.JSONDecodeError, OSError):
        return "Previous round completed. Continue from here."


def get_steps_for_agent(task: dict, agent_id: str) -> list[dict] | None:
    """
    Get steps for a specific agent from task["steps"] dict.
    Returns None if no steps defined for this agent.
    
    New schema: steps is a dict {plan_id: [step, step, ...]}
    """
    steps = task.get("steps")
    if not steps or not isinstance(steps, dict):
        return None
    agent_steps = steps.get(agent_id)
    if agent_steps and isinstance(agent_steps, list):
        return agent_steps
    return None


def normalize_step(step, task: dict | None = None) -> dict:
    """
    Normalize step to {objective, validate_with, validate_only, expected_results}.

    Supports {{worker.objective}} placeholder in step objective - will be replaced
    with the worker's objective value. This allows steps to reference the overall
    task context without duplicating it in instructions.
    """
    worker_objective = ""
    expected = None
    if task and isinstance(task, dict):
        task_block = task.get("worker") or task.get("task") or task
        if isinstance(task_block, dict):
            worker_objective = task_block.get("objective", "") or ""
            expected = task_block.get("expected_results")

    def substitute_worker_objective(text: str) -> str:
        if not text or "{{worker.objective}}" not in text:
            return text
        return text.replace("{{worker.objective}}", worker_objective)

    if isinstance(step, str):
        return {
            "objective": substitute_worker_objective(step),
            "validate_with": None,
            "validate_only": False,
            "expected_results": expected,
        }
    if not isinstance(step, dict):
        return {
            "objective": "",
            "validate_with": None,
            "validate_only": False,
            "expected_results": None,
        }
    step_expected = step.get("expected_results")
    if step_expected is None:
        step_expected = expected
    return {
        "objective": substitute_worker_objective(step.get("objective", "") or ""),
        "validate_with": step.get("validate_with"),
        "validate_only": bool(step.get("validate_only", False)),
        "expected_results": step_expected,
    }


def _prompt_continue_next_step(round_i: int, total: int, verbose: bool = True) -> bool:
    """Human-in-loop: ask whether to continue to next step. Returns True to continue, False to stop.
    Timeout (BROKER_CONFIRM_TIMEOUT) defaults to continue (agree)."""
    if verbose:
        prompt = f"[broker] Round {round_i + 1} of {total} completed. Continue to next step? [Y/n] ({CONFIRM_TIMEOUT}s): "
    else:
        prompt = f"Continue to next step? [Y/n] ({CONFIRM_TIMEOUT}s): "
    reply = prompt_with_timeout(prompt, default="y", timeout_sec=CONFIRM_TIMEOUT).lower()
    return reply in ("", "y", "yes")


def _prompt_escalation_accept_retry(verbose: bool = True) -> str:
    """Post-run audit escalated: human can accept (continue) or retry. Returns 'accept' or 'retry'.
    No timeout: waits indefinitely (high-risk decision)."""
    if verbose:
        print("[broker] Audit escalated. Accept and continue? [a]ccept / [r]etry: ", end="", flush=True)
    else:
        print("Accept and continue? [a/r]: ", end="", flush=True)
    try:
        reply = input().strip().lower()
        return "accept" if reply in ("a", "accept") else "retry"
    except (EOFError, KeyboardInterrupt):
        return "retry"


def _run_agent_steps_docker(
        agent: dict,
        steps: list[dict],
        workspace: Path,
        task: dict,
        source: Path,
        worker_id: str,
        run_id: str,
        auto: bool,
        verbose: bool,
        display_driver: DisplayDriver,
) -> Path:
    """
    Run a single agent through its steps via Docker.
    Returns the work_dir for this agent.
    """
    drv = display_driver
    work_dir = get_work_dir(workspace, worker_id, run_id, agent["role"])
    _emit_subtask_log_path(agent, worker_id, work_dir)
    write_run_meta(work_dir, run_id, worker_id, agent["role"])
    work_dir_rel = f"works/{run_id}/{agent['role']}"
    total_steps = len(steps)
    round_i = 0
    while round_i < total_steps:
        progress = load_progress(worker_id, run_id, subtask_id=agent["id"])
        completed = set(progress["completed_step_indices"]) if progress else set()
        retry_counts = dict((progress or {}).get("retry_counts") or {})

        if round_i in completed:
            round_i += 1
            continue
        step = steps[round_i]
        s = normalize_step(step, task=task)
        current_work_dir.set(work_dir)
        if s["validate_only"] and s["validate_with"]:
            code = _run_sub_task(
                s["validate_with"], workspace, source, local=False,
                params={}, verbose=verbose, display_driver=drv,
            )
            if code != 0:
                raise RuntimeError(f"Validation task {s['validate_with']} failed (exit {code})")
            completed.add(round_i)
            save_progress(worker_id, run_id, list(completed), retry_counts=retry_counts, subtask_id=agent["id"])
            if not auto and round_i < total_steps - 1 and not _prompt_continue_next_step(round_i, total_steps,
                                                                                         verbose=verbose):
                break
            round_i += 1
            continue

        attempt = _prepare_round_payload(agent, retry_counts, round_i, s, task, worker_id, total_steps,
                                         work_dir, workspace)
        current_work_dir.set(work_dir)
        run_container(
            agent["id"],
            agent["role"],
            worker_id,
            workspace,
            work_dir_rel=work_dir_rel,
            source=source,
        )
        audit_record, last_result = _run_round_audit(attempt, round_i, s, work_dir)
        if audit_record["escalated"]:
            action = _handle_audit_escalation(audit_record, worker_id, verbose)
            if action == "retry":
                save_progress(worker_id, run_id, list(completed), last_round_result=last_result,
                              retry_counts=retry_counts, subtask_id=agent["id"])
                continue
        else:
            save_audit_record(worker_id, audit_record)

        completed.add(round_i)
        save_progress(worker_id, run_id, list(completed), last_round_result=last_result, retry_counts=retry_counts,
                      subtask_id=agent["id"])
        if s["validate_with"]:
            code = _run_sub_task(
                s["validate_with"], workspace, source, local=False,
                params={}, verbose=verbose, display_driver=drv,
            )
            if code != 0:
                raise RuntimeError(f"Validation task {s['validate_with']} failed (exit {code})")
        if not auto and round_i < total_steps - 1 and not _prompt_continue_next_step(round_i, total_steps,
                                                                                     verbose=verbose):
            break
        round_i += 1
    return work_dir


def _run_agents_internal(
        agents: list[dict],
        workspace: Path,
        task: dict,
        source: Path,
        batches: list[list[str]] | None = None,
        auto: bool = False,
        verbose: bool = True,
        fresh_level: int = -1,
        parallel: bool = False,
        max_workers: int = 4,
        display_driver: DisplayDriver | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
) -> None:
    """
    Internal: run agents via Docker. Raises on failure. batches = parallel-ready level order.
    auto: skip step confirmation.
    fresh_level: -1=continue all, 0=re-execute all, n>0=continue levels<=n.
    parallel: execute subtasks in parallel using git worktree isolation.
    run_id: use specified run_id instead of generating a new one.
    parent_run_id: parent task's run_id for tracing (used by breakdown subtasks).
    
    Steps execution:
    - If task["steps"] is a dict {agent_id: [step, ...]}, each agent runs its own steps.
    - If no steps, agents run in dependency order without multi-round.
    """
    drv = display_driver or NullDriver()
    worker_id = worker_id_from_task(task)
    run_id = run_id or gen_run_id()
    
    if parent_run_id and parent_run_id != run_id:
        from broker.utils.work_util import set_parent_run_mapping
        set_parent_run_mapping(workspace, run_id, parent_run_id)
        if verbose:
            drv.verbose(f"[broker] Recorded parent mapping: {run_id} -> parent={parent_run_id}")
    
    steps_dict = task.get("steps")
    has_steps = steps_dict and isinstance(steps_dict, dict) and len(steps_dict) > 0
    agent_by_id = {a["id"]: a for a in agents}

    if has_steps:
        if not agents:
            return
        run_list = _order_agents_by_batches(agent_by_id, agents, batches)
        last_work_dir = None
        for agent in run_list:
            agent_steps = get_steps_for_agent(task, agent["id"])
            if agent_steps:
                last_work_dir = _run_agent_steps_docker(
                    agent, agent_steps, workspace, task, source,
                    worker_id, run_id, auto, verbose, drv
                )
        if last_work_dir:
            breakdown_items = _read_breakdown_from_dir(last_work_dir)
            if breakdown_items:
                if parallel and len(breakdown_items) >= 2:
                    _execute_breakdown_items_docker(breakdown_items, workspace, last_work_dir, source, task, drv,
                                                    {
                                                        'auto': auto,
                                                        'fresh_level': fresh_level,
                                                        'max_workers': max_workers,
                                                        'parallel': parallel,
                                                        'verbose': verbose,
                                                        'parent_run_id': run_id,
                                                    })

        if verbose:
            _emit_aggregation(workspace, worker_id, run_id, agents, drv)
    else:
        run_list = _order_agents_by_batches(agent_by_id, agents, batches)
        audit_context = _build_audit_context(task, worker_id)

        plans_items = [{"id": a["id"], **{k: v for k, v in a.items() if k != "id"}} for a in agents]

        if parallel and len(plans_items) >= 2:
            can_parallel, reason = _check_parallel_conditions(source, plans_items, drv)
            if can_parallel:
                if verbose:
                    drv.verbose(f"[broker] Plans parallel execution (Docker): {reason}")
                code = _run_parallel_with_worktree(
                    plans_items, workspace, source, task,
                    verbose=verbose, fresh_level=fresh_level, max_workers=max_workers,
                    local=False, auto=auto, display_driver=drv,
                )
                if code != 0:
                    raise RuntimeError(f"Parallel execution failed (exit {code})")
            else:
                if verbose:
                    drv.verbose(f"[broker] Fallback to serial execution (Docker): {reason}")
                _run_agents_docker_serial(
                    run_list, workspace, task, worker_id, run_id, audit_context, source, drv
                )
        else:
            _run_agents_docker_serial(
                run_list, workspace, task, worker_id, run_id, audit_context, source, drv
            )

        _auto_audit(drv, run_id, run_list, task, worker_id, verbose, workspace)

        if run_list:
            breakdown_items, first_work_dir = _load_first_agent_breakdown(run_id, run_list, worker_id, workspace)
            if breakdown_items:
                _execute_breakdown_items_docker(breakdown_items, workspace, first_work_dir, source, task, drv,
                                                {
                                                    'auto': auto,
                                                    'fresh_level': fresh_level,
                                                    'max_workers': max_workers,
                                                    'parallel': parallel,
                                                    'verbose': verbose,
                                                    'parent_run_id': run_id,
                                                })

        if verbose:
            _emit_aggregation(workspace, worker_id, run_id, agents, drv)


def _execute_breakdown_items_docker(breakdown_items, workspace, work_dir, source, task, driver, options=None):
    if options is None:
        options = {}

    verbose = options.get('verbose', False)
    parent_run_id = options.get('parent_run_id')

    from broker.utils.work_util import add_child_run_mapping
    child_run_id = gen_run_id()
    if parent_run_id:
        add_child_run_mapping(workspace, parent_run_id, child_run_id)
        if verbose:
            driver.verbose(f"[broker] Generated child_run_id={child_run_id} for breakdown (parent={parent_run_id})")

    parallel = options.get('parallel', False)
    if parallel and len(breakdown_items) >= 2:
        can_parallel, reason = _check_parallel_conditions(source, breakdown_items, driver)
        if can_parallel:
            if verbose:
                driver.verbose(f"[broker] Breakdown parallel execution (Docker): {reason}")
            _run_parallel_with_worktree(breakdown_items, workspace, source, task,
                                        verbose=verbose,
                                        fresh_level=options['fresh_level'],
                                        max_workers=options['max_workers'],
                                        local=False,
                                        auto=options['auto'],
                                        display_driver=driver,
                                        parent_run_id=parent_run_id,
                                        child_run_id=child_run_id)
        else:
            if verbose:
                driver.verbose(f"[broker] Breakdown serial execution (Docker): {reason}")
            _invoke_skill_refs(workspace, work_dir, task, source,
                               verbose=verbose,
                               fresh_level=options['fresh_level'],
                               display_driver=driver,
                               auto=options.get('auto', False),
                               parent_run_id=parent_run_id,
                               child_run_id=child_run_id)
    else:
        _invoke_skill_refs(workspace, work_dir, task, source,
                           verbose=verbose,
                           fresh_level=options['fresh_level'],
                           display_driver=driver,
                           child_run_id=child_run_id,
                           parent_run_id=parent_run_id,
                           auto=options.get('auto', False))


def _run_agents_docker_serial(
        run_list: list[dict],
        workspace: Path,
        task: dict,
        worker_id: str,
        run_id: str,
        audit_context: str,
        source: Path,
        drv: DisplayDriver,
) -> None:
    """串行执行 agents 列表（Docker 模式）"""
    if run_list:
        paths = []
        for a in run_list:
            wd = build_work_dir(workspace, run_id, a["role"])
            p = str(wd / "agent.log")
            paths.append({"path": p, "worker_id": worker_id, "role": a["role"]})
        drv.on_log_paths(paths)
        drv.on_progress(0, len(run_list))

    for i, agent in enumerate(run_list):
        work_dir = get_work_dir(workspace, worker_id, run_id, agent["role"])
        _emit_subtask_log_path(agent, worker_id, work_dir)
        write_run_meta(work_dir, run_id, worker_id, agent["role"])
        work_dir_rel = f"works/{run_id}/{agent['role']}"
        payload = build_task_payload(
            task, agent,
            audit_context=audit_context or None,
            work_dir=work_dir,
        )
        write_task_json(workspace, payload, work_dir)
        current_work_dir.set(work_dir)
        run_container(
            agent["id"],
            agent["role"],
            worker_id,
            workspace,
            work_dir_rel=work_dir_rel,
            source=source,
        )
        drv.on_progress(i + 1, len(run_list))


def _run_round_audit(attempt, round_i, s, work_dir):
    last_result = None
    result_file = work_dir / "result.json"
    if result_file.exists():
        try:
            last_result = json.loads(result_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    audit_record = run_audit(
        work_dir, round_i, last_result, attempt,
        criteria=None, expected_results=s.get("expected_results"),
    )
    return audit_record, last_result


def _emit_subtask_log_path(agent, worker_id, work_dir):
    parent_id = os.environ.get(BRO_PARENT_TASK_ID)
    if parent_id:
        line = json.dumps({"path": str(work_dir / "agent.log"), "worker_id": worker_id, "role": agent["role"]})
        locked_append(_running_file(parent_id), line)


def _prepare_round_payload(first_agent, retry_counts, round_i, s, task, worker_id, total_steps, work_dir, workspace):
    attempt = retry_counts.get(str(round_i), 0) + 1
    retry_counts[str(round_i)] = attempt
    round_objective = s["objective"]
    round_context = f"Round {round_i + 1} of {total_steps}."
    if round_i > 0:
        round_context += " " + _read_previous_round_summary(workspace, work_dir)
    audit_context = ""
    if (task.get("worker") or task.get("task") or task).get("type") == "bootstrap":
        audit_context = get_audit_summary_for_boost(exclude_worker_id=worker_id) or ""
    payload = build_task_payload(
        task, first_agent,
        round_objective=round_objective,
        round_context=round_context,
        audit_context=audit_context or None,
        work_dir=work_dir,
    )
    write_task_json(workspace, payload, work_dir)
    return attempt


def _handle_audit_escalation(audit_record, worker_id, verbose):
    action = _prompt_escalation_accept_retry(verbose=verbose)
    _apply_human_audit_conclusion(action, audit_record, worker_id)
    return action


def _apply_human_audit_conclusion(action, audit_record, worker_id):
    if action == "accept":
        audit_record["conclusion_source"] = "human"
        audit_record["conclusion"] = "accept (human)"
        audit_record["conclusion_notes_source"] = "human"
    else:
        audit_record["conclusion_source"] = "human"
        audit_record["conclusion"] = "retry (human)"
        audit_record["conclusion_notes_source"] = "human"
    save_audit_record(worker_id, audit_record)


def validate_agents_and_task(agents: list, task: dict) -> None:
    """Raise ValueError if agents or task are invalid for run_agents."""
    if not isinstance(agents, list):
        raise ValueError("agents must be a list")
    if not agents:
        raise ValueError("agents must not be empty")
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            raise ValueError(f"agents[{i}] must be a dict")
        if "id" not in a or "role" not in a:
            raise ValueError(f"agents[{i}] must have 'id' and 'role'")
    if not isinstance(task, dict):
        raise ValueError("task must be a dict")


def run_agents(
        agents: list[dict],
        workspace: Path,
        task: dict,
        source: Path | None = None,
        batches: list[list[str]] | None = None,
        auto: bool = False,
        verbose: bool = True,
        fresh_level: int = -1,
        parallel: bool = False,
        max_workers: int = 4,
        display_driver: DisplayDriver | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
):
    """
    Run agents with task payload. Writes task.json under works/{run_id}/{role}/.
    - workspace: work path (task.json, agent.log, works/).
    - source: source path (source code, scripts) for agent to operate on; default = workspace.
    - If task has "steps", runs multi-round: first agent runs once per step.
    - Steps may have validate_with (broker runs sub-task directly, no agent shell).
    - Otherwise runs agents in dependency order; batches (parallel-ready levels) optional.
    - auto: skip confirmation between steps (human-in-loop pause).
    - fresh_level: -1=continue all, 0=re-execute all, n>0=continue levels<=n.
    - parallel: execute plans/breakdown in parallel using git worktree isolation (when source is a git repo).
    - max_workers: max parallel workers when parallel=True.
    - run_id: use specified run_id instead of generating a new one (for parent-child task linking).
    - parent_run_id: parent task's run_id for tracing (used by breakdown subtasks).
    """
    validate_agents_and_task(agents, task)
    src = Path(source).resolve() if source is not None else Path(workspace).resolve()
    _run_agents_internal(
        agents, workspace, task, src,
        batches=batches, auto=auto, verbose=verbose, fresh_level=fresh_level,
        parallel=parallel, max_workers=max_workers,
        display_driver=display_driver, run_id=run_id, parent_run_id=parent_run_id,
    )


def _run_agent_steps_local(
        agent: dict,
        steps: list[dict],
        workspace: Path,
        task: dict,
        source: Path,
        worker_id: str,
        run_id: str,
        auto: bool,
        verbose: bool,
        raise_on_failure: bool,
        display_driver: DisplayDriver,
        cursor_api_key: str | None = None,
) -> tuple[int, Path]:
    """
    Run a single agent through its steps via local cursor-cli.
    Returns (last_exit_code, work_dir).
    """
    drv = display_driver
    work_dir = get_work_dir(workspace, worker_id, run_id, agent["role"])
    write_run_meta(work_dir, run_id, worker_id, agent["role"])
    log_path = work_dir / "agent.log"
    parent_id = os.environ.get(BRO_PARENT_TASK_ID)
    if parent_id:
        line = json.dumps({"path": str(log_path), "worker_id": worker_id, "role": agent["role"]})
        locked_append(_running_file(parent_id), line)
    drv.on_log_paths([{"path": str(log_path), "worker_id": worker_id, "role": agent["role"]}])
    total_steps = len(steps)
    round_i = 0
    last_code = 0
    while round_i < total_steps:
        progress = load_progress(worker_id, run_id, subtask_id=agent["id"])
        completed = set(progress["completed_step_indices"]) if progress else set()
        retry_counts = dict((progress or {}).get("retry_counts") or {})

        if round_i in completed:
            round_i += 1
            continue
        step = steps[round_i]
        s = normalize_step(step, task=task)
        current_work_dir.set(work_dir)
        if s["validate_only"] and s["validate_with"]:
            code = _run_sub_task(
                s["validate_with"], workspace, source, local=True,
                params={}, verbose=verbose, display_driver=drv,
            )
            if code != 0 and raise_on_failure:
                raise SystemExit(code)
            last_code = code
            completed.add(round_i)
            save_progress(worker_id, run_id, list(completed), retry_counts=retry_counts, subtask_id=agent["id"])
            if not auto and round_i < total_steps - 1 and not _prompt_continue_next_step(round_i, total_steps):
                break
            round_i += 1
            continue

        attempt = _prepare_round_payload(agent, retry_counts, round_i, s, task, worker_id, total_steps,
                                         work_dir, workspace)

        round_objective = s["objective"]
        drv.on_task_assigned(worker_id, round_objective[:80] + ("..." if len(round_objective) > 80 else ""),
                             assignee=agent["role"])
        current_work_dir.set(work_dir)
        code = run_local(workspace, work_dir, source=source, verbose=verbose, cursor_api_key=cursor_api_key)
        last_code = code
        audit_record, last_result = _run_round_audit(attempt, round_i, s, work_dir)

        if audit_record["escalated"]:
            action = _prompt_escalation_accept_retry()
            _apply_human_audit_conclusion(action, audit_record, worker_id)

            if action == "retry":
                save_progress(worker_id, run_id, list(completed), last_round_result=last_result,
                              retry_counts=retry_counts, subtask_id=agent["id"])
                continue
        else:
            save_audit_record(worker_id, audit_record)

        completed.add(round_i)
        drv.on_progress(len(completed), total_steps)
        if code != 0 and raise_on_failure:
            raise SystemExit(code)
        save_progress(worker_id, run_id, list(completed), last_round_result=last_result, retry_counts=retry_counts,
                      subtask_id=agent["id"])
        if s["validate_with"]:
            code = _run_sub_task(
                s["validate_with"], workspace, source, local=True,
                params={}, verbose=verbose, display_driver=drv,
            )
            last_code = code
            if code != 0 and raise_on_failure:
                raise SystemExit(code)
        if not auto and round_i < total_steps - 1 and not _prompt_continue_next_step(round_i, total_steps,
                                                                                     verbose=verbose):
            break
        round_i += 1
    return last_code, work_dir


def _run_agents_local_internal(
        agents: list[dict],
        workspace: Path,
        task: dict,
        source: Path,
        raise_on_failure: bool = True,
        batches: list[list[str]] | None = None,
        auto: bool = False,
        verbose: bool = True,
        fresh_level: int = -1,
        parallel: bool = False,
        max_workers: int = 4,
        display_driver: DisplayDriver | None = None,
        run_id: str | None = None,
        cursor_api_key: str | None = None,
        parent_run_id: str | None = None,
) -> int:
    """
    Internal: run agents via local cursor-cli. Returns last exit code. Raises if raise_on_failure and code!=0.
    auto: skip step confirmation.
    fresh_level: -1=continue all, 0=re-execute all, n>0=continue levels<=n.
    parallel: execute subtasks in parallel using git worktree isolation.
    cursor_api_key: Cursor API key to pass to cursor-agent.
    parent_run_id: parent task's run_id for tracing (used by breakdown subtasks).
    
    Steps execution:
    - If task["steps"] is a dict {agent_id: [step, ...]}, each agent runs its own steps.
    - If no steps, agents run in dependency order without multi-round.
    """
    drv = display_driver or NullDriver()
    src = source
    worker_id = worker_id_from_task(task)
    run_id = run_id or gen_run_id()
    
    if parent_run_id and parent_run_id != run_id:
        from broker.utils.work_util import set_parent_run_mapping
        set_parent_run_mapping(workspace, run_id, parent_run_id)
        if verbose:
            drv.verbose(f"[broker] Recorded parent mapping: {run_id} -> parent={parent_run_id}")
    
    steps_dict = task.get("steps")
    has_steps = steps_dict and isinstance(steps_dict, dict) and len(steps_dict) > 0
    agent_by_id = {a["id"]: a for a in agents}
    last_code = 0

    if has_steps:
        if not agents:
            return 0
        run_list = _order_agents_by_batches(agent_by_id, agents, batches)
        last_work_dir = None
        for agent in run_list:
            agent_steps = get_steps_for_agent(task, agent["id"])
            if agent_steps:
                code, work_dir = _run_agent_steps_local(
                    agent, agent_steps, workspace, task, src,
                    worker_id, run_id, auto, verbose, raise_on_failure, drv,
                    cursor_api_key=cursor_api_key
                )
                last_code = code
                last_work_dir = work_dir
        if last_work_dir:
            breakdown_items = _read_breakdown_from_dir(last_work_dir)
            if breakdown_items:
                last_code = _execute_breakdown_items(breakdown_items, workspace, last_work_dir, src, task, drv,
                                                     {
                                                         'auto': auto,
                                                         'fresh_level': fresh_level,
                                                         'max_workers': max_workers,
                                                         'parallel': parallel,
                                                         'verbose': verbose,
                                                         'parent_run_id': run_id,
                                                     })

        if verbose:
            _emit_aggregation(workspace, worker_id, run_id, agents, drv)
    else:
        run_list = _order_agents_by_batches(agent_by_id, agents, batches)
        audit_context = _build_audit_context(task, worker_id)
        plans_items = [{"id": a["id"], **{k: v for k, v in a.items() if k != "id"}} for a in agents]

        if parallel and len(plans_items) >= 2:
            can_parallel, reason = _check_parallel_conditions(src, plans_items, drv)
            if can_parallel:
                if verbose:
                    drv.verbose(f"[broker] Plans parallel execution: {reason}")
                code = _run_parallel_with_worktree(
                    plans_items, workspace, src, task,
                    verbose=verbose, fresh_level=fresh_level, max_workers=max_workers,
                    local=True, auto=auto, display_driver=drv,
                )
                last_code = code
                if code != 0 and raise_on_failure:
                    raise SystemExit(code)
            else:
                if verbose:
                    drv.verbose(f"[broker] Fallback to serial execution: {reason}")
                last_code = _run_agents_serial(
                    run_list, workspace, task, worker_id, run_id, audit_context, src, drv, raise_on_failure, verbose,
                    cursor_api_key=cursor_api_key,
                )
        else:
            last_code = _run_agents_serial(
                run_list, workspace, task, worker_id, run_id, audit_context, src, drv, raise_on_failure, verbose,
                cursor_api_key=cursor_api_key,
            )

        _auto_audit(drv, run_id, run_list, task, worker_id, verbose, workspace)

        if run_list:
            breakdown_items, first_work_dir = _load_first_agent_breakdown(run_id, run_list, worker_id, workspace)
            if breakdown_items:
                last_code = _execute_breakdown_items(breakdown_items, workspace, first_work_dir, src, task, drv,
                                                     {
                                                         'auto': auto,
                                                         'fresh_level': fresh_level,
                                                         'max_workers': max_workers,
                                                         'parallel': parallel,
                                                         'verbose': verbose,
                                                         'parent_run_id': run_id,
                                                     })

        if verbose:
            _emit_aggregation(workspace, worker_id, run_id, agents, drv)
    return last_code


def _execute_breakdown_items(breakdown_items, workspace, work_dir, src, task, driver, options=None):
    if options is None:
        options = {}

    verbose = options.get('verbose', False)
    parent_run_id = options.get('parent_run_id')

    from broker.utils.work_util import add_child_run_mapping
    child_run_id = gen_run_id()
    if parent_run_id:
        add_child_run_mapping(workspace, parent_run_id, child_run_id)
        if verbose:
            driver.verbose(f"[broker] Generated child_run_id={child_run_id} for breakdown (parent={parent_run_id})")

    parallel = options.get('parallel', False)
    if parallel and len(breakdown_items) >= 2:
        can_parallel, reason = _check_parallel_conditions(src, breakdown_items, driver)
        if can_parallel:
            if verbose:
                driver.verbose(f"[broker] Breakdown parallel execution (local steps): {reason}")
            code = _run_parallel_with_worktree(breakdown_items, workspace, src, task,
                                               verbose=verbose,
                                               fresh_level=options['fresh_level'],
                                               max_workers=options['max_workers'],
                                               local=True,
                                               auto=options['auto'],
                                               display_driver=driver,
                                               parent_run_id=parent_run_id,
                                               child_run_id=child_run_id)
        else:
            if verbose:
                driver.verbose(f"[broker] Breakdown serial execution (local steps): {reason}")
            code = _invoke_skill_refs(workspace, work_dir, task, src,
                                      verbose=verbose,
                                      fresh_level=options['fresh_level'],
                                      display_driver=driver,
                                      auto=options.get('auto', False),
                                      parent_run_id=parent_run_id,
                                      child_run_id=child_run_id)
    else:
        code = _invoke_skill_refs(workspace, work_dir, task, src,
                                  verbose=verbose,
                                  fresh_level=options['fresh_level'],
                                  display_driver=driver,
                                  auto=options.get('auto', False),
                                  parent_run_id=parent_run_id,
                                  child_run_id=child_run_id)
    return code


def _load_first_agent_breakdown(run_id, run_list, worker_id, workspace):
    first_work_dir = build_work_dir(workspace, run_id, run_list[0]["role"])
    breakdown_items = _read_breakdown_from_dir(first_work_dir)
    return breakdown_items, first_work_dir


def _read_breakdown_from_dir(work_dir: Path) -> list[dict]:
    breakdown_file = work_dir / BREAKDOWN_JSON
    breakdown_items: list[dict] = []
    if breakdown_file.exists():
        try:
            data = json.loads(breakdown_file.read_text())
            if isinstance(data, list):
                breakdown_items = [x for x in data if isinstance(x, dict)]
        except (json.JSONDecodeError, TypeError, OSError):
            pass
    return breakdown_items


def _run_agents_serial(
        run_list: list[dict],
        workspace: Path,
        task: dict,
        worker_id: str,
        run_id: str,
        audit_context: str,
        src: Path,
        drv: DisplayDriver,
        raise_on_failure: bool,
        verbose: bool,
        cursor_api_key: str | None = None,
) -> int:
    """串行执行 agents 列表"""
    last_code = 0
    if run_list:
        paths = []
        for a in run_list:
            wd = build_work_dir(workspace, run_id, a["role"])
            p = str(wd / "agent.log")
            paths.append({"path": p, "worker_id": worker_id, "role": a["role"]})
            parent_id = os.environ.get(BRO_PARENT_TASK_ID)
            if parent_id:
                line = json.dumps({"path": p, "worker_id": worker_id, "role": a["role"]})
                locked_append(_running_file(parent_id), line)
        drv.on_log_paths(paths)
        drv.on_progress(0, len(run_list))

    for i, agent in enumerate(run_list):
        work_dir = get_work_dir(workspace, worker_id, run_id, agent["role"])
        write_run_meta(work_dir, run_id, worker_id, agent["role"])
        payload = build_task_payload(
            task, agent,
            audit_context=audit_context or None,
            work_dir=work_dir,
        )
        write_task_json(workspace, payload, work_dir)
        _obj = (task.get("worker") or task.get("task") or task).get("objective", "")
        obj_preview = _obj[:80] + ("..." if len(_obj) > 80 else "")
        drv.on_task_assigned(worker_id, obj_preview, assignee=agent["role"])
        current_work_dir.set(work_dir)
        code = run_local(workspace, work_dir, source=src, verbose=verbose, cursor_api_key=cursor_api_key)
        last_code = code
        drv.on_progress(i + 1, len(run_list))
        if code != 0 and raise_on_failure:
            raise SystemExit(code)
    return last_code


def _order_agents_by_batches(agent_by_id, agents, batches):
    run_list = agents
    if batches:
        run_list = [agent_by_id[nid] for batch in batches for nid in batch if nid in agent_by_id]
    return run_list


def _build_audit_context(task, worker_id):
    audit_context = ""
    if (task.get("worker") or task.get("task") or task).get("type") == "bootstrap":
        audit_context = get_audit_summary_for_boost(exclude_worker_id=worker_id) or ""
    return audit_context


def _auto_audit(drv, run_id, run_list, task, worker_id, verbose, workspace):
    task_expected = (task.get("worker") or task.get("task") or task).get("expected_results")
    for agent in run_list:
        work_dir = build_work_dir(workspace, run_id, agent["role"])
        last_result = None
        result_file = work_dir / "result.json"
        if result_file.exists():
            try:
                last_result = json.loads(result_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        audit_record = run_audit(
            work_dir, 0, last_result, 1,
            criteria=None, expected_results=task_expected,
        )
        if audit_record["escalated"]:
            action = _handle_audit_escalation(audit_record, worker_id, verbose)

            if action == "retry":
                drv.verbose("[broker] Retry requested; re-run the task to retry.")
                raise SystemExit(1)
        else:
            save_audit_record(worker_id, audit_record)


def run_agents_local(
        agents: list[dict],
        workspace: Path,
        task: dict,
        source: Path | None = None,
        batches: list[list[str]] | None = None,
        auto: bool = False,
        verbose: bool = True,
        fresh_level: int = -1,
        parallel: bool = False,
        max_workers: int = 4,
        display_driver: DisplayDriver | None = None,
        run_id: str | None = None,
        cursor_api_key: str | None = None,
        parent_run_id: str | None = None,
) -> None:
    """
    Run agents by invoking local cursor-cli (no Docker). For bootstrap when no agent image exists.
    Same task layout as run_agents: writes task.json under works/{run_id}/{role}/.
    Steps may have validate_with (broker runs sub-task directly, no agent shell - avoids timeout).
    source: path for agent to operate on; default = workspace. batches = parallel-ready level order.
    auto: skip confirmation between steps (human-in-loop pause).
    fresh_level: -1=continue all, 0=re-execute all, n>0=continue levels<=n.
    parallel: execute plans/breakdown in parallel using git worktree isolation (when source is a git repo).
    max_workers: max parallel workers when parallel=True.
    cursor_api_key: Cursor API key to pass to cursor-agent (avoids keychain issues).
    parent_run_id: parent task's run_id for tracing (used by breakdown subtasks).
    """
    validate_agents_and_task(agents, task)
    src = Path(source).resolve() if source is not None else Path(workspace).resolve()
    _run_agents_local_internal(
        agents, workspace, task, src,
        raise_on_failure=True, batches=batches, auto=auto, verbose=verbose, fresh_level=fresh_level,
        parallel=parallel, max_workers=max_workers,
        display_driver=display_driver,
        run_id=run_id,
        cursor_api_key=cursor_api_key,
        parent_run_id=parent_run_id,
    )
