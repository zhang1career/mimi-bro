"""
Textual TUI for bro submit: tree, progress, log viewer.
Styled after agent-of-empires with line borders and theme support.
"""
from __future__ import annotations

import json
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

from broker.container.manager import list_visible_containers

from .themes import DEFAULT_THEME, Theme, get_theme


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


class LogViewer(Static, can_focus=True):
    """Scrollable log content. Use update() with new text."""

    pass


class NonFocusableScrollContainer(ScrollableContainer, can_focus=False):
    """ScrollableContainer that cannot receive focus (Tab skips it)."""

    pass


class DialogBase(Container):
    """Base class for modal dialogs.

    Subclass this and override compose() to create custom dialogs.
    Use show() and hide() to control visibility.

    Example:
        class ConfirmDialog(DialogBase):
            def compose(self):
                yield Static("Are you sure?")
                yield Button("Yes", id="yes")
                yield Button("No", id="no")
    """

    DEFAULT_CSS = """
    DialogBase {
        layer: dialog;
        align: center middle;
        width: auto;
        height: auto;
        max-width: 80%;
        max-height: 80%;
        display: none;
    }
    DialogBase.visible {
        display: block;
    }
    """

    def show(self) -> None:
        """Show the dialog."""
        self.add_class("visible")
        self.focus()

    def hide(self) -> None:
        """Hide the dialog."""
        self.remove_class("visible")


