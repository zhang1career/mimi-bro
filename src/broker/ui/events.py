"""
Event protocol for bro submit display layer.
Used by CLI TUI, PlainDriver, and JsonlDriver (IDE plugin consumption).
"""
from __future__ import annotations

import json
from typing import Any


def emit_progress(
    parent_current: int,
    parent_total: int,
    child_tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build progress event. child_tasks = [{subtask_id, current, total, color}, ...]"""
    evt: dict[str, Any] = {
        "type": "progress",
        "parent": {"current": parent_current, "total": parent_total},
    }
    if child_tasks:
        evt["child_tasks"] = child_tasks
    return evt


def emit_task_tree(nodes: list[dict[str, Any]], running_ids: set[str] | None = None) -> dict[str, Any]:
    """Build task tree event. nodes = [{id, label, parent_id?, work_dir?}, ...]"""
    evt: dict[str, Any] = {"type": "task_tree", "nodes": nodes}
    if running_ids is not None:
        evt["running_ids"] = list(running_ids)
    return evt


def emit_log_paths(paths: list[dict[str, Any]], lines_per_file: int = 3) -> dict[str, Any]:
    """Build log paths event. paths = [{path, worker_id?, role?}, ...]"""
    return {
        "type": "log_paths",
        "paths": paths,
        "lines_per_file": lines_per_file,
    }


def emit_task_assigned(
    worker_id: str,
    objective_preview: str,
    assignee: str | None = None,
    subtask_id: str | None = None,
) -> dict[str, Any]:
    """Build task assigned event."""
    evt: dict[str, Any] = {
        "type": "task_assigned",
        "worker_id": worker_id,
        "objective_preview": objective_preview,
    }
    if assignee:
        evt["assignee"] = assignee
    if subtask_id:
        evt["subtask_id"] = subtask_id
    return evt


def emit_result(
    worker_id: str,
    role: str,
    status: str,
    work_dir: str,
    exit_code: int | None = None,
) -> dict[str, Any]:
    """Build result event."""
    evt: dict[str, Any] = {
        "type": "result",
        "worker_id": worker_id,
        "role": role,
        "status": status,
        "work_dir": str(work_dir),
    }
    if exit_code is not None:
        evt["exit_code"] = exit_code
    return evt


def emit_verbose(message: str) -> dict[str, Any]:
    """Build verbose log event (only when --verbose)."""
    return {"type": "verbose", "message": message}


def emit_status(message: str, elapsed_seconds: float | None = None) -> dict[str, Any]:
    """Build status event: current operation and optional elapsed time."""
    evt: dict[str, Any] = {"type": "status", "message": message}
    if elapsed_seconds is not None:
        evt["elapsed_seconds"] = elapsed_seconds
    return evt


def emit_console(message: str) -> dict[str, Any]:
    """Build console message event: broker-level info (e.g. skill API result, choice)."""
    return {"type": "console", "message": message}


def emit_confirm_deps_request(
    request_id: str,
    graph_text: str,
    nodes: list[str],
    edges: list[tuple[str, str]],
) -> dict[str, Any]:
    """Build dependency confirmation request event.

    Args:
        request_id: Unique ID for this request (for matching response)
        graph_text: Human-readable dependency graph text
        nodes: List of node IDs
        edges: List of (from_id, to_id) tuples
    """
    return {
        "type": "confirm_deps_request",
        "request_id": request_id,
        "graph_text": graph_text,
        "nodes": nodes,
        "edges": [(e[0], e[1]) for e in edges],
    }


def emit_confirm_skills_request(
    request_id: str,
    items: list[dict[str, Any]],
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Build skill selection confirmation request event.

    Args:
        request_id: Unique ID for this request (for matching response)
        items: List of items to confirm, each with:
            - item_id: Subtask ID
            - requirement: Brief description
            - current_skill: Currently selected skill ID
            - available_skills: List of available skill IDs
            - source: How skill was selected ("rule", "agent", "default")
        timeout_seconds: Seconds before auto-confirm (default 60)
    """
    return {
        "type": "confirm_skills_request",
        "request_id": request_id,
        "items": items,
        "timeout_seconds": timeout_seconds,
    }


def emit_confirm_skills_timeout(request_id: str) -> dict[str, Any]:
    """Build skill confirmation timeout event (auto-confirm triggered)."""
    return {
        "type": "confirm_skills_timeout",
        "request_id": request_id,
    }


def emit_run_external_request(
    request_id: str,
    args: list[str],
    cwd: str | None = None,
) -> dict[str, Any]:
    """Build external command request event.

    Used for interactive terminal programs (like git mergetool/vimdiff) that need
    exclusive terminal access. The TUI should suspend before running.

    Args:
        request_id: Unique ID for this request (for matching response)
        args: Command and arguments to run
        cwd: Working directory for the command
    """
    evt: dict[str, Any] = {
        "type": "run_external_request",
        "request_id": request_id,
        "args": args,
    }
    if cwd:
        evt["cwd"] = cwd
    return evt


def to_jsonl(event: dict[str, Any]) -> str:
    """Serialize event to JSONL line."""
    return json.dumps(event, ensure_ascii=False) + "\n"
