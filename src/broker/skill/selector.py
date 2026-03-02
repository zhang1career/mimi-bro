"""
Skill 选择器：实现三层选择机制。

选择优先级：
1. 规则匹配（方案 B）：根据 scope 和 keywords 自动匹配
2. Agent 选择（方案 A）：Agent 根据 skill 描述选择
3. 用户确认（方案 C）：60 秒限时确认，超时自动继续

Skill 定义统一从 Knowledge API 加载，包含：
- id: skill ID
- description: 文字描述
- match_rules: 匹配规则 {scope_patterns, keywords, file_patterns}
- invocation: 调用方式
- executors: 执行者
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SkillMatchRule:
    """Skill 匹配规则"""
    scope_patterns: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    file_patterns: list[str] = field(default_factory=list)


@dataclass
class SkillInfo:
    """Skill 完整信息（用于选择）"""
    id: str
    description: str = ""
    match_rules: SkillMatchRule | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str) -> "SkillInfo":
        """
        从 dict 或 string 解析。

        数据来源应为 Knowledge API（通过 registry.get_skill_info() 获取）。

        支持的格式：
        1. 简单字符串: "backend-dev" -> 仅设置 id
        2. 完整 dict: {"id": "...", "description": "...", "match_rules": {...}}
        """
        if isinstance(data, str):
            return cls(id=data)

        skill_id = data.get("id") or data.get("skill_id") or ""
        description = data.get("description") or ""
        rules_data = data.get("match_rules")
        match_rules = None
        if rules_data and isinstance(rules_data, dict):
            match_rules = SkillMatchRule(
                scope_patterns=rules_data.get("scope_patterns") or [],
                keywords=rules_data.get("keywords") or [],
                file_patterns=rules_data.get("file_patterns") or [],
            )
        return cls(id=skill_id, description=description, match_rules=match_rules)


@dataclass
class SkillSelectionResult:
    """Skill 选择结果"""
    skill_id: str
    source: str  # "rule", "agent", "user", "default"
    confidence: float = 1.0
    reason: str = ""


def select_skill_by_rules(
    item: dict[str, Any],
    skill_refs: list[SkillInfo],
) -> SkillSelectionResult | None:
    """
    方案 B：规则匹配选择 skill。

    匹配逻辑：
    1. 检查 scope 是否匹配 scope_patterns
    2. 检查 requirement 是否包含 keywords
    3. 检查涉及的文件是否匹配 file_patterns

    Args:
        item: breakdown 中的单个元素 {requirement, scope, ...}
        skill_refs: 可用的 skill 列表

    Returns:
        匹配结果，如果无法确定返回 None
    """
    if not skill_refs:
        return None

    scope = (item.get("scope") or "").lower()
    requirement = (item.get("requirement") or item.get("objective") or "").lower()
    files = item.get("files") or []

    best_match: tuple[SkillInfo, int, str] | None = None

    for skill in skill_refs:
        if not skill.match_rules:
            continue

        rules = skill.match_rules
        score = 0
        reasons: list[str] = []

        for pattern in rules.scope_patterns:
            if pattern.lower() in scope or re.search(pattern, scope, re.IGNORECASE):
                score += 10
                reasons.append(f"scope matches '{pattern}'")
                break

        for kw in rules.keywords:
            if kw.lower() in requirement:
                score += 5
                reasons.append(f"keyword '{kw}'")

        for fp in rules.file_patterns:
            for f in files:
                if re.search(fp, str(f), re.IGNORECASE):
                    score += 3
                    reasons.append(f"file matches '{fp}'")
                    break

        if score > 0 and (best_match is None or score > best_match[1]):
            best_match = (skill, score, "; ".join(reasons))

    if best_match and best_match[1] >= 5:
        skill, score, reason = best_match
        confidence = min(1.0, score / 20.0)
        return SkillSelectionResult(
            skill_id=skill.id,
            source="rule",
            confidence=confidence,
            reason=reason,
        )

    return None


def format_skill_descriptions(skill_refs: list[SkillInfo]) -> str:
    """
    格式化 skill 描述供 Agent 使用（方案 A）。

    返回格式：
    Available skills:
    - backend-dev: 后端 Python/FastAPI 开发
    - frontend-dev: 前端 React/TypeScript 开发

    Args:
        skill_refs: skill 信息列表

    Returns:
        格式化的描述文本
    """
    if not skill_refs:
        return ""

    lines = ["Available skills for breakdown:"]
    for skill in skill_refs:
        desc = skill.description or "(no description)"
        lines.append(f"- {skill.id}: {desc}")

    lines.append("")
    lines.append(
        "For each sub-task in breakdown.json, select the most appropriate skill "
        "based on the requirement content. The 'skill' field must be one of the above skill IDs."
    )

    return "\n".join(lines)


def validate_skill_selection(
    items: list[dict[str, Any]],
    skill_refs: list[SkillInfo],
) -> list[dict[str, Any]]:
    """
    验证 breakdown items 中的 skill 选择是否有效。
    无效的 skill 会被替换为第一个可用的 skill。

    Args:
        items: breakdown 元素列表
        skill_refs: 可用的 skill 列表

    Returns:
        验证/修正后的 items
    """
    if not skill_refs:
        return items

    valid_ids = {s.id for s in skill_refs}
    default_skill = skill_refs[0].id

    result = []
    for item in items:
        item_copy = dict(item)
        skill_id = item_copy.get("skill") or item_copy.get("skill_id")

        if not skill_id or skill_id not in valid_ids:
            item_copy["skill"] = default_skill
            item_copy["_skill_auto_assigned"] = True

        result.append(item_copy)

    return result


def apply_rule_selection(
    items: list[dict[str, Any]],
    skill_refs: list[SkillInfo],
    callback: Callable[[str, str, str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    对 breakdown items 应用规则匹配选择。

    Args:
        items: breakdown 元素列表
        skill_refs: 可用的 skill 列表
        callback: 可选的回调函数 (item_id, skill_id, reason)

    Returns:
        (rule_matched_items, agent_selected_items):
        - rule_matched_items: 规则匹配成功的项目
        - agent_selected_items: 需要 Agent/用户确认的项目
    """
    rule_matched: list[dict[str, Any]] = []
    need_confirm: list[dict[str, Any]] = []

    for item in items:
        item_copy = dict(item)
        item_id = item_copy.get("id") or "unknown"

        result = select_skill_by_rules(item_copy, skill_refs)

        if result and result.confidence >= 0.5:
            item_copy["skill"] = result.skill_id
            item_copy["_skill_source"] = result.source
            item_copy["_skill_reason"] = result.reason
            rule_matched.append(item_copy)

            if callback:
                callback(item_id, result.skill_id, result.reason)
        else:
            need_confirm.append(item_copy)

    return rule_matched, need_confirm


