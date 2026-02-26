"""
Canonical definitions of task types. DESIGN §4.5, Phase 3.
Extensible: add entries to TASK_TYPES; do not modify core safety logic (broker rules, exit codes, auth).
"""
from __future__ import annotations

# Task type "bootstrap": self-bootstrap constraints (no core modification, generated marker, no autonomous merge)
BOOTSTRAP = "bootstrap"
BOOTSTRAP_CONSTRAINTS = {
    "no_core_modification": True,
    "generated_marker": True,
    "no_autonomous_merge": True,
}
BOOTSTRAP_EXTRA_INSTRUCTIONS = [
    "[Bootstrap] Do not modify core safety logic (broker rules, agent exit codes, auth).",
    "[Bootstrap] Output must be marked generated; no autonomous merge.",
]

TASK_TYPES = {
    BOOTSTRAP: {
        "constraints": BOOTSTRAP_CONSTRAINTS,
        "extra_instructions": BOOTSTRAP_EXTRA_INSTRUCTIONS,
        "generated_marker": True,
    },
}


def get_task_type_config(task_type: str | None) -> dict | None:
    """Return config for task_type from registry, or None if unknown."""
    if not task_type:
        return None
    return TASK_TYPES.get(task_type)
