from __future__ import annotations

import json
from pathlib import Path

from broker.model.task import get_task_block
from broker.task_types import get_task_type_config

# 任务唯一性约定：run_id + plan_id
# - 路径：{run_id}/{plan_id} → task_path_rel, build_work_dir
# - 命名：{run_id}-{plan_id} → task_slug (容器、分支、文件)

BREAKDOWN_JSON = "breakdown.json"
RUN_META_FILE = "run_meta.json"
RUN_MAPPINGS_FILE = "run_mappings.json"


class WorkDirConflictError(Exception):
    """工作目录 run_id + plan_id 冲突异常"""
    pass


def build_work_dir(
        workspace: Path,
        run_id: str,
        plan_id: str,
        task_id: str | None = None,
) -> Path:
    """
    构建工作目录路径：workspace/works/{run_id}/{plan_id}
    
    新结构：两级目录，run_id 作为批次标识，plan_id 作为子任务标识（父上下文的 id）。
    task_id 参数保留用于兼容，但不再用于目录名。
    """
    return workspace / 'works' / run_id / plan_id


def task_path_rel(run_id: str, plan_id: str) -> str:
    """
    任务路径相对段：works/{run_id}/{plan_id}
    用于 work_dir_rel、Docker WORK_DIR 等路径拼接。
    """
    return f"works/{run_id}/{plan_id}"


def task_slug(
    run_id: str,
    plan_id: str,
    truncate_run_id: int | None = None,
    max_plan_id_len: int = 50,
) -> str:
    """
    任务命名 slug：{run_id}-{plan_id}，用于容器名、分支名、文件名等。
    
    Args:
        run_id: 运行 ID
        plan_id: 计划 ID（父上下文的 id）
        truncate_run_id: 若指定，取 run_id 末 N 位（如 8 用于容器名）
        max_plan_id_len: plan_id 最大长度，容器名等场景可用 20
    
    Returns:
        安全命名串
    """
    r = run_id[-truncate_run_id:] if truncate_run_id and len(run_id) > truncate_run_id else run_id
    safe_plan_id = plan_id.replace("/", "-").replace("\\", "-")[:max_plan_id_len]
    return f"{r}-{safe_plan_id}"


def check_work_dir_conflict(workspace: Path, run_id: str, plan_id: str) -> None:
    """
    检查 run_id + plan_id 的唯一性，如果目录已存在且有内容则抛异常。
    
    Raises:
        WorkDirConflictError: 如果目录已存在且包含 task.json
    """
    work_dir = build_work_dir(workspace, run_id, plan_id)
    if work_dir.exists() and (work_dir / "task.json").exists():
        raise WorkDirConflictError(
            f"工作目录冲突：{work_dir} 已存在。"
            f"run_id={run_id}, plan_id={plan_id} 组合必须唯一。"
        )


def get_work_dir(
        workspace: Path,
        task_id: str | None = None,
        run_id: str | None = None,
        plan_id: str | None = None,
        check_conflict: bool = True,
) -> Path:
    """
    获取工作目录。新结构：workspace/works/{run_id}/{plan_id}
    
    Args:
        workspace: 工作空间根目录
        task_id: 任务 ID（保留用于兼容，不再用于目录名）
        run_id: 运行批次 ID
        plan_id: 计划 ID（父上下文的 id）
        check_conflict: 是否检查唯一性冲突（默认 True）
    
    Returns:
        工作目录 Path，自动创建
        
    Raises:
        WorkDirConflictError: 如果 check_conflict=True 且目录已存在
    """
    if run_id is not None and plan_id is not None:
        if check_conflict:
            check_work_dir_conflict(workspace, run_id, plan_id)
        work_dir = build_work_dir(workspace, run_id, plan_id, task_id)
    else:
        work_dir = workspace / 'works'
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def write_run_meta(
        work_dir: Path,
        run_id: str,
        worker_id: str,
        plan_id: str,
        parent_run_id: str | None = None,
) -> Path:
    """
    写入运行元数据文件，记录父子任务关系。
    
    Args:
        work_dir: 工作目录
        run_id: 当前运行 ID
        worker_id: worker ID
        plan_id: 计划 ID（父上下文的 id）
        parent_run_id: 父任务的 run_id（用于 breakdown 子任务追溯）
                       如果为 None，会尝试从环境变量 BRO_PARENT_RUN_ID 读取
    
    Returns:
        元数据文件路径
    """
    import os
    
    meta = {
        "run_id": run_id,
        "worker_id": worker_id,
        "plan_id": plan_id,
    }
    
    effective_parent_run_id = parent_run_id or os.environ.get("BRO_PARENT_RUN_ID")
    if effective_parent_run_id:
        meta["parent_run_id"] = effective_parent_run_id
    
    meta_file = work_dir / RUN_META_FILE
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return meta_file


