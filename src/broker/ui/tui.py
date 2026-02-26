"""
Textual TUI for bro submit: tree, progress, log viewer.
"""
from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.widgets import Log, ProgressBar, Static, Tree
from textual.widgets.tree import TreeNode


# Colors for running workers (tree + layer 2 progress). Use names valid in both Rich markup and Textual CSS.
RUNNING_COLORS = ["red", "orange", "yellow", "green", "blue", "magenta"]


def _copy_to_clipboard(text: str) -> None:
    """Copy text to system clipboard via pbcopy (macOS), xclip (Linux), or clip (Windows)."""
    if not text:
        return
    for cmd in (
        ["pbcopy"],  # macOS
        ["xclip", "-selection", "clipboard"],  # Linux X
        ["xclip", "-selection", "c"],  # Linux
        ["clip"],  # Windows
    ):
        exe = shutil.which(cmd[0])
        if exe:
            try:
                p = subprocess.run(
                    [exe] + cmd[1:],
                    input=text.encode("utf-8"),
                    capture_output=True,
                    timeout=2,
                )
                if p.returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass


class LogViewer(Static):
    """Scrollable log content. Use update() with new text."""
    pass


class SubmitTUI(App):
    """TUI for bro submit: task tree, progress bars, log viewer.
    Use 'q' to quit; Ctrl+Q can trigger OSError when broker is blocking on I/O.
    """

    TITLE = "bro submit"
    CSS_PATH = str(Path(__file__).parent / "tui.css")
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "copy_region", "Copy", show=False),
    ]

    def __init__(
        self,
        event_queue: queue.Queue,
        verbose: bool = False,
    ) -> None:
        super().__init__()
        self._queue = event_queue
        self._verbose = verbose
        self._task_node_data: dict[str, dict] = {}
        self._node_by_id: dict[str, TreeNode] = {}
        self._running_ids: set[str] = set()
        self._color_for_task: dict[str, str] = {}
        self._parent_progress: tuple[int, int] = (0, 0)
        self._child_tasks: list[dict] = []
        self._child_color_index = 0
        self._broker_done = False
        self._selected_log_path: Path | None = None
        self._log_viewer_content: str = ""
        self._console_lines: list[str] = []
        self._log_paths_by_task: dict[str, list[tuple[str, str]]] = {}
        self._selected_node_label: str = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal():
                with Container(classes="tree-container"):
                    yield Tree("Tasks", id="task-tree")
                with Vertical(id="log-container", classes="log-container"):
                    yield Static("Log", id="log-title")
                    with ScrollableContainer(id="log-scroll"):
                        yield LogViewer("", id="log-viewer", markup=False)
            with Container(id="console-container"):
                yield Static("Brief Info", id="console-title", markup=False)
                yield Log(id="console-log")
            with Container(id="progress-container"):
                with Horizontal(id="progress-bar-row"):
                    yield Static("Parent: ", id="progress-label", markup=False)
                    yield ProgressBar(total=1, id="progress-bar", show_eta=False)
                yield Static("", id="child-progress-line", markup=False)
                yield Static("", id="status-line", markup=False)

    def on_mount(self) -> None:
        self.set_interval(0.3, self._drain_queue)
        self.set_interval(0.5, self._refresh_log_viewer)
        tree = self.query_one("#task-tree", Tree)
        tree.root.expand()

    def _drain_queue(self) -> None:
        while True:
            try:
                evt = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(evt)

    def _handle_event(self, evt: dict) -> None:
        t = evt.get("type")
        if t == "progress":
            self._parent_progress = (
                evt.get("parent", {}).get("current", 0),
                evt.get("parent", {}).get("total", 1),
            )
            self._child_tasks = evt.get("child_tasks") or []
            for ct in self._child_tasks:
                tid = ct.get("task_id")
                if tid and tid not in self._color_for_task:
                    self._color_for_task[tid] = RUNNING_COLORS[len(self._color_for_task) % len(RUNNING_COLORS)]
                if tid:
                    self._running_ids.add(tid)
            if self._child_tasks:
                self._apply_tree_colors()
            self._refresh_progress()
        elif t == "task_tree":
            nodes = evt.get("nodes", [])
            running = set(evt.get("running_ids") or [])
            self._update_tree(nodes, running)
        elif t == "log_paths":
            paths = evt.get("paths", [])
            self._add_log_paths_to_tree(paths)
        elif t == "task_assigned":
            tid = evt.get("task_id", "?")
            preview = evt.get("objective_preview", "")
            who = evt.get("assignee") or evt.get("subtask_id") or "?"
            self._append_console(f"Assigned: {tid} → {who} | {preview}")
        elif t == "result":
            tid = evt.get("task_id", "?")
            role = evt.get("role", "?")
            status = evt.get("status", "?")
            code = evt.get("exit_code")
            line = f"Result: {tid} ({role}): {status}"
            if code is not None:
                line += f" (exit {code})"
            self._append_console(line)
            self._refresh_progress()
        elif t == "status":
            self._update_status(evt.get("message", ""), evt.get("elapsed_seconds"))
        elif t == "console":
            self._append_console(evt.get("message", ""))
        elif t == "error":
            self._append_console(f"Error: {evt.get('message', '')}")
        elif t == "done":
            self._broker_done = True

    def _update_tree(self, nodes: list[dict], running_ids: set[str]) -> None:
        tree = self.query_one("#task-tree", Tree)
        self._running_ids = set(running_ids) if isinstance(running_ids, (list, set)) else set()

        for node in nodes:
            nid = str(node.get("id") or node.get("label", "?"))
            label = str(node.get("label", nid))
            parent_id = node.get("parent_id")
            work_dir = node.get("work_dir")
            log_path = node.get("log_path") or (str(Path(work_dir) / "agent.log") if work_dir else None)

            if nid in self._running_ids and nid not in self._color_for_task:
                self._color_for_task[nid] = RUNNING_COLORS[len(self._color_for_task) % len(RUNNING_COLORS)]

            if nid in self._node_by_id:
                continue

            if parent_id and str(parent_id) in self._node_by_id:
                parent_node = self._node_by_id[str(parent_id)]
                child = parent_node.add_leaf(label, data=log_path)
                self._node_by_id[nid] = child
            else:
                child = tree.root.add(label, expand=True)
                child.data = log_path
                self._node_by_id[nid] = child

            self._task_node_data[nid] = node

        self._apply_tree_colors()

    def _add_log_paths_to_tree(self, paths: list[dict]) -> None:
        """Add log path nodes. Paths = [{path, task_id, role?, parent_id?}].
        Node label = 任务名称 = folder name under workspace/works/ (basename of path's parent)."""
        tree = self.query_one("#task-tree", Tree)
        for p in paths:
            path = p.get("path")
            task_id = str(p.get("task_id", "?"))
            role = str(p.get("role", "?"))
            parent_id = p.get("parent_id")
            node_id = f"{task_id}-{role}"
            if not path or node_id in self._node_by_id:
                continue
            task_name_label = Path(path).parent.name  # 任务名称 = folder name in workspace/works/
            if parent_id and str(parent_id) in self._node_by_id:
                parent_node = self._node_by_id[str(parent_id)]
                if task_id not in self._node_by_id:
                    child = parent_node.add_leaf(task_name_label, data=path)
                    self._node_by_id[task_id] = child
                    self._node_by_id[node_id] = child
                    self._task_node_data[task_id] = {"id": task_id, "label": task_name_label}
                continue
            if task_id not in self._node_by_id:
                parent = tree.root.add(task_name_label, expand=True)
                parent.data = None
                self._node_by_id[task_id] = parent
            parent_node = self._node_by_id[task_id]
            self._log_paths_by_task.setdefault(task_id, []).append((role, path))
            n = len(self._log_paths_by_task[task_id])
            if n == 1:
                parent_node.data = path
                parent_node.label = task_name_label
                self._task_node_data[task_id] = {"id": task_id, "label": task_name_label}
                self._node_by_id[node_id] = parent_node
                continue
            if n == 2:
                parent_node.data = None
                for r, pth in self._log_paths_by_task[task_id]:
                    rid = f"{task_id}-{r}"
                    if rid not in self._node_by_id:
                        label = Path(pth).parent.name
                        child = parent_node.add_leaf(label, data=pth)
                        self._node_by_id[rid] = child
                        self._task_node_data[rid] = {"id": rid, "label": label}
            else:
                child = parent_node.add_leaf(task_name_label, data=path)
                self._node_by_id[node_id] = child
                self._task_node_data[node_id] = {"id": node_id, "label": task_name_label}

    def _apply_tree_colors(self) -> None:
        """Color running task labels using Rich markup [color]label[/]. Only update when changed to reduce flicker."""
        for nid in list(self._node_by_id):
            if nid not in self._task_node_data:
                continue
            tree_node = self._node_by_id.get(nid)
            if tree_node is None:
                continue
            base_label = self._task_node_data.get(nid, {}).get("label", nid) or nid
            if nid in self._running_ids:
                color = self._color_for_task.get(nid, RUNNING_COLORS[0])
                new_label = f"[{color}]{base_label}[/]"
            else:
                new_label = base_label
            if getattr(tree_node, "label", None) != new_label:
                tree_node.label = new_label

    def _append_console(self, message: str) -> None:
        if not message:
            return
        self._console_lines.append(message)
        try:
            console = self.query_one("#console-log", Log)
            console.write_line(message)
        except Exception:
            pass

    def _update_status(self, message: str, elapsed_seconds: float | None = None) -> None:
        status_widget = self.query_one("#status-line", Static)
        if not message:
            status_widget.update("")
            return
        if elapsed_seconds is not None:
            m = int(elapsed_seconds // 60)
            s = int(elapsed_seconds % 60)
            elapsed_str = f"{m}m {s}s" if m else f"{s}s"
            status_widget.update(f"Current: {message} | Elapsed: {elapsed_str}")
        else:
            status_widget.update(f"Current: {message}")

    def _refresh_progress(self) -> None:
        pc, pt = self._parent_progress
        bar = self.query_one("#progress-bar", ProgressBar)
        label = self.query_one("#progress-label", Static)
        if pt > 0:
            bar.update(total=pt, progress=pc)
            label.update(f"Parent: {pc}/{pt}")
        else:
            bar.update(total=1, progress=0)
            label.update("Parent:")
        # Progress bar color: match running task(s), cycle if multiple
        for cls in ("progress-color-red", "progress-color-orange", "progress-color-yellow",
                    "progress-color-green", "progress-color-blue", "progress-color-magenta"):
            bar.remove_class(cls)
        if self._child_tasks:
            idx = int(pc) % len(self._child_tasks) if self._child_tasks else 0
            color = self._child_tasks[idx].get("color") or RUNNING_COLORS[0]
            bar.add_class(f"progress-color-{color}")
        elif self._running_ids:
            first_id = next(iter(self._running_ids), None)
            if first_id:
                color = self._color_for_task.get(first_id, RUNNING_COLORS[0])
                bar.add_class(f"progress-color-{color}")
        child_line = self.query_one("#child-progress-line", Static)
        if self._child_tasks:
            parts = [f"{t.get('task_id', '?')}: {t.get('current', 0)}/{t.get('total', 1)}" for t in self._child_tasks]
            child_line.update("Child: " + " | ".join(parts))
        else:
            child_line.update("")

    def _refresh_log_viewer(self) -> None:
        """Periodically re-read selected log file and update viewer."""
        if self._selected_log_path is None:
            return
        path = self._selected_log_path
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-10:]) if len(lines) > 10 else "\n".join(lines)
            self._log_viewer_content = tail or "(empty)"
            viewer = self.query_one("#log-viewer", LogViewer)
            viewer.update(self._log_viewer_content)
        except OSError:
            pass

    def action_copy_region(self) -> None:
        """Copy log or console content to clipboard (Ctrl+C). Prefer focused region; else console, else log."""
        content = None
        try:
            focused = self.focused
            if focused:
                log_container = self.query_one("#log-container", Container)
                console_container = self.query_one("#console-container", Container)
                if log_container and (focused == log_container or log_container.is_ancestor_of(focused)):
                    content = self._log_viewer_content
                elif console_container and (focused == console_container or console_container.is_ancestor_of(focused)):
                    content = "\n".join(self._console_lines) if self._console_lines else ""
            if not content:
                content = "\n".join(self._console_lines) if self._console_lines else self._log_viewer_content or ""
        except Exception:
            content = (self._console_lines and "\n".join(self._console_lines)) or self._log_viewer_content or ""
        if content:
            try:
                self.copy_to_clipboard(content)
            except Exception:
                _copy_to_clipboard(content)
        else:
            self.bell()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node = event.node
        if node is None:
            return
        label = getattr(node, "label", "") or ""
        self._selected_node_label = _plain_label(label)
        title = self.query_one("#log-title", Static)
        title.update(f"Log ({self._selected_node_label})" if self._selected_node_label else "Log")
        log_path = getattr(node, "data", None) if node else None
        if isinstance(log_path, str) and log_path:
            self._selected_log_path = Path(log_path)
            self._refresh_log_viewer()
        else:
            self._selected_log_path = None
            self._log_viewer_content = "(no log)"
            viewer = self.query_one("#log-viewer", LogViewer)
            viewer.update("(no log)")

    def run_broker(self, fn) -> None:
        """Run broker fn in thread. Call this before app.run()."""
        def worker():
            import traceback
            from broker.context import current_work_dir
            try:
                fn()
            except BaseException as e:
                work_dir = current_work_dir.get()
                msg = str(e)
                if work_dir is not None:
                    try:
                        tb = traceback.format_exc()
                        err_file = work_dir / "error.log"
                        err_file.write_text(tb, encoding="utf-8")
                        msg = f"{msg} (traceback → {err_file})"
                    except OSError:
                        pass
                self._queue.put({"type": "error", "message": msg})
            finally:
                self._queue.put({"type": "done"})

        t = threading.Thread(target=worker, daemon=True)
        t.start()


def _plain_label(label: str | Any) -> str:
    """Strip Rich markup [color]text[/] to plain text. label may be str or Rich Text."""
    if label is None:
        return ""
    if hasattr(label, "plain"):
        return str(getattr(label, "plain", "") or "")
    s = str(label)
    if not s:
        return ""
    return re.sub(r"\[/?[^\]]*\]", "", s).strip() or s
