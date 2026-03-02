"""
Load task definition from JSON. System input and internal data use JSON for a unified format.
Templates: any string value (including task.id, task.objective, task.instructions[i]) may use
placeholders {{key}}; fill with substitute_task(task, params) and --arg key=value in bro submit.
Double braces reduce conflict with common text like {variable} or {filename}.
"""
from __future__ import annotations

import json
from pathlib import Path


def _substitute_value(val, params: dict):
    """Recursively replace {{key}} in all string values; list/dict traversed, other types unchanged."""
    if isinstance(val, dict):
        return {k: _substitute_value(v, params) for k, v in val.items()}
    if isinstance(val, list):
        return [_substitute_value(item, params) for item in val]
    if isinstance(val, str):
        for k, v in params.items():
            val = val.replace("{{" + k + "}}", str(v))
        return val
    return val


def substitute_task(task: dict, params: dict) -> dict:
    """
    Return a copy of task with every string value substituted: {{key}} -> params[key].
    Supports task.id, task.objective, task.instructions[i], and any nested string.
    Missing params leave placeholder unchanged (e.g. {{unknown}} stays as-is).
    """
    if not isinstance(task, dict):
        raise TypeError("task must be a dict")
    if params is None or not isinstance(params, dict):
        return task
    return _substitute_value(task, params)


PLANS_OVERRIDE_FIELDS = {"role"}


def apply_params_to_plans(task: dict, params: dict) -> dict:
    """
    Apply params to plans elements by field-level override (not template substitution).
    
    For fields in PLANS_OVERRIDE_FIELDS (e.g. 'role'), if params contains the field,
    override that field in each plans element.
    
    This allows parent task to pass role to child task via --arg role=xxx,
    and the child task's plans will use the parent's role instead of its own.
    """
    if not isinstance(task, dict) or not isinstance(params, dict):
        return task
    
    plans = task.get("plans")
    if not plans or not isinstance(plans, list):
        return task
    
    overrides = {k: v for k, v in params.items() if k in PLANS_OVERRIDE_FIELDS}
    if not overrides:
        return task
    
    import copy
    task = copy.deepcopy(task)
    for plan_item in task["plans"]:
        if isinstance(plan_item, dict):
            for field, value in overrides.items():
                plan_item[field] = value
    
    return task


def _find_project_root(workspace: Path) -> Path:
    """Find project root: first ancestor of workspace that contains a 'workers' directory."""
    p = Path(workspace).resolve()
    while p != p.parent:
        if (p / "workers").is_dir():
            return p
        p = p.parent
    return Path(workspace).resolve()


def load_task(path: str, workspace: Path | None = None, project_root: Path | None = None) -> dict:
    """Load task from a JSON file. Path may be .json or omit extension.
    If path is relative and file not found under cwd, resolve under workspace (when provided),
    then project_root when provided (e.g. for --local when workers/ is at repo root)."""
    p = Path(path)
    if not p.suffix:
        p = p.with_suffix(".json")
    if not p.is_absolute() and not p.exists():
        p = _resolve_in_bases(p, workspace, project_root, path)
    if not p.exists():
        # Fallback: tasks/xxx.json -> workers/xxx.json (skills may still use tasks/)
        if path.startswith("tasks/"):
            fallback_path = "workers/" + path[6:]
            p = _resolve_in_bases(p, workspace, project_root, fallback_path)
        if not p.exists():
            msg = f"Task file not found: {p}"
            if workspace is not None or project_root is not None:
                msg += f" (also tried under workspace {workspace}, project_root {project_root})"
            raise FileNotFoundError(msg)
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"Task file must contain a JSON object (dict), got {type(raw).__name__}")
    return raw


def _resolve_in_bases(path, workspace, project_root, fallback_path):
    for base in (workspace, project_root):
        if base is None:
            continue
        cand = (Path(base) / fallback_path).resolve()
        if not cand.suffix:
            cand = cand.with_suffix(".json")
        if cand.exists():
            path = cand
            break
    return path
