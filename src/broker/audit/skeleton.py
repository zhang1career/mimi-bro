"""
Post-run auto-audit skeleton. Criteria optional; AI supplements; conclusion source = human | AI.
Escalate when: ambiguous (unclear outcome), hard (non-zero exit), retry_count > 3,
 or deviation from optional expected_results.
"""
from __future__ import annotations

from pathlib import Path


def _check_expected(result: dict | None, expected: dict) -> tuple[bool, list[str]]:
    """
    Compare result to optional expected_results. Returns (deviated, list of deviation reasons).
    expected may have: status, exit_code (or code).
    """
    deviated = False
    reasons: list[str] = []
    if not result and expected:
        return True, ["no result vs expected"]
    actual_status = (result or {}).get("status", "unknown")
    actual_code = (result or {}).get("code", (result or {}).get("exit_code", 1))
    if isinstance(actual_code, str):
        try:
            actual_code = int(actual_code)
        except (TypeError, ValueError):
            actual_code = 1
    if "status" in expected:
        exp_status = expected["status"]
        if str(actual_status) != str(exp_status):
            deviated = True
            reasons.append(f"status: expected {exp_status}, got {actual_status}")
    if "exit_code" in expected:
        exp_code = expected["exit_code"]
        if isinstance(exp_code, str):
            try:
                exp_code = int(exp_code)
            except (TypeError, ValueError):
                exp_code = 0
        if actual_code != exp_code:
            deviated = True
            reasons.append(f"exit_code: expected {exp_code}, got {actual_code}")
    if "code" in expected and "exit_code" not in expected:
        exp_code = expected["code"]
        if isinstance(exp_code, str):
            try:
                exp_code = int(exp_code)
            except (TypeError, ValueError):
                exp_code = 0
        if actual_code != exp_code:
            deviated = True
            reasons.append(f"code: expected {exp_code}, got {actual_code}")
    return deviated, reasons


def supplement_with_ai(record: dict) -> dict:
    """
    Hook for AI to supplement the audit record. Override or replace to call external AI;
    default no-op. Return the same or updated record (e.g. extra keys, refined conclusion).
    """
    return record


def run_audit(
    work_dir: Path,
    round_index: int,
    result: dict | None,
    retry_count: int,
    criteria: list[str] | None = None,
    expected_results: dict | None = None,
) -> dict:
    """
    Run post-run audit for one round. Does not call external AI; AI supplementation is a future hook.
    Returns record with conclusion, conclusion_source ("ai" | "human"), escalated, criteria_used.
    Phase 4.2: optional expected_results; deviation triggers escalation (auto rule) and human override.
    """
    status = (result or {}).get("status", "unknown")
    exit_code = (result or {}).get("code", 1)
    if isinstance(exit_code, str):
        try:
            exit_code = int(exit_code)
        except (TypeError, ValueError):
            exit_code = 1

    criteria_used = list(criteria) if criteria else []
    # Rule-based supplement (skeleton: no real AI)
    ambiguous = status not in ("success", "failed")
    hard = exit_code != 0
    retry_over = retry_count > 3

    # Phase 4.2: optional expected_results as auto rule; deviation => escalate
    deviation = False
    deviation_reasons: list[str] = []
    if expected_results:
        deviation, deviation_reasons = _check_expected(result, expected_results)
        if deviation:
            criteria_used.append("deviation_from_expected")

    if ambiguous:
        criteria_used.append("status_not_success_or_failed")
    if hard:
        criteria_used.append("exit_code_nonzero")
    if retry_over:
        criteria_used.append("retry_count_gt_3")

    escalated = ambiguous or hard or retry_over or deviation
    conclusion = "pass"
    if escalated:
        reasons = []
        if deviation:
            reasons.append("deviation: " + "; ".join(deviation_reasons))
        if ambiguous:
            reasons.append("ambiguous outcome")
        if hard:
            reasons.append("non-zero exit")
        if retry_over:
            reasons.append("retries>3")
        conclusion = "; ".join(reasons)

    record = {
        "round_index": round_index,
        "work_dir": str(work_dir),
        "conclusion": conclusion,
        "conclusion_source": "ai",  # skeleton: rule-based only; human override sets "human"
        "conclusion_notes": None,  # optional free-text; human or AI can set
        "conclusion_notes_source": "ai",  # "human" | "AI"; who supplied conclusion_notes
        "criteria_used": criteria_used,
        "escalated": escalated,
        "reason": conclusion if escalated else None,
        "retry_count": retry_count,
        "status": status,
        "exit_code": exit_code,
    }
    if expected_results is not None:
        record["expected_results"] = expected_results
    if deviation_reasons:
        record["deviation_reasons"] = deviation_reasons
    return supplement_with_ai(record)
