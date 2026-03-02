"""
Run ID (snowflake-like) for works path isolation.
v0.1: local generation; optional SNOWFLAKE_ID_API_URL for future API.
"""
import os
import random
import time


def gen_run_id() -> str:
    """
    Return a unique run id for this execution (e.g. works/{run_id}/{role}).
    If SNOWFLAKE_ID_API_URL is set, call that API; otherwise generate locally (timestamp + random).
    """
    api_url = os.getenv("SNOWFLAKE_ID_API_URL", "").strip()
    if not api_url:
        # v0.1 local: timestamp (ms) + random 4 digits
        return f"{int(time.time() * 1000)}{random.randint(1000, 9999)}"

    try:
        import urllib.request
        with urllib.request.urlopen(api_url, timeout=5) as resp:
            data = resp.read().decode()
            import json
            obj = json.loads(data)
            return str(obj.get("id", obj.get("id_str", data)))
    except Exception:
        pass
