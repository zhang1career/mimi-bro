"""
Container manager for subtask execution in Docker.

Provides:
- Container creation with proper volume mounting
- Container lifecycle management (stop, restart, remove)
- Status queries and listing
- Log streaming
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator

import docker
from docker.errors import NotFound

from broker.utils.env_util import load_dotenv_from_dir
from broker.utils.path_util import PROJECT_ROOT
from broker.utils.work_util import build_work_dir, task_slug


class ContainerStatus(str, Enum):
    """Container status."""
    PENDING = "pending"
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    REMOVED = "removed"


@dataclass
class SubtaskContainer:
    """Subtask container state."""
    run_id: str
    plan_id: str
    container_name: str
    container_id: str | None = None
    status: ContainerStatus = ContainerStatus.PENDING
    exit_code: int | None = None
    error_message: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    work_dir: Path | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "work_dir": str(self.work_dir) if self.work_dir else None,
        }


_client = None


def get_docker_client():
    """Lazy initialization of docker client."""
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _load_env_vars() -> dict[str, str]:
    """Load environment variables for container (from project root .env only)."""
    load_dotenv_from_dir(PROJECT_ROOT)
    env_vars = {}
    cursor_api_key = os.getenv("CURSOR_API_KEY")
    if cursor_api_key:
        env_vars["CURSOR_API_KEY"] = cursor_api_key
    return env_vars


def _resolve_docker_socket() -> str | None:
    """
    Resolve host path to Docker socket for mounting into containers.
    Use /var/run/docker.sock without resolving symlinks - on macOS Docker Desktop,
    resolve() yields ~/.docker/run/docker.sock which fails bind mount with "operation not supported".
    """
    path = Path("/var/run/docker.sock")
    if path.exists():
        return "/var/run/docker.sock"
    return None


def _get_self_container_id() -> str | None:
    """
    Get current container ID/name when running inside Docker.
    Tries: hostname (docker-compose uses service name; plain docker uses short ID),
    /proc/self/cgroup for container ID.
    """
    try:
        import socket
        hostname = socket.gethostname()
        if hostname:
            # Plain docker: hostname is 12-char hex. docker-compose: hostname is service name.
            if len(hostname) == 12 and hostname.isalnum():
                return hostname
            return hostname  # Use as container name (e.g. cursor-agent)
    except Exception:
        pass
    try:
        cgroup_path = Path("/proc/self/cgroup")
        if cgroup_path.exists():
            for line in cgroup_path.read_text().splitlines():
                if "docker" in line:
                    parts = line.split("docker/")
                    if len(parts) >= 2:
                        cid = parts[-1].strip().split(".")[0].split("\n")[0]
                        if len(cid) >= 12:
                            return cid[:12]
    except Exception:
        pass
    return None


def get_host_mount_from_docker(container_path: str) -> str | None:
    """
    When running inside Docker, get the host path for a container mount point
    (e.g. /workspace) via docker inspect. Requires Docker socket access.
    """
    if not container_path or not container_path.startswith("/"):
        return None
    container_path = container_path.rstrip("/") or "/"
    try:
        cid = _get_self_container_id()
        if not cid:
            return None
        client = get_docker_client()
        container = client.containers.get(cid)
        insp = container.attrs
        for m in insp.get("Mounts") or []:
            dest = (m.get("Destination") or "").rstrip("/") or "/"
            if dest == container_path and m.get("Type") == "bind":
                src = m.get("Source")
                if src:
                    return src
        return None
    except Exception:
        return None


class ContainerManager:
    """
    Manager for subtask Docker containers.
    
    Handles container lifecycle:
    - Create containers with proper volume mounting
    - Track container status
    - Stop/restart/remove containers
    - Stream logs
    """

    CONTAINER_PREFIX = "bro-subtask"
    DEFAULT_IMAGE = "cursor-agent:latest"

    def __init__(
            self,
            workspace: Path,
            source: Path | None = None,
            image: str | None = None,
            on_status_change: Callable[[SubtaskContainer], None] | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.source = Path(source).resolve() if source else self.workspace
        self.image = image or self.DEFAULT_IMAGE
        self.on_status_change = on_status_change
        self._containers: dict[str, SubtaskContainer] = {}
        self._lock = threading.Lock()

    def _generate_container_name(self, run_id: str, plan_id: str) -> str:
        """Generate unique container name: {prefix}-{task_slug}."""
        slug = task_slug(run_id, plan_id, truncate_run_id=8, max_plan_id_len=20)
        return f"{self.CONTAINER_PREFIX}-{slug}"

    def _notify_status_change(self, container: SubtaskContainer) -> None:
        """Notify status change callback."""
        if self.on_status_change:
            try:
                self.on_status_change(container)
            except Exception:
                pass

    def create_subtask_container(
            self,
            run_id: str,
            plan_id: str,
            command: list[str] | str,
            parent_run_id: str | None = None,
            extra_env: dict[str, str] | None = None,
    ) -> SubtaskContainer:
        """
        Create and start a container for a subtask.
        
        Volume mounting:
        - {workspace}/works/{run_id}/{plan_id} -> /workspace/work (subtask work dir)
        - {source} -> /source (rw; agent creates/modifies files)
        - {workspace} -> /workspace (full workspace access)
        
        Args:
            run_id: Subtask run ID
            plan_id: Subtask plan ID (parent context id)
            command: Command to execute in container
            parent_run_id: Parent task's run ID (for tracking)
            extra_env: Additional environment variables
            
        Returns:
            SubtaskContainer with container info
            
        Raises:
            docker.errors.APIError: If container creation fails
        """
        container_name = self._generate_container_name(run_id, plan_id)
        work_dir = build_work_dir(self.workspace, run_id, plan_id)

        container_info = SubtaskContainer(
            run_id=run_id,
            plan_id=plan_id,
            container_name=container_name,
            status=ContainerStatus.CREATING,
            created_at=datetime.now(),
            work_dir=work_dir,
        )

        with self._lock:
            self._containers[container_name] = container_info
        self._notify_status_change(container_info)

        try:
            client = get_docker_client()

            # Remove existing container if it exists
            try:
                existing = client.containers.get(container_name)
                existing.remove(force=True)
            except NotFound:
                pass

            # Prepare environment variables
            env_vars = _load_env_vars()
            env_vars.update({
                "RUN_ID": run_id,
                "PLAN_ID": plan_id,
                "WORKSPACE": "/workspace",
                "SOURCE": "/source" if self.source != self.workspace else "/workspace",
                "WORK_DIR": "/workspace/work",
            })
            if parent_run_id:
                env_vars["PARENT_RUN_ID"] = parent_run_id
            if extra_env:
                env_vars.update(extra_env)

            # Ensure work directory exists
            work_dir.mkdir(parents=True, exist_ok=True)

            # Host paths for volume mounts (nested broker sees workspace=/workspace; need actual host path)
            ws_str = str(self.workspace)
            src_str = str(self.source)
            host_workspace = None
            host_source = None
            if ws_str == "/workspace" or ws_str.startswith("/workspace/"):
                host_workspace = get_host_mount_from_docker("/workspace")
            if src_str == "/source" or src_str.startswith("/source/"):
                host_source = get_host_mount_from_docker("/source")
            if not host_source and self.source == self.workspace and host_workspace:
                host_source = host_workspace
            mount_workspace = host_workspace if host_workspace else ws_str
            mount_source = host_source if host_source else src_str
            if mount_workspace == "/workspace" or mount_workspace.startswith("/workspace/"):
                raise RuntimeError(
                    "Cannot create nested container: workspace /workspace is a container path but host path is unknown. "
                    "Ensure Docker socket is available and /workspace is a bind mount so docker inspect can resolve it."
                )
            mount_work_dir = build_work_dir(Path(mount_workspace), run_id, plan_id)

            # Prepare volume mounts (host paths must be on host filesystem)
            volumes = {
                mount_workspace: {"bind": "/workspace", "mode": "rw"},
                str(mount_work_dir): {"bind": "/workspace/work", "mode": "rw"},
            }
            if self.source != self.workspace:
                # Agent must create/modify files in source (e.g. worktree); use rw
                volumes[mount_source] = {"bind": "/source", "mode": "rw"}

            # Mount Docker socket so broker inside container can spawn nested sub-task containers
            _docker_socket = _resolve_docker_socket()
            if _docker_socket:
                volumes[_docker_socket] = {"bind": "/var/run/docker.sock", "mode": "rw"}

            # Convert command to list if string
            if isinstance(command, str):
                cmd = ["sh", "-c", command]
            else:
                cmd = command

            # mem_limit: bro-subtask (serial path only) runs broker (Python + docker client)
            subtask_mem = "512m"
            run_kw = {
                "image": self.image,
                "name": container_name,
                "environment": env_vars,
                "volumes": volumes,
                "command": cmd,
                "working_dir": "/workspace/work",
                "detach": True,
                "mem_limit": subtask_mem,
            }
            container = client.containers.run(**run_kw)

            container_info.container_id = container.id
            container_info.status = ContainerStatus.RUNNING
            container_info.started_at = datetime.now()

            with self._lock:
                self._containers[container_name] = container_info
            self._notify_status_change(container_info)

            return container_info

        except Exception as e:
            container_info.status = ContainerStatus.FAILED
            container_info.error_message = str(e)
            container_info.finished_at = datetime.now()

            with self._lock:
                self._containers[container_name] = container_info
            self._notify_status_change(container_info)

            raise

    def wait_for_container(
            self,
            container_name: str,
            timeout: int | None = None,
            on_log: Callable[[str], None] | None = None,
    ) -> int:
        """
        Wait for container to finish and return exit code.
        
        Args:
            container_name: Container name
            timeout: Timeout in seconds (None for no timeout)
            on_log: Callback for log lines
            
        Returns:
            Exit code
        """
        client = get_docker_client()

        try:
            container = client.containers.get(container_name)
        except NotFound:
            return -1

        # Start log streaming in background if callback provided
        log_thread = None
        if on_log:
            def stream_logs():
                try:
                    for line in container.logs(stream=True, follow=True):
                        if line:
                            on_log(line.decode("utf-8", errors="replace"))
                except Exception:
                    pass

            log_thread = threading.Thread(target=stream_logs, daemon=True)
            log_thread.start()

        # Wait for container
        try:
            result = container.wait(timeout=timeout)
            exit_code = result.get("StatusCode", -1)
        except Exception as e:
            exit_code = -1

        if log_thread:
            log_thread.join(timeout=2.0)

        # Update container info; capture detailed error when failed
        with self._lock:
            if container_name in self._containers:
                info = self._containers[container_name]
                info.exit_code = exit_code
                info.finished_at = datetime.now()
                info.status = ContainerStatus.STOPPED if exit_code == 0 else ContainerStatus.FAILED
                if exit_code != 0 and not info.error_message:
                    try:
                        logs = container.logs(stdout=True, stderr=True, tail=500)
                        if logs:
                            info.error_message = f"Exit code: {exit_code}\n\n--- container output ---\n{logs.decode('utf-8', errors='replace')}"
                        else:
                            info.error_message = f"Exit code: {exit_code}"
                    except Exception:
                        info.error_message = f"Exit code: {exit_code}"
                    if exit_code == 137:
                        oom = ""
                        try:
                            state = container.attrs.get("State", {})
                            oom_val = state.get("OOMKilled", None)
                            oom = f" OOMKilled={oom_val}" if oom_val is not None else ""
                        except Exception:
                            pass
                        info.error_message += f"\n[bro-subtask exit 137 = SIGKILL{oom}]"
                self._notify_status_change(info)

        # Do NOT remove container here - defer to cleanup_all() when bro submit completes.
        # Immediate removal causes 409 when TUI/logs fetch tries to read from removed container.

        return exit_code

    def run_subtask(
            self,
            run_id: str,
            plan_id: str,
            command: list[str] | str,
            parent_run_id: str | None = None,
            extra_env: dict[str, str] | None = None,
            timeout: int | None = None,
            on_log: Callable[[str], None] | None = None,
    ) -> tuple[SubtaskContainer, int]:
        """
        Create container, run command, and wait for completion.
        
        Combines create_subtask_container and wait_for_container.
        
        Returns:
            Tuple of (container_info, exit_code)
        """
        container_info = self.create_subtask_container(
            run_id=run_id,
            plan_id=plan_id,
            command=command,
            parent_run_id=parent_run_id,
            extra_env=extra_env,
        )

        exit_code = self.wait_for_container(
            container_info.container_name,
            timeout=timeout,
            on_log=on_log,
        )

        return container_info, exit_code

    def stop_container(self, container_name: str, timeout: int = 10) -> bool:
        """Stop a running container."""
        client = get_docker_client()

        try:
            container = client.containers.get(container_name)
            container.stop(timeout=timeout)

            with self._lock:
                if container_name in self._containers:
                    info = self._containers[container_name]
                    info.status = ContainerStatus.STOPPED
                    info.finished_at = datetime.now()
                    self._notify_status_change(info)

            return True
        except NotFound:
            return False
        except Exception:
            return False

    def restart_container(self, container_name: str) -> bool:
        """Restart a stopped container."""
        client = get_docker_client()

        try:
            container = client.containers.get(container_name)
            container.restart()

            with self._lock:
                if container_name in self._containers:
                    info = self._containers[container_name]
                    info.status = ContainerStatus.RUNNING
                    info.started_at = datetime.now()
                    self._notify_status_change(info)

            return True
        except NotFound:
            return False
        except Exception:
            return False

    def remove_container(self, container_name: str, force: bool = False) -> bool:
        """Remove a container. Saves logs to work_dir/container.log before removal for later inspection."""
        client = get_docker_client()

        try:
            container = client.containers.get(container_name)
            work_dir = None
            with self._lock:
                if container_name in self._containers:
                    work_dir = self._containers[container_name].work_dir
            if work_dir:
                try:
                    logs = container.logs(stdout=True, stderr=True, tail=1000)
                    if logs:
                        (Path(work_dir) / "container.log").write_bytes(logs)
                except Exception:
                    pass
            container.remove(force=force)

            with self._lock:
                if container_name in self._containers:
                    info = self._containers[container_name]
                    info.status = ContainerStatus.REMOVED
                    self._notify_status_change(info)

            return True
        except NotFound:
            return True
        except Exception:
            return False

    def get_container_status(self, container_name: str) -> ContainerStatus | None:
        """Get container status from Docker."""
        client = get_docker_client()

        try:
            container = client.containers.get(container_name)
            status = container.status

            if status == "running":
                return ContainerStatus.RUNNING
            elif status == "exited":
                exit_code = container.attrs.get("State", {}).get("ExitCode", 0)
                return ContainerStatus.STOPPED if exit_code == 0 else ContainerStatus.FAILED
            elif status == "created":
                return ContainerStatus.CREATING
            else:
                return ContainerStatus.STOPPED
        except NotFound:
            return None
        except Exception:
            return None

    def get_container_logs(
            self,
            container_name: str,
            tail: int = 100,
            stream: bool = False,
    ) -> str | Iterator[str]:
        """
        Get container logs.
        
        Args:
            container_name: Container name
            tail: Number of lines from end (for non-streaming)
            stream: If True, return iterator for streaming logs
            
        Returns:
            Log string or iterator
        """
        client = get_docker_client()

        try:
            container = client.containers.get(container_name)
            # Container may be dead/removed; logs() can raise 409 Conflict
            if container.status in ("dead", "removal", "removing"):
                return "" if not stream else iter([])
        except NotFound:
            return "" if not stream else iter([])
        except Exception:
            return "" if not stream else iter([])

        try:
            if stream:
                def log_generator():
                    for line in container.logs(stream=True, follow=True):
                        if line:
                            yield line.decode("utf-8", errors="replace")

                return log_generator()
            else:
                logs = container.logs(tail=tail)
                return logs.decode("utf-8", errors="replace")
        except NotFound:
            return "" if not stream else iter([])
        except Exception:
            return "" if not stream else iter([])

    def list_containers(self, include_stopped: bool = True) -> list[SubtaskContainer]:
        """List all tracked containers."""
        with self._lock:
            containers = list(self._containers.values())

        if not include_stopped:
            containers = [c for c in containers if c.status == ContainerStatus.RUNNING]

        return containers

    def list_docker_containers(self, all_containers: bool = False) -> list[dict]:
        """List containers from Docker daemon with bro-subtask prefix."""
        client = get_docker_client()

        try:
            containers = client.containers.list(all=all_containers)
            result = []

            for c in containers:
                if c.name.startswith(self.CONTAINER_PREFIX):
                    result.append({
                        "name": c.name,
                        "id": c.short_id,
                        "status": c.status,
                        "image": c.image.tags[0] if c.image.tags else "unknown",
                    })

            return result
        except Exception:
            return []

    def cleanup_all(self, force: bool = False, success_only: bool = True) -> int:
        """Remove managed containers. Returns count of removed containers.
        success_only: if True, only remove containers that exited with 0 (keep failed ones for inspection).
        """
        removed = 0

        with self._lock:
            to_remove = []
            for name, info in self._containers.items():
                if success_only and info.exit_code is not None and info.exit_code != 0:
                    continue
                to_remove.append(name)

        for name in to_remove:
            if self.remove_container(name, force=force):
                removed += 1

        return removed


def list_visible_containers(all_containers: bool = True) -> list[dict]:
    """List containers from Docker that TUI should display (agent-* and bro-subtask-*)."""
    client = get_docker_client()

    try:
        containers = client.containers.list(all=all_containers)
        result = []

        for c in containers:
            name = c.name
            if not (name.startswith("agent-") or name.startswith(f"{ContainerManager.CONTAINER_PREFIX}-")):
                continue

            exit_code = None
            try:
                state = c.attrs.get("State", {})
                exit_code = state.get("ExitCode")
            except Exception:
                pass

            result.append({
                "name": name,
                "id": c.short_id,
                "status": c.status,
                "image": c.image.tags[0] if c.image.tags else "unknown",
                "exit_code": exit_code,
            })

        return result
    except Exception:
        return []
