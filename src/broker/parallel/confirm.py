"""
人工确认流程模块。

展示依赖图，等待用户确认/修改依赖关系。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from broker.parallel.analyzer import DependencyEdge, DependencyGraph


def format_dependency_graph(graph: DependencyGraph) -> str:
    """格式化依赖图为可读文本"""
    lines = ["=" * 60, "子任务依赖关系分析结果", "=" * 60, "", f"子任务列表 ({len(graph.nodes)} 个):"]

    for i, node in enumerate(graph.nodes, 1):
        deps = graph.get_dependencies(node)
        if deps:
            lines.append(f"  {i}. {node} (依赖: {', '.join(deps)})")
        else:
            lines.append(f"  {i}. {node} (无依赖)")
    lines.append("")

    if graph.edges:
        lines.append(f"依赖关系 ({len(graph.edges)} 条):")
        for i, edge in enumerate(graph.edges, 1):
            lines.append(f"  {i}. {edge.from_task} → {edge.to_task}")
            lines.append(f"     原因: {edge.reason}")
            if edge.details:
                lines.append(f"     详情: {edge.details}")
        lines.append("")
    else:
        lines.append("未检测到依赖关系，所有子任务可并行执行。")
        lines.append("")

    parallel_groups = compute_parallel_groups(graph)
    lines.append("并行执行批次:")
    for i, group in enumerate(parallel_groups, 1):
        lines.append(f"  批次 {i}: {', '.join(group)}")
    lines.append("")

    return "\n".join(lines)


def compute_parallel_groups(graph: DependencyGraph) -> list[list[str]]:
    """计算可并行执行的任务分组（拓扑排序分层）"""
    in_degree: dict[str, int] = {node: 0 for node in graph.nodes}
    for edge in graph.edges:
        if edge.to_task in in_degree:
            in_degree[edge.to_task] += 1

    groups = []
    remaining = set(graph.nodes)

    while remaining:
        ready = [node for node in remaining if in_degree.get(node, 0) == 0]
        if not ready:
            ready = list(remaining)[:1]

        groups.append(sorted(ready))

        for node in ready:
            remaining.discard(node)
            for dependent in graph.get_dependents(node):
                if dependent in in_degree:
                    in_degree[dependent] -= 1

    return groups


def prompt_confirm_dependencies(
    graph: DependencyGraph,
    output_func: Callable[[str], None] | None = None,
    input_func: Callable[[str], str] | None = None,
) -> DependencyGraph:
    """
    交互式确认依赖关系。

    Args:
        graph: 原始依赖图
        output_func: 输出函数（默认 print）
        input_func: 输入函数（默认 input）

    Returns:
        确认后的依赖图
    """
    output = output_func or print
    get_input = input_func or input

    output(format_dependency_graph(graph))
    output("-" * 60)
    output("操作说明:")
    output("  [Enter] 确认当前依赖关系")
    output("  [a] 添加依赖: a <from_task> <to_task>")
    output("  [d] 删除依赖: d <序号>")
    output("  [r] 重新显示依赖图")
    output("  [q] 取消并退出")
    output("-" * 60)

    confirmed_graph = DependencyGraph()
    for node in graph.nodes:
        confirmed_graph.add_node(node)
    for edge in graph.edges:
        confirmed_graph.add_edge(edge)

    while True:
        try:
            cmd = get_input("\n请输入操作 (Enter 确认): ").strip()
        except (EOFError, KeyboardInterrupt):
            output("\n操作取消")
            raise SystemExit(1)

        if not cmd:
            output("\n依赖关系已确认！")
            return confirmed_graph

        parts = cmd.split()
        action = parts[0].lower()

        if action == "q":
            output("操作取消")
            raise SystemExit(1)

        elif action == "r":
            output(format_dependency_graph(confirmed_graph))

        elif action == "a" and len(parts) >= 3:
            from_task = parts[1]
            to_task = parts[2]
            if from_task not in confirmed_graph.nodes:
                output(f"错误: 任务 '{from_task}' 不存在")
                continue
            if to_task not in confirmed_graph.nodes:
                output(f"错误: 任务 '{to_task}' 不存在")
                continue
            if from_task == to_task:
                output("错误: 不能自己依赖自己")
                continue
            confirmed_graph.add_edge(DependencyEdge(
                from_task=from_task,
                to_task=to_task,
                reason="manual",
                details="用户手动添加",
            ))
            output(f"已添加依赖: {from_task} → {to_task}")

        elif action == "d" and len(parts) >= 2:
            try:
                idx = int(parts[1]) - 1
                if 0 <= idx < len(confirmed_graph.edges):
                    edge = confirmed_graph.edges.pop(idx)
                    output(f"已删除依赖: {edge.from_task} → {edge.to_task}")
                else:
                    output(f"错误: 无效的序号 {idx + 1}")
            except ValueError:
                output("错误: 请输入有效的序号")

        else:
            output("未知命令。输入 'r' 查看帮助。")


def confirm_dependencies(
    graph: DependencyGraph,
    output_path: Path | None = None,
    auto_confirm: bool = False,
) -> DependencyGraph:
    """
    确认依赖关系（主入口）。

    Args:
        graph: 原始依赖图
        output_path: 保存确认后依赖图的路径
        auto_confirm: 是否自动确认（跳过交互）

    Returns:
        确认后的依赖图
    """
    if auto_confirm:
        confirmed = graph
    else:
        confirmed = prompt_confirm_dependencies(graph)

    if output_path:
        confirmed.save(output_path)

    return confirmed


def load_confirmed_dependencies(path: Path) -> DependencyGraph:
    """加载已确认的依赖图"""
    return DependencyGraph.load(path)


class ConfirmationUI:
    """确认界面（可用于 TUI 集成）"""

    def __init__(self, graph: DependencyGraph):
        self.original_graph = graph
        self.graph = DependencyGraph()
        for node in graph.nodes:
            self.graph.add_node(node)
        for edge in graph.edges:
            self.graph.add_edge(edge)
        self.selected_edge_idx = 0
        self.confirmed = False
        self.cancelled = False

    def get_display_lines(self) -> list[str]:
        """获取显示行（用于 TUI）"""
        lines = ["子任务依赖关系确认", ""]

        parallel_groups = compute_parallel_groups(self.graph)
        lines.append("执行批次预览:")
        for i, group in enumerate(parallel_groups, 1):
            lines.append(f"  批次 {i}: {', '.join(group)}")
        lines.append("")

        lines.append("依赖关系:")
        if not self.graph.edges:
            lines.append("  (无依赖，所有任务可并行)")
        else:
            for i, edge in enumerate(self.graph.edges):
                prefix = "→ " if i == self.selected_edge_idx else "  "
                lines.append(f"{prefix}{i + 1}. {edge.from_task} → {edge.to_task} [{edge.reason}]")
        lines.append("")

        lines.append("操作: [Enter] 确认 | [↑↓] 选择 | [d] 删除 | [a] 添加 | [q] 取消")

        return lines

    def move_selection(self, delta: int) -> None:
        """移动选择"""
        if self.graph.edges:
            self.selected_edge_idx = (self.selected_edge_idx + delta) % len(self.graph.edges)

    def delete_selected(self) -> None:
        """删除选中的依赖"""
        if self.graph.edges:
            self.graph.edges.pop(self.selected_edge_idx)
            if self.selected_edge_idx >= len(self.graph.edges):
                self.selected_edge_idx = max(0, len(self.graph.edges) - 1)

    def add_edge(self, from_task: str, to_task: str) -> bool:
        """添加依赖"""
        if from_task not in self.graph.nodes or to_task not in self.graph.nodes:
            return False
        if from_task == to_task:
            return False
        self.graph.add_edge(DependencyEdge(
            from_task=from_task,
            to_task=to_task,
            reason="manual",
            details="用户手动添加",
        ))
        return True

    def confirm(self) -> None:
        """确认"""
        self.confirmed = True

    def cancel(self) -> None:
        """取消"""
        self.cancelled = True

    def get_result(self) -> DependencyGraph | None:
        """获取结果"""
        if self.confirmed:
            return self.graph
        return None
