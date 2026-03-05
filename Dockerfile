FROM python:3.12-slim

LABEL name="cursor-agent"
LABEL version="0.5"

ENV PYTHONUNBUFFERED=1
ENV WORKSPACE=/workspace
ENV WORK_DIR=/workspace/work

# Install system dependencies + tini (proper PID 1: signal handling, zombie reaping)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
        tini \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install cursor-cli (Linux version)
RUN curl -fsSL https://cursor.com/install | bash || true
ENV PATH="/root/.cursor/bin:/workspace/agents:/workspace/tools/node/bin:${PATH}"

# Install mimi-bro package (provides `bro` CLI: bro submit, bro run)
COPY pyproject.toml .
COPY src src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e .

# Agent entrypoint paths (used by python /src/agent.py when running main agent)
ENV PYTHONPATH=/app/src:/src:$PYTHONPATH
COPY src/agent.py /src/agent.py
COPY src/common /src/common

# Verify bro CLI available
RUN bro --help 2>&1 | head -1 || true

# Use tini as init (PID 1) - handles signals correctly, reaps zombies. Python as PID 1 can cause 137.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["sleep", "infinity"]
