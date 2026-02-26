"""
Hard rules for the decision plane (DESIGN 4.3):
- Forbidden paths: node ids that must not appear in a plan.
- Max parallel: cap on agents per batch (0 = unlimited).
"""
from __future__ import annotations

import os

# Defaults; can be overridden by env or future config.
DEFAULT_FORBIDDEN_NODE_IDS: list[str] = []
DEFAULT_MAX_PARALLEL = 0  # 0 = no cap


def get_rules() -> dict:
    """
    Return current rules: forbidden_node_ids, max_parallel.
    Env: BROKER_FORBIDDEN_NODES (comma-separated ids), BROKER_MAX_PARALLEL (int).
    """
    forbidden = os.environ.get("BROKER_FORBIDDEN_NODES", "")
    forbidden_node_ids = [x.strip() for x in forbidden.split(",") if x.strip()]
    max_parallel_str = os.environ.get("BROKER_MAX_PARALLEL", "")
    try:
        max_parallel = int(max_parallel_str) if max_parallel_str else DEFAULT_MAX_PARALLEL
    except ValueError:
        max_parallel = DEFAULT_MAX_PARALLEL
    return {
        "forbidden_node_ids": forbidden_node_ids or DEFAULT_FORBIDDEN_NODE_IDS,
        "max_parallel": max_parallel if max_parallel > 0 else DEFAULT_MAX_PARALLEL,
    }


def apply_rules(
        agents: list[dict],
        batches: list[list[str]],
        rules: dict | None = None,
) -> tuple[list[dict], list[list[str]]]:
    """
    Apply hard rules to a single plan: drop forbidden nodes, cap batch size by max_parallel.
    Returns (filtered_agents, filtered_batches).
    """
    rules = rules or get_rules()
    forbidden = set(rules.get("forbidden_node_ids") or [])
    max_parallel = rules.get("max_parallel") or 0

    # Drop forbidden nodes from agents and batches
    if forbidden:
        agents = [a for a in agents if a.get("id") not in forbidden]
        batches = [[n for n in batch if n not in forbidden] for batch in batches]
        batches = [b for b in batches if b]

    # Cap batch size: split any batch with len > max_parallel into chunks
    if max_parallel > 0:
        new_batches: list[list[str]] = []
        for batch in batches:
            for i in range(0, len(batch), max_parallel):
                new_batches.append(batch[i: i + max_parallel])
        batches = new_batches

    return agents, batches
