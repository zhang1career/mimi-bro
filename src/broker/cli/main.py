"""Main CLI commands: submit, status, stop, run, parallel."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import typer

from broker.agent.runner import run_agents, run_agents_local, worker_id_from_task
from broker.decision.propose import propose
from broker.decision.record import record as record_decision
from broker.planner import plan_task
from broker.state.progress import clear_progress, clear_subtasks_progress
from broker.task import load_task, substitute_task, _find_project_root
from broker.ui import CLIDriver, JsonlDriver, PlainDriver
from broker.utils.env_util import load_dotenv_from_dir
from broker.utils.path_util import PROJECT_ROOT
from broker.utils.prompt_util import CONFIRM_TIMEOUT, prompt_with_timeout
from broker.utils.validate_util import validate_workspace

SCORE_GAP_THRESHOLD = float(os.environ.get("BROKER_SCORE_GAP_THRESHOLD", "0.5"))

app = typer.Typer()


@app.command()
def submit(
        task_file: str,
        workspace: Path = typer.Option(
            None,
            "--workspace",
            "-w",
            help="Workspace path for work dir (task.json, agent.log, works/); default: current directory",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
        source_path: Path = typer.Option(
            None,
            "--source",
            "-s",
            help="Source path (source code, scripts, tools) for agent to operate on; default: same as --workspace",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
        fresh: int = typer.Option(
            -1,
            "--fresh",
            help="Control re-execution level: -1=continue all (default), 0=re-execute all, n>0=continue levels<=n and re-execute levels>n (1=parent layer).",
        ),
        local: bool = typer.Option(
            False,
            "--local",
            help="Run locally without Docker container",
        ),
        auto: bool = typer.Option(
            False,
            "--auto",
            help="Use first plan without prompting and skip confirmation between steps (unattended/CI).",
        ),
        parallel: bool = typer.Option(
            False,
            "--parallel",
            "-p",
            help="Execute subtasks (skill_refs) in parallel using git worktree isolation.",
        ),
        run_id: str = typer.Option(
            None,
            "--run-id",
            help="Use specified run_id instead of generating a new one. Used by parent task to pass run_id to child task.",
        ),
        parent_run_id: str = typer.Option(
            None,
            "--parent-run-id",
            help="Parent task's run_id for tracing. Used by breakdown subtasks to record parent-child relationship.",
        ),
        max_workers: int = typer.Option(
            4,
            "--max-workers",
            "-j",
            help="Max parallel workers when --parallel is enabled.",
        ),
        args: list[str] = typer.Option(
            [],
            "--arg",
            "-a",
            help="Template params: KEY=VALUE (repeatable). Fills {{key}} in worker.id, worker.objective, worker.instructions, etc.",
        ),
        verbose: bool = typer.Option(
            False,
            "--verbose",
            "-v",
            help="Output all logs; when omitted, only Log path and step breakdown content are shown.",
        ),
        output_format: str = typer.Option(
            "auto",
            "--output-format",
            "-o",
            help="Output format: auto (TUI when TTY, plain otherwise), plain (line-based), jsonl (machine-readable for IDE plugin).",
        ),
        theme: str = typer.Option(
            None,
            "--theme",
            "-t",
            help="TUI color theme: phosphor (green terminal), tokyo-night-storm (blue-purple), catppuccin-latte (light). Default from .bro/config.toml or catppuccin-latte.",
        ),
):
    """Submit a task from JSON; broker writes task.json and runs agent(s)."""
    ws = workspace or PROJECT_ROOT
    task_id = None
    try:
        project_root = _find_project_root(ws)
        task = load_task(task_file, workspace=ws, project_root=project_root)
        task_id = worker_id_from_task(task)
    except Exception:
        pass
    try:
        _submit_impl(
            task_file, ws, source_path, fresh, local, auto, parallel,
            max_workers, args, verbose, output_format, theme, run_id, parent_run_id,
        )
    except BaseException as e:
        if task_id:
            err_dir = ws / "works" / task_id
            err_dir.mkdir(parents=True, exist_ok=True)
            err_file = err_dir / "error.log"
        else:
            err_file = ws / "error.log"
        try:
            err_file.write_text(traceback.format_exc(), encoding="utf-8")
            typer.echo(f"Error: {e}\n(traceback → {err_file})", err=True)
        except OSError:
            typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


def _submit_impl(
        task_file: str,
        workspace: Path,
        source_path: Path | None,
        fresh: int,
        local: bool,
        auto: bool,
        parallel: bool,
        max_workers: int,
        args: list[str],
        verbose: bool,
        output_format: str,
        theme: str | None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
):
    """Inner submit logic; exceptions propagate to submit() for error.log write."""
    if parent_run_id:
        os.environ["BRO_PARENT_RUN_ID"] = parent_run_id
    load_dotenv_from_dir(workspace)
    validate_workspace(workspace, verbose=verbose)
    res = source_path or workspace

    project_root = _find_project_root(workspace)
    task = load_task(task_file, workspace=workspace, project_root=project_root)
    params = dict(task.get("params") or {})
    for s in args:
        if "=" in s:
            k, v = s.split("=", 1)
            params[k.strip()] = v.strip()
    task = substitute_task(task, params)
    from broker.task import apply_params_to_plans
    task = apply_params_to_plans(task, params)
    if "params" in task:
        del task["params"]

    task_id = (task.get("worker") or task.get("task") or task).get("id") or task.get("id") or "demo"
    if fresh == 0:
        clear_progress(task_id)
    elif fresh > 0:
        if fresh == 1:
            clear_subtasks_progress(task_id, keep_parent=True)

    dag = plan_task(task)
    result = propose(dag)
    rules = result["rules"]
    plans = result["plans"]

    if not plans:
        typer.echo("Error: no execution plans (task has no agents in plan).", err=True)
        raise typer.Exit(1)

    if verbose:
        typer.echo("Rules: forbidden_node_ids=%s, max_parallel=%s" % (
            rules.get("forbidden_node_ids") or [], rules.get("max_parallel") or 0))
        typer.echo("Proposed execution plan(s) (scores placeholder):")
        for i, p in enumerate(plans):
            sc = p.get("score", 0)
            typer.echo(f"  [{i}] {p['summary']} (score=%s)" % sc)

    plans_sorted = sorted(plans, key=lambda _plan: _plan.get("score", 0), reverse=True)
    best_idx = plans.index(plans_sorted[0])
    choice = best_idx
    source = "human"

    if len(plans) == 1:
        if verbose:
            typer.echo(f"Selected plan: {plans[0]['summary']}")
        source = "auto"
    elif auto:
        if verbose:
            typer.echo(f"Selected plan (--auto): {plans[choice]['summary']}")
        source = "auto"
    else:
        gap = (
            (plans_sorted[0].get("score", 0) - plans_sorted[1].get("score", 0))
            if len(plans_sorted) >= 2
            else 0.0
        )
        if gap >= SCORE_GAP_THRESHOLD:
            choice = best_idx
            if verbose:
                typer.echo(f"Selected plan (score gap {gap:.2f} >= {SCORE_GAP_THRESHOLD}): {plans[choice]['summary']}")
            source = "score_gap"
        else:
            prompt = f"Select plan [0-{len(plans) - 1}] (default {best_idx}, {CONFIRM_TIMEOUT}s): "
            raw = prompt_with_timeout(prompt, default=str(best_idx), timeout_sec=CONFIRM_TIMEOUT)
            try:
                choice = int(raw) if raw.strip() else best_idx
            except ValueError:
                choice = best_idx
            if choice < 0 or choice >= len(plans):
                typer.echo(f"Error: plan index must be 0..{len(plans) - 1}", err=True)
                raise typer.Exit(1)
            if verbose:
                typer.echo(f"Selected plan: {plans[choice]['summary']}")

    record_decision({"event": "decision", "source": source, "choice": choice, "plan_summary": plans[choice]["summary"]})
    selected = plans[choice]
    agents = selected["agents"]
    batches = selected.get("batches")

    if output_format == "jsonl":
        driver = JsonlDriver(verbose=verbose)
        _run_agents_with_driver(
            agents, auto, batches, driver, fresh, local, parallel, max_workers, res, task, verbose, workspace, run_id, parent_run_id
        )
    elif output_format == "plain" or not sys.stdout.isatty():
        driver = PlainDriver(verbose=verbose)
        _run_agents_with_driver(
            agents, auto, batches, driver, fresh, local, parallel, max_workers, res, task, verbose, workspace, run_id, parent_run_id
        )
    else:
        from broker.ui.config import get_theme_name
        theme_name = get_theme_name(workspace, theme)
        driver = CLIDriver(verbose=verbose, theme_name=theme_name)

        def run_broker():
            if local:
                run_agents_local(
                    agents, workspace=workspace, source=res, task=task,
                    batches=batches, auto=auto, verbose=verbose, fresh_level=fresh,
                    parallel=parallel, max_workers=max_workers,
                    display_driver=driver, run_id=run_id, parent_run_id=parent_run_id,
                )
            else:
                run_agents(
                    agents, workspace=workspace, source=res, task=task,
                    batches=batches, auto=auto, verbose=verbose, fresh_level=fresh,
                    parallel=parallel, max_workers=max_workers,
                    display_driver=driver, run_id=run_id, parent_run_id=parent_run_id,
                )

        driver.run_with(run_broker)


def _run_agents_with_driver(
        agents, auto, batches, driver, fresh, local, parallel, max_workers, res, task, verbose, ws, run_id=None, parent_run_id=None
):
    if local:
        if verbose:
            typer.echo("[local] running with host cursor-cli (no Docker agent)")
        run_agents_local(
            agents, workspace=ws, source=res, task=task,
            batches=batches, auto=auto, verbose=verbose, fresh_level=fresh,
            parallel=parallel, max_workers=max_workers,
            display_driver=driver, run_id=run_id, parent_run_id=parent_run_id,
        )
    else:
        run_agents(
            agents, workspace=ws, source=res, task=task,
            batches=batches, auto=auto, verbose=verbose, fresh_level=fresh,
            parallel=parallel, max_workers=max_workers,
            display_driver=driver, run_id=run_id, parent_run_id=parent_run_id,
        )


@app.command()
def status():
    typer.echo("Status: v0.1 does not persist runtime state yet.")


@app.command()
def stop():
    typer.echo("Stop: not implemented in v0.1")


@app.command()
def run(
        role: str = typer.Argument(..., help="Agent role, e.g. backend / tester / docs"),
        workspace: Path = typer.Option(
            ...,
            "--workspace",
            "-w",
            help="Absolute path to workspace (work dir: task.json, agent.log, works/)",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
        source_path: Path = typer.Option(
            None,
            "--source",
            "-s",
            help="Source path (source code, scripts) for agent to operate on; default: same as --workspace",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
        agent_id: str = typer.Option(
            "agent-1",
            "--agent-id",
            help="Agent ID",
        ),
        task_id: str = typer.Option(
            "task-1",
            "--task-id",
            help="Task ID",
        ),
        run_id: str = typer.Option(
            "",
            "--run-id",
            help="Run ID (for work directory naming); if empty, a new one is generated",
        ),
        objective: str = typer.Option(
            "",
            "--objective",
            "-o",
            help="Task objective (prompt for cursor-cli); required for agent to run",
        ),
        local: bool = typer.Option(
            False,
            "--local",
            help="Run locally without Docker container",
        ),
        api_key: str = typer.Option(
            "",
            "--api-key",
            help="Cursor API key (passed to cursor-agent --api-key)",
        ),
        parent_run_id: str = typer.Option(
            "",
            "--parent-run-id",
            help="Parent task's run_id for tracing (used by breakdown subtasks).",
        ),
):
    """Run a single agent container. Writes workspace/works/task.json from --objective then starts container."""
    validate_workspace(workspace)
    res = source_path or workspace
    if not objective.strip():
        typer.echo("Error: --objective is required so that task.json can be written for the agent.", err=True)
        raise typer.Exit(1)

    agent = {
        "id": agent_id,
        "role": role,
        "mode": "agent",
    }
    task = {
        "id": task_id,
        "objective": objective,
        "instructions": [],
        "entrypoint": ".",
    }

    if local:
        run_agents_local([agent], workspace=workspace, source=res, task=task, run_id=run_id or None, cursor_api_key=api_key or None, parent_run_id=parent_run_id or None)
    else:
        run_agents([agent], workspace=workspace, source=res, task=task, run_id=run_id or None, parent_run_id=parent_run_id or None)
