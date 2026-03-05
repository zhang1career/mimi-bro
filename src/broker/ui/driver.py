"""
Display driver abstraction for bro submit.
CLI TUI, PlainDriver (non-TTY), JsonlDriver (plugin), NullDriver (test).
"""
from __future__ import annotations

import queue as _queue_mod
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from broker.ui import events

if TYPE_CHECKING:
    from broker.parallel.analyzer import DependencyGraph
    from broker.container.manager import SubtaskContainer


class DisplayDriver:
    """Interface for display output. All methods no-op by default."""

    def on_progress(
        self,
        parent_current: int,
        parent_total: int,
        child_tasks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Progress update. child_tasks = [{subtask_id, current, total, color}, ...]"""
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
        """Log paths available. paths = [{path, worker_id?, role?}, ...]"""
        pass

    def on_task_assigned(
        self,
        worker_id: str,
        objective_preview: str,
        assignee: str | None = None,
        subtask_id: str | None = None,
    ) -> None:
        """Task assigned to agent."""
        pass

    def on_result(
        self,
        worker_id: str,
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

    def on_container_status(self, container: "SubtaskContainer") -> None:
        """Container status update for Docker execution."""
        pass

    def confirm_dependencies(
        self,
        graph: "DependencyGraph",
        graph_text: str,
        confirmed_deps_path: Path | None = None,
    ) -> "DependencyGraph":
        """Request user confirmation for dependency graph. Returns confirmed graph.

        Default implementation returns original graph (auto-confirm).
        Subclasses can override to implement interactive confirmation.
        """
        return graph

    def confirm_skill_selection(
        self,
        items: list[dict[str, Any]],
        timeout_seconds: int = 60,
    ) -> dict[str, str]:
        """Request user confirmation for skill selection with timeout.

        Args:
            items: List of items to confirm, each with:
                - item_id: Subtask ID
                - requirement: Brief description
                - current_skill: Currently selected skill ID
                - available_skills: List of available skill IDs
                - source: How skill was selected ("rule", "agent", "default")
            timeout_seconds: Seconds before auto-confirm (default 60)

        Returns:
            Dict mapping item_id -> confirmed skill_id.
            Default implementation returns current selections (auto-confirm).
        """
        return {item["item_id"]: item["current_skill"] for item in items}

    def run_external_command(
        self,
        args: list[str],
        cwd: str | Path | None = None,
    ) -> int:
        """Run an external command that requires exclusive terminal access.

        This method is used for interactive terminal programs like git mergetool/vimdiff
        that need full terminal control. TUI implementations should suspend the UI
        before running the command.

        Args:
            args: Command and arguments to run
            cwd: Working directory for the command

        Returns:
            Exit code of the command
        """
        import subprocess
        result = subprocess.run(args, cwd=str(cwd) if cwd else None, check=False)
        return result.returncode


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
        worker_id: str,
        objective_preview: str,
        assignee: str | None = None,
        subtask_id: str | None = None,
    ) -> None:
        evt = events.emit_task_assigned(worker_id, objective_preview, assignee, subtask_id)
        print(events.to_jsonl(evt), end="", flush=True)

    def on_result(
        self,
        worker_id: str,
        role: str,
        status: str,
        work_dir: Path | str,
        exit_code: int | None = None,
    ) -> None:
        evt = events.emit_result(worker_id, role, status, work_dir, exit_code)
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

    def on_container_status(self, container: "SubtaskContainer") -> None:
        work_dir_str = str(container.work_dir) if container.work_dir else None
        evt = events.emit_container_status(
            container_name=container.container_name,
            run_id=container.run_id,
            role=container.role,
            status=container.status.value,
            exit_code=container.exit_code,
            error_message=container.error_message,
            work_dir=work_dir_str,
        )
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
        worker_id: str,
        objective_preview: str,
        assignee: str | None = None,
        subtask_id: str | None = None,
    ) -> None:
        preview = (objective_preview[:60] + "…") if len(objective_preview) > 60 else objective_preview
        who = assignee or subtask_id or "?"
        print(f"[{worker_id}] {preview} → {who}", flush=True)

    def on_result(
        self,
        worker_id: str,
        role: str,
        status: str,
        work_dir: Path | str,
        exit_code: int | None = None,
    ) -> None:
        code = f" (exit_code={exit_code})" if exit_code is not None else ""
        print(f"[{worker_id}] {role}: {status}{code}", flush=True)

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

    def on_container_status(self, container: "SubtaskContainer") -> None:
        status = container.status.value
        if container.exit_code is not None:
            print(f"[container] {container.container_name}: {status} (exit {container.exit_code})", flush=True)
        else:
            print(f"[container] {container.container_name}: {status}", flush=True)

    def confirm_dependencies(
        self,
        graph: "DependencyGraph",
        graph_text: str,
        confirmed_deps_path: Path | None = None,
    ) -> "DependencyGraph":
        """Request user confirmation for dependency graph via terminal."""
        from broker.parallel.confirm import confirm_dependencies as terminal_confirm
        if confirmed_deps_path is None:
            from broker.utils.path_util import PROJECT_ROOT
            confirmed_deps_path = PROJECT_ROOT / ".state" / "parallel" / "confirmed_deps.json"
        return terminal_confirm(graph, confirmed_deps_path, auto_confirm=False)

    def confirm_skill_selection(
        self,
        items: list[dict[str, Any]],
        timeout_seconds: int = 60,
    ) -> dict[str, str]:
        """Request user confirmation for skill selection via terminal with timeout."""
        from broker.skill.confirm import confirm_skills_terminal
        return confirm_skills_terminal(items, timeout_seconds)


class CLIDriver(DisplayDriver):
    """TUI driver: pushes events to queue; run with SubmitTUI to display."""

    def __init__(
        self,
        event_queue: _queue_mod.Queue | None = None,
        *,
        verbose: bool = False,
        theme_name: str | None = None,
    ) -> None:
        self._queue = event_queue or _queue_mod.Queue()
        self._verbose = verbose
        self._theme_name = theme_name
        self._response_queue: _queue_mod.Queue = _queue_mod.Queue()
        self._pending_confirms: dict[str, threading.Event] = {}
        self._confirm_results: dict[str, Any] = {}

    @property
    def queue(self) -> _queue_mod.Queue:
        return self._queue

    @property
    def response_queue(self) -> _queue_mod.Queue:
        return self._response_queue

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
        worker_id: str,
        objective_preview: str,
        assignee: str | None = None,
        subtask_id: str | None = None,
    ) -> None:
        self._queue.put(events.emit_task_assigned(worker_id, objective_preview, assignee, subtask_id))

    def on_result(
        self,
        worker_id: str,
        role: str,
        status: str,
        work_dir: Path | str,
        exit_code: int | None = None,
    ) -> None:
        self._queue.put(events.emit_result(worker_id, role, status, work_dir, exit_code))

    def verbose(self, message: str) -> None:
        if self._verbose:
            self._queue.put(events.emit_verbose(message))

    def on_status(self, message: str, elapsed_seconds: float | None = None) -> None:
        self._queue.put(events.emit_status(message, elapsed_seconds))

    def on_console_message(self, message: str) -> None:
        self._queue.put(events.emit_console(message))

    def on_container_status(self, container: "SubtaskContainer") -> None:
        work_dir_str = str(container.work_dir) if container.work_dir else None
        evt = events.emit_container_status(
            container_name=container.container_name,
            run_id=container.run_id,
            role=container.role,
            status=container.status.value,
            exit_code=container.exit_code,
            error_message=container.error_message,
            work_dir=work_dir_str,
        )
        self._queue.put(evt)

    def confirm_dependencies(
        self,
        graph: "DependencyGraph",
        graph_text: str,
        confirmed_deps_path: Path | None = None,
    ) -> "DependencyGraph":
        """Request user confirmation for dependency graph via TUI dialog."""
        request_id = str(uuid.uuid4())

        wait_event = threading.Event()
        self._pending_confirms[request_id] = wait_event

        nodes = list(graph.nodes)
        edges = [(e.from_task, e.to_task) for e in graph.edges]
        evt = events.emit_confirm_deps_request(request_id, graph_text, nodes, edges)
        self._queue.put(evt)

        wait_event.wait(timeout=300.0)

        result = self._confirm_results.pop(request_id, None)
        self._pending_confirms.pop(request_id, None)

        if result is None or result.get("cancelled"):
            raise SystemExit(1)

        from broker.parallel.analyzer import DependencyGraph as DG, DependencyEdge
        confirmed = DG()
        for node_id in result.get("nodes", nodes):
            confirmed.add_node(node_id)
        for from_id, to_id in result.get("edges", edges):
            confirmed.add_edge(DependencyEdge(from_task=from_id, to_task=to_id, reason="confirmed"))

        return confirmed

    def handle_confirm_response(self, request_id: str, result: dict[str, Any]) -> None:
        """Called by TUI when user confirms/cancels dependency/skill dialog."""
        self._confirm_results[request_id] = result
        wait_event = self._pending_confirms.get(request_id)
        if wait_event:
            wait_event.set()

    def confirm_skill_selection(
        self,
        items: list[dict[str, Any]],
        timeout_seconds: int = 60,
    ) -> dict[str, str]:
        """Request user confirmation for skill selection via TUI dialog with timeout."""
        request_id = str(uuid.uuid4())

        wait_event = threading.Event()
        self._pending_confirms[request_id] = wait_event

        evt = events.emit_confirm_skills_request(request_id, items, timeout_seconds)
        self._queue.put(evt)

        timed_out = not wait_event.wait(timeout=float(timeout_seconds))

        result = self._confirm_results.pop(request_id, None)
        self._pending_confirms.pop(request_id, None)

        if timed_out:
            self._queue.put(events.emit_confirm_skills_timeout(request_id))
            return {item["item_id"]: item["current_skill"] for item in items}

        if result is None or result.get("cancelled"):
            return {item["item_id"]: item["current_skill"] for item in items}

        return result.get("skills", {item["item_id"]: item["current_skill"] for item in items})

    def run_external_command(
        self,
        args: list[str],
        cwd: str | Path | None = None,
    ) -> int:
        """Run an external command that requires exclusive terminal access.

        Sends a request to TUI to suspend and run the command.
        """
        request_id = str(uuid.uuid4())

        wait_event = threading.Event()
        self._pending_confirms[request_id] = wait_event

        evt = events.emit_run_external_request(
            request_id,
            args,
            str(cwd) if cwd else None,
        )
        self._queue.put(evt)

        wait_event.wait(timeout=600.0)

        result = self._confirm_results.pop(request_id, None)
        self._pending_confirms.pop(request_id, None)

        if result is None:
            return 1
        return result.get("exit_code", 1)

    def run_with(self, broker_fn: Callable[[], None]) -> None:
        """Run broker_fn in background thread and TUI in foreground."""
        import errno
        import os
        import sys
        import threading
        from broker.ui.tui import SubmitTUI
        from broker.ui.themes import DEFAULT_THEME

        try:
            import termios
            has_termios = True
        except ImportError:
            termios = None
            has_termios = False

        theme = self._theme_name or DEFAULT_THEME
        app = SubmitTUI(self._queue, verbose=self._verbose, theme_name=theme, driver=self)
        app.run_broker(broker_fn)

        orig_excepthook = threading.excepthook
        stdin_fd = None
        orig_termios = None

        if has_termios and sys.stdin.isatty():
            try:
                stdin_fd = sys.stdin.fileno()
                orig_termios = termios.tcgetattr(stdin_fd)
            except (OSError, termios.error):
                stdin_fd = None
                orig_termios = None

        def _suppress_quit_io_error(args: threading.ExceptHookArgs) -> None:
            if (
                args.exc_type is OSError
                and getattr(args.exc_value, "errno", None) == errno.EIO
            ):
                return
            if args.exc_type is SystemExit:
                return
            orig_excepthook(args)

        try:
            threading.excepthook = _suppress_quit_io_error
            app.run(mouse=False)
        except OSError as e:
            if e.errno != errno.EIO:
                raise
        except SystemExit:
            pass
        finally:
            threading.excepthook = orig_excepthook

            app._shutting_down = True
            for handle in app._interval_handles:
                try:
                    handle.stop()
                except Exception:
                    pass

            if app._broker_thread is not None and app._broker_thread.is_alive():
                app._broker_thread.join(timeout=2.0)

            if has_termios and stdin_fd is not None and orig_termios is not None:
                try:
                    termios.tcsetattr(stdin_fd, termios.TCSANOW, orig_termios)
                except (OSError, termios.error):
                    pass

            try:
                print("\033[?25h", end="", flush=True)
                os.system("stty sane 2>/dev/null")
            except Exception:
                pass
