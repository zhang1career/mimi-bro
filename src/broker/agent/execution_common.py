"""Shared helpers for agent execution (steps, rounds, audit, ordering). Used by DockerExecutor and LocalExecutor."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from broker.audit.store import save_audit_record
from broker.model.task import get_task_block
from broker.utils.file_lock import locked_append
from broker.utils.path_util import PROJECT_ROOT
from broker.utils.prompt_util import CONFIRM_TIMEOUT, prompt_with_timeout
from broker.utils.work_util import build_work_dir, build_task_payload, write_task_json

if __name__ == "__main__":
    from broker.ui.driver import DisplayDriver
else:
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from broker.ui.driver import DisplayDriver


def running_file(parent_worker_id: str) -> Path:
    """Path to .state/running/{parent_worker_id}.jsonl for subtask work_dir discovery."""
    d = PROJECT_ROOT / ".state" / "running"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{parent_worker_id}.jsonl"


def get_steps_for_agent(task: dict, agent_id: str) -> list[dict] | None:
    """Get steps for a specific agent from task[\"steps\"] dict. Returns None if no steps defined."""
    steps = task.get("steps")
    if not steps or not isinstance(steps, dict):
        return None
    agent_steps = steps.get(agent_id)
    if agent_steps and isinstance(agent_steps, list):
        return agent_steps
    return None


def normalize_step(step: Any, task: dict | None = None) -> dict:
    """Normalize step to {objective, validate_with, validate_only, expected_results}."""
    worker_objective = ""
    expected = None
    if task and isinstance(task, dict):
        task_block = get_task_block(task)
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


def order_agents_by_batches(agent_by_id: dict, agents: list, batches: list[list[str]] | None) -> list:
    """Order agents by batches; if no batches return agents as-is."""
    if batches:
        return [agent_by_id[nid] for batch in batches for nid in batch if nid in agent_by_id]
    return agents


def build_audit_context(task: dict, worker_id: str) -> str:
    """Build audit context string for bootstrap type tasks."""
    from broker.audit.store import get_audit_summary_for_boost
    audit_context = ""
    if get_task_block(task).get("type") == "bootstrap":
        audit_context = get_audit_summary_for_boost(exclude_worker_id=worker_id) or ""
    return audit_context


def emit_aggregation(
        workspace: Path,
        worker_id: str,
        run_id: str,
        agents: list[dict],
        display_driver: "DisplayDriver",
) -> None:
    """Emit results aggregation via display driver: on_result per agent."""
    for agent in agents:
        plan_id = agent.get("id", "?")
        work_dir = build_work_dir(workspace, run_id, plan_id)
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
        display_driver.on_result(worker_id, plan_id, status, work_dir, exit_code=exit_code)


def read_previous_round_summary(workspace: Path, work_dir: Path | None) -> str:
    """Read result.json to summarize previous round for next round context."""
    from broker.utils.work_util import get_work_dir
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


def prompt_continue_next_step(round_i: int, total: int, verbose: bool = True) -> bool:
    """Ask whether to continue to next step. Returns True to continue."""
    if verbose:
        prompt = f"[broker] Round {round_i + 1} of {total} completed. Continue to next step? [Y/n] ({CONFIRM_TIMEOUT}s): "
    else:
        prompt = f"Continue to next step? [Y/n] ({CONFIRM_TIMEOUT}s): "
    reply = prompt_with_timeout(prompt, default="y", timeout_sec=CONFIRM_TIMEOUT).lower()
    return reply in ("", "y", "yes")


def prompt_escalation_accept_retry(verbose: bool = True) -> str:
    """Post-run audit escalated: human can accept or retry. Returns 'accept' or 'retry'."""
    if verbose:
        print("[broker] Audit escalated. Accept and continue? [a]ccept / [r]etry: ", end="", flush=True)
    else:
        print("Accept and continue? [a/r]: ", end="", flush=True)
    try:
        reply = input().strip().lower()
        return "accept" if reply in ("a", "accept") else "retry"
    except (EOFError, KeyboardInterrupt):
        return "retry"


def prepare_round_payload(
        first_agent: dict,
        retry_counts: dict,
        round_i: int,
        s: dict,
        task: dict,
        worker_id: str,
        total_steps: int,
        work_dir: Path,
        workspace: Path,
) -> int:
    """Prepare task.json for this round; return attempt number."""
    attempt = retry_counts.get(str(round_i), 0) + 1
    retry_counts[str(round_i)] = attempt
    round_objective = s["objective"]
    round_context = f"Round {round_i + 1} of {total_steps}."
    if round_i > 0:
        round_context += " " + read_previous_round_summary(workspace, work_dir)
    audit_context = build_audit_context(task, worker_id)
    payload = build_task_payload(
        task, first_agent,
        round_objective=round_objective,
        round_context=round_context,
        audit_context=audit_context or None,
        work_dir=work_dir,
    )
    write_task_json(workspace, payload, work_dir)
    return attempt


def run_round_audit(attempt: int, round_i: int, s: dict, work_dir: Path):
    """Run audit for one round; return (audit_record, last_result)."""
    from broker.audit.skeleton import run_audit
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


def apply_human_audit_conclusion(action: str, audit_record: dict, worker_id: str) -> None:
    """Apply human decision to audit record and save."""
    if action == "accept":
        audit_record["conclusion_source"] = "human"
        audit_record["conclusion"] = "accept (human)"
        audit_record["conclusion_notes_source"] = "human"
    else:
        audit_record["conclusion_source"] = "human"
        audit_record["conclusion"] = "retry (human)"
        audit_record["conclusion_notes_source"] = "human"
    save_audit_record(worker_id, audit_record)


def handle_audit_escalation(audit_record: dict, worker_id: str, verbose: bool = True) -> str:
    """Post-run audit escalated: prompt human, apply conclusion, return 'accept' or 'retry'."""
    action = prompt_escalation_accept_retry(verbose=verbose)
    apply_human_audit_conclusion(action, audit_record, worker_id)
    return action


def emit_subtask_log_path(
        agent: dict,
        worker_id: str,
        work_dir: Path,
        parent_worker_id: str,
) -> None:
    """Append log path to parent's running file for TUI discovery. parent_worker_id = BRO_PARENT_TASK_ID value."""
    if not parent_worker_id:
        return
    line = json.dumps({
        "path": str(work_dir / "agent.log"),
        "worker_id": worker_id,
        "plan_id": agent["id"],
    })
    locked_append(running_file(parent_worker_id), line)
