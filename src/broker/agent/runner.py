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
from broker.planner import plan_task
from broker.skill import get_invocation, load_skill_registry
from broker.skill.registry import set_console_callback
from broker.state.progress import load_progress, save_progress
from broker.task import _find_project_root, load_task, substitute_task
from broker.ui import NullDriver
from broker.ui.driver import DisplayDriver
from broker.utils.id_client import get_run_id
from broker.utils.path_util import PROJECT_ROOT
from broker.utils.prompt_util import CONFIRM_TIMEOUT, prompt_with_timeout
from broker.utils.work_util import (
    BREAKDOWN_JSON,
    build_task_payload,
    get_work_dir,
    write_task_json,
)

BRO_PARENT_TASK_ID = "BRO_PARENT_TASK_ID"


def _running_file(parent_task_id: str) -> Path:
    """Path to .state/running/{parent_task_id}.jsonl for subtask work_dir discovery."""
    d = PROJECT_ROOT / ".state" / "running"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{parent_task_id}.jsonl"


def _resolve_validation_path(validate_with: str, workspace: Path, local: bool = False) -> Path:
    """Resolve validate_with path. When local: use project root's workers/ first."""
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
        resource: Path,
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
    res = Path(resource).resolve() if resource else ws
    path = _resolve_validation_path(validate_with, ws, local=local)
    task = load_task(str(path), workspace=ws)
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
            agents, ws, task, res, raise_on_failure=False, verbose=verbose, display_driver=drv
        )
    try:
        _run_agents_internal(agents, ws, task, res, verbose=verbose, display_driver=drv)
        return 0
    except Exception:
        return 1


def _task_id_from_task(task: dict) -> str:
    """Logical task id (task.id from task JSON) for progress key etc."""
    task_block = task.get("worker") or task.get("task") or task
    return task_block.get("id") or task.get("id") or "demo"


def _task_name_from_task(task: dict) -> str:
    """Task name for works subdir: works/{{task_name}}-{{run_id}}-{{role}}."""
    return _task_id_from_task(task)


def _generate_subtask_id(index: int, skill_id: str) -> str:
    """Generate a unique subtask ID when not provided in breakdown.json."""
    from broker.utils.id_client import get_run_id
    return f"{skill_id}-{index}-{get_run_id()[:8]}"


def _ensure_subtask_ids(items: list[dict], skill_refs: list[str]) -> list[dict]:
    """Ensure each item in breakdown has a unique 'id' field. Generate if missing."""
    result = []
    for i, item in enumerate(items):
        item_copy = dict(item)
        if not item_copy.get("id"):
            skill_id = item_copy.get("skill") or item_copy.get("skill_id") or "subtask"
            item_copy["id"] = _generate_subtask_id(i, skill_id)
        result.append(item_copy)
    return result


