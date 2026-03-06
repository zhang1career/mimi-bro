import networkx as nx

from broker.model.plan_item import get_plan_item_type


def plan_task(task: dict) -> nx.DiGraph:
    """
    Build a DAG from task["plans"]. Each node may have optional "deps": list of
    plan node ids that must complete before this node. Edges go from dep -> node.

    Supports unified schema with two execution types:
    - skill: call skill invocation (worker is a type of skill with invocation.type="bro_submit")
    - inline: directly execute with mode + objective

    Raises ValueError if deps reference missing nodes or form a cycle.
    """
    dag = nx.DiGraph()
    plan = task.get("plans", [])
    node_ids = {item["id"] for item in plan}

    for item in plan:
        nid = item["id"]
        exec_type = get_plan_item_type(item)
        dag.add_node(
            nid,
            exec_type=exec_type.value,
            mode=item.get("mode", "agent"),
            objective=item.get("objective"),
            skill=item.get("skill", ""),
            requirement=item.get("requirement", ""),
            scope=item.get("scope", ""),
            params=dict(item.get("params") or {}),
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
