import json
from pathlib import Path

LOG = Path("logs/decisions.jsonl")


def record(decision: dict):
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(decision) + "\n")
