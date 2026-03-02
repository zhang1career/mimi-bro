"""
Input validation and edge-case handling for broker.agent.runner.
Tests _validate_agents_and_task and _normalize_step behavior.
Generated. Do not modify core safety logic via agent.
"""

import pytest

from broker.agent.runner import normalize_step, validate_agents_and_task


def test_validate_agents_empty_raises() -> None:
    """_validate_agents_and_task raises when agents is empty."""
    with pytest.raises(ValueError) as exc_info:
        validate_agents_and_task([], {"id": "t"})
    assert "empty" in str(exc_info.value).lower() or "not" in str(exc_info.value).lower()


def test_validate_agents_not_list_raises() -> None:
    """_validate_agents_and_task raises when agents is not a list."""
    with pytest.raises(ValueError) as exc_info:
        validate_agents_and_task({"id": "a", "role": "r"}, {"id": "t"})
    assert "list" in str(exc_info.value).lower()


def test_validate_agents_missing_id_raises() -> None:
    """_validate_agents_and_task raises when an agent has no 'id'."""
    with pytest.raises(ValueError) as exc_info:
        validate_agents_and_task([{"role": "r"}], {"id": "t"})
    assert "id" in str(exc_info.value).lower()


def test_validate_agents_missing_role_raises() -> None:
    """_validate_agents_and_task raises when an agent has no 'role'."""
    with pytest.raises(ValueError) as exc_info:
        validate_agents_and_task([{"id": "a"}], {"id": "t"})
    assert "role" in str(exc_info.value).lower()


def test_validate_agents_non_dict_item_raises() -> None:
    """_validate_agents_and_task raises when an agent item is not a dict."""
    with pytest.raises(ValueError) as exc_info:
        validate_agents_and_task([{"id": "a", "role": "r"}, "not a dict"], {"id": "t"})
    assert "dict" in str(exc_info.value).lower()


def test_validate_agents_task_not_dict_raises() -> None:
    """_validate_agents_and_task raises when task is not a dict."""
    with pytest.raises(ValueError) as exc_info:
        validate_agents_and_task([{"id": "a", "role": "r"}], [])
    assert "task" in str(exc_info.value).lower() or "dict" in str(exc_info.value).lower()


def test_validate_agents_and_task_valid() -> None:
    """_validate_agents_and_task does not raise for valid agents and task."""
    validate_agents_and_task([{"id": "a", "role": "r"}], {"id": "t"})


def test_normalize_step_string() -> None:
    """_normalize_step with str step returns objective and no validate_with."""
    out = normalize_step("do something", None)
    assert out["objective"] == "do something"
    assert out["validate_with"] is None
    assert out["validate_only"] is False


def test_normalize_step_dict() -> None:
    """_normalize_step with dict step returns fields from step."""
    step = {"objective": "build", "validate_with": "workers/check.json", "validate_only": True}
    out = normalize_step(step, None)
    assert out["objective"] == "build"
    assert out["validate_with"] == "workers/check.json"
    assert out["validate_only"] is True


def test_normalize_step_non_str_non_dict() -> None:
    """_normalize_step with non-str non-dict returns safe default (empty objective)."""
    out = normalize_step(123, None)
    assert out["objective"] == ""
    assert out["validate_with"] is None
    assert out["validate_only"] is False
