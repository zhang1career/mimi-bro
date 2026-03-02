"""
同步 skills/ 目录下的 JSON 文件到 knowledge 数据库。

映射关系（见 docs/SKILL.md）：
| skill       | knowledge API | 说明                     |
|-------------|---------------|-------------------------|
| id          | title         | knowledge 数据库中已存在  |
| description | content       | knowledge 数据库中已存在  |
| match_rules | description   | 新增                     |
| invocation  | description   | knowledge 数据库中已存在  |
| executor    | description   | knowledge 数据库中已存在  |

description 字段存储 JSON：{"match_rules": {...}, "invocation": {...}, "executors": {...}}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from broker.skill.registry import (
    SkillNotFoundError,
    create_skill,
    get_skill_entity_id,
    update_skill,
    _fetch_by_title,
)


def _get_skills_dir() -> Path:
    """获取 skills/ 目录路径"""
    return Path(__file__).resolve().parents[3] / "skills"


def _load_existing_skill(skill_id: str) -> dict[str, Any] | None:
    """
    从 knowledge API 加载现有 skill 定义。

    使用 /knowledge?title=... 接口按 title 前缀匹配查询，
    然后精确匹配 skill_id。
    """
    try:
        items = _fetch_by_title(skill_id)
        for k in items:
            if k.get("title") == skill_id:
                return k
    except Exception:
        pass
    return None


def _merge_description(
    existing_parsed: dict[str, Any],
    match_rules: dict | None,
    invocation: dict | None = None,
    executors: dict | None = None,
) -> str:
    """
    合并 description JSON 字段。

    Args:
        existing_parsed: 已解析的现有 description JSON
        match_rules: 新的 match_rules
        invocation: 新的 invocation
        executors: 新的 executors

    Returns:
        合并后的 JSON 字符串
    """
    result = dict(existing_parsed)

    if match_rules:
        result["match_rules"] = match_rules
    if invocation:
        result["invocation"] = invocation
    if executors:
        result["executors"] = executors

    return json.dumps(result, ensure_ascii=False, indent=2)


def sync_skill_file(skill_file: Path, dry_run: bool = False) -> dict[str, Any]:
    """
    同步单个 skill 文件到 knowledge 数据库。

    Args:
        skill_file: skill JSON 文件路径
        dry_run: 如果为 True，只打印不执行

    Returns:
        {"action": "create"|"update"|"skip", "skill_id": str, "message": str}
    """
    try:
        data = json.loads(skill_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"action": "error", "skill_id": skill_file.stem, "message": str(e)}

    skill_id = data.get("id") or skill_file.stem
    description = data.get("description") or ""
    match_rules = data.get("match_rules")
    invocation = data.get("invocation")
    executors = data.get("executors")

    existing = _load_existing_skill(skill_id)

    if existing:
        entity_id = existing.get("id")
        if not entity_id:
            return {"action": "error", "skill_id": skill_id, "message": "existing skill has no id"}

        existing_desc = existing.get("description") or ""
        try:
            existing_parsed = json.loads(existing_desc) if existing_desc else {}
        except json.JSONDecodeError:
            existing_parsed = {}

        existing_content = existing.get("content") or ""
        need_update = False
        updates = []

        if description and description != existing_content:
            need_update = True
            updates.append(f"content: {existing_content[:30]}... → {description[:30]}...")

        if match_rules and existing_parsed.get("match_rules") != match_rules:
            need_update = True
            updates.append("match_rules updated")

        if invocation and existing_parsed.get("invocation") != invocation:
            need_update = True
            updates.append("invocation updated")

        if executors and existing_parsed.get("executors") != executors:
            need_update = True
            updates.append("executors updated")

        if not need_update:
            return {"action": "skip", "skill_id": skill_id, "message": "no changes"}

        new_desc = _merge_description(existing_parsed, match_rules, invocation, executors)

        if dry_run:
            return {
                "action": "update (dry-run)",
                "skill_id": skill_id,
                "message": "; ".join(updates),
            }

        update_skill(
            entity_id,
            content=description if description else None,
            description=new_desc,
        )
        return {"action": "update", "skill_id": skill_id, "message": "; ".join(updates)}

    else:
        desc_data: dict[str, Any] = {}
        if match_rules:
            desc_data["match_rules"] = match_rules
        if invocation:
            desc_data["invocation"] = invocation
        if executors:
            desc_data["executors"] = executors
        desc_json = json.dumps(desc_data, ensure_ascii=False) if desc_data else "{}"

        if dry_run:
            return {
                "action": "create (dry-run)",
                "skill_id": skill_id,
                "message": f"content={description[:50]}...",
            }

        create_skill(
            title=skill_id,
            content=description,
            description=desc_json,
            source_type="mimi-bro",
        )
        return {"action": "create", "skill_id": skill_id, "message": "created"}


def sync_all_skills(dry_run: bool = False) -> list[dict[str, Any]]:
    """
    同步 skills/ 目录下所有 JSON 文件到 knowledge 数据库。

    Args:
        dry_run: 如果为 True，只打印不执行

    Returns:
        每个文件的同步结果列表
    """
    skills_dir = _get_skills_dir()
    if not skills_dir.exists():
        return [{"action": "error", "skill_id": "*", "message": f"skills dir not found: {skills_dir}"}]

    results = []
    for f in sorted(skills_dir.glob("*.json")):
        result = sync_skill_file(f, dry_run=dry_run)
        results.append(result)
        print(f"[{result['action']}] {result['skill_id']}: {result['message']}")

    return results


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="Sync skills/ to knowledge database")
    parser.add_argument("--dry-run", action="store_true", help="Only print, don't execute")
    parser.add_argument("--file", type=str, help="Sync single file instead of all")
    args = parser.parse_args()

    if args.file:
        result = sync_skill_file(Path(args.file), dry_run=args.dry_run)
        print(f"[{result['action']}] {result['skill_id']}: {result['message']}")
    else:
        results = sync_all_skills(dry_run=args.dry_run)
        print(f"\nTotal: {len(results)} skills")
        actions = {}
        for r in results:
            actions[r["action"]] = actions.get(r["action"], 0) + 1
        for action, count in actions.items():
            print(f"  {action}: {count}")


if __name__ == "__main__":
    main()
