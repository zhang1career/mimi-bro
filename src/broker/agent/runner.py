from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from broker.agent.docker import run_container
from broker.agent.execution_common import (
    build_audit_context,
    emit_aggregation,
    get_steps_for_agent,
    handle_audit_escalation,
    order_agents_by_batches,
    running_file,
)
from broker.agent.executors import DockerExecutor, LocalExecutor
from broker.agent.executors.subtask_invoker import run_one_subtask_docker, run_one_subtask_local
from broker.audit.skeleton import run_audit
from broker.audit.store import save_audit_record
from broker.container.manager import get_host_mount_from_docker
from broker.decision.propose import propose
from broker.model.breakdown_options import BreakdownOptions
from broker.model.plan_item import PlanItemType, get_plan_item_type
from broker.model.task import get_task_block, get_task_id
from broker.parallel.worktree import GitWorktree
from broker.planner import plan_task
from broker.skill import get_invocation, load_skill_registry
from broker.task import _find_project_root, load_task, substitute_task
from broker.ui import NullDriver
from broker.ui.driver import DisplayDriver
from broker.utils.id_client import gen_run_id
from broker.utils.path_util import BRO_PROJECT_ROOT_ENV, PROJECT_ROOT
from broker.utils.traceback_util import error_summary_for_console, format_exc as traceback_format_exc
from broker.utils.work_util import (
    BREAKDOWN_JSON,
    build_task_payload,
    build_work_dir,
    get_work_dir,
    task_path_rel,
    write_run_meta,
    write_task_json,
)

BRO_PARENT_TASK_ID = "BRO_PARENT_TASK_ID"


