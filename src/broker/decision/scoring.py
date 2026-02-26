def score(plan, style: str = "serial") -> float:
    """
    Placeholder scoring (DESIGN 4.3). Default: serial (one agent per batch, sequential).
    style: "serial" (default, one-by-one) vs "parallel" (batched).
    """
    return 1.0 if style == "serial" else 0.5
