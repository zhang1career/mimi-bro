"""Worker subcommands: register, list."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from broker.skill import create_skill, update_skill, get_skill_entity_id, SkillNotFoundError
from broker.task import load_task

worker_app = typer.Typer(help="Worker 管理：注册 worker 为 skill")


def _build_worker_description(worker_config: dict, task_file: str) -> dict:
    """
    构建 worker 的 skill description。
    Worker 是一种 skill，invocation.type = "bro_submit"。
    """
    worker_block = worker_config.get("worker") or worker_config.get("task") or worker_config
    worker_id = worker_block.get("id", "")

    return {
        "invocation": {
            "type": "bro_submit",
            "task_file": task_file,
            "source": "{src_path}",
            "local": True,
        },
        "executors": {
            worker_id: {
                "mode": "agent",
                "method": "local",
            }
        },
    }


def _get_worker_title(worker_config: dict, task_file: str) -> str:
    """获取 worker 的 title（用作 skill_id）"""
    worker_block = worker_config.get("worker") or worker_config.get("task") or worker_config
    worker_id = worker_block.get("id", "")
    if worker_id:
        return worker_id
    return Path(task_file).stem


@worker_app.command("register")
def worker_register(
    task_file: str = typer.Argument(..., help="Worker JSON 文件路径（如 workers/backend-dev.json）"),
    title: str | None = typer.Option(None, "--title", "-t", help="自定义 skill title（默认使用 worker.id）"),
    force: bool = typer.Option(False, "--force", "-f", help="如果已存在则更新"),
):
    """
    注册 worker 为 skill。
    Worker 是一种 skill，invocation.type = "bro_submit"。
    """
    try:
        worker_config = load_task(task_file)
    except Exception as e:
        typer.echo(f"加载 worker 文件失败: {e}", err=True)
        raise typer.Exit(1) from e

    skill_title = title or _get_worker_title(worker_config, task_file)
    description = _build_worker_description(worker_config, task_file)
    description_json = json.dumps(description, ensure_ascii=False)

    worker_block = worker_config.get("worker") or worker_config.get("task") or worker_config
    objective = worker_block.get("objective", "")
    content = objective if objective else None

    try:
        if force:
            try:
                entity_id = get_skill_entity_id(skill_title)
                result = update_skill(
                    entity_id,
                    description=description_json,
                    content=content,
                )
                typer.echo(f"已更新 worker '{skill_title}' (entity_id={entity_id})")
                return
            except SkillNotFoundError:
                pass

        result = create_skill(
            title=skill_title,
            description=description_json,
            content=content,
            source_type="mimi-bro",
        )
        typer.echo(f"已注册 worker '{skill_title}' 为 skill")
        typer.echo(f"  invocation.type: bro_submit")
        typer.echo(f"  task_file: {task_file}")
    except RuntimeError as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            typer.echo(f"Worker '{skill_title}' 已存在。使用 --force 更新。", err=True)
        else:
            typer.echo(f"注册失败: {e}", err=True)
        raise typer.Exit(1) from e


@worker_app.command("list")
def worker_list():
    """列出 workers/ 目录下的所有 worker 文件"""
    from broker.utils.path_util import PROJECT_ROOT

    workers_dir = PROJECT_ROOT / "workers"
    if not workers_dir.exists():
        typer.echo("workers/ 目录不存在", err=True)
        raise typer.Exit(1)

    json_files = sorted(workers_dir.glob("*.json"))
    if not json_files:
        typer.echo("workers/ 目录下没有 JSON 文件")
        return

    for f in json_files:
        try:
            config = load_task(str(f))
            worker_block = config.get("worker") or config.get("task") or config
            worker_id = worker_block.get("id", f.stem)
            objective = worker_block.get("objective", "")[:50]
            typer.echo(f"  {f.name}: {worker_id} - {objective}...")
        except Exception:
            typer.echo(f"  {f.name}: (解析失败)")
