"""
Task/Worker 结构统一解析。

Worker JSON 可能使用 worker / task 字段包裹主体，或直接是根对象。
本模块提供统一的取值入口，消除各处 task.get("worker") or task.get("task") or task 的重复。
"""
from __future__ import annotations

from typing import Any


def get_task_block(task: dict[str, Any]) -> dict[str, Any]:
    """
    获取 task 的主体块（worker 或 task 或根对象）。

    Worker JSON 格式：
    - {"worker": {...}} 或 {"task": {...}} → 返回内部对象
    - {...} 直接是主体 → 返回自身

    Returns:
        主体 dict，用于读取 id, objective, type, params, instructions 等字段
    """
    block = task.get("worker") or task.get("task")
    return block if block is not None and isinstance(block, dict) else task


def get_task_id(task: dict[str, Any]) -> str:
    """从 task 解析 id，兼容 worker/task/根结构。"""
    block = get_task_block(task)
    return block.get("id") or task.get("id") or "demo"
