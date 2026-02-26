"""Skill subcommands: list, assignees, invoke, how, create, modify."""
from __future__ import annotations

import json

import typer

from broker.skill import (
    SkillNotFoundError,
    create_skill,
    get_assignees,
    get_assignment_how,
    get_invocation,
    get_skill_entity_id,
    list_skills,
    load_skill_registry,
    update_skill,
)

skill_app = typer.Typer(help="临时技能接口：查询技能可指派给谁、如何指派")


@skill_app.command("list")
def skill_list():
    """列出所有已注册技能（从知识库 API 加载）"""
    try:
        load_skill_registry()
        for s in list_skills():
            typer.echo(s)
    except (SkillNotFoundError, RuntimeError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@skill_app.command("assignees")
def skill_assignees(skill: str = typer.Argument(..., help="技能 ID")):
    """查询某技能可指派给谁"""
    try:
        load_skill_registry()
        assignees = get_assignees(skill)
        if not assignees:
            typer.echo(f"技能 '{skill}' 未注册或无可指派执行者", err=True)
            raise typer.Exit(1)
        for a in assignees:
            typer.echo(f"  executor={a.get('executor')} mode={a.get('mode')} method={a.get('method')}")
    except (SkillNotFoundError, RuntimeError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@skill_app.command("invoke")
def skill_invoke(
    skill: str = typer.Argument(..., help="技能 ID"),
    src_path: str = typer.Option("src", "--src", "-r", help="resource 占位符 {src_path} 的取值"),
    set_params: list[str] = typer.Option([], "--set", "-s", help="KEY=VALUE，可重复"),
):
    """查询某技能的调用方法（bro submit / shell 命令，或 http 请求描述）"""
    try:
        load_skill_registry()
        params = {}
        for s in set_params:
            if "=" in s:
                k, v = s.split("=", 1)
                params[k.strip()] = v.strip()
        result = get_invocation(skill, src_path=src_path, **params)
        if result is None:
            typer.echo(f"技能 '{skill}' 未配置 invocation，无法生成调用", err=True)
            raise typer.Exit(1)
        if isinstance(result, dict):
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            typer.echo(result)
    except (SkillNotFoundError, RuntimeError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@skill_app.command("how")
def skill_how(
    skill: str = typer.Argument(..., help="技能 ID"),
    executor: str = typer.Argument(..., help="执行者 ID"),
):
    """查询某技能、某执行者的指派方法"""
    try:
        load_skill_registry()
        cfg = get_assignment_how(skill, executor)
        if cfg is None:
            typer.echo(f"技能 '{skill}' + 执行者 '{executor}' 未注册", err=True)
            raise typer.Exit(1)
        typer.echo(f"mode={cfg.get('mode')} method={cfg.get('method')}")
    except (SkillNotFoundError, RuntimeError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@skill_app.command("create")
def skill_create(
    title: str = typer.Argument(..., help="技能 ID（title，最大 512 字符）"),
    description: str = typer.Option(..., "--description", "-d", help="技能 JSON（{ executors, invocation }）"),
    content: str | None = typer.Option(None, "--content", "-c", help="内容"),
    source_type: str = typer.Option("mimi-bro", "--source-type", help="来源类型（最大 64 字符）"),
):
    """创建技能（POST /knowledge）。直接执行，无需人工确认。"""
    try:
        result = create_skill(
            title=title,
            description=description,
            content=content,
            source_type=source_type,
        )
        typer.echo(result)
    except (ValueError, RuntimeError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@skill_app.command("modify")
def skill_modify(
    entity_or_skill: str = typer.Argument(
        ...,
        help="知识 entity_id（整数）或 skill_id（按 title 查询得到 entity_id）",
    ),
    title: str | None = typer.Option(None, "--title", "-t", help="新 title"),
    description: str | None = typer.Option(None, "--description", "-d", help="新技能 JSON"),
    content: str | None = typer.Option(None, "--content", "-c", help="新 content"),
    source_type: str | None = typer.Option(None, "--source-type", help="新 source_type"),
):
    """修改技能（PUT /knowledge/{entity_id}）。执行前需人工确认。"""
    if not any([title is not None, description is not None, content is not None, source_type is not None]):
        typer.echo("至少指定一个要更新的字段: --title, --description, --content, --source-type", err=True)
        raise typer.Exit(1)
    try:
        entity_id: int
        if entity_or_skill.strip().isdigit():
            entity_id = int(entity_or_skill.strip())
        else:
            entity_id = get_skill_entity_id(entity_or_skill.strip())
        if not typer.confirm(f"确认修改技能 entity_id={entity_id}？"):
            typer.echo("已取消")
            raise typer.Exit(0)
        result = update_skill(
            entity_id,
            title=title,
            description=description,
            content=content,
            source_type=source_type,
        )
        typer.echo(result)
    except (ValueError, SkillNotFoundError, RuntimeError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
