"""Context vars for broker execution (e.g. current work_dir for error logging)."""
from __future__ import annotations

import contextvars
from pathlib import Path

current_work_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "current_work_dir", default=None
)
