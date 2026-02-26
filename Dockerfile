FROM python:3.12-slim

LABEL name="cursor-agent"
LABEL version="0.3"

ENV PYTHONUNBUFFERED=1
ENV WORKSPACE=/workspace
ENV CURSOR_BIN=/workspace/agents/cursor

# Install dependencies for cursor-agent installation
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /

ENV PYTHONPATH=/src:$PYTHONPATH

# Install cursor-agent in the container
# This provides Linux native bindings and ensures cursor CLI is available
RUN curl -fsSL https://cursor.com/install | bash || true

# Set up cursor-agent path
ENV PATH="/workspace/agents:/workspace/tools/node/bin:${PATH}"

# Broker agent entrypoint (task.json -> cursor-cli -> result.json)
COPY src/agent.py /src/agent.py
COPY src/common /src/common

# Default: idle for debug; broker overrides with command when running agent
CMD ["sleep", "infinity"]
