from broker.agent.docker import run_container


def run_agents(agents: list[dict]):
    for agent in agents:
        run_container(agent["id"], agent["role"])

