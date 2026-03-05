"""Subtask execution: LocalSubtaskInvoker and DockerSubtaskInvoker. Unifies serial and parallel subtask run logic."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from broker.agent.docker import run_container
from broker.ui.driver import DisplayDriver
from broker.utils.path_util import BRO_PROJECT_ROOT_ENV, PROJECT_ROOT
from broker.utils.work_util import (
    build_task_payload,
    get_work_dir,
    task_path_rel,
    write_run_meta,
    write_task_json,
)

BRO_PARENT_TASK_ID = "BRO_PARENT_TASK_ID"


def _emit_error_preview(on_msg: Callable[[str], None], text: str, max_lines: int = 5) -> None:
    lines = [ln for ln in text.split("\n") if ln.strip()]
    count = 0
    for ln in lines:
        if count >= max_lines:
            break
        s = ln.strip()
        if s.startswith("File ") or s.startswith("  File ") or s == "Traceback (most recent call last):":
            continue
        on_msg(f"  {ln[:200]}")
        count += 1


def run_one_subtask_local(
        item: dict,
        workspace: Path,
        source_path: Path,
        task: dict,
        parent_worker_id: str,
        child_run_id: str | None,
        parent_run_id: str | None,
        work_dir: Path,
        default_objective: str,
        fresh_level: int,
        verbose: bool,
        drv: DisplayDriver,
        build_cmd_fn: Callable[..., str | None],
        running_file_fn: Callable[[str], Path],
        cursor_api_key: str | None = None,
        cwd: Path | None = None,
        use_polling: bool = False,
        skill_timeout: int = 7200,
) -> int:
    """Run one breakdown subtask via local cursor-cli. Returns exit code."""
    from broker.utils.traceback_util import error_summary_for_console
    subtask_id = item.get("id", "?")
    role = item.get("role") or item.get("id", "worker")

    cmd = build_cmd_fn(
        item,
        src_path=str(source_path),
        fresh_level=fresh_level,
        local=True,
        workspace_path=str(workspace),
        default_objective=default_objective,
        run_id=child_run_id,
        parent_run_id=parent_run_id,
        cursor_api_key=cursor_api_key,
        worker_id=parent_worker_id,
    )
    if cmd is None:
        if verbose:
            drv.verbose(f"[broker] subtask {subtask_id} has no valid command, skip")
        return 0

    env = os.environ.copy()
    env[BRO_PARENT_TASK_ID] = parent_worker_id
    env[BRO_PROJECT_ROOT_ENV] = str(PROJECT_ROOT)
    if cursor_api_key:
        env["CURSOR_API_KEY"] = cursor_api_key

    run_file = running_file_fn(parent_worker_id)
    if use_polling and run_file.exists():
        run_file.unlink()

    run_cwd = str(cwd) if cwd is not None else str(source_path)
    start = time.time()
    if use_polling:
        proc = subprocess.Popen(cmd, shell=True, cwd=run_cwd, env=env, stdin=subprocess.DEVNULL)
        seen_paths: set[str] = set()
        last_elapsed_emit = -1
        while proc.poll() is None:
            if run_file.exists():
                for line in run_file.read_text().splitlines():
                    if line.strip() and line not in seen_paths:
                        seen_paths.add(line)
                        try:
                            data = json.loads(line)
                            drv.on_log_paths([{
                                "path": data.get("path", ""),
                                "worker_id": data.get("worker_id") or data.get("task_id"),
                                "role": data.get("role"),
                                "parent_id": parent_worker_id,
                            }])
                        except json.JSONDecodeError:
                            pass
            elapsed = time.time() - start
            if elapsed >= skill_timeout:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                if verbose:
                    drv.verbose(f"[broker] subtask {subtask_id} timed out after {skill_timeout}s")
                drv.on_console_message(f"Result: {subtask_id}: timeout ({skill_timeout}s)")
                return 124
            if int(elapsed) >= last_elapsed_emit + 2:
                last_elapsed_emit = int(elapsed)
                drv.on_status(f"Running subtask {subtask_id}...", elapsed_seconds=elapsed)
            time.sleep(0.3)
        last_code = proc.returncode
    else:
        try:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=run_cwd, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            stdout, stderr = proc.communicate(timeout=skill_timeout)
            last_code = proc.returncode
            if last_code != 0 and stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text:
                    drv.on_console_message(f"  stderr: {stderr_text[:200]}")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            if verbose:
                drv.verbose(f"[broker] subtask {subtask_id} timed out after {skill_timeout}s")
            drv.on_console_message(f"Result: {subtask_id}: timeout ({skill_timeout}s)")
            return 124
        except (OSError, subprocess.SubprocessError) as e:
            drv.on_console_message(f"[ERROR] Subtask {subtask_id} exception: {error_summary_for_console(e)}")
            return 1

    drv.on_console_message(f"Result: {subtask_id}: {'ok' if last_code == 0 else 'failed'} (exit {last_code})")
    return last_code


def run_one_subtask_docker(
        item: dict,
        workspace: Path,
        source_path: Path,
        task: dict,
        parent_worker_id: str,
        child_run_id: str | None,
        parent_run_id: str | None,
        work_dir: Path,
        default_objective: str,
        fresh_level: int,
        verbose: bool,
        drv: DisplayDriver,
        build_cmd_fn: Callable[..., str | None],
        cursor_api_key: str | None = None,
        docker_workspace: str = "/workspace",
        docker_source: str = "/source",
        skill_timeout: int = 7200,
) -> int:
    """Run one breakdown subtask via Docker (host runs container or skill cmd). Returns exit code."""
    from broker.utils.traceback_util import error_summary_for_console
    from broker.utils.id_client import gen_run_id

    subtask_id = item.get("id", "?")
    role = item.get("role") or item.get("id", "worker")

    if (item.get("objective") or item.get("requirement")) and not item.get("skill"):
        # INLINE: run cursor-agent directly from host
        subtask_run_id = child_run_id or gen_run_id()
        subtask_work_dir = get_work_dir(workspace, run_id=subtask_run_id, role=role, check_conflict=False)
        log_path = str(subtask_work_dir / "agent.log")
        drv.on_log_paths([{
            "path": log_path,
            "worker_id": parent_worker_id,
            "role": role,
            "parent_id": work_dir.name,
        }])
        write_run_meta(subtask_work_dir, subtask_run_id, parent_worker_id, role, parent_run_id)
        agent = {"id": role, "role": role, "mode": "plan", **item}
        payload = build_task_payload(task, agent, work_dir=subtask_work_dir)
        write_task_json(workspace, payload, subtask_work_dir)
        drv.on_console_message(f"[container] Starting cursor-agent for {subtask_id}...")
        try:
            run_container(
                role, role,
                task_id=subtask_run_id,
                workspace=workspace,
                work_dir_rel=task_path_rel(subtask_run_id, role),
                source=Path(source_path),
            )
            return 0
        except Exception as e:
            from docker.errors import ContainerError
            last_code = e.exit_status if isinstance(e, ContainerError) else 1
            drv.on_console_message(f"[ERROR] Subtask {subtask_id} failed (exit {last_code})")
            if isinstance(e, ContainerError) and getattr(e, "stderr", ""):
                _emit_error_preview(drv.on_console_message, e.stderr, max_lines=5)
            elif str(e):
                drv.on_console_message(f"  {error_summary_for_console(e)}")
            return last_code
    else:
        # SKILL: run cmd on host. Use docker_workspace/docker_source for cmd paths when in container.
        if docker_workspace and docker_source:
            cmd_ws_path = docker_workspace
            cmd_src_path = docker_source if Path(source_path).resolve() != Path(
                workspace).resolve() else docker_workspace
        else:
            cmd_ws_path = str(workspace.resolve())
            cmd_src_path = str(Path(source_path).resolve())
        proc_cwd = str(Path(source_path).resolve())
        cmd_host = build_cmd_fn(
            item,
            src_path=cmd_src_path,
            fresh_level=fresh_level,
            local=False,
            workspace_path=cmd_ws_path,
            default_objective=default_objective,
            run_id=child_run_id,
            parent_run_id=parent_run_id,
            cursor_api_key=cursor_api_key,
            worker_id=parent_worker_id,
        )
        if cmd_host is None:
            drv.on_console_message(f"[ERROR] Cannot build command for subtask {subtask_id}")
            return 1
        drv.on_console_message(f"[host] Running skill for {subtask_id}...")
        env = os.environ.copy()
        env[BRO_PARENT_TASK_ID] = parent_worker_id
        env[BRO_PROJECT_ROOT_ENV] = str(PROJECT_ROOT)
        if cursor_api_key:
            env["CURSOR_API_KEY"] = cursor_api_key
        try:
            proc = subprocess.Popen(
                cmd_host, shell=True, cwd=proc_cwd, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            stdout, stderr = proc.communicate(timeout=skill_timeout)
            last_code = proc.returncode
            drv.on_console_message(
                f"Result: {subtask_id}: {'ok' if last_code == 0 else 'failed'} (exit {last_code})")
            if last_code != 0 and stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text:
                    drv.on_console_message(f"  stderr: {stderr_text[:300]}")
            return last_code
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            if verbose:
                drv.verbose(f"[broker] subtask {subtask_id} timed out after {skill_timeout}s")
            drv.on_console_message(f"Result: {subtask_id}: timeout ({skill_timeout}s)")
            return 124
        except (OSError, subprocess.SubprocessError) as e:
            drv.on_console_message(f"[ERROR] Subtask {subtask_id} exception: {error_summary_for_console(e)}")
            return 1
