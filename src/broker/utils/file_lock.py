"""
Simple file locking utilities for concurrent file access.
Uses fcntl on Unix (macOS/Linux) with fallback for Windows.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

if sys.platform != "win32":
    import fcntl

    def _lock_file(f: IO, exclusive: bool = True) -> None:
        """Acquire file lock (Unix)."""
        lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(f.fileno(), lock_type)

    def _unlock_file(f: IO) -> None:
        """Release file lock (Unix)."""
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
else:
    import msvcrt

    def _lock_file(f: IO, exclusive: bool = True) -> None:
        """Acquire file lock (Windows)."""
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(f: IO) -> None:
        """Release file lock (Windows)."""
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextmanager
def file_lock(filepath: Path | str, mode: str = "a", exclusive: bool = True) -> Iterator[IO]:
    """
    Context manager for locked file access.
    
    Args:
        filepath: Path to the file
        mode: File open mode ('a' for append, 'r+' for read/write, etc.)
        exclusive: True for exclusive lock (write), False for shared lock (read)
    
    Usage:
        with file_lock("data.jsonl", "a") as f:
            f.write(json.dumps(data) + "\\n")
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    if not filepath.exists() and "r" in mode and "+" not in mode:
        filepath.touch()
    
    f = open(filepath, mode)
    try:
        _lock_file(f, exclusive)
        yield f
    finally:
        f.flush()
        _unlock_file(f)
        f.close()


def atomic_json_update(filepath: Path | str, update_func: callable) -> None:
    """
    Atomically read, update, and write a JSON file with locking.
    
    Args:
        filepath: Path to the JSON file
        update_func: Function that takes current data (dict) and returns updated data
    
    Usage:
        def add_item(data):
            data.setdefault("items", []).append("new")
            return data
        atomic_json_update("config.json", add_item)
    """
    import json
    
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    lock_file_path = filepath.with_suffix(filepath.suffix + ".lock")
    
    with file_lock(lock_file_path, "a", exclusive=True):
        current_data = {}
        if filepath.exists():
            try:
                current_data = json.loads(filepath.read_text())
            except (json.JSONDecodeError, OSError):
                current_data = {}
        
        updated_data = update_func(current_data)
        
        temp_path = filepath.with_suffix(filepath.suffix + ".tmp")
        temp_path.write_text(json.dumps(updated_data, indent=2, ensure_ascii=False))
        temp_path.replace(filepath)


def locked_append(filepath: Path | str, line: str) -> None:
    """
    Append a line to a file with locking.
    
    Args:
        filepath: Path to the file
        line: Line to append (newline will be added if not present)
    """
    if not line.endswith("\n"):
        line = line + "\n"
    
    with file_lock(filepath, "a", exclusive=True) as f:
        f.write(line)
