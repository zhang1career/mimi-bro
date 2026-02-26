"""
Display driver abstraction for bro submit.
CLI TUI, PlainDriver (non-TTY), JsonlDriver (plugin), NullDriver (test).
"""
from __future__ import annotations

import queue as _queue_mod
from pathlib import Path
from typing import Any, Callable

from broker.ui import events


class DisplayDriver:
    """Interface for display output. All methods no-op by default."""

    def on_progress(
        self,
        parent_current: int,
        parent_total: int,
        child_tasks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Progress update. child_tasks = [{task_id, current, total, color}, ...]"""
        pass

    def on_task_tree(
        self,
        nodes: list[dict[str, Any]],
        running_ids: set[str] | None = None,
    ) -> None:
        """Task tree update. nodes = [{id, label, parent_id?, work_dir?}, ...]"""
        pass

    def on_log_paths(
        self,
        paths: list[dict[str, Any]],
        lines_per_file: int = 3,
    ) -> None:
        """Log paths available. paths = [{path, task_id?, role?}, ...]"""
        pass

    def on_task_assigned(
        self,
        task_id: str,
        objective_preview: str,
        assignee: str | None = None,
        subtask_id: str | None = None,
    ) -> None:
        """Task assigned to agent."""
        pass

    def on_result(
        self,
        task_id: str,
        role: str,
        status: str,
        work_dir: Path | str,
        exit_code: int | None = None,
    ) -> None:
        """Task result."""
        pass

    def verbose(self, message: str) -> None:
        """Verbose log (only when --verbose)."""
        pass

    def on_status(self, message: str, elapsed_seconds: float | None = None) -> None:
        """Current operation and optional elapsed time."""
        pass

    def on_console_message(self, message: str) -> None:
        """Broker-level message (e.g. skill API result, which skill was chosen)."""
        pass


class NullDriver(DisplayDriver):
    """No-op driver for testing or when display is disabled."""

    pass


class JsonlDriver(DisplayDriver):
    """Emit JSONL events to stdout for IDE plugin consumption."""

    def __init__(self, *, verbose: bool = False) -> None:
        self._verbose = verbose

    def on_progress(
        self,
        parent_current: int,
        parent_total: int,
        child_tasks: list[dict[str, Any]] | None = None,
    ) -> None:
        evt = events.emit_progress(parent_current, parent_total, child_tasks)
        print(events.to_jsonl(evt), end="", flush=True)

    def on_task_tree(
        self,
        nodes: list[dict[str, Any]],
        running_ids: set[str] | None = None,
    ) -> None:
        evt = events.emit_task_tree(nodes, running_ids)
        print(events.to_jsonl(evt), end="", flush=True)

    def on_log_paths(
        self,
        paths: list[dict[str, Any]],
        lines_per_file: int = 3,
    ) -> None:
        evt = events.emit_log_paths(paths, lines_per_file)
        print(events.to_jsonl(evt), end="", flush=True)

    def on_task_assigned(
        self,
        task_id: str,
        objective_preview: str,
        assignee: str | None = None,
        subtask_id: str | None = None,
    ) -> None:
        evt = events.emit_task_assigned(task_id, objective_preview, assignee, subtask_id)
        print(events.to_jsonl(evt), end="", flush=True)

    def on_result(
        self,
        task_id: str,
        role: str,
        status: str,
        work_dir: Path | str,
        exit_code: int | None = None,
    ) -> None:
        evt = events.emit_result(task_id, role, status, work_dir, exit_code)
        print(events.to_jsonl(evt), end="", flush=True)

    def verbose(self, message: str) -> None:
        if self._verbose:
            evt = events.emit_verbose(message)
            print(events.to_jsonl(evt), end="", flush=True)

    def on_status(self, message: str, elapsed_seconds: float | None = None) -> None:
        evt = events.emit_status(message, elapsed_seconds)
        print(events.to_jsonl(evt), end="", flush=True)

    def on_console_message(self, message: str) -> None:
        evt = events.emit_console(message)
        print(events.to_jsonl(evt), end="", flush=True)


class PlainDriver(DisplayDriver):
    """Minimal line-based output for non-TTY (CI, pipe)."""

    def __init__(self, *, verbose: bool = False) -> None:
        self._verbose = verbose

    def on_log_paths(
        self,
        paths: list[dict[str, Any]],
        lines_per_file: int = 3,
    ) -> None:
        if self._verbose and paths and paths[0].get("path"):
            print(f"Log: tail -f {paths[0]['path']}", flush=True)

    def on_task_assigned(
        self,
        task_id: str,
        objective_preview: str,
        assignee: str | None = None,
        subtask_id: str | None = None,
    ) -> None:
        preview = (objective_preview[:60] + "…") if len(objective_preview) > 60 else objective_preview
        who = assignee or subtask_id or "?"
        print(f"[{task_id}] {preview} → {who}", flush=True)

    def on_result(
        self,
        task_id: str,
        role: str,
        status: str,
        work_dir: Path | str,
        exit_code: int | None = None,
    ) -> None:
        code = f" (exit_code={exit_code})" if exit_code is not None else ""
        print(f"[{task_id}] {role}: {status}{code}", flush=True)

    def on_progress(
        self,
        parent_current: int,
        parent_total: int,
        child_tasks: list[dict[str, Any]] | None = None,
    ) -> None:
        if self._verbose and parent_total > 0:
            print(f"[broker] progress {parent_current}/{parent_total}", flush=True)

    def verbose(self, message: str) -> None:
        if self._verbose:
            print(message, flush=True)

    def on_console_message(self, message: str) -> None:
        print(f"[broker] {message}", flush=True)

    def on_status(self, message: str, elapsed_seconds: float | None = None) -> None:
        if not message:
            print(flush=True)
            return
        if elapsed_seconds is not None:
            m = int(elapsed_seconds // 60)
            s = int(elapsed_seconds % 60)
            elapsed_str = f"{m}m {s}s" if m else f"{s}s"
            print(f"\r[broker] {message} | Elapsed: {elapsed_str}   ", end="", flush=True)
        else:
            print(f"[broker] {message}", flush=True)


class CLIDriver(DisplayDriver):
    """TUI driver: pushes events to queue; run with SubmitTUI to display."""

    def __init__(
        self,
        event_queue: _queue_mod.Queue | None = None,
        *,
        verbose: bool = False,
    ) -> None:
        self._queue = event_queue or _queue_mod.Queue()
        self._verbose = verbose

    @property
    def queue(self) -> _queue_mod.Queue:
        return self._queue

    def on_progress(
        self,
        parent_current: int,
        parent_total: int,
        child_tasks: list[dict[str, Any]] | None = None,
    ) -> None:
        self._queue.put(events.emit_progress(parent_current, parent_total, child_tasks))

    def on_task_tree(
        self,
        nodes: list[dict[str, Any]],
        running_ids: set[str] | None = None,
    ) -> None:
        self._queue.put(events.emit_task_tree(nodes, running_ids))

    def on_log_paths(
        self,
        paths: list[dict[str, Any]],
        lines_per_file: int = 3,
    ) -> None:
        self._queue.put(events.emit_log_paths(paths, lines_per_file))

    def on_task_assigned(
        self,
        task_id: str,
        objective_preview: str,
        assignee: str | None = None,
        subtask_id: str | None = None,
    ) -> None:
        self._queue.put(events.emit_task_assigned(task_id, objective_preview, assignee, subtask_id))

    def on_result(
        self,
        task_id: str,
        role: str,
        status: str,
        work_dir: Path | str,
        exit_code: int | None = None,
    ) -> None:
        self._queue.put(events.emit_result(task_id, role, status, work_dir, exit_code))

    def verbose(self, message: str) -> None:
        if self._verbose:
            self._queue.put(events.emit_verbose(message))

    def on_status(self, message: str, elapsed_seconds: float | None = None) -> None:
        self._queue.put(events.emit_status(message, elapsed_seconds))

    def on_console_message(self, message: str) -> None:
        self._queue.put(events.emit_console(message))

    def run_with(self, broker_fn: Callable[[], None]) -> None:
        """Run broker_fn in background thread and TUI in foreground."""
        import errno
        import threading
        from broker.ui.tui import SubmitTUI
        app = SubmitTUI(self._queue, verbose=self._verbose)
        app.run_broker(broker_fn)
        orig_excepthook = threading.excepthook

        def _suppress_quit_io_error(args: threading.ExceptHookArgs) -> None:
            if (
                args.exc_type is OSError
                and getattr(args.exc_value, "errno", None) == errno.EIO
            ):
                return
            orig_excepthook(args)

        try:
            threading.excepthook = _suppress_quit_io_error
            app.run()
        except OSError as e:
            if e.errno != errno.EIO:
                raise
        finally:
            threading.excepthook = orig_excepthook
