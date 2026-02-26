一、总原则（v0.1 不做的事）

在列技术栈前，先说清楚 明确不做什么：

❌ 不上 Kubernetes

❌ 不做 Web UI

❌ 不做自动决策（只有建议 + 人确认）

❌ 不做复杂 Agent 间对话

❌ 不做 ML / 学习

👉 v0.1 = CLI + Docker + 人在回路

二、语言与运行时（锁死）
✅ Python 3.11

为什么锁 Python：

Docker SDK 成熟

DAG / 规则 / CLI 生态完整

Cursor / Agent 本身对 Python 友好

最适合“胶水型 Broker”

明确不选：

Node（异步好，但生态碎）

Go（过早工程化）

三、Broker 进程模型（锁死）
✅ 单进程 + 内部模块化

Broker 是一个 CLI 程序

每个命令是一次控制行为

Agent 是外部 Docker 容器

bro submit task.json
broker status
broker logs task-id
broker stop task-id

四、CLI 技术选型（锁死）
✅ Typer
pip install typer[all]


理由

基于 Click，稳定

类型友好

自动生成 help

非常适合工程 CLI

五、Agent 执行与容器管理（锁死）
✅ Docker Engine + docker-py
pip install docker


Broker 职责

pull image

run container

exec cursor cli

stream logs

stop / remove

Agent 镜像约定

cursor-agent:latest


容器启动参数：

volume: project workspace

env: task context

entrypoint: cursor cli

六、任务与计划建模（锁死）
✅ NetworkX
pip install networkx


用途

Task DAG

并行度分析

拓扑排序

失败传播

v0.1 只支持

静态 DAG

人工确认 DAG

七、Decision Plane（v0.1 最小实现）
✅ 手写模块（不引规则引擎）
结构：
decision/
├─ rules.py      # 硬过滤
├─ scoring.py    # 简单评分
├─ propose.py    # 生成选项
└─ record.py     # 决策日志

v0.1 行为：

输出 1–3 个方案

CLI 打印 diff / reason

等人选

评分实现

纯 Python 函数

不引 ML

八、任务描述格式（锁死）
✅ JSON
{
  "worker": {
    "id": "auth-refactor",
    "type": "system_development",
    "risk": "high"
  },
  "plan": [
    { "id": "agent-auth", "role": "backend" },
    { "id": "agent-test", "role": "tester" }
  ]
}

理由

系统输入与内部数据统一格式，便于数据扩散

人可读、Agent 可生成、易版本管理

九、日志与审计（锁死）
✅ 标准 logging + JSONL
logging.info({
  "event": "decision",
  "source": "human",
  "choice": "plan_B"
})


输出

logs/broker.log

logs/decisions.jsonl

十、状态存储（锁死）
✅ 本地文件系统（v0.1）
.state/
├─ tasks/           # 多步任务进度：.state/tasks/<task_id>/progress.json（断点续跑）
├─ agents/
├─ decisions/
└─ artifacts/


❌ 不上数据库
❌ 不上 Redis

十一、并发模型（锁死）
✅ ThreadPoolExecutor

每个 Agent = 一个线程管理

容器是真并行

Python 线程足够

十二、测试（最低限）
✅ pytest
pip install pytest


只测

DAG 校验

Decision Plane

Docker 启停（mock）

十三、开发辅助工具（强烈推荐）
工具	用途
pre-commit	格式 + lint
ruff	快速 lint
mypy	类型约束
make / taskfile	常用命令
十四、Broker v0.1 目录结构（最终锁定）
broker/
├─ broker/
│  ├─ cli.py
│  ├─ task.py
│  ├─ planner.py
│  ├─ decision/
│  │  ├─ rules.py
│  │  ├─ scoring.py
│  │  ├─ propose.py
│  │  └─ record.py
│  ├─ agent/
│  │  ├─ docker.py
│  │  ├─ runner.py
│  │  └─ logs.py
│  └─ state/
├─ tasks/
├─ logs/
├─ .state/
├─ pyproject.toml
└─ README.md

十五、为什么这个选型“不会后悔”

全部是 低锁定、可替换组件

v0.2 可自然升级：

NetworkX → Airflow / Argo

本地 FS → SQLite / DB

CLI → IDE 插件 / Web

没有任何一步是推倒重来

十六、一句话总结

Broker v0.1 是一个“工程控制器”，不是 AI 系统。
先稳定控制，再逐步放权。
