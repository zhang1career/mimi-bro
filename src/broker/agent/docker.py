from __future__ import annotations

import json
import os
import threading

import docker

from broker.container.manager import get_host_mount_from_docker
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


_client = None


def _get_docker_client():
    """Lazy initialization of docker client."""
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _load_dotenv_from_project_root() -> None:
    """Load .env from project root so CURSOR_API_KEY etc. are available when not set in shell."""
    from broker.utils.env_util import load_dotenv_from_dir
    load_dotenv_from_dir(PROJECT_ROOT)
    # When inside bro-subtask: PROJECT_ROOT=/workspace; load docker/.env from /source (worktree has repo)
    docker_env = Path("/source/docker/.env") if Path("/source").exists() else PROJECT_ROOT / "docker" / ".env"
    if docker_env.exists():
        load_dotenv_from_dir(docker_env.parent)


def run_container(
    agent_id: str,
    plan_id: str,
    task_id="demo",
    workspace=PROJECT_ROOT,
    work_dir_rel: str | None = None,
    source=None,
):
    """
    workspace: work path (task.json, agent.log, works/) -> mounted at /workspace.
    source: source path (source code, scripts) for agent to operate on; default = workspace.
    When source != workspace, mount source at /source and set SOURCE=/source; else SOURCE=/workspace.
    """
    src = source if source is not None else workspace
    workspace = workspace.resolve() if hasattr(workspace, "resolve") else Path(workspace).resolve()
    src = src.resolve() if hasattr(src, "resolve") else Path(src).resolve()

    print(f"[docker] starting agent {agent_id} ({plan_id})", flush=True)

    container_name = f"agent-{agent_id}"

    # Remove existing container if it exists
    try:
        existing = _get_docker_client().containers.get(container_name)
        existing.remove(force=True)
    except docker.errors.NotFound:
        pass

    try:
        # Prepare environment variables: WORKSPACE = work root, SOURCE = source root for cursor --workspace
        env_vars = {
            "AGENT_ID": agent_id,
            "AGENT_PLAN_ID": plan_id,
            "TASK_ID": task_id,
            "WORKSPACE": "/workspace",
            "SOURCE": "/source" if src != workspace else "/workspace",
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

        # Resolve host paths when running inside a container (workspace=/workspace)
        ws_str, src_str = str(workspace), str(src)
        mount_workspace = None
        mount_source = None
        if ws_str == "/workspace" or ws_str.startswith("/workspace/"):
            mount_workspace = get_host_mount_from_docker("/workspace")
        if src_str == "/source" or src_str.startswith("/source/"):
            mount_source = get_host_mount_from_docker("/source")
        if not mount_source and src == workspace and mount_workspace:
            mount_source = mount_workspace
        mount_workspace = mount_workspace or ws_str
        mount_source = mount_source or src_str

        if mount_workspace == "/workspace":
            raise RuntimeError(
                "Cannot mount /workspace: broker is running inside a container but host path is unknown. "
                "Ensure Docker socket is available and /workspace is a bind mount so docker inspect can resolve it."
            )
        print(f"[docker] workspace (host): {mount_workspace} -> /workspace", flush=True)
        if src != workspace:
            print(f"[docker] source (host): {mount_source} -> /source", flush=True)

        volumes = {mount_workspace: {"bind": "/workspace", "mode": "rw"}}
        if src != workspace:
            # Agent must create/modify files in source (e.g. worktree); use rw
            volumes[mount_source] = {"bind": "/source", "mode": "rw"}

        # shm_size: /dev/shm default 64MB can cause issues with cursor-cli/Node. Use 1g.
        run_kw = {
            "image": "cursor-agent:latest",
            "name": container_name,
            "environment": env_vars,
            "volumes": volumes,
            "command": ["python", "/src/agent.py"],
            "detach": True,
            "shm_size": "1g",
            "healthcheck": {"test": ["NONE"]},  # disable inherited healthcheck
        }

        print("[docker] starting container (image cursor-agent:latest, may pull if missing)...", flush=True)
        container = _get_docker_client().containers.run(**run_kw)
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
        logs = None
        if exit_code != 0:
            try:
                logs = container.logs(stdout=True, stderr=True, tail=500)
                if logs:
                    print("[docker] container logs (tail):")
                    print(logs.decode("utf-8", errors="replace"))
            except Exception as e:
                if "409" not in str(e) and "dead or marked for removal" not in str(e).lower():
                    print(f"[docker] could not get logs: {e}")

        # Do not remove container here. Cleanup is deferred to atexit (cleanup_subtask_containers)
        # when BROKER_AUTO_CLEANUP_CONTAINER=1, so TUI can still read logs while running.

        if exit_code != 0:
            # Use actual entrypoint as command so error message is clear (not 'None')
            cmd_str = "python /src/agent.py"
            stderr_text = logs.decode('utf-8', errors='replace') if logs else ''
            # Hint for 128+signal exit codes (e.g. 137 = 128+9 = SIGKILL, often OOM)
            if 128 <= exit_code <= 159:
                sig = exit_code - 128
                hint = f"\n[docker] hint: exit code {exit_code} = 128+{sig} (process may have received signal {sig}, e.g. killed/stopped)."
                if exit_code == 137:
                    hint += " Exit 137 = SIGKILL. Common causes: OOM (check docker inspect ID --format '{{.State.OOMKilled}}'); /dev/shm too small (we set shm_size=1g); Docker healthcheck; host VM limits."
                stderr_text = stderr_text + hint
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
        # Try to get logs if container still exists (skip if 404/NotFound)
        try:
            container = _get_docker_client().containers.get(container_name)
            if container.status not in ("dead", "removal", "removing"):
                logs = container.logs(stdout=True, stderr=True, tail=100)
                if logs:
                    print("[docker] container logs:")
                    print(logs.decode("utf-8", errors="replace"))
            try:
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
            except Exception:
                pass
        except docker.errors.NotFound:
            pass
        except Exception:
            pass
        raise