def _invoke_skill_refs(
        workspace: Path,
        work_dir: Path,
        task: dict,
        resource: Path,
        verbose: bool = True,
        fresh_level: int = -1,
        display_driver: DisplayDriver | None = None,
) -> int:
    """
    After prep work, invoke skills from skill_refs. Reads work_dir/breakdown.json:
    [{"id": "<subtask-id>", "skill": "frontend-dev", "requirement": "..."}, ...].
    Each item MUST have 'id' (auto-generated if missing for backward compatibility).
    If missing, for single-shot workers invoke each skill once with task params.
    Returns last exit code.

    fresh_level: controls which levels to re-execute:
      -1: continue all (default)
       0: re-execute all
       n (n>0): continue levels <= n, re-execute levels > n
    """
    skill_refs = task.get("skill_refs")
    if not skill_refs:
        return 0
    drv = display_driver or NullDriver()
    try:
        set_console_callback(drv.on_console_message if display_driver else None)
        load_skill_registry(skill_refs=skill_refs)
    finally:
        set_console_callback(None)
    project_root = _find_project_root(workspace)
    res = Path(resource).resolve()
    src_path = str(res)
    breakdown_file = work_dir / BREAKDOWN_JSON
    items = []
    if breakdown_file.exists():
        try:
            data = json.loads(breakdown_file.read_text())
            if isinstance(data, list):
                items = data
        except (json.JSONDecodeError, TypeError):
            drv = display_driver or NullDriver()
            if verbose:
                drv.verbose(f"[broker] {BREAKDOWN_JSON} invalid or empty")
    if not items:
        params = task.get("params") or {}
        params.update((task.get("worker") or task.get("task") or task).get("params") or {})
        for skill_id in skill_refs:
            items.append({"skill": skill_id, **params})

    items = _ensure_subtask_ids(items, skill_refs)

    if breakdown_file.exists():
        breakdown_file.write_text(json.dumps(items, indent=2, ensure_ascii=False))

    last_code = 0
    for i, item in enumerate(items):
        subtask_id = item.get("id")
        skill_id = item.get("skill") or item.get("skill_id")
        if not skill_id or skill_id not in skill_refs:
            continue
        params = {k: str(v) for k, v in item.items() if k not in ("skill", "skill_id", "id")}
        fresh_flag = ""
        if fresh_level == 0:
            fresh_flag = " --fresh 0"
        elif fresh_level > 0:
            fresh_flag = f" --fresh {fresh_level - 1}"
        inv = get_invocation(skill_id, src_path=src_path, **params)
        if inv is None:
            if verbose:
                drv.verbose(f"[broker] skill {skill_id} has no invocation, skip")
            continue
        if isinstance(inv, dict):
            if verbose:
                drv.verbose(f"[broker] http invocation not yet supported for {skill_id}")
            continue
        if fresh_flag and "bro submit" in inv:
            inv = inv.rstrip() + fresh_flag
        parent_task_id = _task_id_from_task(task)
        drv.on_console_message(f"Skill: {skill_id}")
        drv.on_console_message(f"Assigned: {subtask_id} → {skill_id}")
        scope_val = params.get("scope", "")
        drv.on_console_message(f"Scope: {scope_val if scope_val else '(not set)'}")
        if verbose:
            drv.verbose(f"[broker] invoking subtask {subtask_id} ({skill_id}) ({i + 1}/{len(items)}): {inv[:80]}...")
        drv.on_status(f"Invoking subtask {i + 1}/{len(items)}: {subtask_id} ({skill_id})")
        try:
            env = os.environ.copy()
            env[BRO_PARENT_TASK_ID] = parent_task_id
            run_file = _running_file(parent_task_id)
            if run_file.exists():
                run_file.unlink()
            proc = subprocess.Popen(inv, shell=True, cwd=str(project_root), env=env)
            child_tasks = [{"task_id": subtask_id, "current": 1, "total": 1}]
            drv.on_progress(i + 1, len(items), child_tasks=child_tasks)
            seen_paths: set[str] = set()
            start = time.time()
            last_elapsed_emit = -1
            while proc.poll() is None:
                if run_file.exists():
                    for line in run_file.read_text().splitlines():
                        if line.strip() and line not in seen_paths:
                            seen_paths.add(line)
                            try:
                                data = json.loads(line)
                                drv.on_log_paths([{
                                    "path": data.get("path", ""),
                                    "task_id": data.get("task_id"),
                                    "role": data.get("role"),
                                    "parent_id": parent_task_id,
                                }])
                            except json.JSONDecodeError:
                                pass
                elapsed = time.time() - start
                if int(elapsed) >= last_elapsed_emit + 2:
                    last_elapsed_emit = int(elapsed)
                    drv.on_progress(i + 1, len(items), child_tasks=child_tasks)
                    drv.on_status(f"Running subtask {subtask_id}...", elapsed_seconds=elapsed)
                time.sleep(0.3)
            last_code = proc.returncode
            drv.on_console_message(f"Result: {subtask_id}: {'ok' if last_code == 0 else 'failed'} (exit {last_code})")
            drv.on_status("")
        except Exception as e:
            if verbose:
                (display_driver or NullDriver()).verbose(f"[broker] skill invoke failed: {e}")
            last_code = 1
    return last_code


