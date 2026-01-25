import docker

client = docker.from_env()


def run_container(name: str, role: str):
    print(f"[docker] starting agent {name} ({role})")

    container = client.containers.run(
        image="cursor-agent:latest",
        name=f"agent-{name}",
        command=["echo", f"hello from {name} ({role})"],
        detach=True,
        remove=True
    )

    return container

