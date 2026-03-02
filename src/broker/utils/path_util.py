import os
from pathlib import Path

BRO_PROJECT_ROOT_ENV = "BRO_PROJECT_ROOT"

def _get_project_root() -> Path:
    """Get project root from env var or cwd.
    
    In worktree subtasks, BRO_PROJECT_ROOT is set to the main repo path.
    This ensures state/logs are written to the main repo, not the worktree.
    """
    env_root = os.environ.get(BRO_PROJECT_ROOT_ENV)
    if env_root:
        return Path(env_root)
    return Path.cwd()

PROJECT_ROOT = _get_project_root()
