"""Local (cursor-cli) execution: serial agents and multi-step agent run."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from broker.agent.execution_common import (
    apply_human_audit_conclusion,
    emit_subtask_log_path,
    normalize_step,
    prepare_round_payload,
    prompt_continue_next_step,
    prompt_escalation_accept_retry,
    run_round_audit,
    running_file,
)
from broker.agent.local import run_local
from broker.context import current_work_dir
from broker.state.progress import load_progress, save_progress
from broker.ui.driver import DisplayDriver
from broker.utils.file_lock import locked_append
from broker.model.task import get_task_block
from broker.utils.work_util import (
    build_work_dir,
    get_work_dir,
    write_run_meta,
    build_task_payload,
    write_task_json,
)

BRO_PARENT_TASK_ID = "BRO_PARENT_TASK_ID"


class LocalExecutor:
    """Execute agents via local cursor-cli: serial run and multi-step run."""

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
            *,
            raise_on_failure: bool = True,
            verbose: bool = True,
            cursor_api_key: str | None = None,
    ) -> int:
        """Run agents in order via local cursor-cli. Returns last exit code; raises SystemExit if raise_on_failure and code!=0."""
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
                    locked_append(running_file(parent_id), line)
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
            _obj = get_task_block(task).get("objective", "")
            obj_preview = _obj[:80] + ("..." if len(_obj) > 80 else "")
            drv.on_task_assigned(worker_id, obj_preview, assignee=agent["role"])
            current_work_dir.set(work_dir)
            code = run_local(workspace, work_dir, source=source, verbose=verbose, cursor_api_key=cursor_api_key)
            last_code = code
            drv.on_progress(i + 1, len(run_list))
            if code != 0 and raise_on_failure:
                raise SystemExit(code)
        return last_code

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
            *,
            raise_on_failure: bool = True,
            cursor_api_key: str | None = None,
    ) -> Path:
        """Run one agent through its steps via local cursor-cli. Returns work_dir."""
        work_dir = get_work_dir(workspace, worker_id, run_id, agent["role"])
        write_run_meta(work_dir, run_id, worker_id, agent["role"])
        log_path = work_dir / "agent.log"
        parent_id = os.environ.get(BRO_PARENT_TASK_ID)
        if parent_id:
            emit_subtask_log_path(agent, worker_id, work_dir, parent_id)
        drv.on_log_paths([{"path": str(log_path), "worker_id": worker_id, "role": agent["role"]}])
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
                    s["validate_with"], workspace, source, local=True,
                    params={}, verbose=verbose, display_driver=drv,
                )
                if code != 0 and raise_on_failure:
                    raise SystemExit(code)
                last_code = code
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
            round_objective = s["objective"]
            drv.on_task_assigned(
                worker_id,
                round_objective[:80] + ("..." if len(round_objective) > 80 else ""),
                assignee=agent["role"],
            )
            current_work_dir.set(work_dir)
            code = run_local(workspace, work_dir, source=source, verbose=verbose, cursor_api_key=cursor_api_key)
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
            drv.on_progress(len(completed), total_steps)
            if code != 0 and raise_on_failure:
                raise SystemExit(code)
            save_progress(worker_id, run_id, list(completed), last_round_result=last_result, retry_counts=retry_counts,
                          subtask_id=agent["id"])
            if s["validate_with"]:
                code = run_sub_task_fn(
                    s["validate_with"], workspace, source, local=True,
                    params={}, verbose=verbose, display_driver=drv,
                )
                if code != 0 and raise_on_failure:
                    raise SystemExit(code)
            if not auto and round_i < total_steps - 1 and not prompt_continue_next_step(round_i, total_steps,
                                                                                        verbose=verbose):
                break
            round_i += 1
        return work_dir
