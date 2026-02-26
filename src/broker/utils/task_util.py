from datetime import datetime


def generate_task_id(serial: int) -> str:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{serial:04d}"
