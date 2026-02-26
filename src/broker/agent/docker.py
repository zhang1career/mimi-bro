from __future__ import annotations

import json
import os
import threading

import docker

from broker.utils.path_util import PROJECT_ROOT
from pathlib import Path


def _filter_result_only(container):
    """Stream container logs; only print the 'result' field of lines where type=='result'."""
    def process_line(line_bytes: bytes) -> None:
        if not line_bytes:
            return
        try:
            obj = json.loads(line_bytes.decode("utf-8", errors="replace"))
            if obj.get("type") == "result":
                out = obj.get("result")
                if out is not None:
                    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                    print(text, end="" if text.endswith("\n") else "\n", flush=True)
        except (json.JSONDecodeError, TypeError):
            pass

    buffer = b""
    for chunk in container.logs(stdout=True, stderr=True, stream=True, follow=True):
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            process_line(line)
    if buffer:
        process_line(buffer)


client = docker.from_env()


def _load_dotenv_from_project_root() -> None:
    """Load .env from project root so CURSOR_API_KEY etc. are available when not set in shell."""
    from broker.utils.env_util import load_dotenv_from_dir
    load_dotenv_from_dir(PROJECT_ROOT)


def run_container(
    agent_id: str,
    role: str,
    task_id="demo",
    workspace=PROJECT_ROOT,
    work_dir_rel: str | None = None,
    resource=None,
):
    """
    workspace: work path (task.json, agent.log, works/) -> mounted at /workspace.
    resource: resource path (source code, scripts) for agent to operate on; default = workspace.
    When resource != workspace, mount resource at /resource and set RESOURCE=/resource; else RESOURCE=/workspace.
    """
    res = resource if resource is not None else workspace
    workspace = workspace.resolve() if hasattr(workspace, "resolve") else Path(workspace).resolve()
    res = res.resolve() if hasattr(res, "resolve") else Path(res).resolve()

    print(f"[docker] starting agent {agent_id} ({role})", flush=True)
    print(f"[docker] workspace (work): {workspace} -> /workspace", flush=True)
    if res != workspace:
        print(f"[docker] resource: {res} -> /resource", flush=True)

    container_name = f"agent-{agent_id}"

    # Remove existing container if it exists
    try:
        existing = client.containers.get(container_name)
        existing.remove(force=True)
    except docker.errors.NotFound:
        pass

    try:
        # Prepare environment variables: WORKSPACE = work root, RESOURCE = resource root for cursor --workspace
        env_vars = {
            "AGENT_ID": agent_id,
            "AGENT_ROLE": role,
            "TASK_ID": task_id,
            "WORKSPACE": "/workspace",
            "RESOURCE": "/resource" if res != workspace else "/workspace",
        }
        if work_dir_rel:
            env_vars["WORK_DIR_REL"] = work_dir_rel

        # Add Cursor API key: from env, or from project root .env
        _load_dotenv_from_project_root()
        cursor_api_key = os.getenv("CURSOR_API_KEY")
        if cursor_api_key:
            env_vars["CURSOR_API_KEY"] = cursor_api_key
            print(f"[docker] Cursor API key configured (from host environment)", flush=True)
        else:
            print(f"[docker] warning: CURSOR_API_KEY not set in host environment", flush=True)
            print(f"[docker] hint: Set CURSOR_API_KEY environment variable to authenticate cursor-agent", flush=True)

        volumes = {str(workspace): {"bind": "/workspace", "mode": "rw"}}
        if res != workspace:
            volumes[str(res)] = {"bind": "/resource", "mode": "ro"}

        print("[docker] starting container (image cursor-agent:latest, may pull if missing)...", flush=True)
        # Run agent entrypoint; image default CMD is sleep infinity for debug
        container = client.containers.run(
            image="cursor-agent:latest",
            name=container_name,
            environment=env_vars,
            volumes=volumes,
            command=["python", "/src/agent.py"],
            detach=True,
        )
        print("[docker] container started, attaching log stream...", flush=True)

        # Stream container stdout/stderr: only print type=="result" -> "result" field; full log in agent.log inside container
        def stream_logs():
            try:
                print("[docker] log stream attached, waiting for container output...", flush=True)
                _filter_result_only(container)
            except Exception as e:
                print(f"[docker] log stream error: {e}", flush=True)

        stream_thread = threading.Thread(target=stream_logs, daemon=True)
        stream_thread.start()

        # Wait for container to finish (sync blocking).
        exit_code = container.wait()["StatusCode"]
        stream_thread.join(timeout=2.0)

        # On failure, print tail of logs again for debugging
        if exit_code != 0:
            logs = container.logs(stdout=True, stderr=True, tail=500)
            if logs:
                print("[docker] container logs (tail):")
                print(logs.decode("utf-8", errors="replace"))
        
        # Remove container
        container.remove()
        
        if exit_code != 0:
            # Use actual entrypoint as command so error message is clear (not 'None')
            cmd_str = "python /src/agent.py"
            stderr_text = logs.decode('utf-8', errors='replace') if logs else ''
            # Hint for 128+signal exit codes (e.g. 148 = 128+20 = SIGTSTP)
            if 128 <= exit_code <= 159:
                sig = exit_code - 128
                stderr_text = stderr_text + f"\n[docker] hint: exit code {exit_code} = 128+{sig} (process may have received signal {sig}, e.g. killed/stopped)."
            raise docker.errors.ContainerError(
                container=container_name,
                exit_status=exit_code,
                command=cmd_str,
                image="cursor-agent:latest",
                stderr=stderr_text
            )
        
        return container
        
    except docker.errors.ContainerError as e:
        print(f"[docker] container error: exit code {e.exit_status}")
        if hasattr(e, 'stderr') and e.stderr:
            print(f"[docker] error output: {e.stderr}")
        raise
    except Exception as e:
        print(f"[docker] unexpected error: {e}")
        # Try to get logs from container if it still exists
        try:
            container = client.containers.get(container_name)
            logs = container.logs(stdout=True, stderr=True, tail=100)
            if logs:
                print("[docker] container logs:")
                print(logs.decode('utf-8', errors='replace'))
            container.remove()
        except:
            pass
        raise

