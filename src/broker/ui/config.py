"""
Configuration loading for bro TUI.
Reads settings from .bro/config.toml in the workspace.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore

from .themes import DEFAULT_THEME


def load_config(workspace: Path | None = None) -> dict[str, Any]:
    """Load configuration from .bro/config.toml.

    Searches in order:
    1. workspace/.bro/config.toml (if workspace provided)
    2. cwd/.bro/config.toml (current working directory)
    """
    if tomllib is None:
        return {}

    candidates = []
    if workspace:
        candidates.append(workspace / ".bro" / "config.toml")
    candidates.append(Path.cwd() / ".bro" / "config.toml")

    for config_path in candidates:
        if config_path.exists():
            try:
                with config_path.open("rb") as f:
                    return tomllib.load(f)
            except Exception:
                continue
    return {}


def get_theme_name(workspace: Path, cli_override: str | None = None) -> str:
    """
    Get the theme name to use.

    Priority:
    1. CLI override (--theme)
    2. Config file (.bro/config.toml -> ui.theme)
    3. Default theme
    """
    if cli_override:
        return cli_override
    config = load_config(workspace)
    return config.get("ui", {}).get("theme", DEFAULT_THEME)
