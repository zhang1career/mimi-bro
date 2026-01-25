import networkx as nx


def plan_task(task: dict) -> nx.DiGraph:
    dag = nx.DiGraph()

    for item in task.get("plan", []):
        dag.add_node(item["id"], role=item.get("role"))

    return dag

