"""Skill management CLI commands."""
from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer()
skill_app = app  # alias for cli/__init__.py import


@app.command()
def sync(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Only print changes, don't execute",
    ),
    file: Path = typer.Option(
        None,
        "--file",
        "-f",
        help="Sync single file instead of all skills/",
        exists=True,
        file_okay=True,
        dir_okay=False,
    ),
):
    """
    Sync skills/ directory to knowledge database.

    Mapping (see docs/SKILL.md):
    - skill.id → knowledge.title
    - skill.description → knowledge.content
    - skill.match_rules → knowledge.description (JSON)
    """
    from broker.skill.sync import sync_all_skills, sync_skill_file

    if file:
        result = sync_skill_file(file, dry_run=dry_run)
        print(f"[{result['action']}] {result['skill_id']}: {result['message']}")
    else:
        results = sync_all_skills(dry_run=dry_run)
        print(f"\nTotal: {len(results)} skills")
        actions: dict[str, int] = {}
        for r in results:
            actions[r["action"]] = actions.get(r["action"], 0) + 1
        for action, count in actions.items():
            print(f"  {action}: {count}")


@app.command("list")
def list_skills():
    """List all skill definitions from Knowledge API."""
    from broker.skill.registry import load_skill_registry, get_all_skill_infos

    try:
        load_skill_registry()
    except Exception as e:
        print(f"Error loading skills from API: {e}")
        return

    skills = get_all_skill_infos()
    if not skills:
        print("No skills loaded from Knowledge API")
        return

    print(f"Found {len(skills)} skill(s):\n")
    for skill_id, data in sorted(skills.items()):
        desc = data.get("description", "(no description)")[:60]
        rules = data.get("match_rules") or {}
        scope = ", ".join(rules.get("scope_patterns", [])[:3]) if rules else "(none)"
        kw = ", ".join(rules.get("keywords", [])[:5]) if rules else "(none)"
        print(f"  {skill_id}")
        print(f"    Description: {desc}")
        print(f"    Scope: {scope}")
        print(f"    Keywords: {kw}")
        print()
