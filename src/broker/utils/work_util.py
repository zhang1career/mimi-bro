from __future__ import annotations

import json
from pathlib import Path

from broker.task_types import get_task_type_config


BREAKDOWN_JSON = "breakdown.json"


def build_work_dir(
        workspace: Path,
        task_name: str,
        task_id: str,
        role: str
) -> Path:
    return (
            workspace
            / 'works'
            / f'{task_name}-{task_id}-{role}'
    )


def get_work_dir(
        workspace: Path,
        task_name: str | None = None,
        task_id: str | None = None,
        role: str | None = None,
) -> Path:
    """
    Return work dir: if task_name, task_id, role all provided -> workspace/works/{{task_name}}-{{task_id}}-{{role}};
    else -> workspace/works (legacy). Creates dir(s) if needed.
    """
    if task_name is not None and task_id is not None and role is not None:
        work_dir = build_work_dir(workspace, task_name, task_id, role)
    else:
        work_dir = workspace / 'works'
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def write_task_json(workspace: Path, payload: dict, work_dir: Path | None = None) -> Path:
    """
    Write task.json for the agent container to read.
    If work_dir is provided, write work_dir/task.json; else use get_work_dir(workspace) (legacy).
    Returns the path to the written file.
    """
    if work_dir is None:
        work_dir = get_work_dir(workspace)
    work_dir.mkdir(parents=True, exist_ok=True)
    task_file = work_dir / 'task.json'
    task_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return task_file




def build_task_payload(
        task: dict,
        agent: dict,
        round_objective: str | None = None,
        round_context: str | None = None,
        audit_context: str | None = None,
        work_dir: Path | None = None,
) -> dict:
    """
    Build the task.json payload for one agent run.
    task: full task dict (objective, instructions, entrypoint, type, etc.)
    agent: agent dict (mode, objective override)
    round_objective: if set (multi-round), use as objective for this round
    round_context: if set, append to instructions for this round
    audit_context: if set (Phase 5.1 audit-to-bootstrap), append as instruction for boost to consider.
    When task.type is "bootstrap", adds constraints and ensures payload marks generated; no core safety logic change.
    """
    task_block = task.get("worker") or task.get("task") or task
    base_instructions = list(task_block.get("instructions") or [])
    if round_context:
        base_instructions = base_instructions + [round_context]
    if audit_context and audit_context.strip():
        base_instructions = base_instructions + [audit_context.strip()]

    skill_refs = task.get("skill_refs")
    if skill_refs:
        write_path = ""
        if work_dir is not None:
            write_path = f"Write to {work_dir.resolve() / BREAKDOWN_JSON}. "
        base_instructions = base_instructions + [
            f"Implementation skills (skill_refs): {', '.join(skill_refs)}. "
            f"After preparation (validation, analysis): {write_path}Create breakdown.json with format "
            '[{"id": "<unique-subtask-id>", "skill": "<skill_id>", "requirement": "..."}, ...]. The broker will invoke these skills. '
            "Each item MUST have 'id' (unique identifier for tracking, e.g. 'auth-api', 'user-service', use descriptive kebab-case), "
            "'skill' (from skill_refs), and params like 'requirement'. "
            "When the requirement explicitly specifies a platform (e.g. native, iOS, web, weapp), add 'scope' to each item, e.g. {\"scope\": \"apps/native\"} for native iOS."
        ]

    task_type = task_block.get("type") or task.get("type")
    type_config = get_task_type_config(task_type)
    if type_config:
        base_instructions = base_instructions + list(type_config.get("extra_instructions") or [])

    objective = (
        round_objective
        if round_objective is not None
        else (agent.get("objective") or task_block.get("objective") or "")
    )

    payload = {
        "objective": objective,
        "instructions": base_instructions,
        "mode": agent.get("mode", "agent"),
        "entrypoint": task_block.get("entrypoint", "."),
    }
    if task_type:
        payload["type"] = task_type
    if skill_refs:
        payload["skill_refs"] = list(skill_refs)
    if type_config:
        payload["constraints"] = dict(type_config.get("constraints") or {})
        if type_config.get("generated_marker"):
            payload["generated_marker"] = True
    return payload
