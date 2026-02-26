"""
Prompt utilities with optional timeout. Used for bro submit confirmations.
- Plan selection, continue-next-step: timeout with default "agree" (BROKER_CONFIRM_TIMEOUT, default 60s)
- Audit escalation (accept/retry): no timeout, waits for user (high-risk)
"""
from __future__ import annotations

import os
import queue
import sys
import threading

CONFIRM_TIMEOUT = int(os.environ.get("BROKER_CONFIRM_TIMEOUT", "60"))


def prompt_with_timeout(
    prompt_text: str,
    default: str,
    timeout_sec: int | None = None,
) -> str:
    """
    Prompt for input with optional timeout. On timeout, return default.
    On EOF/KeyboardInterrupt, return default.
    """
    if timeout_sec is None or timeout_sec <= 0:
        timeout_sec = CONFIRM_TIMEOUT
    out = queue.Queue()

    def get_input():
        try:
            print(prompt_text, end="", flush=True)
            line = sys.stdin.readline()
            out.put(line.strip() if line else "")
        except (EOFError, KeyboardInterrupt):
            out.put("")

    t = threading.Thread(target=get_input, daemon=True)
    t.start()
    try:
        return out.get(timeout=timeout_sec)
    except queue.Empty:
        print(f" (timeout {timeout_sec}s, default)", flush=True)
        return default
