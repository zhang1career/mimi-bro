"""
Phase 5.1: Audit-to-bootstrap. Tests list_task_ids_with_audits and get_audit_summary_for_boost.
Generated. Do not modify core safety logic via agent.
"""
from pathlib import Path

import pytest

from broker.audit.store import (
    get_audit_summary_for_boost,
    list_task_ids_with_audits,
    load_audits,
    save_audit_record,
)
from broker.utils.work_util import build_task_payload


def test_list_task_ids_with_audits_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("broker.audit.store.PROJECT_ROOT", tmp_path)
    assert list_task_ids_with_audits() == []


def test_list_task_ids_with_audits_and_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("broker.audit.store.PROJECT_ROOT", tmp_path)
    (tmp_path / ".state" / "workers" / "greetings" / "audit").mkdir(parents=True)
    (tmp_path / ".state" / "workers" / "boost" / "audit").mkdir(parents=True)
    # Write audit files via save_audit_record (it uses PROJECT_ROOT)
    save_audit_record("greetings", {"round_index": 0, "escalated": True, "conclusion": "non-zero exit", "criteria_used": ["exit_code_nonzero"]})
    save_audit_record("boost", {"round_index": 0, "escalated": False, "conclusion": "pass"})

    ids = list_task_ids_with_audits()
    assert "boost" in ids
    assert "greetings" in ids

    summary = get_audit_summary_for_boost(exclude_task_id="boost", max_per_task=10)
    assert "greetings" in summary
    assert "boost" not in summary
    assert "non-zero exit" in summary or "exit_code_nonzero" in summary


def test_get_audit_summary_for_boost_empty_when_no_other_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("broker.audit.store.PROJECT_ROOT", tmp_path)
    (tmp_path / ".state" / "workers" / "boost" / "audit").mkdir(parents=True)
    save_audit_record("boost", {"round_index": 0, "escalated": True, "conclusion": "x"})
    summary = get_audit_summary_for_boost(exclude_task_id="boost")
    assert summary == ""


def test_audit_summary_includes_escalation_criteria_for_system_fixes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit context from other workers: criteria (retry_count_gt_3, status_not_success_or_failed, exit_code_nonzero) surface for boost to consider system fixes."""
    monkeypatch.setattr("broker.audit.store.PROJECT_ROOT", tmp_path)
    for tid in ("audit-test-task", "test-audit", "boost"):
        (tmp_path / ".state" / "workers" / tid / "audit").mkdir(parents=True)
    save_audit_record("audit-test-task", {
        "round_index": 0,
        "escalated": True,
        "conclusion": "retries>3",
        "criteria_used": ["retry_count_gt_3"],
    })
    save_audit_record("test-audit", {
        "round_index": 0,
        "escalated": True,
        "conclusion": "ambiguous outcome; non-zero exit; retries>3",
        "criteria_used": ["status_not_success_or_failed", "exit_code_nonzero", "retry_count_gt_3"],
    })
    save_audit_record("boost", {"round_index": 0, "escalated": False, "conclusion": "pass"})

    summary = get_audit_summary_for_boost(exclude_task_id="boost", max_per_task=10)
    assert "Audit context from other workers" in summary
    assert "consider for system fixes" in summary
    assert "audit-test-task" in summary
    assert "test-audit" in summary
    assert "boost" not in summary
    assert "retry_count_gt_3" in summary
    assert "status_not_success_or_failed" in summary
    assert "exit_code_nonzero" in summary


def test_bootstrap_payload_includes_audit_context_in_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 5.1: bootstrap task payload gets audit_context injected as instruction for boost agent."""
    monkeypatch.setattr("broker.audit.store.PROJECT_ROOT", tmp_path)
    (tmp_path / ".state" / "workers" / "greetings" / "audit").mkdir(parents=True)
    save_audit_record("greetings", {"round_index": 0, "escalated": True, "conclusion": "non-zero exit", "criteria_used": ["exit_code_nonzero"]})

    task = {"worker": {"id": "boost", "type": "bootstrap", "objective": "Self-bootstrap.", "instructions": ["Follow DESIGN.md."]}}
    agent = {"id": "backend", "role": "backend", "mode": "agent"}
    audit_context = get_audit_summary_for_boost(exclude_task_id="boost")
    payload = build_task_payload(task, agent, audit_context=audit_context or None)

    assert payload.get("type") == "bootstrap"
    instructions = payload.get("instructions") or []
    assert any("Audit context from other workers" in (i or "") for i in instructions)
    assert any("greetings" in (i or "") for i in instructions)