def _emit_aggregation(
        workspace: Path,
        task_name: str,
        task_id: str,
        run_id: str,
        agents: list[dict],
        display_driver: DisplayDriver,
) -> None:
    """Emit results aggregation via display driver: on_result per agent."""
    for agent in agents:
        role = agent.get("role", "?")
        work_dir = get_work_dir(workspace, task_name, run_id, role)
        result_file = work_dir / "result.json"
        status = "no result.json"
        exit_code: int | None = None
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
                status = data.get("status", "?")
                exit_code = data.get("code")
            except Exception:
                status = "read error"
        display_driver.on_result(task_id, role, status, work_dir, exit_code=exit_code)


def _read_previous_round_summary(workspace: Path, work_dir: Path | None = None) -> str:
    """Read result.json to summarize previous round for next round context."""
    if work_dir is None:
        work_dir = get_work_dir(workspace)
    result_file = work_dir / "result.json"
    if not result_file.exists():
        return ""
    try:
        data = json.loads(result_file.read_text())
        status = data.get("status", "unknown")
        code = data.get("code", "")
        # 日志约定：与 result.json 同目录的 agent.log
        return f"Previous round: status={status}, exit_code={code}, log=agent.log (same dir). Continue from here."
    except Exception:
        return "Previous round completed. Continue from here."


