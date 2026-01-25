import yaml
from pathlib import Path


def load_task(path: str) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data

