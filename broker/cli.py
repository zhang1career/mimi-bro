import typer
from broker.task import load_task
from broker.planner import plan_task
from broker.decision.propose import propose
from broker.agent.runner import run_agents

app = typer.Typer()


@app.command()
def submit(task_file: str):
    task = load_task(task_file)
    dag = plan_task(task)

    plans = propose(dag)

    typer.echo("Proposed execution plan:")
    for i, p in enumerate(plans):
        typer.echo(f"[{i}] {p['summary']}")

    choice = typer.prompt("Select plan", type=int)
    selected = plans[choice]

    typer.echo(f"Selected plan: {selected['summary']}")
    run_agents(selected["agents"])


@app.command()
def status():
    typer.echo("Status: v0.1 does not persist runtime state yet.")


@app.command()
def stop():
    typer.echo("Stop: not implemented in v0.1")


if __name__ == "__main__":
    app()