def cleanup_subtask_containers(success_only: bool = True) -> None:
    """Remove subtask containers at process exit (atexit). Only runs when BROKER_AUTO_CLEANUP_CONTAINER=1.
    When enabled, removes only successfully exited (exit code 0) agent-* / bro-subtask-* containers.
    """
    val = os.environ.get("BROKER_AUTO_CLEANUP_CONTAINER", "0")
    auto_cleanup = val.lower() not in ("0", "false", "no")
    if not auto_cleanup:
        return
    try:
        from broker.container.manager import ContainerManager, get_docker_client
        client = get_docker_client()
        containers = client.containers.list(all=True)
        prefix_bro = f"{ContainerManager.CONTAINER_PREFIX}-"
        for c in containers:
            if not (c.name.startswith("agent-") or c.name.startswith(prefix_bro)):
                continue
            if c.status != "exited":
                continue
            exit_code = c.attrs.get("State", {}).get("ExitCode", -1)
            if success_only and exit_code != 0:
                continue
            try:
                c.remove()
            except Exception:
                pass
    except Exception:
        pass


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
    return get_task_id(task)


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
                base_id = item_copy.get("id") or "inline"
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
        plan_id = item.get("id", "worker")
        ws = workspace_path or src_path
        task_id = worker_id or plan_id
        cmd = f'bro run {plan_id} --workspace "{ws}" --source "{src_path}" --objective "{objective}" --task-id "{task_id}"'
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
        *,
        local: bool = False,
        display_driver: DisplayDriver | None = None,
        child_run_id: str | None = None,
        options: BreakdownOptions | dict | None = None,
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
    """
    opts = BreakdownOptions.from_dict(options) if isinstance(options, dict) else (options or BreakdownOptions())
    project_root, src, skill_refs_list, drv = _prepare_skill_execution_context(workspace, source, task, display_driver)
    breakdown_file, items = _load_breakdown_items(work_dir, drv, opts.verbose)
    if not items and not skill_refs_list:
        return 0
    items = _prepare_breakdown_items(
        breakdown_file, items, skill_refs_list, task,
        display_driver=drv, auto=opts.auto, verbose=opts.verbose,
    )

    parent_worker_id = worker_id_from_task(task)
    last_code = 0

    task_block = get_task_block(task)
    default_objective = task_block.get("objective", "")

    skill_timeout = int(os.environ.get("BROKER_SKILL_TIMEOUT", "7200"))
    cursor_api_key = os.environ.get("CURSOR_API_KEY") if not local else None

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        subtask_id = item.get("id")
        exec_type = get_plan_item_type(item)
        type_label = exec_type.value
        target = item.get("skill") or item.get("objective", "")[:30]
        drv.on_console_message(f"Type: {type_label}")
        drv.on_console_message(f"Assigned: {subtask_id} → {target}")
        scope_val = item.get("scope", "")
        drv.on_console_message(f"Scope: {scope_val if scope_val else '(not set)'}")

        if opts.verbose:
            drv.verbose(f"[broker] invoking subtask {subtask_id} ({type_label}) ({i + 1}/{len(items)})...")
        drv.on_status(f"Invoking subtask {i + 1}/{len(items)}: {subtask_id} ({type_label})")
        drv.on_progress(i + 1, len(items), child_tasks=[{"subtask_id": subtask_id, "current": 1, "total": 1}])

        try:
            src_path_arg = Path(src)
            if local:
                last_code = run_one_subtask_local(
                    item, workspace, src_path_arg, task,
                    parent_worker_id, child_run_id, opts.parent_run_id, work_dir,
                    default_objective, opts.fresh_level, opts.verbose, drv,
                    build_cmd_fn=_build_subtask_command,
                    running_file_fn=running_file,
                    cursor_api_key=cursor_api_key,
                    cwd=project_root,
                    use_polling=True,
                    skill_timeout=skill_timeout,
                )
            else:
                last_code = run_one_subtask_docker(
                    item, workspace, src_path_arg, task,
                    parent_worker_id, child_run_id, opts.parent_run_id, work_dir,
                    default_objective, opts.fresh_level, opts.verbose, drv,
                    build_cmd_fn=_build_subtask_command,
                    cursor_api_key=cursor_api_key,
                    docker_workspace=opts.docker_workspace,
                    docker_source=opts.docker_source,
                    skill_timeout=skill_timeout,
                )
        except Exception as e:
            if opts.verbose:
                drv.verbose(f"[broker] subtask invoke failed: {e}")
            last_code = 1

        drv.on_status("")
    return last_code


def _build_items_from_skill_refs(task, skill_refs_list):
    items = []
    params = task.get("params") or {}
    params.update(get_task_block(task).get("params") or {})
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
        load_skill_registry(
            skill_refs=skill_refs_list,
            on_message=drv.on_console_message if display_driver else None,
        )
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
        docker_workspace: str = "/workspace",
        docker_source: str = "/source",
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

    # DOCKER_PARALLEL_STAGGER_SECONDS: delay between starting subtasks (Docker default 30s)
    stagger_default = "30" if not local else "5"
    stagger_sec = float(os.environ.get("DOCKER_PARALLEL_STAGGER_SECONDS", stagger_default))
    if verbose and stagger_sec > 0:
        drv.verbose(f"[broker] stagger {stagger_sec}s between parallel subtasks")

    scheduler = ParallelScheduler(
        workspace=scheduler_workspace,
        worker_id=worker_id,
        run_id=run_id,
        dep_graph=confirmed_graph,
        breakdown=items,
        max_workers=max_workers,
        state_workspace=workspace,
        stagger_seconds=stagger_sec,
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
        plan_id = item.get("id", "unknown")
        wd = build_work_dir(workspace, run_id, plan_id)
        log_path = wd / "agent.log"
        log_paths.append({
            "path": str(log_path),
            "worker_id": worker_id,
            "plan_id": plan_id,
        })
    drv.on_log_paths(log_paths)
    drv.on_progress(0, total_items)

    task_block = get_task_block(task)
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
        }
        effective_run_id = child_run_id if child_run_id else run_id
        effective_parent_run_id = parent_run_id if child_run_id else None
        if local:
            ws_path = str(workspace)
            src_path_arg = str(worktree_path)
        else:
            ws_path = docker_workspace
            src_path_arg = docker_source
        cmd = _build_subtask_command(
            _item,
            src_path=src_path_arg,
            fresh_level=fresh_level,
            local=local,
            workspace_path=ws_path,
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

        if local:
            # Local execution via subprocess
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
                subtask.error_message = f"Exception: {e}\n\n{traceback_format_exc()}"
                drv.on_console_message(f"[ERROR] Subtask {subtask.id} exception: {error_summary_for_console(e)}")
                return 1
        else:
            # Docker parallel: host creates cursor-agent / runs skill directly (no bro-subtask)
            if (_item.get("objective") or _item.get("requirement")) and not _item.get("skill"):
                # INLINE: run cursor-agent directly from host
                _plan_id = subtask.id
                subtask_run_id = effective_run_id or gen_run_id()
                work_dir = get_work_dir(workspace, run_id=subtask_run_id, plan_id=_plan_id)
                write_run_meta(work_dir, subtask_run_id, worker_id, _plan_id, effective_parent_run_id)
                agent = {"id": _plan_id, "mode": "plan", **_item}
                payload = build_task_payload(task, agent)
                write_task_json(workspace, payload, work_dir)
                drv.on_console_message(f"[container] Starting cursor-agent for {subtask.id}...")
                try:
                    run_container(
                        _plan_id, _plan_id,
                        task_id=subtask_run_id,
                        workspace=workspace,
                        work_dir_rel=task_path_rel(subtask_run_id, _plan_id),
                        source=worktree_path,
                    )
                    return 0
                except Exception as e:
                    from docker.errors import ContainerError
                    subtask.error_message = str(e)
                    if isinstance(e, ContainerError):
                        drv.on_console_message(f"[ERROR] Subtask {subtask.id} failed (exit {e.exit_status})")
                        return e.exit_status or 1
                    drv.on_console_message(f"[ERROR] Subtask {subtask.id} exception: {error_summary_for_console(e)}")
                    return 1
            # SKILL: run cmd on host (bro submit, pwd, etc.)
            cmd_host = _build_subtask_command(
                _item,
                src_path=str(worktree_path),
                fresh_level=fresh_level,
                local=False,
                workspace_path=str(workspace),
                default_objective=default_objective,
                worker_id=worker_id,
                run_id=effective_run_id,
                cursor_api_key=cursor_api_key,
                parent_run_id=effective_parent_run_id,
            )
            if cmd_host is None:
                drv.on_console_message(f"[ERROR] Cannot build command for subtask {subtask.id}")
                return 1
            drv.on_console_message(f"[host] Running skill for {subtask.id}...")
            env = os.environ.copy()
            env[BRO_PARENT_TASK_ID] = worker_id
            env["BRO_PARALLEL_RUN_ID"] = run_id
            env[BRO_PROJECT_ROOT_ENV] = str(PROJECT_ROOT)
            if cursor_api_key:
                env["CURSOR_API_KEY"] = cursor_api_key
            try:
                proc = subprocess.Popen(
                    cmd_host, shell=True, cwd=str(worktree_path), env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = proc.communicate()
                if proc.returncode != 0:
                    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
                    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
                    subtask.error_message = f"Exit code: {proc.returncode}"
                    if stderr_text:
                        subtask.error_message += f"\n\nstderr:\n{stderr_text[:1000]}"
                    if stdout_text:
                        subtask.error_message += f"\n\nstdout (last 500 chars):\n{stdout_text[-500:]}"
                    drv.on_console_message(f"[ERROR] Subtask {subtask.id} failed (exit {proc.returncode})")
                    if stderr_text:
                        drv.on_console_message(f"  stderr: {stderr_text[:200]}")
                return proc.returncode
            except (OSError, subprocess.SubprocessError) as e:
                subtask.error_message = f"Exception: {e}\n\n{traceback_format_exc()}"
                drv.on_console_message(f"[ERROR] Subtask {subtask.id} exception: {error_summary_for_console(e)}")
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

        def run_external_fn(args: list[str], cwd) -> int:
            return drv.run_external_command(args, cwd)

        merger.set_run_external_fn(run_external_fn)

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
        *,
        local: bool = False,
        display_driver: DisplayDriver | None = None,
        parent_run_id: str | None = None,
        child_run_id: str | None = None,
        interactive_merge: bool = True,
        options: BreakdownOptions | dict | None = None,
) -> int:
    """
    统一的 Git Worktree + Cherry-pick 并行执行函数。

    适用于：
    - plans 字段的多个元素（child_run_id=None，继承父 run_id）
    - breakdown.json 的多个元素（child_run_id 为统一生成的新 run_id）

    Args:
        interactive_merge: 是否启用交互式合并（遇到冲突时弹出 GUI mergetool）
    """
    opts = BreakdownOptions.from_dict(options) if isinstance(options, dict) else (options or BreakdownOptions())
    drv = display_driver or NullDriver()

    skill_refs = task.get("skill_refs")
    skill_refs_list: list[str] = []
    if skill_refs:
        if isinstance(skill_refs, list):
            skill_refs_list = skill_refs
        elif isinstance(skill_refs, str):
            skill_refs_list = [skill_refs]

    if skill_refs_list:
        load_skill_registry(
            skill_refs=skill_refs_list,
            on_message=drv.on_console_message if display_driver else None,
        )

    items = _ensure_subtask_ids(items)
    project_root = _find_project_root(workspace)
    src = Path(source).resolve()

    # When broker runs inside Docker, source may be /source (container path).
    # Worktrees must be created on the host filesystem so they persist and are visible
    # at the source path. Resolve container path to host mount.
    src_str = str(src)
    if src_str == "/source" or src_str.startswith("/source/"):
        host_src = get_host_mount_from_docker("/source")
        if host_src:
            src = Path(host_src)
            if opts.verbose:
                drv.verbose(f"[broker] Resolved source /source -> host {src} (for worktree creation)")

    return _execute_parallel_items(
        items=items,
        workspace=workspace,
        source=src,
        task=task,
        project_root=project_root,
        scheduler_workspace=src,
        drv=drv,
        verbose=opts.verbose,
        fresh_level=opts.fresh_level,
        max_workers=opts.max_workers,
        local=local,
        auto=opts.auto,
        parent_run_id=parent_run_id,
        child_run_id=child_run_id,
        interactive_merge=interactive_merge,
        docker_workspace=opts.docker_workspace,
        docker_source=opts.docker_source,
    )


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
        docker_workspace: str = "/workspace",
        docker_source: str = "/source",
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
        run_list = order_agents_by_batches(agent_by_id, agents, batches)
        last_work_dir = None
        for agent in run_list:
            agent_steps = get_steps_for_agent(task, agent["id"])
            if agent_steps:
                last_work_dir = DockerExecutor().run_agent_steps(
                    agent, agent_steps, workspace, task, source,
                    worker_id, run_id, auto, verbose, drv,
                    run_sub_task_fn=_run_sub_task,
                )
        if last_work_dir:
            breakdown_items = _read_breakdown_from_dir(last_work_dir)
            if breakdown_items:
                _execute_breakdown(breakdown_items, workspace, last_work_dir, source, task, drv,
                                   local=False,
                                   options={
                                       'auto': auto,
                                       'fresh_level': fresh_level,
                                       'max_workers': max_workers,
                                       'parallel': parallel,
                                       'verbose': verbose,
                                       'parent_run_id': run_id,
                                       'docker_workspace': docker_workspace,
                                       'docker_source': docker_source,
                                   })

        if verbose:
            emit_aggregation(workspace, worker_id, run_id, agents, drv)
    else:
        run_list = order_agents_by_batches(agent_by_id, agents, batches)
        audit_context = build_audit_context(task, worker_id)

        plans_items = [{"id": a["id"], **{k: v for k, v in a.items() if k != "id"}} for a in agents]

        if parallel and len(plans_items) >= 2:
            can_parallel, reason = _check_parallel_conditions(source, plans_items, drv)
            if can_parallel:
                if verbose:
                    drv.verbose(f"[broker] Plans parallel execution (Docker): {reason}")
                code = _run_parallel_with_worktree(
                    plans_items, workspace, source, task,
                    local=False,
                    display_driver=drv,
                    parent_run_id=run_id,
                    options=BreakdownOptions(
                        verbose=verbose, fresh_level=fresh_level, max_workers=max_workers,
                        auto=auto, parent_run_id=run_id,
                        docker_workspace=docker_workspace, docker_source=docker_source,
                    ),
                )
                if code != 0:
                    raise RuntimeError(f"Parallel execution failed (exit {code})")
            else:
                if verbose:
                    drv.verbose(f"[broker] Fallback to serial execution (Docker): {reason}")
                DockerExecutor().run_serial_agents(
                    run_list, workspace, task, worker_id, run_id, audit_context, source, drv
                )
        else:
            DockerExecutor().run_serial_agents(
                run_list, workspace, task, worker_id, run_id, audit_context, source, drv
            )

        _auto_audit(drv, run_id, run_list, task, worker_id, verbose, workspace)

        if run_list:
            breakdown_items, first_work_dir = _load_first_agent_breakdown(run_id, run_list, workspace)
            if breakdown_items:
                _execute_breakdown(breakdown_items, workspace, first_work_dir, source, task, drv,
                                   local=False,
                                   options={
                                       'auto': auto,
                                       'fresh_level': fresh_level,
                                       'max_workers': max_workers,
                                       'parallel': parallel,
                                       'verbose': verbose,
                                       'parent_run_id': run_id,
                                       'docker_workspace': docker_workspace,
                                       'docker_source': docker_source,
                                   })

        if verbose:
            emit_aggregation(workspace, worker_id, run_id, agents, drv)


def validate_agents_and_task(agents: list, task: dict) -> None:
    """Raise ValueError if agents or task are invalid for run_agents."""
    if not isinstance(agents, list):
        raise ValueError("agents must be a list")
    if not agents:
        raise ValueError("agents must not be empty")
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            raise ValueError(f"agents[{i}] must be a dict")
        if "id" not in a:
            raise ValueError(f"agents[{i}] must have 'id'")
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
        docker_workspace: str = "/workspace",
):
    """
    Run agents with task payload. Writes task.json under works/{run_id}/{plan_id}/.
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
        docker_workspace=docker_workspace,
    )


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
        run_list = order_agents_by_batches(agent_by_id, agents, batches)
        last_work_dir = None
        for agent in run_list:
            agent_steps = get_steps_for_agent(task, agent["id"])
            if agent_steps:
                work_dir = LocalExecutor().run_agent_steps(
                    agent, agent_steps, workspace, task, src,
                    worker_id, run_id, auto, verbose, drv,
                    run_sub_task_fn=_run_sub_task,
                    raise_on_failure=raise_on_failure,
                    cursor_api_key=cursor_api_key,
                )
                last_code = 0
                last_work_dir = work_dir
        if last_work_dir:
            breakdown_items = _read_breakdown_from_dir(last_work_dir)
            if breakdown_items:
                last_code = _execute_breakdown(breakdown_items, workspace, last_work_dir, src, task, drv,
                                               local=True,
                                               options={
                                                   'auto': auto,
                                                   'fresh_level': fresh_level,
                                                   'max_workers': max_workers,
                                                   'parallel': parallel,
                                                   'verbose': verbose,
                                                   'parent_run_id': run_id,
                                               })

        if verbose:
            emit_aggregation(workspace, worker_id, run_id, agents, drv)
    else:
        run_list = order_agents_by_batches(agent_by_id, agents, batches)
        audit_context = build_audit_context(task, worker_id)
        plans_items = [{"id": a["id"], **{k: v for k, v in a.items() if k != "id"}} for a in agents]

        if parallel and len(plans_items) >= 2:
            can_parallel, reason = _check_parallel_conditions(src, plans_items, drv)
            if can_parallel:
                if verbose:
                    drv.verbose(f"[broker] Plans parallel execution: {reason}")
                code = _run_parallel_with_worktree(
                    plans_items, workspace, src, task,
                    local=True,
                    display_driver=drv,
                    parent_run_id=run_id,
                    options=BreakdownOptions(
                        verbose=verbose, fresh_level=fresh_level, max_workers=max_workers,
                        auto=auto, parent_run_id=run_id,
                    ),
                )
                last_code = code
                if code != 0 and raise_on_failure:
                    raise SystemExit(code)
            else:
                if verbose:
                    drv.verbose(f"[broker] Fallback to serial execution: {reason}")
                last_code = LocalExecutor().run_serial_agents(
                    run_list, workspace, task, worker_id, run_id, audit_context, src, drv,
                    raise_on_failure=raise_on_failure, verbose=verbose,
                    cursor_api_key=cursor_api_key,
                )
        else:
            last_code = LocalExecutor().run_serial_agents(
                run_list, workspace, task, worker_id, run_id, audit_context, src, drv,
                raise_on_failure=raise_on_failure, verbose=verbose,
                cursor_api_key=cursor_api_key,
            )

        _auto_audit(drv, run_id, run_list, task, worker_id, verbose, workspace)

        if run_list:
            breakdown_items, first_work_dir = _load_first_agent_breakdown(run_id, run_list, workspace)
            if breakdown_items:
                last_code = _execute_breakdown(breakdown_items, workspace, first_work_dir, src, task, drv,
                                               local=True,
                                               options={
                                                   'auto': auto,
                                                   'fresh_level': fresh_level,
                                                   'max_workers': max_workers,
                                                   'parallel': parallel,
                                                   'verbose': verbose,
                                                   'parent_run_id': run_id,
                                               })

        if verbose:
            emit_aggregation(workspace, worker_id, run_id, agents, drv)
    return last_code


