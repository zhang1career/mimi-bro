"""
Skill 选择确认模块：实现限时交互式确认。

用于方案 C：给用户 60 秒时间修改 Agent 选择的 skill。
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any


SKILL_CONFIRM_TIMEOUT = 60


def format_skill_selection(items: list[dict[str, Any]]) -> str:
    """格式化 skill 选择列表供显示"""
    lines = [
        "=" * 60,
        "Skill 选择确认",
        "=" * 60,
        "",
        f"共 {len(items)} 个子任务需要确认 skill:",
        "",
    ]

    for i, item in enumerate(items, 1):
        item_id = item.get("item_id", "unknown")
        requirement = item.get("requirement", "")[:60]
        current = item.get("current_skill", "?")
        available = item.get("available_skills", [])
        source = item.get("source", "agent")

        source_label = {
            "rule": "[规则匹配]",
            "agent": "[Agent选择]",
            "default": "[默认]",
            "user": "[用户指定]",
        }.get(source, f"[{source}]")

        lines.append(f"  {i}. [{item_id}]")
        lines.append(f"     需求: {requirement}...")
        lines.append(f"     当前: {current} {source_label}")
        lines.append(f"     可选: {', '.join(available)}")
        lines.append("")

    return "\n".join(lines)


def confirm_skills_terminal(
    items: list[dict[str, Any]],
    timeout_seconds: int = SKILL_CONFIRM_TIMEOUT,
) -> dict[str, str]:
    """
    终端交互式确认 skill 选择（带超时）。

    Args:
        items: 待确认的选择列表
        timeout_seconds: 超时秒数

    Returns:
        {item_id: skill_id} 映射
    """
    if not items:
        return {}

    result: dict[str, str] = {
        item["item_id"]: item["current_skill"]
        for item in items
    }

    print(format_skill_selection(items))
    print("-" * 60)
    print("操作说明:")
    print("  [Enter] 确认当前选择")
    print("  [数字 skill_id] 修改指定项的 skill，如: 1 frontend-dev")
    print("  [r] 重新显示列表")
    print("-" * 60)
    print(f"⏱ 限时 {timeout_seconds} 秒，超时自动确认当前选择")
    print()

    input_queue: queue.Queue[str] = queue.Queue()
    stop_event = threading.Event()

    def read_input() -> None:
        while not stop_event.is_set():
            try:
                if sys.stdin.closed:
                    break
                line = sys.stdin.readline()
                if line:
                    input_queue.put(line.strip())
                else:
                    break
            except (EOFError, OSError):
                break

    input_thread = threading.Thread(target=read_input, daemon=True)
    input_thread.start()

    start_time = time.time()
    available_skills = items[0].get("available_skills", []) if items else []

    try:
        while True:
            remaining = timeout_seconds - (time.time() - start_time)
            if remaining <= 0:
                print(f"\n⏱ 超时 ({timeout_seconds}s)，自动确认当前选择")
                break

            print(f"\r请输入操作 (剩余 {int(remaining)}s): ", end="", flush=True)

            try:
                cmd = input_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if not cmd:
                print("\n\n✓ 确认完成！")
                break

            if cmd.lower() == "r":
                print()
                print(format_skill_selection([
                    {**item, "current_skill": result[item["item_id"]]}
                    for item in items
                ]))
                continue

            parts = cmd.split(maxsplit=1)
            if len(parts) == 2:
                try:
                    idx = int(parts[0])
                    skill_id = parts[1].strip()

                    if 1 <= idx <= len(items):
                        item_id = items[idx - 1]["item_id"]
                        if skill_id in available_skills:
                            result[item_id] = skill_id
                            print(f"\n✓ 已修改: [{item_id}] → {skill_id}")
                        else:
                            print(f"\n✗ 无效的 skill: {skill_id}")
                            print(f"  可用: {', '.join(available_skills)}")
                    else:
                        print(f"\n✗ 无效的序号: {idx}")
                except ValueError:
                    print(f"\n✗ 无效命令: {cmd}")
            else:
                print(f"\n✗ 无效命令: {cmd}")
                print("  格式: <序号> <skill_id>，如: 1 frontend-dev")

    finally:
        stop_event.set()

    return result


class SkillConfirmationUI:
    """Skill 确认 UI（可用于 TUI 集成）"""

    def __init__(
        self,
        items: list[dict[str, Any]],
        timeout_seconds: int = SKILL_CONFIRM_TIMEOUT,
    ):
        self.items = items
        self.timeout_seconds = timeout_seconds
        self.selections: dict[str, str] = {
            item["item_id"]: item["current_skill"]
            for item in items
        }
        self.selected_idx = 0
        self.confirmed = False
        self.cancelled = False
        self.start_time = time.time()

    @property
    def remaining_seconds(self) -> float:
        return max(0, self.timeout_seconds - (time.time() - self.start_time))

    @property
    def is_timed_out(self) -> bool:
        return self.remaining_seconds <= 0

    def get_display_lines(self) -> list[str]:
        """获取显示行（用于 TUI）"""
        remaining = int(self.remaining_seconds)
        lines = [
            f"Skill 选择确认 (剩余 {remaining}s)",
            "",
        ]

        for i, item in enumerate(self.items):
            item_id = item.get("item_id", "unknown")
            requirement = item.get("requirement", "")[:40]
            current = self.selections.get(item_id, "?")
            source = item.get("source", "agent")

            prefix = "→ " if i == self.selected_idx else "  "
            source_mark = {"rule": "R", "agent": "A", "default": "D"}.get(source, "?")

            lines.append(f"{prefix}{i + 1}. [{item_id}] {current} [{source_mark}]")
            lines.append(f"     {requirement}...")

        lines.append("")
        lines.append("操作: [Enter] 确认 | [↑↓] 选择 | [←→] 切换 skill | [q] 取消")

        return lines

    def move_selection(self, delta: int) -> None:
        """移动选择"""
        if self.items:
            self.selected_idx = (self.selected_idx + delta) % len(self.items)

    def cycle_skill(self, delta: int) -> None:
        """切换当前项的 skill"""
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

        new_idx = (idx + delta) % len(available)
        self.selections[item_id] = available[new_idx]

    def confirm(self) -> None:
        self.confirmed = True

    def cancel(self) -> None:
        self.cancelled = True

    def get_result(self) -> dict[str, str] | None:
        if self.confirmed or self.is_timed_out:
            return self.selections
        if self.cancelled:
            return None
        return None
