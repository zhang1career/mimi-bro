FROM python:3.12-slim

LABEL name="cursor-agent"
LABEL version="0.1"

ENV PYTHONUNBUFFERED=1
ENV WORKSPACE=/workspace

WORKDIR /agent

# 不依赖外网，不 pip install
COPY agent.py /agent/agent.py

ENTRYPOINT ["python", "/agent/agent.py"]