class DependencyConfirmDialog(DialogBase, can_focus=True):
    """Dialog for confirming dependency relationships before parallel execution."""

    DEFAULT_CSS = """
    DependencyConfirmDialog {
        layer: dialog;
        width: 100%;
        height: 100%;
        align: center middle;
        display: none;
        background: transparent;
    }
    DependencyConfirmDialog.visible {
        display: block;
    }
    DependencyConfirmDialog #dialog-box {
        width: 70%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
    }
    DependencyConfirmDialog #dialog-title {
        height: 2;
        text-align: center;
        text-style: bold;
    }
    DependencyConfirmDialog #graph-content {
        height: auto;
        min-height: 3;
        max-height: 12;
        overflow-y: auto;
        padding: 1;
    }
    DependencyConfirmDialog #hint {
        height: 2;
        text-align: center;
    }
    DependencyConfirmDialog #button-row {
        height: auto;
        align: center middle;
        padding: 1 0;
    }
    DependencyConfirmDialog .dialog-button {
        margin: 0 2;
        min-width: 12;
        height: 3;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("enter", "confirm", "Confirm", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("a", "confirm", "Accept", priority=True),
        Binding("n", "cancel", "No", priority=True),
        Binding("j", "scroll_down", "Down", priority=True),
        Binding("k", "scroll_up", "Up", priority=True),
        Binding("down", "scroll_down", "Down", priority=True),
        Binding("up", "scroll_up", "Up", priority=True),
        Binding("g", "scroll_home", "Top", priority=True),
        Binding("G", "scroll_end", "Bottom", priority=True),
        Binding("home", "scroll_home", "Top", priority=True),
        Binding("end", "scroll_end", "Bottom", priority=True),
    ]

    def __init__(
        self,
        request_id: str = "",
        graph_text: str = "",
        nodes: list[str] | None = None,
        edges: list[tuple[str, str]] | None = None,
        theme: Theme | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.request_id = request_id
        self.graph_text = graph_text
        self.nodes = nodes or []
        self.edges = edges or []
        self._confirmed: bool | None = None
        self._theme = theme

    def compose(self) -> ComposeResult:
        from textual.widgets import Button
        with Vertical(id="dialog-box"):
            yield Static("Confirm Dependencies", id="dialog-title")
            yield ScrollableContainer(
                Static(self.graph_text or "(no dependencies)", id="graph-text"),
                id="graph-content",
            )
            yield Static("Press Enter/A to confirm, Esc/N to cancel", id="hint")
            with Horizontal(id="button-row"):
                yield Button("Confirm (A)", id="btn-confirm", variant="success", classes="dialog-button")
                yield Button("Cancel (N)", id="btn-cancel", variant="error", classes="dialog-button")

    def on_mount(self) -> None:
        """Apply theme colors after mount."""
        from textual.widgets import Button

        theme = self._theme
        if theme is None:
            return
        try:
            dialog_box = self.query_one("#dialog-box", Vertical)
            dialog_box.styles.background = theme.background
            dialog_box.styles.border = ("solid", theme.accent)

            dialog_title = self.query_one("#dialog-title", Static)
            dialog_title.styles.color = theme.title

            graph_content = self.query_one("#graph-content", ScrollableContainer)
            graph_content.styles.background = theme.selection
            graph_content.styles.border = ("solid", theme.border)

            hint = self.query_one("#hint", Static)
            hint.styles.color = theme.dimmed

            btn_confirm = self.query_one("#btn-confirm", Button)
            btn_confirm.styles.background = theme.running
            btn_confirm.styles.color = theme.background

            btn_cancel = self.query_one("#btn-cancel", Button)
            btn_cancel.styles.background = theme.error
            btn_cancel.styles.color = theme.background
        except Exception:
            pass

    def on_key(self, event) -> None:
        """Handle key events directly."""
        if event.key in ("a", "A", "enter"):
            event.stop()
            self.action_confirm()
        elif event.key in ("n", "N", "escape"):
            event.stop()
            self.action_cancel()
        elif event.key in ("j", "down"):
            event.stop()
            self.action_scroll_down()
        elif event.key in ("k", "up"):
            event.stop()
            self.action_scroll_up()
        elif event.key in ("g", "home"):
            event.stop()
            self.action_scroll_home()
        elif event.key in ("G", "end"):
            event.stop()
            self.action_scroll_end()

    def action_scroll_down(self) -> None:
        """Scroll content down."""
        try:
            container = self.query_one("#graph-content", ScrollableContainer)
            container.scroll_down()
        except Exception:
            pass

    def action_scroll_up(self) -> None:
        """Scroll content up."""
        try:
            container = self.query_one("#graph-content", ScrollableContainer)
            container.scroll_up()
        except Exception:
            pass

    def action_scroll_home(self) -> None:
        """Scroll to top."""
        try:
            container = self.query_one("#graph-content", ScrollableContainer)
            container.scroll_home()
        except Exception:
            pass

    def action_scroll_end(self) -> None:
        """Scroll to bottom."""
        try:
            container = self.query_one("#graph-content", ScrollableContainer)
            container.scroll_end()
        except Exception:
            pass

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-confirm":
            self.action_confirm()
        elif event.button.id == "btn-cancel":
            self.action_cancel()

    def action_confirm(self) -> None:
        self._confirmed = True
        self.hide()
        self._send_response()
        self._restore_focus()
        self.remove()

    def action_cancel(self) -> None:
        self._confirmed = False
        self.hide()
        self._send_response()
        self._restore_focus()
        self.remove()

    def _restore_focus(self) -> None:
        """Restore focus to task tree after dialog closes."""
        try:
            tree = self.app.query_one("#task-tree")
            tree.focus()
        except Exception:
            pass

    def _send_response(self) -> None:
        app = self.app
        if hasattr(app, "_broker_driver") and app._broker_driver is not None:
            result = {
                "confirmed": self._confirmed,
                "cancelled": not self._confirmed,
                "nodes": self.nodes,
                "edges": self.edges,
            }
            app._broker_driver.handle_confirm_response(self.request_id, result)


class SkillConfirmDialog(DialogBase, can_focus=True):
    """Dialog for confirming skill selection with timeout."""

    DEFAULT_CSS = """
    SkillConfirmDialog {
        layer: dialog;
        width: 100%;
        height: 100%;
        align: center middle;
        display: none;
        background: transparent;
    }
    SkillConfirmDialog.visible {
        display: block;
    }
    SkillConfirmDialog #skill-dialog-box {
        width: 75%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
    }
    SkillConfirmDialog #skill-dialog-title {
        height: 2;
        text-align: center;
        text-style: bold;
    }
    SkillConfirmDialog #skill-content {
        height: auto;
        min-height: 5;
        max-height: 16;
        overflow-y: auto;
        padding: 1;
    }
    SkillConfirmDialog #skill-hint {
        height: 2;
        text-align: center;
    }
    SkillConfirmDialog #skill-timer {
        height: 2;
        text-align: center;
    }
    SkillConfirmDialog #skill-button-row {
        height: auto;
        align: center middle;
        padding: 1 0;
    }
    SkillConfirmDialog .dialog-button {
        margin: 0 2;
        min-width: 12;
        height: 3;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("enter", "confirm", "Confirm", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("j", "move_down", "Down", priority=True),
        Binding("k", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("h", "prev_skill", "Prev Skill", priority=True),
        Binding("l", "next_skill", "Next Skill", priority=True),
        Binding("left", "prev_skill", "Prev Skill", priority=True),
        Binding("right", "next_skill", "Next Skill", priority=True),
    ]

    def __init__(
        self,
        request_id: str = "",
        items: list[dict] | None = None,
        timeout_seconds: int = 60,
        theme: Theme | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.request_id = request_id
        self.items = items or []
        self.timeout_seconds = timeout_seconds
        self.remaining_seconds = timeout_seconds
        self.selected_idx = 0
        self.selections: dict[str, str] = {
            item["item_id"]: item["current_skill"]
            for item in self.items
        }
        self._confirmed: bool | None = None
        self._theme = theme
        self._timer_handle = None

    def compose(self) -> ComposeResult:
        from textual.widgets import Button
        with Vertical(id="skill-dialog-box"):
            yield Static(f"Skill Selection ({self.remaining_seconds}s left)", id="skill-dialog-title")
            yield ScrollableContainer(
                Static(self._format_items(), id="skill-items-text"),
                id="skill-content",
            )
            yield Static("jk/Arrows: select | hl/Arrows: switch skill | Enter: confirm | Esc: cancel", id="skill-hint")
            yield Static(f"Auto-confirm in {self.remaining_seconds}s", id="skill-timer")
            with Horizontal(id="skill-button-row"):
                yield Button("Confirm", id="skill-btn-confirm", variant="success", classes="dialog-button")
                yield Button("Cancel", id="skill-btn-cancel", variant="error", classes="dialog-button")

    def _format_items(self) -> str:
        """Format items for display."""
        lines = []
        for i, item in enumerate(self.items):
            item_id = item.get("item_id", "unknown")
            requirement = (item.get("requirement") or "")[:50]
            available = item.get("available_skills", [])
            current = self.selections.get(item_id, "?")
            source = item.get("source", "agent")
            source_mark = {"rule": "R", "agent": "A", "default": "D"}.get(source, "?")

            prefix = "> " if i == self.selected_idx else "  "
            skill_display = f"[{current}]" if current in available else current

            lines.append(f"{prefix}{i + 1}. [{item_id}] {skill_display} [{source_mark}]")
            lines.append(f"      {requirement}...")
            lines.append(f"      Options: {', '.join(available)}")
            lines.append("")

        return "\n".join(lines) if lines else "(no items)"

    def _refresh_display(self) -> None:
        """Refresh the display."""
        try:
            items_widget = self.query_one("#skill-items-text", Static)
            items_widget.update(self._format_items())

            title_widget = self.query_one("#skill-dialog-title", Static)
            title_widget.update(f"Skill Selection ({self.remaining_seconds}s left)")

            timer_widget = self.query_one("#skill-timer", Static)
            timer_widget.update(f"Auto-confirm in {self.remaining_seconds}s")
        except Exception:
            pass

    def on_mount(self) -> None:
        """Start countdown timer."""
        theme = self._theme
        if theme:
            try:
                dialog_box = self.query_one("#skill-dialog-box", Vertical)
                dialog_box.styles.background = theme.background
                dialog_box.styles.border = ("solid", theme.accent)

                title = self.query_one("#skill-dialog-title", Static)
                title.styles.color = theme.title

                content = self.query_one("#skill-content", ScrollableContainer)
                content.styles.background = theme.selection
                content.styles.border = ("solid", theme.border)

                hint = self.query_one("#skill-hint", Static)
                hint.styles.color = theme.dimmed

                timer = self.query_one("#skill-timer", Static)
                timer.styles.color = theme.running
            except Exception:
                pass

        self._timer_handle = self.set_interval(1.0, self._tick_timer)

    def _tick_timer(self) -> None:
        """Decrement timer and auto-confirm on timeout."""
        self.remaining_seconds -= 1
        if self.remaining_seconds <= 0:
            if self._timer_handle:
                self._timer_handle.stop()
            self._confirmed = True
            self.hide()
            self._send_response()
            self._restore_focus()
            self.remove()
        else:
            self._refresh_display()

    def on_key(self, event) -> None:
        """Handle key events."""
        if event.key in ("enter",):
            event.stop()
            self.action_confirm()
        elif event.key in ("escape",):
            event.stop()
            self.action_cancel()
        elif event.key in ("j", "down"):
            event.stop()
            self.action_move_down()
        elif event.key in ("k", "up"):
            event.stop()
            self.action_move_up()
        elif event.key in ("h", "left"):
            event.stop()
            self.action_prev_skill()
        elif event.key in ("l", "right"):
            event.stop()
            self.action_next_skill()

    def action_move_down(self) -> None:
        if self.items:
            self.selected_idx = (self.selected_idx + 1) % len(self.items)
            self._refresh_display()

    def action_move_up(self) -> None:
        if self.items:
            self.selected_idx = (self.selected_idx - 1) % len(self.items)
            self._refresh_display()

    def action_next_skill(self) -> None:
        """Cycle to next skill for selected item."""
        if not self.items:
            return
        item = self.items[self.selected_idx]
        item_id = item["item_id"]
        available = item.get("available_skills", [])
        if not available:
            return
        current = self.selections.get(item_id, available[0])
        try:
            idx = available.index(current)
        except ValueError:
            idx = 0
        new_idx = (idx + 1) % len(available)
        self.selections[item_id] = available[new_idx]
        self._refresh_display()

    def action_prev_skill(self) -> None:
        """Cycle to previous skill for selected item."""
        if not self.items:
            return
        item = self.items[self.selected_idx]
        item_id = item["item_id"]
        available = item.get("available_skills", [])
        if not available:
            return
        current = self.selections.get(item_id, available[0])
        try:
            idx = available.index(current)
        except ValueError:
            idx = 0
        new_idx = (idx - 1) % len(available)
        self.selections[item_id] = available[new_idx]
        self._refresh_display()

    def on_button_pressed(self, event) -> None:
        if event.button.id == "skill-btn-confirm":
            self.action_confirm()
        elif event.button.id == "skill-btn-cancel":
            self.action_cancel()

    def action_confirm(self) -> None:
        if self._timer_handle:
            self._timer_handle.stop()
        self._confirmed = True
        self.hide()
        self._send_response()
        self._restore_focus()
        self.remove()

    def action_cancel(self) -> None:
        if self._timer_handle:
            self._timer_handle.stop()
        self._confirmed = False
        self.hide()
        self._send_response()
        self._restore_focus()
        self.remove()

    def _restore_focus(self) -> None:
        try:
            tree = self.app.query_one("#task-tree")
            tree.focus()
        except Exception:
            pass

    def _send_response(self) -> None:
        app = self.app
        if hasattr(app, "_broker_driver") and app._broker_driver is not None:
            result = {
                "confirmed": self._confirmed,
                "cancelled": not self._confirmed,
                "skills": self.selections,
            }
            app._broker_driver.handle_confirm_response(self.request_id, result)


class SubmitTUI(App):
    """TUI for bro submit: task tree, progress bars, log viewer.
    Use 'q' to quit; Ctrl+Q can trigger OSError when broker is blocking on I/O.
    """

    TITLE = "bro submit"
    CSS_PATH = str(Path(__file__).parent / "tui.css")
    ENABLE_COMMAND_PALETTE = False
    LAYERS = ["base", "dialog"]

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("y", "copy_region", "Copy", show=False, priority=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("h", "cursor_left", "Left", show=False),
        Binding("l", "cursor_right", "Right", show=False),
        Binding("left", "cursor_left", "Left", show=False),
        Binding("right", "cursor_right", "Right", show=False),
        Binding("tab", "focus_next", "Next", show=False),
        Binding("shift+tab", "focus_previous", "Prev", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
        Binding("home", "scroll_home", "Top", show=False),
        Binding("end", "scroll_end", "Bottom", show=False),
        Binding("0", "line_start", "Start", show=False),
        Binding("$", "line_end", "End", show=False),
        Binding("s", "stop_container", "Stop", show=False),
        Binding("r", "restart_container", "Restart", show=False),
        Binding("x", "remove_container", "Remove", show=False),
    ]

    def __init__(
        self,
        event_queue: queue.Queue,
        verbose: bool = False,
        theme_name: str = DEFAULT_THEME,
        driver: Any = None,
    ) -> None:
        super().__init__()
        self._queue = event_queue
        self._verbose = verbose
        self._theme = get_theme(theme_name)
        self._broker_driver = driver
        self._task_node_data: dict[str, dict] = {}
        self._node_by_id: dict[str, TreeNode] = {}
        self._running_ids: set[str] = set()
        self._color_by_node_id: dict[str, int] = {}  # node_id (task ID) -> color index
        self._parent_progress: tuple[int, int] = (0, 0)
        self._child_tasks: list[dict] = []
        self._child_color_index = 0
        self._broker_done = False
        self._broker_thread: threading.Thread | None = None
        self._selected_log_path: Path | None = None
        self._log_viewer_content: str = ""
        self._console_lines: list[str] = []
        self._log_paths_by_worker: dict[str, list[tuple[str, str]]] = {}  # worker_id -> [(plan_id, path), ...]
        self._selected_node_label: str = ""
        self._selected_node_id: str | None = None
        self._parent_tasks_progress: list[dict] = []
        self._progress_carousel_index: int = 0
        self._completed_per_worker: dict[str, set[str]] = {}  # worker_id -> set of completed plan_id
        self._progress_carousel_timer: float = 0.0
        self._processed_result_hashes: set[int] = set()
        self._notification_queue: list[str] = []
        self._notification_visible: bool = False
        self._confirm_dialog: DependencyConfirmDialog | None = None
        self._skill_confirm_dialog: SkillConfirmDialog | None = None
        self._shutting_down: bool = False
        self._interval_handles: list = []
        self._containers: dict[str, dict] = {}
        self._container_node_by_name: dict[str, TreeNode] = {}
        self._selected_container_name: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="main-row"):
                with Vertical(id="left-column"):
                    with Container(id="tree-panel"):
                        yield Tree("Tasks", id="task-tree")
                    with Container(id="container-panel"):
                        yield Tree("Containers", id="container-tree")
                with Container(id="log-panel"):
                    with NonFocusableScrollContainer(id="log-scroll"):
                        yield LogViewer("", id="log-viewer", markup=False)
            with Container(id="console-panel"):
                yield Log(id="console-log")
            with Container(id="status-row"):
                yield Static("", id="status-line", markup=False)
            with Horizontal(id="progress-row"):
                yield Static("", id="progress-label", markup=False)
                yield ProgressBar(total=1, id="progress-bar", show_eta=False, show_percentage=False)
            with Horizontal(id="footer"):
                yield Static("", id="footer-help", markup=True)
                yield Static("", id="footer-notify", markup=True)

    def on_mount(self) -> None:
        self._apply_theme()
        self._setup_panel_titles()
        self._setup_footer()

        self._interval_handles.append(self.set_interval(0.3, self._drain_queue))
        self._interval_handles.append(self.set_interval(0.5, self._refresh_log_viewer))
        self._interval_handles.append(self.set_interval(2.0, self._refresh_containers_from_docker))
        self._interval_handles.append(self.set_interval(5.0, self._carousel_progress))

        tree = self.query_one("#task-tree", Tree)
        tree.root.expand()
        tree.focus()

        container_tree = self.query_one("#container-tree", Tree)
        container_tree.root.expand()

        self._refresh_containers_from_docker()

    def on_descendant_focus(self, event) -> None:
        """Update footer hints when focus changes."""
        try:
            container_tree = self.query_one("#container-tree", Tree)
            is_container_focused = event.widget == container_tree
            self._update_footer_hints(container_focused=is_container_focused)
        except Exception:
            pass

    def _apply_theme(self) -> None:
        """Apply theme colors via CSS variables."""
        theme = self._theme
        self.app.set_class(True, f"theme-{theme.name}")

        css_vars = f"""
        $bg: {theme.background};
        $border: {theme.border};
        $selection: {theme.selection};
        $title: {theme.title};
        $text: {theme.text};
        $dimmed: {theme.dimmed};
        $running: {theme.running};
        $waiting: {theme.waiting};
        $error: {theme.error};
        $accent: {theme.accent};
        """

        try:
            screen = self.screen
            screen.styles.background = theme.background
        except Exception:
            pass

    def _setup_panel_titles(self) -> None:
        """Set border titles on panels."""
        try:
            tree_panel = self.query_one("#tree-panel", Container)
            tree_panel.border_title = " Tasks "

            log_panel = self.query_one("#log-panel", Container)
            log_panel.border_title = " Log "

            container_panel = self.query_one("#container-panel", Container)
            container_panel.border_title = " Containers "

            console_panel = self.query_one("#console-panel", Container)
            console_panel.border_title = " Console "

        except Exception:
            pass

    def _setup_footer(self) -> None:
        """Set up the keyboard shortcuts footer (htop style)."""
        self._update_footer_hints()

    def _update_footer_hints(self, container_focused: bool = False) -> None:
        """Update footer hints based on current focus."""
        theme = self._theme
        
        if container_focused:
            hints = [
                ("↑/k", "Up"),
                ("↓/j", "Down"),
                ("s", "Stop"),
                ("r", "Restart"),
                ("x", "Remove"),
                ("Tab", "Switch"),
                ("q", "Quit"),
            ]
        else:
            hints = [
                ("↑/k", "Up"),
                ("↓/j", "Down"),
                ("←/h", "Left"),
                ("→/l", "Right"),
                ("g/G", "Top/Bottom"),
                ("Tab", "Switch"),
                ("Enter", "Select"),
                ("y", "Copy"),
                ("q", "Quit"),
            ]
        
        parts = []
        for key, desc in hints:
            parts.append(f"[bold on {theme.selection}]{key}[/]{desc}")
        footer_text = " ".join(parts)
        try:
            footer_help = self.query_one("#footer-help", Static)
            footer_help.update(footer_text)
        except Exception:
            pass

    def _notify(self, message: str) -> None:
        """Show a notification in the footer. Auto-hides after 3 seconds."""
        self._notification_queue.append(message)
        if not self._notification_visible:
            self._show_next_notification()

    def _show_next_notification(self) -> None:
        """Display the next notification from queue."""
        if not self._notification_queue:
            self._notification_visible = False
            try:
                footer_notify = self.query_one("#footer-notify", Static)
                footer_notify.update("")
            except Exception:
                pass
            return

        self._notification_visible = True
        message = self._notification_queue.pop(0)
        theme = self._theme
        try:
            footer_notify = self.query_one("#footer-notify", Static)
            footer_notify.update(f"[{theme.accent}]{message}[/]")
        except Exception:
            pass
        self.set_timer(3.0, self._show_next_notification)

    def _get_node_color(self, node_id: str) -> int:
        """Get color index for a node. Assigns new color if not yet assigned."""
        if node_id not in self._color_by_node_id:
            self._color_by_node_id[node_id] = len(self._color_by_node_id) % len(
                self._theme.task_colors
            )
        return self._color_by_node_id[node_id]

    def _get_node_color_hex(self, node_id: str) -> str:
        """Get hex color for a node."""
        idx = self._get_node_color(node_id)
        return self._theme.task_colors[idx]

    def action_cursor_down(self) -> None:
        """Move cursor down in focused widget."""
        focused = self.focused
        if isinstance(focused, Tree):
            focused.action_cursor_down()
        elif hasattr(focused, "scroll_down"):
            focused.scroll_down()

    def action_cursor_up(self) -> None:
        """Move cursor up in focused widget."""
        focused = self.focused
        if isinstance(focused, Tree):
            focused.action_cursor_up()
        elif hasattr(focused, "scroll_up"):
            focused.scroll_up()

    def action_scroll_home(self) -> None:
        """Scroll to top."""
        focused = self.focused
        if isinstance(focused, Tree):
            focused.scroll_home()
            if focused.root.children:
                focused.select_node(focused.root.children[0])
        elif hasattr(focused, "scroll_home"):
            focused.scroll_home()

    def action_scroll_end(self) -> None:
        """Scroll to bottom."""
        focused = self.focused
        if isinstance(focused, Tree):
            focused.scroll_end()
        elif hasattr(focused, "scroll_end"):
            focused.scroll_end()

    def action_cursor_left(self) -> None:
        """Collapse node in tree, or scroll left in other widgets."""
        focused = self.focused
        if isinstance(focused, Tree):
            node = focused.cursor_node
            if node is not None and node.is_expanded:
                node.collapse()
        elif hasattr(focused, "scroll_left"):
            focused.scroll_left()

    def action_cursor_right(self) -> None:
        """Expand node in tree, or scroll right in other widgets."""
        focused = self.focused
        if isinstance(focused, Tree):
            node = focused.cursor_node
            if node is not None and not node.is_expanded and node.children:
                node.expand()
        elif hasattr(focused, "scroll_right"):
            focused.scroll_right()

    def action_line_start(self) -> None:
        """Scroll to line start (horizontal)."""
        focused = self.focused
        if hasattr(focused, "scroll_home"):
            focused.scroll_home()

    def action_line_end(self) -> None:
        """Scroll to line end (horizontal)."""
        focused = self.focused
        if hasattr(focused, "scroll_end"):
            focused.scroll_end()

    def action_quit(self) -> None:
        """Quit the application with proper cleanup to restore terminal state."""
        self._shutting_down = True

        for handle in self._interval_handles:
            try:
                handle.stop()
            except Exception:
                pass
        self._interval_handles.clear()

        self._queue.put({"type": "shutdown"})

        self.exit()

    def _get_selected_container_name(self) -> str | None:
        """Get the currently selected container name from container tree."""
        try:
            container_tree = self.query_one("#container-tree", Tree)
            node = container_tree.cursor_node
            if node is None or node.data is None:
                return None
            if isinstance(node.data, dict):
                return node.data.get("container_name")
            return None
        except Exception:
            return None

    def action_stop_container(self) -> None:
        """Stop the selected container."""
        container_name = self._get_selected_container_name()
        if not container_name:
            self._notify("No container selected")
            return

        try:
            from broker.container.manager import ContainerManager
            manager = ContainerManager(workspace=Path("/tmp"))
            if manager.stop_container(container_name):
                self._notify(f"Stopped: {container_name}")
                self._append_console(f"[container] Stopped: {container_name}")
            else:
                self._notify(f"Failed to stop: {container_name}")
        except Exception as e:
            self._notify(f"Error: {e}")

    def action_restart_container(self) -> None:
        """Restart the selected container."""
        container_name = self._get_selected_container_name()
        if not container_name:
            self._notify("No container selected")
            return

        try:
            from broker.container.manager import ContainerManager
            manager = ContainerManager(workspace=Path("/tmp"))
            if manager.restart_container(container_name):
                self._notify(f"Restarted: {container_name}")
                self._append_console(f"[container] Restarted: {container_name}")
            else:
                self._notify(f"Failed to restart: {container_name}")
        except Exception as e:
            self._notify(f"Error: {e}")

    def action_remove_container(self) -> None:
        """Remove the selected container."""
        container_name = self._get_selected_container_name()
        if not container_name:
            self._notify("No container selected")
            return

        try:
            from broker.container.manager import ContainerManager
            manager = ContainerManager(workspace=Path("/tmp"))
            if manager.remove_container(container_name, force=True):
                self._notify(f"Removed: {container_name}")
                self._append_console(f"[container] Removed: {container_name}")
                if container_name in self._containers:
                    del self._containers[container_name]
                if container_name in self._container_node_by_name:
                    try:
                        node = self._container_node_by_name[container_name]
                        node.remove()
                    except Exception:
                        pass
                    del self._container_node_by_name[container_name]
            else:
                self._notify(f"Failed to remove: {container_name}")
        except Exception as e:
            self._notify(f"Error: {e}")

    def _carousel_progress(self) -> None:
        """Rotate progress bar display among parent tasks every 5 seconds."""
        if self._shutting_down:
            return
        if len(self._parent_tasks_progress) > 1:
            self._progress_carousel_index = (self._progress_carousel_index + 1) % len(
                self._parent_tasks_progress
            )
            self._refresh_progress()

    def _drain_queue(self) -> None:
        if self._shutting_down:
            return
        while True:
            try:
                evt = self._queue.get_nowait()
            except queue.Empty:
                break
            if self._shutting_down:
                break
            self._handle_event(evt)

    def _handle_event(self, evt: dict) -> None:
        if not isinstance(evt, dict):
            return
        t = evt.get("type")
        if t == "progress":
            parent = evt.get("parent")
            if isinstance(parent, dict):
                pc = parent.get("current", 0)
                pt = parent.get("total", 1)
            else:
                pc, pt = 0, 1
            self._parent_progress = (
                max(0, int(pc) if isinstance(pc, (int, float)) else 0),
                max(1, int(pt) if isinstance(pt, (int, float)) else 1),
            )
            child_tasks = evt.get("child_tasks")
            self._child_tasks = child_tasks if isinstance(child_tasks, list) else []
            for ct in self._child_tasks:
                tid = ct.get("subtask_id") or ct.get("task_id")
                if tid:
                    self._get_node_color(tid)
                    self._running_ids.add(tid)
            if self._child_tasks:
                self._apply_tree_colors()
            self._update_parent_tasks_progress()
            self._refresh_progress()
        elif t == "task_tree":
            nodes = evt.get("nodes", [])
            nodes = nodes if isinstance(nodes, list) else []
            running_raw = evt.get("running_ids")
            running = set(running_raw) if isinstance(running_raw, (list, set)) else set()
            self._update_tree(nodes, running)
        elif t == "log_paths":
            paths = evt.get("paths", [])
            paths = paths if isinstance(paths, list) else []
            self._add_log_paths_to_tree(paths)
        elif t == "task_assigned":
            wid = evt.get("worker_id") or evt.get("task_id") or "?"
            preview = evt.get("objective_preview", "")
            who = evt.get("assignee") or evt.get("subtask_id") or "?"
            self._append_console(f"Assigned: {wid} -> {who} | {preview}")
        elif t == "result":
            wid = evt.get("worker_id") or evt.get("task_id") or "?"
            plan_id = evt.get("plan_id", evt.get("role", "?"))
            if wid and plan_id and plan_id != "?":
                self._completed_per_worker.setdefault(wid, set()).add(plan_id)
            status = evt.get("status", "?")
            code = evt.get("exit_code")
            line = f"Result: {wid} ({plan_id}): {status}"
            if code is not None:
                line += f" (exit {code})"
            self._append_console(line)
            self._refresh_progress()
        elif t == "status":
            self._update_status(evt.get("message", ""), evt.get("elapsed_seconds"))
        elif t == "console":
            self._append_console(evt.get("message", ""))
        elif t == "verbose":
            if self._verbose:
                self._append_console(evt.get("message", ""))
        elif t == "error":
            self._append_console(f"Error: {evt.get('message', '')}")
        elif t == "confirm_deps_request":
            self._show_confirm_dialog(evt)
        elif t == "confirm_skills_request":
            self._show_skill_confirm_dialog(evt)
        elif t == "confirm_skills_timeout":
            self._append_console("Skill selection timeout, auto-confirmed")
        elif t == "run_external_request":
            self._handle_run_external(evt)
        elif t == "container_status":
            self._handle_container_status(evt)
        elif t == "done":
            self._broker_done = True

    def _update_tree(self, nodes: list[dict], running_ids: set[str]) -> None:
        tree = self.query_one("#task-tree", Tree)
        self._running_ids = set(running_ids) if isinstance(running_ids, (list, set)) else set()
        if not isinstance(nodes, list):
            return

        for node in nodes:
            if not isinstance(node, dict):
                continue
            nid = str(node.get("id") or node.get("label", "?"))
            label = str(node.get("label", nid))
            parent_id = node.get("parent_id")
            work_dir = node.get("work_dir")
            log_path = node.get("log_path") or (
                str(Path(work_dir) / "agent.log") if work_dir else None
            )

            if nid in self._running_ids:
                self._get_node_color(nid)

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
        """Add log path nodes. Paths = [{path, worker_id, plan_id?, parent_id?}]."""
        if not isinstance(paths, list):
            return
        tree = self.query_one("#task-tree", Tree)
        added_any = False
        for p in paths:
            if not isinstance(p, dict):
                continue
            path = p.get("path")
            worker_id = str(p.get("worker_id") or p.get("task_id") or "?")
            plan_id = str(p.get("plan_id", p.get("role", "?")))
            parent_id = p.get("parent_id")
            # node_id = directory name = real task ID (e.g. "test-parallel-deps-17722110687154797-first")
            node_label = Path(path).parent.name if path else f"{worker_id}-{plan_id}"
            node_id = node_label
            if not path or node_id in self._node_by_id:
                continue

            if parent_id and str(parent_id) in self._node_by_id:
                parent_node = self._node_by_id[str(parent_id)]
                child = parent_node.add_leaf(node_label, data=path)
                self._node_by_id[node_id] = child
                self._task_node_data[node_id] = {"id": node_id, "label": node_label}
            else:
                child = tree.root.add(node_label, expand=True)
                child.data = path
                self._node_by_id[node_id] = child
                self._task_node_data[node_id] = {"id": node_id, "label": node_label}

            self._running_ids.add(node_id)
            self._get_node_color(node_id)

            self._log_paths_by_worker.setdefault(worker_id, []).append((plan_id, path))
            added_any = True

            if self._selected_log_path is None and path:
                self._selected_log_path = Path(path)
                self._selected_node_id = node_id
                self._selected_node_label = node_label
                self._update_log_title(node_label)
                tree.select_node(child)

        if added_any:
            self._apply_tree_colors()
            self._refresh_log_viewer()

    def _handle_container_status(self, evt: dict) -> None:
        """Handle container status update event."""
        container_name = evt.get("container_name", "")
        run_id = evt.get("run_id", "")
        plan_id = evt.get("plan_id", evt.get("role", ""))
        status = evt.get("status", "")
        exit_code = evt.get("exit_code")
        error_message = evt.get("error_message", "")

        if not container_name:
            return

        self._containers[container_name] = {
            "container_name": container_name,
            "run_id": run_id,
            "plan_id": plan_id,
            "status": status,
            "exit_code": exit_code,
            "error_message": error_message,
            "work_dir": evt.get("work_dir", ""),
        }

        self._update_container_tree()

    def _refresh_containers_from_docker(self) -> None:
        """Poll Docker for agent-* and bro-subtask-* containers and update the tree."""
        try:
            items = list_visible_containers(all_containers=True)
        except Exception:
            return

        status_map = {
            "running": "running",
            "exited": "stopped",
            "created": "creating",
            "paused": "stopped",
        }

        names_from_docker = {item.get("name") for item in items if item.get("name")}
        changed = False
        for name in list(self._containers):
            if name not in names_from_docker:
                del self._containers[name]
                if name in self._container_node_by_name:
                    try:
                        node = self._container_node_by_name[name]
                        node.remove()
                    except Exception:
                        pass
                    del self._container_node_by_name[name]
                changed = True
        for item in items:
            name = item.get("name", "")
            if not name:
                continue

            raw_status = (item.get("status") or "").lower()
            status = status_map.get(raw_status, raw_status or "?")
            if status == "stopped" and item.get("exit_code") not in (None, 0):
                status = "failed"

            prev = self._containers.get(name, {})
            if (
                prev.get("status") != status
                or prev.get("exit_code") != item.get("exit_code")
            ):
                changed = True

            self._containers[name] = {
                "container_name": name,
                "run_id": prev.get("run_id", ""),
                "plan_id": prev.get("plan_id", prev.get("role", name.replace("bro-subtask-", "").replace("agent-", ""))),
                "status": status,
                "exit_code": item.get("exit_code"),
                "error_message": prev.get("error_message", ""),
                "work_dir": prev.get("work_dir", ""),
            }

        if changed:
            self._update_container_tree()

    def _update_container_tree(self) -> None:
        """Update the container tree with current container states."""
        try:
            container_tree = self.query_one("#container-tree", Tree)
        except Exception:
            return

        theme = self._theme

        for name, data in self._containers.items():
            status = data.get("status", "")
            plan_id = data.get("plan_id", data.get("role", ""))
            exit_code = data.get("exit_code")

            if status == "running":
                icon = "▶"
                color = theme.running
            elif status == "stopped":
                icon = "■"
                color = theme.dimmed
            elif status == "failed":
                icon = "✗"
                color = theme.error
            elif status == "creating":
                icon = "◌"
                color = theme.waiting
            elif status == "removed":
                icon = "○"
                color = theme.dimmed
            else:
                icon = "?"
                color = theme.dimmed

            short_name = name.replace("bro-subtask-", "").replace("agent-", "")
            label = f"[{color}]{icon}[/] {short_name}"
            if exit_code is not None and status != "running":
                label += f" (exit {exit_code})"

            if name in self._container_node_by_name:
                node = self._container_node_by_name[name]
                if getattr(node, "label", None) != label:
                    node.label = label
            else:
                node = container_tree.root.add_leaf(label, data=data)
                self._container_node_by_name[name] = node

    def _apply_tree_colors(self) -> None:
        """Color running task labels using Rich markup."""
        for nid in list(self._node_by_id):
            if nid not in self._task_node_data:
                continue
            tree_node = self._node_by_id.get(nid)
            if tree_node is None:
                continue
            base_label = self._task_node_data.get(nid, {}).get("label", nid) or nid
            if nid in self._running_ids:
                color = self._get_node_color_hex(nid)
                new_label = f"[{color}]{base_label}[/]"
            else:
                new_label = base_label
            if getattr(tree_node, "label", None) != new_label:
                tree_node.label = new_label

    def _update_parent_tasks_progress(self) -> None:
        """Update parent tasks progress list from _log_paths_by_worker. One entry per (worker, plan_id) for carousel."""
        parent_tasks = []
        for worker_id, plan_paths in self._log_paths_by_worker.items():
            if not plan_paths:
                continue
            total = len(plan_paths)
            for plan_id, path in plan_paths:
                node_id = Path(path).parent.name if path else f"{worker_id}-{plan_id}"
                color_idx = self._get_node_color(node_id)
                parent_tasks.append(
                    {
                        "worker_id": worker_id,
                        "label": node_id,
                        "node_id": node_id,
                        "color_idx": color_idx,
                        "total": total,
                    }
                )
        if parent_tasks:
            self._parent_tasks_progress = parent_tasks
            if self._progress_carousel_index >= len(parent_tasks):
                self._progress_carousel_index = 0

    def _append_console(self, message: str) -> None:
        if not message:
            return
        self._console_lines.append(message)
        try:
            console = self.query_one("#console-log", Log)
            console.write_line(message)
        except Exception:
            pass

    def _show_confirm_dialog(self, evt: dict) -> None:
        """Show dependency confirmation dialog."""
        request_id = evt.get("request_id", "")
        graph_text = evt.get("graph_text", "")
        nodes = evt.get("nodes", [])
        edges = evt.get("edges", [])
        edges = [(e[0], e[1]) if isinstance(e, (list, tuple)) else e for e in edges]

        self._append_console(f"[DEBUG] Dependency confirm request: {len(nodes)} nodes, {len(edges)} edges")

        if self._confirm_dialog is not None:
            try:
                self._confirm_dialog.remove()
            except Exception:
                pass

        self._confirm_dialog = DependencyConfirmDialog(
            request_id=request_id,
            graph_text=graph_text,
            nodes=nodes,
            edges=edges,
            theme=self._theme,
            id="confirm-dialog",
        )
        self.mount(self._confirm_dialog)
        self._confirm_dialog.show()
        self._confirm_dialog.focus()
        self._append_console("[DEBUG] Dialog shown, press Enter/A to confirm or Esc/N to cancel")

    def _handle_run_external(self, evt: dict) -> None:
        """Handle external command request by suspending TUI and running the command."""
        request_id = evt.get("request_id", "")
        args = evt.get("args", [])
        cwd = evt.get("cwd")

        if not args:
            self._send_external_response(request_id, 1)
            return

        self._append_console(f"[mergetool] Launching: {' '.join(args)}")

        exit_code = 1
        try:
            with self.suspend():
                result = subprocess.run(args, cwd=cwd, check=False)
                exit_code = result.returncode
        except Exception as e:
            self._append_console(f"[mergetool] Error: {e}")
            exit_code = 1

        self._send_external_response(request_id, exit_code)
        self._append_console(f"[mergetool] Completed with exit code {exit_code}")

    def _send_external_response(self, request_id: str, exit_code: int) -> None:
        """Send response for external command request."""
        if hasattr(self, "_broker_driver") and self._broker_driver is not None:
            result = {"exit_code": exit_code}
            self._broker_driver.handle_confirm_response(request_id, result)

    def _show_skill_confirm_dialog(self, evt: dict) -> None:
        """Show skill selection confirmation dialog with timeout."""
        request_id = evt.get("request_id", "")
        items = evt.get("items", [])
        timeout_seconds = evt.get("timeout_seconds", 60)

        self._append_console(f"[DEBUG] Skill confirm request: {len(items)} items, {timeout_seconds}s timeout")

        if self._skill_confirm_dialog is not None:
            try:
                self._skill_confirm_dialog.remove()
            except Exception:
                pass

        self._skill_confirm_dialog = SkillConfirmDialog(
            request_id=request_id,
            items=items,
            timeout_seconds=timeout_seconds,
            theme=self._theme,
            id="skill-confirm-dialog",
        )
        self.mount(self._skill_confirm_dialog)
        self._skill_confirm_dialog.show()
        self._skill_confirm_dialog.focus()
        self._append_console(f"[DEBUG] Skill dialog shown, auto-confirm in {timeout_seconds}s")

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
        if pt < 1:
            pt = 1
        if pc < 0:
            pc = 0
        if pc > pt:
            pc = pt
        bar = self.query_one("#progress-bar", ProgressBar)
        label = self.query_one("#progress-label", Static)

        for i in range(6):
            bar.remove_class(f"task-color-{i}")

        if len(self._parent_tasks_progress) >= 1:
            idx = self._progress_carousel_index % len(self._parent_tasks_progress)
            current_task = self._parent_tasks_progress[idx]
            task_label = current_task.get("label", "?")
            color_idx = current_task.get("color_idx", 0)
            worker_id = current_task.get("worker_id", "")
            task_total = current_task.get("total", 1)
            task_completed = len(self._completed_per_worker.get(worker_id, set()))
            # Fallback: broker often doesn't emit result events (e.g. serial docker) - use progress events instead
            if task_completed == 0 and pt > 0:
                task_completed, task_total = pc, pt
            carousel_indicator = (
                f" [{idx + 1}/{len(self._parent_tasks_progress)}]" if len(self._parent_tasks_progress) > 1 else ""
            )
            if task_total > 0:
                bar.update(total=task_total, progress=task_completed)
                label.update(f"{task_label}{carousel_indicator}: {task_completed}/{task_total}")
            else:
                bar.update(total=1, progress=0)
                label.update(f"{task_label}{carousel_indicator}:")
            bar.add_class(f"task-color-{color_idx % 6}")
        else:
            if pt > 0:
                bar.update(total=pt, progress=pc)
                label.update(f"Progress: {pc}/{pt}")
            else:
                bar.update(total=1, progress=0)
                label.update("Progress:")
            if self._child_tasks:
                idx = int(pc) % len(self._child_tasks) if self._child_tasks else 0
                tid = self._child_tasks[idx].get("subtask_id") or self._child_tasks[idx].get("task_id")
                if tid:
                    color_idx = self._get_node_color(tid)
                    bar.add_class(f"task-color-{color_idx % 6}")
            elif self._running_ids:
                first_id = next(iter(self._running_ids), None)
                if first_id:
                    color_idx = self._get_node_color(first_id)
                    bar.add_class(f"task-color-{color_idx % 6}")

    def _refresh_log_viewer(self) -> None:
        """Periodically re-read selected log file or container logs and update viewer."""
        if self._shutting_down:
            return

        if self._selected_container_name:
            # Container mode: fetch container logs from Docker
            container_name = self._selected_container_name
            logs = ""
            work_dir = (self._containers.get(container_name) or {}).get("work_dir", "")
            try:
                from broker.container.manager import ContainerManager
                manager = ContainerManager(workspace=Path("/tmp"))
                logs = manager.get_container_logs(container_name, tail=500)
            except Exception:
                pass
            if not logs and work_dir:
                try:
                    fallback_path = Path(work_dir) / "container.log"
                    if fallback_path.exists():
                        logs = fallback_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            content = logs if logs else "(no container logs)"
            self._log_viewer_content = content
            try:
                viewer = self.query_one("#log-viewer", LogViewer)
                viewer.update(content)
            except Exception:
                pass
            return

        if self._selected_log_path is not None:
            path = self._selected_log_path
            if path.exists():
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    tail = "\n".join(lines[-10:]) if len(lines) > 10 else "\n".join(lines)
                    self._log_viewer_content = tail or "(empty)"
                    viewer = self.query_one("#log-viewer", LogViewer)
                    viewer.update(self._log_viewer_content)
                except OSError:
                    pass

        for worker_id, plan_paths in self._log_paths_by_worker.items():
            for plan_id, log_path in plan_paths:
                p = Path(log_path)
                if not p.exists():
                    continue
                try:
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    task_label = p.parent.name if p.parent else f"{worker_id}-{plan_id}"
                    self._parse_task_results(lines, task_label)
                except OSError:
                    continue

    def _parse_task_results(self, lines: list[str], label: str | None = None) -> None:
        """Parse JSON lines for task results and display to console."""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            line_hash = hash(line)
            if line_hash in self._processed_result_hashes:
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "result":
                    continue
                self._processed_result_hashes.add(line_hash)
                is_error = obj.get("is_error", False)
                result_text = obj.get("result", "")
                if result_text:
                    preview = result_text[:200].replace("\n", " ")
                    if len(result_text) > 200:
                        preview += "..."
                    task_prefix = f"{label}: " if label else ""
                    if is_error:
                        self._append_console(f"{task_prefix}[ERROR] {preview}")
                    else:
                        self._append_console(f"{task_prefix}[RESULT] {preview}")
            except (json.JSONDecodeError, TypeError):
                continue

    def _update_log_title(self, label: str) -> None:
        """Update log panel border title."""
        try:
            log_panel = self.query_one("#log-panel", Container)
            if label:
                log_panel.border_title = f" Log ({label}) "
            else:
                log_panel.border_title = " Log "
        except Exception:
            pass

    def _get_tree_text(self) -> str:
        """Get text representation of the task tree."""
        lines = []
        try:
            tree = self.query_one("#task-tree", Tree)

            def walk_node(node, indent: int = 0) -> None:
                label = _plain_label(getattr(node, "label", "") or "")
                if label:
                    lines.append("  " * indent + label)
                for child in node.children:
                    walk_node(child, indent + 1)

            walk_node(tree.root)
        except Exception:
            pass
        return "\n".join(lines)

    def action_copy_region(self) -> None:
        """Copy content from focused panel to clipboard (y key, vim-style yank)."""
        content = None
        panel_name = None
        try:
            focused = self.focused
            if focused:
                # Check ancestors of focused widget to determine which panel it's in
                ancestor_ids = {w.id for w in focused.ancestors if hasattr(w, "id") and w.id}
                focused_id = getattr(focused, "id", None)

                if "log-panel" in ancestor_ids or focused_id == "log-panel" or focused_id == "log-viewer":
                    content = self._log_viewer_content
                    panel_name = "Log"
                elif "console-panel" in ancestor_ids or focused_id == "console-panel" or focused_id == "console-log":
                    content = "\n".join(self._console_lines) if self._console_lines else ""
                    panel_name = "Console"
                elif "tree-panel" in ancestor_ids or focused_id == "tree-panel" or focused_id == "task-tree":
                    if self._selected_node_id:
                        content = self._selected_node_id
                        panel_name = "Task ID"
                    else:
                        content = self._get_tree_text()
                        panel_name = "Tasks"
        except Exception:
            pass

        if content:
            try:
                self.copy_to_clipboard(content)
            except Exception:
                _copy_to_clipboard(content)
            self._notify(f"Copied {panel_name}")
        else:
            self.bell()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node = event.node
        if node is None:
            self._selected_node_id = None
            return

        # Check if this is from the container tree (event.control is the Tree that sent the message)
        try:
            if getattr(event.control, "id", None) == "container-tree":
                self._handle_container_node_selected(node)
                return
        except Exception:
            pass

        self._selected_container_name = None  # Switch out of container log mode
        label = getattr(node, "label", "") or ""
        self._selected_node_label = _plain_label(label)
        self._update_log_title(self._selected_node_label)

        # Find node id by reverse lookup in _node_by_id
        self._selected_node_id = None
        for nid, tree_node in self._node_by_id.items():
            if tree_node is node:
                self._selected_node_id = nid
                break

        # Accordion behavior: collapse all other expanded nodes, expand selected
        self._collapse_other_nodes(node)

        log_path = getattr(node, "data", None) if node else None
        if isinstance(log_path, str) and log_path:
            self._selected_log_path = Path(log_path)
            self._refresh_log_viewer()
        else:
            self._selected_log_path = None
            self._log_viewer_content = "(no log)"
            viewer = self.query_one("#log-viewer", LogViewer)
            viewer.update("(no log)")

    def _handle_container_node_selected(self, node: TreeNode) -> None:
        """Handle container tree node selection - show container logs."""
        data = getattr(node, "data", None)
        if not isinstance(data, dict):
            return

        container_name = data.get("container_name", "")
        if not container_name:
            return

        self._selected_container_name = container_name
        self._selected_log_path = None  # Switch out of task log mode
        label = _plain_label(getattr(node, "label", "") or container_name)
        self._update_log_title(f"Container: {label}")

        # Get container logs: try Docker API first, fallback to work_dir/container.log
        logs = ""
        work_dir = (self._containers.get(container_name) or {}).get("work_dir", "")
        try:
            from broker.container.manager import ContainerManager
            manager = ContainerManager(workspace=Path("/tmp"))
            logs = manager.get_container_logs(container_name, tail=500)
        except Exception:
            pass
        if not logs and work_dir:
            try:
                fallback_path = Path(work_dir) / "container.log"
                if fallback_path.exists():
                    logs = fallback_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if logs:
            self._log_viewer_content = logs
            viewer = self.query_one("#log-viewer", LogViewer)
            viewer.update(logs)
        else:
            self._log_viewer_content = "(no container logs)"
            viewer = self.query_one("#log-viewer", LogViewer)
            viewer.update("(no container logs)")

    def _collapse_other_nodes(self, selected_node: TreeNode) -> None:
        """Collapse all nodes except the selected one and its ancestors (accordion behavior)."""
        try:
            tree = self.query_one("#task-tree", Tree)
        except Exception:
            return

        ancestors: set[TreeNode] = set()
        current = selected_node
        while current is not None:
            ancestors.add(current)
            current = current.parent

        def collapse_recursively(node: TreeNode) -> None:
            if node is selected_node:
                if node.children:
                    node.expand()
                return
            if node in ancestors:
                for child in node.children:
                    collapse_recursively(child)
                return
            if node.is_expanded:
                node.collapse()

        collapse_recursively(tree.root)

    def run_broker(self, fn) -> None:
        """Run broker fn in thread. Call this before app.run()."""

        def worker():
            from broker.context import current_work_dir
            from broker.utils.traceback_util import error_summary_for_console, format_exc as traceback_format_exc

            try:
                fn()
            except BaseException as e:
                work_dir = current_work_dir.get()
                msg = error_summary_for_console(e)
                if work_dir is not None:
                    try:
                        tb = traceback_format_exc()
                        err_file = work_dir / "error.log"
                        err_file.write_text(tb, encoding="utf-8")
                        msg = f"{msg} (traceback -> {err_file})"
                    except OSError:
                        pass
                self._queue.put({"type": "error", "message": msg})
            finally:
                self._queue.put({"type": "done"})

        t = threading.Thread(target=worker, daemon=False)
        t.start()
        self._broker_thread = t


def _plain_label(label: str | Any) -> str:
    """Strip Rich markup [color]text[/] to plain text."""
    if label is None:
        return ""
    if hasattr(label, "plain"):
        return str(getattr(label, "plain", "") or "")
    s = str(label)
    if not s:
        return ""
    return re.sub(r"\[/?[^\]]*\]", "", s).strip() or s