def _normalize_step(step, round_i: int, total: int, task: dict | None = None) -> dict:
    """Normalize step to {objective, validate_with, validate_only, expected_results}. Phase 4.2: optional expected_results from step or task."""
    if isinstance(step, str):
        expected = None
        if task:
            task_block = task.get("worker") or task.get("task") or task
            expected = task_block.get("expected_results")
        return {
            "objective": step,
            "validate_with": None,
            "validate_only": False,
            "expected_results": expected,
        }
    expected = step.get("expected_results")
    if expected is None and task:
        task_block = task.get("worker") or task.get("task") or task
        expected = task_block.get("expected_results")
    return {
        "objective": step.get("objective", ""),
        "validate_with": step.get("validate_with"),
        "validate_only": bool(step.get("validate_only", False)),
        "expected_results": expected,
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


def _run_agents_internal(
        agents: list[dict],
        workspace: Path,
        task: dict,
        resource: Path,
        batches: list[list[str]] | None = None,
        auto: bool = False,
        verbose: bool = True,
        fresh_level: int = -1,
        display_driver: DisplayDriver | None = None,
) -> None:
    """Internal: run agents via Docker. Raises on failure. batches = parallel-ready level order. auto: skip step confirmation.
    fresh_level: -1=continue all, 0=re-execute all, n>0=continue levels<=n."""
    drv = display_driver or NullDriver()
    task_id = _task_id_from_task(task)
    drv.on_task_tree([{"id": task_id, "label": task_id}], running_ids={task_id})
    task_name = _task_name_from_task(task)
    run_id = get_run_id()
    steps = task.get("steps")
    agent_by_id = {a["id"]: a for a in agents}

    if steps:
        if not agents:
            return
        first_agent = agents[0]
        work_dir = get_work_dir(workspace, task_name, run_id, first_agent["role"])
        _emit_subtask_log_path(first_agent, task_id, work_dir)
        work_dir_rel = f"works/{task_name}-{run_id}-{first_agent['role']}"
        total_steps = len(steps)
        round_i = 0
        while round_i < total_steps:
            progress = load_progress(task_id, run_id)
            completed = set(progress["completed_step_indices"]) if progress else set()
            retry_counts = dict((progress or {}).get("retry_counts") or {})

            if round_i in completed:
                round_i += 1
                continue
            step = steps[round_i]
            s = _normalize_step(step, round_i, total_steps, task=task)
            current_work_dir.set(work_dir)
            if s["validate_only"] and s["validate_with"]:
                code = _run_sub_task(
                    s["validate_with"], workspace, resource, local=False,
                    params={}, verbose=verbose, display_driver=drv,
                )
                if code != 0:
                    raise RuntimeError(f"Validation task {s['validate_with']} failed (exit {code})")
                completed.add(round_i)
                save_progress(task_id, run_id, list(completed), retry_counts=retry_counts)
                if not auto and round_i < total_steps - 1 and not _prompt_continue_next_step(round_i, total_steps,
                                                                                             verbose=verbose):
                    break
                round_i += 1
                continue

            attempt = _prepare_round_payload(first_agent, retry_counts, round_i, s, task, task_id, total_steps,
                                             work_dir, workspace)
            current_work_dir.set(work_dir)
            run_container(
                first_agent["id"],
                first_agent["role"],
                task_id,
                workspace,
                work_dir_rel=work_dir_rel,
                resource=resource,
            )
            audit_record, last_result = _run_round_audit(attempt, round_i, s, work_dir)
            if audit_record["escalated"]:
                action = _handle_audit_escalation(audit_record, task_id, verbose)
                if action == "retry":
                    save_progress(task_id, run_id, list(completed), last_round_result=last_result,
                                  retry_counts=retry_counts)
                    continue
            else:
                save_audit_record(task_id, audit_record)

            completed.add(round_i)
            save_progress(task_id, run_id, list(completed), last_round_result=last_result, retry_counts=retry_counts)
            if s["validate_with"]:
                code = _run_sub_task(
                    s["validate_with"], workspace, resource, local=False,
                    params={}, verbose=verbose, display_driver=drv,
                )
                if code != 0:
                    raise RuntimeError(f"Validation task {s['validate_with']} failed (exit {code})")
            if not auto and round_i < total_steps - 1 and not _prompt_continue_next_step(round_i, total_steps,
                                                                                         verbose=verbose):
                break
            round_i += 1
        if task.get("skill_refs"):
            _invoke_skill_refs(
                workspace, work_dir, task, resource,
                verbose=verbose, fresh_level=fresh_level, display_driver=drv,
            )
        if verbose:
            _emit_aggregation(workspace, task_name, task_id, run_id, agents, drv)
    else:
        run_list = _order_agents_by_batches(agent_by_id, agents, batches)
        audit_context = _build_audit_context(task, task_id)
        for agent in run_list:
            work_dir = get_work_dir(workspace, task_name, run_id, agent["role"])
            _emit_subtask_log_path(agent, task_id, work_dir)
            work_dir_rel = f"works/{task_name}-{run_id}-{agent['role']}"
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
                task_id,
                workspace,
                work_dir_rel=work_dir_rel,
                resource=resource,
            )
        # Post-run auto-audit (Phase 4.1) for single-shot; Phase 4.2 optional expected_results
        _auto_audit(drv, run_id, run_list, task, task_id, task_name, verbose, workspace)

        if task.get("skill_refs") and run_list:
            first_work_dir = get_work_dir(workspace, task_name, run_id, run_list[0]["role"])
            _invoke_skill_refs(
                workspace, first_work_dir, task, resource,
                verbose=verbose, fresh_level=fresh_level, display_driver=drv,
            )
        if verbose:
            _emit_aggregation(workspace, task_name, task_id, run_id, agents, drv)


def _run_round_audit(attempt, round_i, s, work_dir):
    last_result = None
    result_file = work_dir / "result.json"
    if result_file.exists():
        try:
            last_result = json.loads(result_file.read_text())
        except Exception:
            pass
    audit_record = run_audit(
        work_dir, round_i, last_result, attempt,
        criteria=None, expected_results=s.get("expected_results"),
    )
    return audit_record, last_result


def _emit_subtask_log_path(agent, task_id, work_dir):
    parent_id = os.environ.get(BRO_PARENT_TASK_ID)
    if parent_id:
        line = json.dumps({"path": str(work_dir / "agent.log"), "task_id": task_id, "role": agent["role"]}) + "\n"
        _running_file(parent_id).open("a").write(line)


