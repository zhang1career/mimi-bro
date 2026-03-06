"""Docker execution: serial agents and multi-step agent run."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from broker.agent.docker import run_container
from broker.agent.execution_common import (
    apply_human_audit_conclusion,
    emit_subtask_log_path,
    normalize_step,
    prepare_round_payload,
    prompt_continue_next_step,
    prompt_escalation_accept_retry,
    run_round_audit,
)
from broker.context import current_work_dir
from broker.state.progress import load_progress, save_progress
from broker.ui.driver import DisplayDriver
from broker.utils.work_util import (
    build_work_dir,
    get_work_dir,
    task_path_rel,
    write_run_meta,
    build_task_payload,
    write_task_json,
)

BRO_PARENT_TASK_ID = "BRO_PARENT_TASK_ID"


class DockerExecutor:
    """Execute agents via Docker: serial run and multi-step run."""

    def run_serial_agents(
            self,
            run_list: list[dict],
            workspace: Path,
            task: dict,
            worker_id: str,
            run_id: str,
            audit_context: str,
            source: Path,
            drv: DisplayDriver,
    ) -> None:
        """Run agents in order (one round each) via Docker."""
        if run_list:
            paths = []
            for a in run_list:
                wd = build_work_dir(workspace, run_id, a["id"])
                p = str(wd / "agent.log")
                paths.append({"path": p, "worker_id": worker_id, "plan_id": a["id"]})
            drv.on_log_paths(paths)
            drv.on_progress(0, len(run_list))

        for i, agent in enumerate(run_list):
            work_dir = get_work_dir(workspace, worker_id, run_id, agent["id"])
            parent_id = os.environ.get(BRO_PARENT_TASK_ID)
            if parent_id:
                emit_subtask_log_path(agent, worker_id, work_dir, parent_id)
            write_run_meta(work_dir, run_id, worker_id, agent["id"])
            work_dir_rel = task_path_rel(run_id, agent["id"])
            payload = build_task_payload(
                task, agent,
                audit_context=audit_context or None,
                work_dir=work_dir,
            )
            write_task_json(workspace, payload, work_dir)
            current_work_dir.set(work_dir)
            run_container(
                agent["id"],
                agent["id"],
                worker_id,
                workspace,
                work_dir_rel=work_dir_rel,
                source=source,
            )
            drv.on_progress(i + 1, len(run_list))

    def run_agent_steps(
            self,
            agent: dict,
            steps: list[dict],
            workspace: Path,
            task: dict,
            source: Path,
            worker_id: str,
            run_id: str,
            auto: bool,
            verbose: bool,
            drv: DisplayDriver,
            run_sub_task_fn: Callable[..., int],
    ) -> Path:
        """Run one agent through its steps via Docker. Returns work_dir."""
        work_dir = get_work_dir(workspace, worker_id, run_id, agent["id"])
        parent_id = os.environ.get(BRO_PARENT_TASK_ID)
        if parent_id:
            emit_subtask_log_path(agent, worker_id, work_dir, parent_id)
        write_run_meta(work_dir, run_id, worker_id, agent["id"])
        work_dir_rel = task_path_rel(run_id, agent["id"])
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
                code = run_sub_task_fn(
                    s["validate_with"], workspace, source, local=False,
                    params={}, verbose=verbose, display_driver=drv,
                )
                if code != 0:
                    raise RuntimeError(f"Validation task {s['validate_with']} failed (exit {code})")
                completed.add(round_i)
                save_progress(worker_id, run_id, list(completed), retry_counts=retry_counts, subtask_id=agent["id"])
                if not auto and round_i < total_steps - 1 and not prompt_continue_next_step(round_i, total_steps,
                                                                                            verbose=verbose):
                    break
                round_i += 1
                continue

            attempt = prepare_round_payload(
                agent, retry_counts, round_i, s, task, worker_id, total_steps,
                work_dir, workspace,
            )
            current_work_dir.set(work_dir)
            run_container(
                agent["id"],
                agent["id"],
                worker_id,
                workspace,
                work_dir_rel=work_dir_rel,
                source=source,
            )
            audit_record, last_result = run_round_audit(attempt, round_i, s, work_dir)
            if audit_record["escalated"]:
                action = prompt_escalation_accept_retry(verbose=verbose)
                apply_human_audit_conclusion(action, audit_record, worker_id)
                if action == "retry":
                    save_progress(worker_id, run_id, list(completed), last_round_result=last_result,
                                  retry_counts=retry_counts, subtask_id=agent["id"])
                    continue
            else:
                from broker.audit.store import save_audit_record
                save_audit_record(worker_id, audit_record)

            completed.add(round_i)
            save_progress(worker_id, run_id, list(completed), last_round_result=last_result, retry_counts=retry_counts,
                          subtask_id=agent["id"])
            if s["validate_with"]:
                code = run_sub_task_fn(
                    s["validate_with"], workspace, source, local=False,
                    params={}, verbose=verbose, display_driver=drv,
                )
                if code != 0:
                    raise RuntimeError(f"Validation task {s['validate_with']} failed (exit {code})")
            if not auto and round_i < total_steps - 1 and not prompt_continue_next_step(round_i, total_steps,
                                                                                        verbose=verbose):
                break
            round_i += 1
        return work_dir
