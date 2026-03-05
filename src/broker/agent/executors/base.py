"""Protocol for agent execution (serial run, multi-step run)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

if __name__ == "__main__":
    from broker.ui.driver import DisplayDriver
else:
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from broker.ui.driver import DisplayDriver


class AgentExecutor(Protocol):
    """Interface for running agents: serial list or multi-step per agent."""

    def run_serial_agents(
            self,
            run_list: list[dict],
            workspace: Path,
            task: dict,
            worker_id: str,
            run_id: str,
            audit_context: str,
            source: Path,
            drv: "DisplayDriver",
    ) -> None:
        """Run agents in order (one round each). Raises on failure."""
        ...

    def run_agent_steps(
            self,
            agent: dict,
            steps: list[dict],
            workspace: Path,
            task: dict,
            source: Path,
            worker_id: str,
            run_id: str,
            auto: bool,
            verbose: bool,
            drv: "DisplayDriver",
            run_sub_task_fn: Callable[..., int],
    ) -> Path:
        """Run one agent through its steps. Returns work_dir for this agent."""
        ...