def read_run_meta(work_dir: Path) -> dict | None:
    """读取运行元数据"""
    meta_file = work_dir / RUN_META_FILE
    if not meta_file.exists():
        return None
    try:
        return json.loads(meta_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def get_run_dir(workspace: Path, run_id: str) -> Path:
    """
    获取 run_id 对应的目录：workspace/works/{run_id}/
    用于存放 run_mappings.json 等 run 级别的元数据。
    """
    run_dir = workspace / 'works' / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def read_run_mappings(workspace: Path, run_id: str) -> dict:
    """
    读取 run_mappings.json，返回映射关系。
    
    Returns:
        {"parent": "", "children": []} 格式的字典
    """
    run_dir = get_run_dir(workspace, run_id)
    mappings_file = run_dir / RUN_MAPPINGS_FILE
    if not mappings_file.exists():
        return {"parent": "", "children": []}
    try:
        data = json.loads(mappings_file.read_text())
        return {
            "parent": data.get("parent", ""),
            "children": list(data.get("children", [])),
        }
    except (json.JSONDecodeError, OSError):
        return {"parent": "", "children": []}


def write_run_mappings(workspace: Path, run_id: str, parent: str = "", children: list[str] | None = None) -> Path:
    """
    写入 run_mappings.json，记录父子任务映射关系。
    
    Args:
        workspace: 工作空间根目录
        run_id: 当前 run_id
        parent: 父任务的 run_id（空字符串表示无父任务）
        children: 子任务 run_id 列表
    
    Returns:
        映射文件路径
    """
    run_dir = get_run_dir(workspace, run_id)
    mappings_file = run_dir / RUN_MAPPINGS_FILE
    
    data = {
        "parent": parent,
        "children": children or [],
    }
    mappings_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return mappings_file


def add_child_run_mapping(workspace: Path, parent_run_id: str, child_run_id: str) -> Path:
    """
    向父任务的 run_mappings.json 追加一个子 run_id。
    
    Args:
        workspace: 工作空间根目录
        parent_run_id: 父任务的 run_id
        child_run_id: 要添加的子任务 run_id
    
    Returns:
        映射文件路径
    """
    mappings = read_run_mappings(workspace, parent_run_id)
    if child_run_id not in mappings["children"]:
        mappings["children"].append(child_run_id)
    return write_run_mappings(workspace, parent_run_id, mappings["parent"], mappings["children"])


def set_parent_run_mapping(workspace: Path, child_run_id: str, parent_run_id: str) -> Path:
    """
    设置子任务的 run_mappings.json 的 parent 字段。
    
    Args:
        workspace: 工作空间根目录
        child_run_id: 子任务的 run_id
        parent_run_id: 父任务的 run_id
    
    Returns:
        映射文件路径
    """
    mappings = read_run_mappings(workspace, child_run_id)
    mappings["parent"] = parent_run_id
    return write_run_mappings(workspace, child_run_id, mappings["parent"], mappings["children"])


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


def _parse_skill_refs(skill_refs: list) -> tuple[list[str], str]:
    """
    解析 skill_refs，从 Knowledge API 缓存获取 skill 信息。

    旧格式: ["backend-dev", "frontend-dev"]
    新格式: [{"id": "backend-dev", "description": "后端开发"}, ...]

    skill 信息统一从 Knowledge API 加载（通过 registry 缓存）。

    Returns:
        (skill_ids, skill_descriptions_text)
    """
    if not skill_refs:
        return [], ""

    from broker.skill.registry import get_skill_info

    skill_ids: list[str] = []
    descriptions: list[str] = []

    for ref in skill_refs:
        if isinstance(ref, str):
            skill_id = ref
            skill_ids.append(skill_id)
            try:
                info = get_skill_info(skill_id)
                desc = info.get("description") or ""
                if desc:
                    descriptions.append(f"- {skill_id}: {desc}")
            except Exception:
                pass
        elif isinstance(ref, dict):
            skill_id = ref.get("id") or ref.get("skill_id") or ""
            if skill_id:
                skill_ids.append(skill_id)
                desc = ref.get("description") or ""
                if not desc:
                    try:
                        info = get_skill_info(skill_id)
                        desc = info.get("description") or ""
                    except Exception:
                        pass
                if desc:
                    descriptions.append(f"- {skill_id}: {desc}")

    desc_text = ""
    if descriptions:
        desc_text = (
            "\n\nAvailable skills and their descriptions:\n"
            + "\n".join(descriptions)
            + "\n\nSelect the most appropriate skill for each sub-task based on the requirement content."
        )

    return skill_ids, desc_text


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

    skill_refs supports two formats:
    - Old: ["backend-dev", "frontend-dev"]
    - New: [{"id": "backend-dev", "description": "后端开发", "match_rules": {...}}, ...]
    """
    task_block = get_task_block(task)
    base_instructions = list(task_block.get("instructions") or [])
    if round_context:
        base_instructions = base_instructions + [round_context]
    if audit_context and audit_context.strip():
        base_instructions = base_instructions + [audit_context.strip()]

    skill_refs = task.get("skill_refs")
    skill_ids: list[str] = []
    if skill_refs:
        skill_ids, skill_desc_text = _parse_skill_refs(skill_refs)
        write_path = ""
        if work_dir is not None:
            write_path = f"Write to {work_dir.resolve() / BREAKDOWN_JSON}. "
        skill_instruction = (
            f"Implementation skills (skill_refs): {', '.join(skill_ids)}. "
            f"After preparation (validation, analysis): {write_path}Create breakdown.json with format "
            '[{"id": "<unique-subtask-id>", "skill": "<skill_id>", "requirement": "..."}, ...]. The broker will invoke these skills. '
            "Each item MUST have 'id' (unique identifier for tracking, e.g. 'auth-api', 'user-service', use descriptive kebab-case), "
            "'skill' (from skill_refs), and params like 'requirement'. "
            "When the requirement explicitly specifies a platform (e.g. native, iOS, web, weapp), add 'scope' to each item, e.g. {\"scope\": \"apps/native\"} for native iOS."
        )
        if skill_desc_text:
            skill_instruction += skill_desc_text
        base_instructions = base_instructions + [skill_instruction]

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
    if skill_ids:
        payload["skill_refs"] = skill_ids
    if type_config:
        payload["constraints"] = dict(type_config.get("constraints") or {})
        if type_config.get("generated_marker"):
            payload["generated_marker"] = True
    return payload
