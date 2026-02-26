import networkx as nx


def plan_task(task: dict) -> nx.DiGraph:
    """
    Build a DAG from task["plans"]. Each node may have optional "deps": list of
    plan node ids that must complete before this node. Edges go from dep -> node.
    Raises ValueError if deps reference missing nodes or form a cycle.
    """
    dag = nx.DiGraph()
    plan = task.get("plans", [])
    node_ids = {item["id"] for item in plan}

    for item in plan:
        nid = item["id"]
        dag.add_node(
            nid,
            role=item.get("role", "generic"),
            mode=item.get("mode", "agent"),
            objective=item.get("objective"),
        )

    for item in plan:
        nid = item["id"]
        for dep in item.get("deps", []):
            if dep not in node_ids:
                raise ValueError(f"Plan node '{nid}' dep '{dep}' not in plan")
            dag.add_edge(dep, nid)

    if not nx.is_directed_acyclic_graph(dag):
        raise ValueError("Plan dependencies form a cycle")

    return dag
