"""
Persist audit records under .state/workers/<worker_id>/audit/.
Conclusion notes source (human vs AI) is stored in each record.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from broker.utils.path_util import PROJECT_ROOT


def _audit_dir(worker_id: str) -> Path:
    return PROJECT_ROOT / ".state" / "workers" / worker_id / "audit"


def _audit_file(worker_id: str) -> Path:
    return _audit_dir(worker_id) / "audit.json"


def load_audits(worker_id: str) -> list[dict]:
    """Load all audit records for worker_id; returns list, newest last."""
    path = _audit_file(worker_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_audit_record(worker_id: str, record: dict) -> None:
    """Append one audit record; add timestamp; ensure conclusion_source and conclusion_notes_source."""
    d = _audit_dir(worker_id)
    d.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    record["recorded_at"] = datetime.now(tz=timezone.utc).isoformat()
    record.setdefault("conclusion_source", "ai")
    record.setdefault("conclusion_notes_source", "ai")
    records = load_audits(worker_id)
    records.append(record)
    _audit_file(worker_id).write_text(
        json.dumps(records, indent=2, ensure_ascii=False)
    )


def list_task_ids_with_audits() -> list[str]:
    """List worker_ids that have at least one audit record under .state/workers/<worker_id>/audit/."""
    tasks_root = PROJECT_ROOT / ".state" / "workers"
    if not tasks_root.exists():
        return []
    out: list[str] = []
    for path in tasks_root.iterdir():
        if path.is_dir() and (_audit_file(path.name).exists()):
            out.append(path.name)
    return sorted(out)


def get_audit_summary_for_boost(
    exclude_worker_id: str = "boost",
    max_per_worker: int = 10,
) -> str:
    """
    Build a short summary of audit findings from other workers for injection into boost.
    Phase 5.1: audit-to-bootstrap. Only includes workers other than exclude_worker_id.
    Prefer escalated or non-pass records; cap at max_per_worker records per worker.
    """
    worker_ids = [wid for wid in list_task_ids_with_audits() if wid != exclude_worker_id]
    if not worker_ids:
        return ""

    lines: list[str] = ["Audit context from other workers (consider for system fixes):"]
    for wid in worker_ids:
        records = load_audits(wid)
        # newest last; take last N
        recent = records[-max_per_worker:] if len(records) > max_per_worker else records
        # prefer escalated or conclusion != "pass"
        relevant = [r for r in recent if r.get("escalated") or (r.get("conclusion") or "").strip() != "pass"]
        if not relevant:
            continue
        for r in relevant:
            round_i = r.get("round_index", "?")
            conclusion = r.get("conclusion") or r.get("reason") or "—"
            criteria = r.get("criteria_used") or []
            parts = [f"worker {wid} round {round_i}: {conclusion}"]
            if criteria:
                parts.append(f" criteria={criteria}")
            lines.append(" - " + "".join(parts))
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
