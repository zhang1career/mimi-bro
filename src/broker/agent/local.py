"""
Run task by invoking local cursor-cli (no Docker agent).
Used for bootstrap / self-hosting when no agent image is available.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

CURSOR_CLI_TIMEOUT = 1800  # 30 min


def _log(msg: str, verbose: bool = True) -> None:
    if verbose:
        print(f"[local] {msg}", flush=True)


def _resolve_cursor_bin(cursor_bin: Path | None, workspace: Path) -> tuple[Path | None, bool]:
    """
    Resolve headless cursor-cli for host execution. Prefer cursor-agent/agent
    (headless CLI); cursor from PATH is often the editor CLI and mis-handles prompt.
    Returns (resolved_path, use_run_subcommand): use_run is False for cursor-agent/agent.
    """
    if cursor_bin is not None:
        r = cursor_bin.resolve() if cursor_bin.exists() else None
        if r is None:
            return None, True
        use_run = r.name.lower() not in ("cursor-agent", "agent")
        return r, use_run

    env_path = os.getenv("CURSOR_CLI_PATH")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.exists():
            use_run = p.name.lower() not in ("cursor-agent", "agent")
            return p, use_run

    # Prefer headless CLI: cursor-agent then agent (correct prompt handling)
    for name in ("cursor-agent", "agent"):
        path_bin = shutil.which(name)
        if path_bin:
            return Path(path_bin), False
    cursor_agent_local = Path(os.path.expanduser("~/.local/bin/cursor-agent"))
    if cursor_agent_local.exists():
        return cursor_agent_local, False

    # Fallback: cursor from PATH (maybe editor CLI)
    path_cursor = shutil.which("cursor")
    if path_cursor:
        return Path(path_cursor), True
    # Last: workspace/agents/cursor (Docker-style script, supports "run")
    fallback = workspace.resolve() / "agents" / "cursor"
    return (fallback, True) if fallback.exists() else (None, True)


def run_local(
    workspace: Path,
    work_dir: Path,
    cursor_bin: Path | None = None,
    source: Path | None = None,
    verbose: bool = True,
    cursor_api_key: str | None = None,
) -> int:
    """
    Read task.json from work_dir, run cursor-cli on the host, write result.json.
    workspace: work path (task.json, works/). source: path for cursor --workspace; default = workspace.
    cursor_api_key: Cursor API key passed via --api-key (avoids keychain issues in parallel execution).
    Returns cursor-cli exit code (0 = success).
    """
    work_dir = work_dir.resolve()
    workspace = workspace.resolve()
    src = (source.resolve() if source else workspace)
    work_dir.mkdir(parents=True, exist_ok=True)

    resolved, use_run = _resolve_cursor_bin(cursor_bin, workspace)
    if resolved is None:
        _log("cursor cli not found (try: cursor agent, or set CURSOR_CLI_PATH, or workspace/agents/cursor)", verbose)
        _log("hint: for --local use headless CLI: cursor agent then use cursor-agent or agent from PATH", verbose)
        return 1
    cursor_bin = resolved
    cli_name = cursor_bin.name
    _log(f"using {cli_name}: {cursor_bin}", verbose)

    task_file = work_dir / "task.json"
    result_file = work_dir / "result.json"
    log_file = work_dir / "agent.log"

    if not task_file.exists():
        _log(f"task file not found: {task_file}", verbose)
        return 1

    task = json.loads(task_file.read_text())
    objective = task.get("objective", "")
    instructions = task.get("instructions", [])
    entrypoint = task.get("entrypoint", ".")
    mode = task.get("mode", "agent")
    task_type = task.get("type", "")
    if task_type == "bootstrap":
        _log("task type=bootstrap (no core modification, generated marker, no autonomous merge)", verbose)
    # Cursor CLI supports plan/ask only; map agent -> plan (execution allowed)
    if mode == "agent":
        mode = "plan"

    prompt = objective
    if instructions:
        prompt += "\n\nConstraints:\n"
        for i in instructions:
            prompt += f"- {i}\n"

    _log(f"workspace (work)={workspace} source={src} work_dir={work_dir}", verbose)
    _log(f"objective: {objective[:80]}..." if len(objective) > 80 else f"objective: {objective}", verbose)
    output_fmt = os.getenv("AGENT_OUTPUT_FORMAT", "stream-json").strip().lower()
    if output_fmt not in ("text", "stream-json"):
        output_fmt = "stream-json"

    if cursor_api_key:
        _log(f"CURSOR_API_KEY provided (length={len(cursor_api_key)})", verbose)
    else:
        _log("WARNING: CURSOR_API_KEY not provided", verbose)

    # cursor-agent/agent: no "run" subcommand; cursor (editor/workspace script): use "run"
    cmd = [str(cursor_bin)]
    if use_run:
        cmd.append("run")
    if cursor_api_key:
        cmd.extend(["--api-key", cursor_api_key])
    cmd.extend([
        "-p",
        "-f",
        "--output-format", output_fmt,
        "--mode", mode,
        "--workspace", str(src / entrypoint),
        prompt,
    ])
    _log(f"cmd: {' '.join(cmd[:10])}...", verbose)

    proc = None

    def stream_output():
        with log_file.open("w") as lf:
            for line in proc.stdout:
                line_ = line if line.endswith("\n") else line + "\n"
                lf.write(line_)
                lf.flush()
                try:
                    obj = json.loads(line.strip())
                    if obj.get("type") == "result":
                        out = obj.get("result")
                        if out is not None:
                            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                            sys.stdout.write(text if text.endswith("\n") else text + "\n")
                            sys.stdout.flush()
                except (json.JSONDecodeError, TypeError):
                    pass

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(src),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        reader = threading.Thread(target=stream_output, daemon=True)
        reader.start()
        proc.wait(timeout=CURSOR_CLI_TIMEOUT)
    except KeyboardInterrupt:
        _log("interrupted (Ctrl+C), terminating cursor cli...", verbose)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return 130
    except subprocess.TimeoutExpired:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()
        _log("cursor cli timeout", verbose)
        return 124
    except Exception as e:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        _log(f"cursor cli exception: {e}", verbose)
        if log_file.exists():
            _log(f"cursor cli output saved to {log_file}", verbose)
            if os.getenv("AGENT_LOG") == "1":
                sys.stdout.write(log_file.read_text())
                sys.stdout.flush()
        return 1

    code = proc.returncode
    _log(f"cursor exit code: {code}", verbose)

    if code != 0 and log_file.exists():
        _log(f"cursor cli output saved to {log_file}", verbose)
        log_content = log_file.read_text()
        if "Password not found for account" in log_content and "cursor-access-token" in log_content:
            _log("ERROR: Cursor keychain authentication failed", verbose)
            _log("hint: Set CURSOR_API_KEY environment variable to avoid keychain issues", verbose)
            _log("hint: export CURSOR_API_KEY='your-api-key' or add to .env file", verbose)
        if os.getenv("AGENT_LOG") == "1":
            sys.stdout.write(log_content)
            sys.stdout.flush()

    # token_usage: 当前 Cursor CLI stream-json 未暴露用量，预留字段；日后可填 {"input": n, "output": n} 等
    # generated: DESIGN §4.5 — agent 产物必须标记 generated
    result = {
        "agent_id": "local",
        "plan_id": "local",
        "status": "success" if code == 0 else "failed",
        "code": code,
        "token_usage": None,
        "generated": True,
    }
    result_file.write_text(json.dumps(result, indent=2))
    return code
