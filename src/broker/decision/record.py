import json
from pathlib import Path

from broker.utils.file_lock import locked_append
from broker.utils.path_util import PROJECT_ROOT

LOG = PROJECT_ROOT / "logs" / "decisions.jsonl"


def record(decision: dict):
    locked_append(LOG, json.dumps(decision))
