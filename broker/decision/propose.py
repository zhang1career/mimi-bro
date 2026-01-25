def propose(dag):
    agents = []
    for node, data in dag.nodes(data=True):
        agents.append({
            "id": node,
            "role": data.get("role", "generic")
        })

    return [{
        "summary": f"{len(agents)} agents in parallel",
        "agents": agents
    }]

