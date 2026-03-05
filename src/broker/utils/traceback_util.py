"""Traceback formatting utilities."""
from __future__ import annotations

import sys
import traceback
from typing import Any

DEFAULT_LINE_LIMIT = 120


def format_exc(limit: int = DEFAULT_LINE_LIMIT) -> str:
    """
    Format current exception traceback.
    - Line length limited to `limit` characters (default 120)
    - No decorative borders
    """
    lines = traceback.format_exception(*sys.exc_info())
    return _process_traceback_lines(lines, limit)


def format_exception(exc: BaseException, limit: int = 120) -> str:
    """Format exception traceback. Same options as format_exc."""
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return _process_traceback_lines(lines, limit)


def _process_traceback_lines(lines: list[str], limit: int) -> str:
    out = []
    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        while len(line) > limit:
            out.append(line[:limit])
            line = "  " + line[limit:]
        out.append(line)
    return "\n".join(out)


def error_summary_for_console(exc: BaseException, max_chars: int = 200) -> str:
    """Short error message for Console, no traceback."""
    return str(exc)[:max_chars]


def install_excepthook(limit: int = DEFAULT_LINE_LIMIT) -> None:
    """Install custom excepthook: line length limit, no extra borders."""
    def _hook(etyp: type[BaseException], evalue: BaseException, etb: Any) -> None:
        if etb is None:
            sys.__excepthook__(etyp, evalue, etb)
            return
        lines = traceback.format_exception(etyp, evalue, etb)
        tb_str = _process_traceback_lines(lines, limit)
        sys.stderr.write(tb_str.rstrip() + "\n")

    sys.excepthook = _hook
