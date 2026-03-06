"""
Plan Item Schema 定义。

plans 元素（workers/xxx.json 中的 plans 字段）和 breakdown.json 元素使用相同的 schema。
这是计划层面的概念，区别于执行时产生的 task.json。

Schema 字段：
- id: 必须，唯一标识
- deps: 可选，显式依赖列表

执行方式（二选一）：
- skill: 调用 skill（worker 是一种 skill，invocation.type = "bro_submit"）
- (无 skill): 直接执行，需要 mode + objective（inline 方式）

执行参数：
- mode: 可选，默认 "agent"
- objective: 任务目标
- requirement: 需求描述（传给 skill）

元信息：
- scope: 可选，代码范围
- params: 可选，其他参数
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PlanItemType(str, Enum):
    """Plan item 执行类型"""
    SKILL = "skill"    # 调用 skill（包括 worker，worker 是 invocation.type=bro_submit 的 skill）
    INLINE = "inline"  # 直接执行，需要 mode + objective


@dataclass
class PlanItem:
    """统一的 plan item 定义"""
    id: str
    exec_type: PlanItemType
    deps: list[str] = field(default_factory=list)

    # 执行目标（skill id）
    skill: str = ""

    # 执行参数
    mode: str = "agent"
    objective: str = ""
    requirement: str = ""

    # 元信息
    scope: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为 dict 格式"""
        result: dict[str, Any] = {"id": self.id}
        if self.deps:
            result["deps"] = self.deps
        if self.exec_type == PlanItemType.SKILL:
            result["skill"] = self.skill
        if self.mode != "agent":
            result["mode"] = self.mode
        if self.objective:
            result["objective"] = self.objective
        if self.requirement:
            result["requirement"] = self.requirement
        if self.scope:
            result["scope"] = self.scope
        if self.params:
            result["params"] = self.params
        return result


def get_plan_item_type(item: dict[str, Any]) -> PlanItemType:
    """判断 plan item 的执行类型"""
    if "skill" in item and item["skill"]:
        return PlanItemType.SKILL
    return PlanItemType.INLINE


def parse_plan_item(item: dict[str, Any]) -> PlanItem:
    """从 dict 解析 plan item"""
    exec_type = get_plan_item_type(item)

    return PlanItem(
        id=item.get("id", ""),
        exec_type=exec_type,
        deps=list(item.get("deps") or []),
        skill=item.get("skill", ""),
        mode=item.get("mode", "agent"),
        objective=item.get("objective", ""),
        requirement=item.get("requirement", ""),
        scope=item.get("scope", ""),
        params=dict(item.get("params") or {}),
    )


def parse_plan_items(items: list[dict[str, Any]]) -> list[PlanItem]:
    """解析 plan item 列表"""
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        plan_item = parse_plan_item(item)
        if plan_item.id:
            result.append(plan_item)
    return result


def validate_plan_deps(items: list[PlanItem]) -> list[str]:
    """
    验证 plan item 依赖是否合法。
    返回错误列表，空列表表示验证通过。
    """
    errors = []
    ids = {item.id for item in items}

    for item in items:
        for dep in item.deps:
            if dep not in ids:
                errors.append(f"Plan item '{item.id}' depends on '{dep}' which does not exist")
            if dep == item.id:
                errors.append(f"Plan item '{item.id}' depends on itself")

    return errors


def build_dependency_map(items: list[PlanItem]) -> dict[str, list[str]]:
    """
    构建依赖映射：{item_id: [dependency_ids]}
    """
    return {item.id: list(item.deps) for item in items}


def build_dependents_map(items: list[PlanItem]) -> dict[str, list[str]]:
    """
    构建反向依赖映射：{item_id: [dependent_ids]}
    即哪些 plan item 依赖于该 item。
    """
    dependents: dict[str, list[str]] = {item.id: [] for item in items}
    for item in items:
        for dep in item.deps:
            if dep in dependents:
                dependents[dep].append(item.id)
    return dependents
