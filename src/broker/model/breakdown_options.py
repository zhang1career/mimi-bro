"""
Breakdown 执行配置，用于 _execute_breakdown / _invoke_skill_refs 等函数。

替代原先散落的 kwargs 和 options dict，统一长参数列表。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BreakdownOptions:
    """Breakdown 执行的选项，用于串行/并行、fresh_level、auto 等控制。"""

    verbose: bool = False
    fresh_level: int = -1
    parallel: bool = False
    max_workers: int = 4
    auto: bool = False
    parent_run_id: str | None = None
    docker_workspace: str = "/workspace"
    docker_source: str = "/source"

    @classmethod
    def from_dict(cls, d: dict | None) -> BreakdownOptions:
        """从 options dict 构造（兼容旧调用方）。"""
        if not d:
            return cls()
        return cls(
            verbose=d.get("verbose", False),
            fresh_level=d.get("fresh_level", -1),
            parallel=d.get("parallel", False),
            max_workers=d.get("max_workers", 4),
            auto=d.get("auto", False),
            parent_run_id=d.get("parent_run_id"),
            docker_workspace=d.get("docker_workspace", "/workspace"),
            docker_source=d.get("docker_source", "/source"),
        )