@dataclass
class SkillConfirmationItem:
    """用于确认的 skill 选择项"""
    item_id: str
    requirement: str
    current_skill: str
    available_skills: list[str]
    source: str  # "rule", "agent", "default"
    confidence: float = 1.0


def prepare_confirmation_items(
    items: list[dict[str, Any]],
    skill_refs: list[SkillInfo],
) -> list[SkillConfirmationItem]:
    """
    准备用于确认的 skill 选择列表。

    Args:
        items: breakdown 元素列表（已有 skill 字段）
        skill_refs: 可用的 skill 列表

    Returns:
        确认项列表
    """
    available = [s.id for s in skill_refs]
    result = []

    for item in items:
        item_id = item.get("id") or "unknown"
        requirement = item.get("requirement") or item.get("objective") or ""
        current_skill = item.get("skill") or item.get("skill_id") or (available[0] if available else "")
        source = item.get("_skill_source") or "agent"

        result.append(SkillConfirmationItem(
            item_id=item_id,
            requirement=requirement[:100],
            current_skill=current_skill,
            available_skills=available,
            source=source,
        ))

    return result


def apply_confirmation_result(
    items: list[dict[str, Any]],
    confirmed_skills: dict[str, str],
) -> list[dict[str, Any]]:
    """
    应用用户确认的 skill 选择结果。

    Args:
        items: 原始 breakdown 元素列表
        confirmed_skills: {item_id: skill_id} 映射

    Returns:
        更新后的 items
    """
    result = []
    for item in items:
        item_copy = dict(item)
        item_id = item_copy.get("id")
        if item_id and item_id in confirmed_skills:
            item_copy["skill"] = confirmed_skills[item_id]
            item_copy["_skill_source"] = "user"
        result.append(item_copy)
    return result
