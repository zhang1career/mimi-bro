"""Parallel execution management CLI commands."""
from __future__ import annotations

from pathlib import Path

import typer

from broker.parallel.analyzer import DependencyGraph
from broker.parallel.merge import ResultMerger, format_merge_summary
from broker.parallel.scheduler import ParallelExecutionState, TaskStatus
from broker.parallel.worktree import GitWorktree

app = typer.Typer(help="Parallel execution management")


def _get_state_dir(workspace: Path, run_id: str) -> Path:
    """获取状态目录（存放在 workspace/works/{run_id}/ 下）"""
    return workspace / "works" / run_id


def _find_latest_run(workspace: Path) -> str | None:
    """查找最近的 run_id"""
    works_dir = workspace / "works"
    if not works_dir.exists():
        return None
    runs = sorted(works_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for run_dir in runs:
        if run_dir.is_dir() and (run_dir / "status.json").exists():
            return run_dir.name
    return None


@app.command()
def status(
        run_id: str = typer.Argument(
            None,
            help="Run ID to check; default: latest",
        ),
        source: Path = typer.Option(
            None,
            "--source",
            "-s",
            help="Source path; default: current directory",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
):
    """Show status of a parallel execution."""
    run_id, state, ws = _resolve_run_and_load_state(run_id, source)

    typer.echo(f"Run ID: {state.run_id}")
    typer.echo(f"Worker ID: {state.worker_id}")
    typer.echo(f"Created: {state.created_at}")
    typer.echo(f"Updated: {state.updated_at}")
    typer.echo("")

    for subtask_id, subtask in state.subtasks.items():
        status_icon = {
            TaskStatus.PENDING: "⏳",
            TaskStatus.WAITING: "⏸️",
            TaskStatus.RUNNING: "🔄",
            TaskStatus.SUCCESS: "✅",
            TaskStatus.FAILED: "❌",
            TaskStatus.SKIPPED: "⏭️",
        }.get(subtask.status, "?")
        typer.echo(f"  {status_icon} {subtask_id}: {subtask.status.value}")
        if subtask.branch:
            typer.echo(f"      Branch: {subtask.branch}")
        if subtask.error_message:
            typer.echo(f"      Error: {subtask.error_message}")


@app.command()
def merge(
        run_id: str = typer.Argument(
            None,
            help="Run ID to merge; default: latest",
        ),
        source: Path = typer.Option(
            None,
            "--source",
            "-s",
            help="Source path; default: current directory",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
        target_branch: str = typer.Option(
            None,
            "--target",
            "-t",
            help="Target branch for merge; default: current branch",
        ),
        auto_cleanup: bool = typer.Option(
            False,
            "--cleanup",
            help="Automatically cleanup worktrees after merge",
        ),
        interactive: bool = typer.Option(
            True,
            "--interactive/--no-interactive",
            "-i",
            help="Use interactive merge with GUI mergetool on conflicts",
        ),
):
    """Merge results from parallel execution (cherry-pick in topological order)."""
    run_id, state, ws = _resolve_run_and_load_state(run_id, source)

    run_state_dir = _get_state_dir(ws, run_id)
    deps_path = run_state_dir / "confirmed_deps.json"
    if not deps_path.exists():
        deps_path = run_state_dir / "deps.json"
    if not deps_path.exists():
        typer.echo("Error: dependency graph not found", err=True)
        raise typer.Exit(1)

    graph = DependencyGraph.load(deps_path)
    merger = ResultMerger(ws, state, graph)

    def message_callback(msg: str) -> None:
        typer.echo(msg)

    typer.echo("Merge preview:")
    preview = merger.get_merge_preview()
    for item in preview:
        icon = "✓" if item["can_merge"] else "✗"
        commits = item.get("commit_count", 0)
        typer.echo(f"  {icon} {item['subtask_id']} ({item['branch']}) - {commits} commits")

    typer.echo("\nMerging...")
    summary = merger.merge(
        target_branch=target_branch,
        auto_cleanup=auto_cleanup,
        interactive=interactive,
        message_callback=message_callback,
    )
    typer.echo(format_merge_summary(summary))


@app.command()
def cleanup(
        run_id: str = typer.Argument(
            None,
            help="Run ID to cleanup; default: latest",
        ),
        source: Path = typer.Option(
            None,
            "--source",
            "-s",
            help="Source path; default: current directory",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
        force: bool = typer.Option(
            False,
            "--force",
            "-f",
            help="Force cleanup even if worktrees have uncommitted changes",
        ),
):
    """Cleanup worktrees and branches from a parallel execution."""
    run_id, state, ws = _resolve_run_and_load_state(run_id, source)

    run_state_dir = _get_state_dir(ws, run_id)
    deps_path = run_state_dir / "deps.json"
    graph = DependencyGraph.load(deps_path) if deps_path.exists() else DependencyGraph()

    merger = ResultMerger(ws, state, graph)

    typer.echo(f"Cleaning up worktrees for run: {run_id}")
    cleaned, errors = merger.cleanup_worktrees(force=force)

    if cleaned:
        typer.echo(f"Cleaned {len(cleaned)} worktrees:")
        for path in cleaned:
            typer.echo(f"  - {path}")
    else:
        typer.echo("No worktrees to clean up.")

    if errors:
        typer.echo(f"Errors ({len(errors)}):")
        for err in errors:
            typer.echo(f"  - {err}")


def _resolve_run_and_load_state(run_id: str | None, source: Path | None):
    """Resolve run_id (default: latest), load parallel execution state; return (run_id, state, source)."""
    ws = source or Path.cwd()
    if run_id is None:
        run_id = _find_latest_run(ws)
        if run_id is None:
            typer.echo("No parallel executions found.", err=True)
            raise typer.Exit(1)
    state_path = _get_state_dir(ws, run_id) / "status.json"
    if not state_path.exists():
        typer.echo(f"Error: state not found for run_id: {run_id}", err=True)
        raise typer.Exit(1)
    state = ParallelExecutionState.load(state_path)
    return run_id, state, ws


@app.command("worktree")
def worktree_cmd(
        action: str = typer.Argument(
            "list",
            help="Action: list, remove",
        ),
        branch: str = typer.Option(
            None,
            "--branch",
            "-b",
            help="Branch name (for remove)",
        ),
        source: Path = typer.Option(
            None,
            "--source",
            "-s",
            help="Source path; default: current directory",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
):
    """Manage git worktrees."""
    ws = source or Path.cwd()

    try:
        git = GitWorktree(ws)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if action == "list":
        worktrees = git.list_worktrees()
        if not worktrees:
            typer.echo("No worktrees found.")
        else:
            typer.echo("Worktrees:")
            for wt in worktrees:
                branch_info = f" ({wt.branch})" if wt.branch else " (detached)" if wt.is_detached else ""
                typer.echo(f"  - {wt.path}{branch_info}")

    elif action == "remove":
        if not branch:
            typer.echo("Error: --branch is required for remove", err=True)
            raise typer.Exit(1)
        entry = git.find_worktree_by_branch(branch)
        if not entry:
            typer.echo(f"Error: no worktree found for branch: {branch}", err=True)
            raise typer.Exit(1)
        git.remove_worktree(entry.path, force=True)
        typer.echo(f"Removed worktree: {entry.path}")

    else:
        typer.echo(f"Unknown action: {action}. Use 'list' or 'remove'.", err=True)
        raise typer.Exit(1)