def _prepare_round_payload(first_agent, retry_counts, round_i, s, task, task_id, total_steps, work_dir, workspace):
    attempt = retry_counts.get(str(round_i), 0) + 1
    retry_counts[str(round_i)] = attempt
    round_objective = s["objective"]
    round_context = f"Round {round_i + 1} of {total_steps}."
    if round_i > 0:
        round_context += " " + _read_previous_round_summary(workspace, work_dir)
    audit_context = ""
    if (task.get("worker") or task.get("task") or task).get("type") == "bootstrap":
        audit_context = get_audit_summary_for_boost(exclude_task_id=task_id) or ""
    payload = build_task_payload(
        task, first_agent,
        round_objective=round_objective,
        round_context=round_context,
        audit_context=audit_context or None,
        work_dir=work_dir,
    )
    write_task_json(workspace, payload, work_dir)
    return attempt


def _handle_audit_escalation(audit_record, task_id, verbose):
    action = _prompt_escalation_accept_retry(verbose=verbose)
    _apply_human_audit_conclusion(action, audit_record, task_id)
    return action


def _apply_human_audit_conclusion(action, audit_record, task_id):
    if action == "accept":
        audit_record["conclusion_source"] = "human"
        audit_record["conclusion"] = "accept (human)"
        audit_record["conclusion_notes_source"] = "human"
    else:
        audit_record["conclusion_source"] = "human"
        audit_record["conclusion"] = "retry (human)"
        audit_record["conclusion_notes_source"] = "human"
    save_audit_record(task_id, audit_record)


def run_agents(
        agents: list[dict],
        workspace: Path,
        task: dict,
        resource: Path | None = None,
        batches: list[list[str]] | None = None,
        auto: bool = False,
        verbose: bool = True,
        fresh_level: int = -1,
        display_driver: DisplayDriver | None = None,
):
    """
    Run agents with task payload. Writes task.json under works/{{task_name}}-{{run_id}}-{{role}}/.
    - workspace: work path (task.json, agent.log, works/).
    - resource: resource path (source code, scripts) for agent to operate on; default = workspace.
    - If task has "steps", runs multi-round: first agent runs once per step.
    - Steps may have validate_with (broker runs sub-task directly, no agent shell).
    - Otherwise runs agents in dependency order; batches (parallel-ready levels) optional.
    - auto: skip confirmation between steps (human-in-loop pause).
    - fresh_level: -1=continue all, 0=re-execute all, n>0=continue levels<=n.
    """
    res = resource if resource is not None else workspace
    _run_agents_internal(
        agents, workspace, task, res,
        batches=batches, auto=auto, verbose=verbose, fresh_level=fresh_level,
        display_driver=display_driver,
    )