def _execute_breakdown(
        breakdown_items: list,
        workspace: Path,
        work_dir: Path,
        source: Path,
        task: dict,
        driver: DisplayDriver,
        *,
        local: bool = True,
        options: BreakdownOptions | dict | None = None,
) -> int:
    """Unified breakdown execution (serial or parallel). local=True for Local mode, False for Docker."""
    opts = BreakdownOptions.from_dict(options) if isinstance(options, dict) else (options or BreakdownOptions())
    verbose = opts.verbose
    parent_run_id = opts.parent_run_id

    from broker.utils.work_util import add_child_run_mapping
    child_run_id = gen_run_id()
    if parent_run_id:
        add_child_run_mapping(workspace, parent_run_id, child_run_id)
        if verbose:
            driver.verbose(f"[broker] Generated child_run_id={child_run_id} for breakdown (parent={parent_run_id})")

    if opts.parallel and len(breakdown_items) >= 2:
        can_parallel, reason = _check_parallel_conditions(source, breakdown_items, driver)
        if can_parallel:
            mode = "local" if local else "Docker"
            if verbose:
                driver.verbose(f"[broker] Breakdown parallel execution ({mode}): {reason}")
            return _run_parallel_with_worktree(
                breakdown_items, workspace, source, task,
                local=local,
                display_driver=driver,
                parent_run_id=parent_run_id,
                child_run_id=child_run_id,
                options=opts,
            )
        mode = "local" if local else "Docker"
        if verbose:
            driver.verbose(f"[broker] Breakdown serial execution ({mode}): {reason}")
    return _invoke_skill_refs(
        workspace, work_dir, task, source,
        local=local,
        display_driver=driver,
        child_run_id=child_run_id,
        options=opts,
    )


def _load_first_agent_breakdown(run_id, run_list, workspace):
    first_work_dir = build_work_dir(workspace, run_id, run_list[0]["id"])
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


def _auto_audit(drv, run_id, run_list, task, worker_id, verbose, workspace):
    task_expected = get_task_block(task).get("expected_results")
    for agent in run_list:
        work_dir = build_work_dir(workspace, run_id, agent["id"])
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
            action = handle_audit_escalation(audit_record, worker_id, verbose)

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
    Same task layout as run_agents: writes task.json under works/{run_id}/{plan_id}/.
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
