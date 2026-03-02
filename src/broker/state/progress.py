"""
Multi-step task progress: completed_step_indices, last_round_result.
Stored under .state/workers/<task_id>/progress.json (Broker host).

New structure (v2): progress.json is a dict with subtask_id as key:
{
  "<subtask_id>": [
    {"run_id": "...", "completed_step_indices": [...], ...},
    ...
  ]
}

For workers without subtasks (single execution), use task_id as the subtask_id.
This ensures consistent structure while supporting parent-child task hierarchies.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from broker.utils.file_lock import file_lock
from broker.utils.path_util import PROJECT_ROOT


def _progress_dir(task_id: str) -> Path:
    return PROJECT_ROOT / ".state" / "workers" / task_id


def _progress_file(task_id: str) -> Path:
    return _progress_dir(task_id) / "progress.json"


def _lock_file(task_id: str) -> Path:
    return _progress_dir(task_id) / "progress.lock"


def _load_progress_dict_unlocked(task_id: str) -> dict[str, list[dict]]:
    """Load progress dict without locking (caller must hold lock)."""
    path = _progress_file(task_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def _load_progress_dict(task_id: str) -> dict[str, list[dict]]:
    """Load progress dict for task_id; return empty dict if not found or invalid."""
    d = _progress_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    with file_lock(_lock_file(task_id), "a", exclusive=False):
        return _load_progress_dict_unlocked(task_id)


def get_progress_dict(task_id: str) -> dict[str, list[dict]]:
    """Public read-only: return full progress structure for task_id (subtask_id -> list of runs)."""
    return _load_progress_dict(task_id)


def _save_progress_dict_unlocked(task_id: str, progress_dict: dict[str, list[dict]]) -> None:
    """Write progress dict without locking (caller must hold lock)."""
    d = _progress_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    _progress_file(task_id).write_text(json.dumps(progress_dict, indent=2, ensure_ascii=False))


def _save_progress_dict(task_id: str, progress_dict: dict[str, list[dict]]) -> None:
    """Write progress dict to progress file with locking."""
    d = _progress_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    with file_lock(_lock_file(task_id), "a", exclusive=True):
        _save_progress_dict_unlocked(task_id, progress_dict)


def _load_all_runs_unlocked(task_id: str, subtask_id: str | None = None) -> list[dict]:
    """Load all run records without locking (caller must hold lock)."""
    key = subtask_id or task_id
    progress_dict = _load_progress_dict_unlocked(task_id)
    return progress_dict.get(key, [])


def _load_all_runs(task_id: str, subtask_id: str | None = None) -> list[dict]:
    """Load all run records for task_id (optionally filtered by subtask_id).
    If subtask_id is None, uses task_id as the subtask key (backward compatible)."""
    key = subtask_id or task_id
    progress_dict = _load_progress_dict(task_id)
    return progress_dict.get(key, [])


def load_progress(task_id: str, run_id: str, subtask_id: str | None = None) -> dict | None:
    """Load progress for specific run_id; return None if not found.
    subtask_id: if provided, look under that subtask key; else use task_id."""
    runs = _load_all_runs(task_id, subtask_id)
    for r in runs:
        if r.get("run_id") == run_id:
            return r
    return None


def save_progress(
    task_id: str,
    run_id: str,
    completed_step_indices: list[int],
    last_round_result: dict | None = None,
    retry_counts: dict[str, int] | None = None,
    subtask_id: str | None = None,
) -> None:
    """Append or update progress for run_id under subtask_id (or task_id if not provided).
    Most recent run is at list end. Uses file locking for concurrent access."""
    key = subtask_id or task_id
    d = _progress_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    
    with file_lock(_lock_file(task_id), "a", exclusive=True):
        progress_dict = _load_progress_dict_unlocked(task_id)
        runs = progress_dict.get(key, [])
        
        payload = {
            "run_id": run_id,
            "task_id": task_id,
            "completed_step_indices": completed_step_indices,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        if subtask_id:
            payload["subtask_id"] = subtask_id
        if last_round_result is not None:
            payload["last_round_result"] = last_round_result
        if retry_counts is not None:
            payload["retry_counts"] = retry_counts

        found = False
        for i, r in enumerate(runs):
            if r.get("run_id") == run_id:
                runs[i] = payload
                found = True
                break
        if not found:
            runs.append(payload)

        progress_dict[key] = runs
        _save_progress_dict_unlocked(task_id, progress_dict)


def clear_progress(
    task_id: str,
    run_id: str | None = None,
    subtask_id: str | None = None,
    level: int | None = None,
) -> None:
    """Remove progress based on parameters. Uses file locking for concurrent access.

    level controls clearing behavior for parent-child workers:
      None or -1: clear nothing (continue all)
      0: clear all (parent and all subtasks)
      n (n>0): clear subtasks at level > n (keep parent and first n levels)

    If run_id is provided, clear only that specific run.
    If subtask_id is provided, clear only that subtask's runs.
    """
    if level == -1:
        return

    d = _progress_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    
    with file_lock(_lock_file(task_id), "a", exclusive=True):
        if level == 0 or (run_id is None and subtask_id is None and level is None):
            path = _progress_file(task_id)
            if path.exists():
                path.unlink()
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
            return

        progress_dict = _load_progress_dict_unlocked(task_id)

        if subtask_id is not None:
            if run_id is None:
                if subtask_id in progress_dict:
                    del progress_dict[subtask_id]
            else:
                runs = progress_dict.get(subtask_id, [])
                runs = [r for r in runs if r.get("run_id") != run_id]
                if runs:
                    progress_dict[subtask_id] = runs
                elif subtask_id in progress_dict:
                    del progress_dict[subtask_id]
        elif run_id is not None:
            for key in list(progress_dict.keys()):
                runs = progress_dict[key]
                runs = [r for r in runs if r.get("run_id") != run_id]
                if runs:
                    progress_dict[key] = runs
                else:
                    del progress_dict[key]

        _save_progress_or_cleanup_unlocked(progress_dict, task_id)


def clear_subtasks_progress(task_id: str, keep_parent: bool = True) -> None:
    """Clear all subtask progress, optionally keeping the parent task's progress.
    Used when --fresh n where n > 0 (keep parent, re-execute subtasks).
    Uses file locking for concurrent access."""
    d = _progress_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    
    with file_lock(_lock_file(task_id), "a", exclusive=True):
        progress_dict = _load_progress_dict_unlocked(task_id)
        if keep_parent:
            parent_runs = progress_dict.get(task_id, [])
            progress_dict = {task_id: parent_runs} if parent_runs else {}
        else:
            progress_dict = {}

        _save_progress_or_cleanup_unlocked(progress_dict, task_id)


def _save_progress_or_cleanup_unlocked(progress_dict, task_id):
    """Save or cleanup progress dict without locking (caller must hold lock)."""
    if progress_dict:
        _save_progress_dict_unlocked(task_id, progress_dict)
    else:
        path = _progress_file(task_id)
        if path.exists():
            path.unlink()
        d = _progress_dir(task_id)
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
