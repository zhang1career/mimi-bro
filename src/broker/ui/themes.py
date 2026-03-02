"""
Theme system for bro submit TUI.
Provides multiple color themes inspired by agent-of-empires.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Theme:
    """Color theme for the TUI."""

    name: str

    # Background and borders
    background: str
    border: str
    selection: str

    # Text colors
    title: str
    text: str
    dimmed: str

    # Status colors
    running: str
    waiting: str
    idle: str
    error: str
    accent: str

    # Task colors (for distinguishing parallel running tasks)
    task_colors: list[str] = field(default_factory=list)


AVAILABLE_THEMES = ["phosphor", "tokyo-night-storm", "catppuccin-latte"]
DEFAULT_THEME = "catppuccin-latte"


def _phosphor() -> Theme:
    """Green terminal aesthetic - dark background with neon green."""
    return Theme(
        name="phosphor",
        background="#101412",
        border="#2d4637",
        selection="#1e3228",
        title="#39ff14",
        text="#b4ffb4",
        dimmed="#50785a",
        running="#00ffb4",
        waiting="#ffb43c",
        idle="#3c6446",
        error="#ff6450",
        accent="#39ff14",
        task_colors=[
            "#ff6450",  # red-ish
            "#ffb43c",  # orange
            "#ffff00",  # yellow
            "#00ffb4",  # cyan-green
            "#82aaff",  # blue
            "#c87aff",  # magenta
        ],
    )


def _tokyo_night_storm() -> Theme:
    """Blue-purple night theme."""
    return Theme(
        name="tokyo-night-storm",
        background="#24283b",
        border="#414868",
        selection="#364a82",
        title="#7aa2f7",
        text="#c0caf5",
        dimmed="#565f89",
        running="#9ece6a",
        waiting="#e0af68",
        idle="#565f89",
        error="#f7768e",
        accent="#7aa2f7",
        task_colors=[
            "#f7768e",  # red
            "#e0af68",  # orange
            "#e0de6a",  # yellow
            "#9ece6a",  # green
            "#7dcfff",  # cyan
            "#bb9af7",  # purple
        ],
    )


def _catppuccin_latte() -> Theme:
    """Light, eye-friendly theme."""
    return Theme(
        name="catppuccin-latte",
        background="#eff1f5",
        border="#bcc0cc",
        selection="#dce0e8",
        title="#1e66f5",
        text="#4c4f69",
        dimmed="#acb0be",
        running="#40a02b",
        waiting="#df8e1d",
        idle="#9ca0b0",
        error="#d20f39",
        accent="#fe640b",
        task_colors=[
            "#d20f39",  # red
            "#fe640b",  # peach/orange
            "#df8e1d",  # yellow
            "#40a02b",  # green
            "#04a5e5",  # sky/cyan
            "#8839ef",  # mauve/purple
        ],
    )


_THEMES: dict[str, Theme] = {}


def _init_themes() -> None:
    global _THEMES
    if not _THEMES:
        _THEMES = {
            "phosphor": _phosphor(),
            "tokyo-night-storm": _tokyo_night_storm(),
            "catppuccin-latte": _catppuccin_latte(),
        }


def get_theme(name: str) -> Theme:
    """Get a theme by name. Falls back to default if not found."""
    _init_themes()
    if name not in _THEMES:
        return _THEMES[DEFAULT_THEME]
    return _THEMES[name]


def list_themes() -> list[str]:
    """Return list of available theme names."""
    return AVAILABLE_THEMES.copy()
