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
    if not params:
        return task
    return _substitute_value(task, params)


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
        for base in (workspace, project_root):
            if base is None:
                continue
            alt = (Path(base) / path).resolve()
            if not alt.suffix:
                alt = alt.with_suffix(".json")
            if alt.exists():
                p = alt
                break
    if not p.exists():
        msg = f"Task file not found: {p}"
        if workspace is not None or project_root is not None:
            msg += f" (also tried under workspace {workspace}, project_root {project_root})"
        raise FileNotFoundError(msg)
    with open(p, encoding="utf-8") as f:
        return json.load(f)