def _run_agents_local_internal(
        agents: list[dict],
        workspace: Path,
        task: dict,
        resource: Path,
        raise_on_failure: bool = True,
        batches: list[list[str]] | None = None,
        auto: bool = False,
        verbose: bool = True,
        fresh_level: int = -1,
        display_driver: DisplayDriver | None = None,
) -> int:
    """Internal: run agents via local cursor-cli. Returns last exit code. Raises if raise_on_failure and code!=0. auto: skip step confirmation.
    fresh_level: -1=continue all, 0=re-execute all, n>0=continue levels<=n."""
    drv = display_driver or NullDriver()
    res = resource
    task_id = _task_id_from_task(task)
    task_name = _task_name_from_task(task)
    run_id = get_run_id()
    steps = task.get("steps")
    agent_by_id = {a["id"]: a for a in agents}
    last_code = 0

    drv.on_task_tree([{"id": task_id, "label": task_id}], running_ids={task_id})

    if steps:
        if not agents:
            return 0
        first_agent = agents[0]
        work_dir = get_work_dir(workspace, task_name, run_id, first_agent["role"])
        log_path = work_dir / "agent.log"
        parent_id = os.environ.get(BRO_PARENT_TASK_ID)
        if parent_id:
            line = json.dumps({"path": str(log_path), "task_id": task_id, "role": first_agent["role"]}) + "\n"
            _running_file(parent_id).open("a").write(line)
        drv.on_log_paths([{"path": str(log_path), "task_id": task_id, "role": first_agent["role"]}])
        total_steps = len(steps)
        round_i = 0
        while round_i < total_steps:
            progress = load_progress(task_id, run_id)
            completed = set(progress["completed_step_indices"]) if progress else set()
            retry_counts = dict((progress or {}).get("retry_counts") or {})

            if round_i in completed:
                round_i += 1
                continue
            step = steps[round_i]
            s = _normalize_step(step, round_i, total_steps, task=task)
            current_work_dir.set(work_dir)
            if s["validate_only"] and s["validate_with"]:
                code = _run_sub_task(
                    s["validate_with"], workspace, resource, local=True,
                    params={}, verbose=verbose, display_driver=drv,
                )
                if code != 0 and raise_on_failure:
                    raise SystemExit(code)
                last_code = code
                completed.add(round_i)
                save_progress(task_id, run_id, list(completed), retry_counts=retry_counts)
                if not auto and round_i < total_steps - 1 and not _prompt_continue_next_step(round_i, total_steps):
                    break
                round_i += 1
                continue

            attempt = _prepare_round_payload(first_agent, retry_counts, round_i, s, task, task_id, total_steps,
                                             work_dir, workspace)

            round_objective = s["objective"]
            drv.on_task_assigned(task_id, round_objective[:80] + ("..." if len(round_objective) > 80 else ""),
                                 assignee=first_agent["role"])
            current_work_dir.set(work_dir)
            code = run_local(workspace, work_dir, resource=res, verbose=verbose)
            last_code = code
            audit_record, last_result = _run_round_audit(attempt, round_i, s, work_dir)

            if audit_record["escalated"]:
                action = _prompt_escalation_accept_retry()
                _apply_human_audit_conclusion(action, audit_record, task_id)

                if action == "retry":
                    save_progress(task_id, run_id, list(completed), last_round_result=last_result,
                                  retry_counts=retry_counts)
                    continue
            else:
                save_audit_record(task_id, audit_record)

            completed.add(round_i)
            drv.on_progress(len(completed), total_steps)
            if code != 0 and raise_on_failure:
                raise SystemExit(code)
            save_progress(task_id, run_id, list(completed), last_round_result=last_result, retry_counts=retry_counts)
            if s["validate_with"]:
                code = _run_sub_task(
                    s["validate_with"], workspace, resource, local=True,
                    params={}, verbose=verbose, display_driver=drv,
                )
                last_code = code
                if code != 0 and raise_on_failure:
                    raise SystemExit(code)
            if not auto and round_i < total_steps - 1 and not _prompt_continue_next_step(round_i, total_steps,
                                                                                         verbose=verbose):
                break
            round_i += 1
        if task.get("skill_refs"):
            code = _invoke_skill_refs(
                workspace, work_dir, task, res,
                verbose=verbose, fresh_level=fresh_level, display_driver=drv,
            )
            last_code = code
        if verbose:
            _emit_aggregation(workspace, task_name, task_id, run_id, agents, drv)
    else:
        run_list = _order_agents_by_batches(agent_by_id, agents, batches)
        audit_context = _build_audit_context(task, task_id)
        if run_list:
            paths = []
            for a in run_list:
                wd = get_work_dir(workspace, task_name, run_id, a["role"])
                p = str(wd / "agent.log")
                paths.append({"path": p, "task_id": task_id, "role": a["role"]})
                parent_id = os.environ.get(BRO_PARENT_TASK_ID)
                if parent_id:
                    line = json.dumps({"path": p, "task_id": task_id, "role": a["role"]}) + "\n"
                    _running_file(parent_id).open("a").write(line)
            drv.on_log_paths(paths)
            drv.on_progress(0, len(run_list))
        for agent in run_list:
            work_dir = get_work_dir(workspace, task_name, run_id, agent["role"])
            payload = build_task_payload(
                task, agent,
                audit_context=audit_context or None,
                work_dir=work_dir,
            )
            write_task_json(workspace, payload, work_dir)
            _obj = (task.get("worker") or task.get("task") or task).get("objective", "")
            obj_preview = _obj[:80] + ("..." if len(_obj) > 80 else "")
            drv.on_task_assigned(task_id, obj_preview, assignee=agent["role"])
            current_work_dir.set(work_dir)
            code = run_local(workspace, work_dir, resource=res, verbose=verbose)
            last_code = code
            if code != 0 and raise_on_failure:
                raise SystemExit(code)
        # Post-run auto-audit (Phase 4.1) for single-shot local; Phase 4.2 optional expected_results
        _auto_audit(drv, run_id, run_list, task, task_id, task_name, verbose, workspace)
        if task.get("skill_refs") and run_list:
            first_work_dir = get_work_dir(workspace, task_name, run_id, run_list[0]["role"])
            code = _invoke_skill_refs(
                workspace, first_work_dir, task, res,
                verbose=verbose, fresh_level=fresh_level, display_driver=drv,
            )
            last_code = code
        if verbose:
            _emit_aggregation(workspace, task_name, task_id, run_id, agents, drv)
    return last_code


