import networkx as nx

from broker.decision.rules import apply_rules, get_rules
from broker.decision.scoring import score as score_plan


def propose(dag):
    """
    Decision plane: emit 1–2 execution plans with parallel-ready order (DESIGN 4.3).
    Applies Rules (forbidden paths, max_parallel); attaches Scores (placeholder).
    Returns {rules, plans} so callers can expose rules and use score-gap for auto-selection (4.4).
    """
    try:
        generations = list(nx.topological_generations(dag))
    except nx.NetworkXError:
        generations = [list(dag.nodes())]

    batches_parallel = [list(gen) for gen in generations]
    agents_list = []
    for batch in batches_parallel:
        for node in batch:
            data = dag.nodes[node]
            agent = {
                "id": node,
                "role": data.get("role", "generic"),
                "mode": data.get("mode", "agent"),
                "objective": data.get("objective"),
            }
            if data.get("exec_type"):
                agent["exec_type"] = data["exec_type"]
            if data.get("skill"):
                agent["skill"] = data["skill"]
            if data.get("requirement"):
                agent["requirement"] = data["requirement"]
            if data.get("scope"):
                agent["scope"] = data["scope"]
            if data.get("params"):
                agent["params"] = data["params"]
            deps = list(dag.predecessors(node))
            if deps:
                agent["deps"] = deps
            agents_list.append(agent)

    rules = get_rules()
    agents, batches = apply_rules(agents_list, batches_parallel, rules)

    if not agents:
        return {"rules": rules, "plans": []}

    n = len(agents)
    plans = []

    # Default: serial (one agent per batch, sequential)
    batches_serial = [[a["id"]] for a in agents]
    summary_serial = f"{n} agents in {n} batch(es) (serial)"
    plan_serial = {
        "summary": summary_serial,
        "agents": agents,
        "batches": batches_serial,
        "style": "serial",
    }
    plan_serial["score"] = score_plan(plan_serial, "serial")
    plans.append(plan_serial)

    # Alternative: parallel (batched by topological generation) when 2+ agents
    if n >= 2:
        summary_parallel = f"{n} agents in {len(batches)} batch(es) (parallel-ready)"
        plan_parallel = {
            "summary": summary_parallel,
            "agents": agents,
            "batches": batches,
            "style": "parallel",
        }
        plan_parallel["score"] = score_plan(plan_parallel, "parallel")
        plans.append(plan_parallel)

    return {"rules": rules, "plans": plans}
