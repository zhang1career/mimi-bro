"""
Input validation and error handling for broker.task: load_task, substitute_task.
Tests file-not-found, invalid JSON, non-dict payload, and substitute_task type checks.
Generated. Do not modify core safety logic via agent.
"""
from pathlib import Path

import pytest

from broker.task import load_task, substitute_task


def test_load_task_file_not_found(tmp_path: Path) -> None:
    """load_task raises FileNotFoundError when file does not exist."""
    with pytest.raises(FileNotFoundError) as exc_info:
        load_task(str(tmp_path / "nonexistent.json"))
    assert "not found" in str(exc_info.value).lower() or "nonexistent" in str(exc_info.value)


def test_load_task_invalid_json(tmp_path: Path) -> None:
    """load_task raises JSONDecodeError for invalid JSON."""
    import json
    bad = tmp_path / "bad.json"
    bad.write_text("not json {")
    with pytest.raises(json.JSONDecodeError):
        load_task(str(bad))


def test_load_task_non_dict_json(tmp_path: Path) -> None:
    """load_task raises ValueError when JSON is not an object (e.g. array or string)."""
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]")
    with pytest.raises(ValueError) as exc_info:
        load_task(str(arr))
    assert "dict" in str(exc_info.value).lower() or "object" in str(exc_info.value).lower()

    s = tmp_path / "str.json"
    s.write_text('"hello"')
    with pytest.raises(ValueError):
        load_task(str(s))


def test_load_task_valid_dict(tmp_path: Path) -> None:
    """load_task returns dict when file contains valid JSON object."""
    f = tmp_path / "task.json"
    f.write_text('{"id": "test", "objective": "do something"}')
    out = load_task(str(f))
    assert isinstance(out, dict)
    assert out["id"] == "test"


def test_substitute_task_non_dict_raises() -> None:
    """substitute_task raises TypeError when task is not a dict."""
    with pytest.raises(TypeError) as exc_info:
        substitute_task([], {})
    assert "dict" in str(exc_info.value).lower()

    with pytest.raises(TypeError):
        substitute_task("task", {})


def test_substitute_task_none_params_returns_unchanged() -> None:
    """substitute_task returns task unchanged when params is None or not a dict."""
    task = {"id": "x", "objective": "{{foo}}"}
    assert substitute_task(task, None) is task
    assert substitute_task(task, "not a dict") is task  # params is not dict => return task


def test_substitute_task_empty_params_returns_unchanged() -> None:
    """substitute_task returns task when params is empty dict (no substitution)."""
    task = {"id": "x"}
    result = substitute_task(task, {})
    assert result == task