def _order_agents_by_batches(agent_by_id, agents, batches):
    run_list = agents
    if batches:
        run_list = [agent_by_id[nid] for batch in batches for nid in batch if nid in agent_by_id]
    return run_list


def _build_audit_context(task, task_id):
    audit_context = ""
    if (task.get("worker") or task.get("task") or task).get("type") == "bootstrap":
        audit_context = get_audit_summary_for_boost(exclude_task_id=task_id) or ""
    return audit_context


def _auto_audit(drv, run_id, run_list, task, task_id, task_name, verbose, workspace):
    task_expected = (task.get("worker") or task.get("task") or task).get("expected_results")
    for agent in run_list:
        work_dir = get_work_dir(workspace, task_name, run_id, agent["role"])
        last_result = None
        result_file = work_dir / "result.json"
        if result_file.exists():
            try:
                last_result = json.loads(result_file.read_text())
            except Exception:
                pass
        audit_record = run_audit(
            work_dir, 0, last_result, 1,
            criteria=None, expected_results=task_expected,
        )
        if audit_record["escalated"]:
            action = _handle_audit_escalation(audit_record, task_id, verbose)

            if action == "retry":
                drv.verbose("[broker] Retry requested; re-run the task to retry.")
                raise SystemExit(1)
        else:
            save_audit_record(task_id, audit_record)


def run_agents_local(
        agents: list[dict],
        workspace: Path,
        task: dict,
        resource: Path | None = None,
        batches: list[list[str]] | None = None,
        auto: bool = False,
        verbose: bool = True,
        fresh_level: int = -1,
        display_driver: DisplayDriver | None = None,
) -> None:
    """
    Run agents by invoking local cursor-cli (no Docker). For bootstrap when no agent image exists.
    Same task layout as run_agents: writes task.json under works/{{task_name}}-{{run_id}}-{{role}}/.
    Steps may have validate_with (broker runs sub-task directly, no agent shell - avoids timeout).
    resource: path for agent to operate on; default = workspace. batches = parallel-ready level order.
    auto: skip confirmation between steps (human-in-loop pause).
    fresh_level: -1=continue all, 0=re-execute all, n>0=continue levels<=n.
    """
    res = resource if resource is not None else workspace
    _run_agents_local_internal(
        agents, workspace, task, res,
        raise_on_failure=True, batches=batches, auto=auto, verbose=verbose, fresh_level=fresh_level,
        display_driver=display_driver,
    )
