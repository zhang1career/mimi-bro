"""
Phase 4.2: Auto rules/tests for deviation from optional expected_results; human override via accept/retry.
Tests run_audit with expected_results: match => no escalation; deviation => escalation + deviation_reasons.
Generated. Do not modify core safety logic via agent.
"""
from pathlib import Path

import pytest

from broker.audit.skeleton import run_audit


def test_audit_no_expected_results_no_escalation(tmp_path: Path) -> None:
    """Without expected_results, success result => not escalated."""
    result = {"status": "success", "code": 0}
    record = run_audit(tmp_path, 0, result, 1, expected_results=None)
    assert record["escalated"] is False
    assert "deviation_reasons" not in record or not record.get("deviation_reasons")


def test_audit_expected_results_match_no_escalation(tmp_path: Path) -> None:
    """With expected_results matching result => not escalated (Phase 4.2 optional expected)."""
    result = {"status": "success", "code": 0}
    expected = {"status": "success", "exit_code": 0}
    record = run_audit(tmp_path, 0, result, 1, expected_results=expected)
    assert record["escalated"] is False
    assert "deviation_from_expected" not in (record.get("criteria_used") or [])


def test_audit_expected_results_deviation_escalation(tmp_path: Path) -> None:
    """Deviation from expected_results => escalated, deviation_reasons set (Phase 4.2 auto rule)."""
    result = {"status": "success", "code": 1}
    expected = {"status": "success", "exit_code": 0}
    record = run_audit(tmp_path, 0, result, 1, expected_results=expected)
    assert record["escalated"] is True
    assert "deviation_from_expected" in (record.get("criteria_used") or [])
    assert record.get("deviation_reasons")
    assert "exit_code" in " ".join(record["deviation_reasons"])


def test_audit_expected_results_status_deviation(tmp_path: Path) -> None:
    """Status mismatch => deviation_reasons mention status."""
    result = {"status": "failed", "code": 0}
    expected = {"status": "success", "exit_code": 0}
    record = run_audit(tmp_path, 0, result, 1, expected_results=expected)
    assert record["escalated"] is True
    assert any("status" in r for r in record.get("deviation_reasons", []))


def test_audit_no_result_but_expected_deviation(tmp_path: Path) -> None:
    """No result vs expected => deviation (escalate)."""
    expected = {"status": "success", "exit_code": 0}
    record = run_audit(tmp_path, 0, None, 1, expected_results=expected)
    assert record["escalated"] is True
    assert "no result vs expected" in record.get("deviation_reasons", [])
