# Cursor CLI 使用说明

本文说明如何与 cursor-cli（cursor-agent）交互，以及在本项目中的用法。

## 1. 无阻塞运行（避免被确认/权限询问卡住）

### 认证（避免登录询问）

- 设置环境变量 **`CURSOR_API_KEY`**：CLI 会直接使用，不再弹出交互式登录。
- 本项目已支持：`src/agent.py` 从环境变量读取并设置；`src/broker/agent/docker.py` 在启动容器时把主机的 `CURSOR_API_KEY` 传入容器。在主机上执行 `export CURSOR_API_KEY=...` 后再执行 `bro run`，容器内就不会被登录卡住。

### 非交互 + 自动执行（无需人工确认命令/写文件）

- **`-p` / `--print`**：非交互模式，输出到终端，适合脚本/CI。
- **`--force`**：在 print 模式下允许直接修改文件、执行命令，无需逐条确认。

典型无阻塞用法（若直接使用官方安装的 `agent` 命令）：

```bash
export CURSOR_API_KEY=your_key
agent -p --force "你的任务描述"
```

本项目在 `agent.py` 中调用 cursor 时已加入 `-p` 与 `--force`，实现无确认、无阻塞执行。

### 控制台要有实时输出（不阻塞且能看到进度）

- **默认 `-p` 使用 `text` 格式**：官方行为是「只输出最终回答」，中间没有任何进度或工具调用，所以长时间运行时控制台会一直无输出直到结束。
- **需要实时看到输出**：加上 **`--output-format stream-json`**。CLI 会按事件输出 NDJSON 行（system init、assistant 消息、tool_call 开始/完成、最终 result），控制台会持续有输出。
- **可选**：再加 **`--stream-partial-output`** 可得到字符级流式（更碎，适合「打字效果」）。

**直接敲命令时**（注意末尾必须带 **prompt**，否则没有任务可执行）：

```bash
# 有实时 NDJSON 输出
/workspace/agents/cursor run -p --force --output-format stream-json --mode ask --workspace /workspace "列出当前目录下的文件"

# 若希望字符级流式（更碎）
/workspace/agents/cursor run -p --force --output-format stream-json --stream-partial-output --mode ask --workspace /workspace "列出当前目录下的文件"
```

本项目 `agent.py` 默认使用 `--output-format stream-json`，因此控制台会有实时 NDJSON 输出。

**为什么控制台是「时断时续、大量 JSON」？**  
`stream-json` 下每条事件（system init、assistant 消息、tool_call 开始/完成、最终 result）各占一行 NDJSON。工具调用完成事件里常带完整内容（如读文件结果、写文件信息），所以单行很长、输出呈一阵一阵的「大量 JSON」。若希望控制台安静、只在结束时看到最终回答，可设环境变量 **`AGENT_OUTPUT_FORMAT=text`**（例如在 docker run 或 docker-compose 里传 `AGENT_OUTPUT_FORMAT=text`），则不再输出 NDJSON，仅最后打印一段纯文本结果。

### 已知限制

- 非交互模式下，进程有时会完成后不退出（已知 bug）。本项目已设置 30 分钟超时和 Ctrl+C 处理以缓解。

### 本地运行（bro submit --local）

使用 `bro submit task.json -w workspace --local` 时，Broker 会在主机上直接调用 **headless CLI**，不启动 Docker 容器。此时会**优先使用 cursor-agent 或 agent**（官方 headless 命令），解析顺序为：`CURSOR_CLI_PATH` → `cursor-agent`（PATH 或 `~/.local/bin/cursor-agent`）→ `agent`（PATH）→ `cursor`（PATH）→ `workspace/agents/cursor`。

- **推荐**：在主机上安装 headless CLI（运行 `cursor agent` 或按 [Cursor CLI 安装文档](https://cursor.com/docs/cli/installation) 安装），这样会使用 `cursor-agent`/`agent`，prompt 会按约定正确传递，不会出现 “Constraints: command not found” 或选项警告。
- **若仅安装了 Cursor 编辑器**而未安装 headless CLI，PATH 中的 `cursor` 可能是编辑器自带的 CLI，不认识 `-p`、`--workspace` 等选项，且可能把多行 prompt 误当脚本执行，从而出现 “not in the list of known options” 警告或 “Constraints:: command not found” 等错误。解决方式：在终端执行 `cursor agent` 安装 headless CLI，或设置 `CURSOR_CLI_PATH` 指向已安装的 `cursor-agent`/`agent` 可执行文件。

失败时，完整输出会写入 `works/.../agent.log`；控制台只提示该路径。若需在失败时再次在控制台打印完整日志，可设置环境变量 **`AGENT_LOG=1`** 后重新执行。

---

## 2. 带背景知识

在 **`--workspace` 指向的目录**下放置以下任一即可为 agent 提供背景知识：

- **`.cursor/rules`**：目录下放 `.md` / `.mdc` 规则文件，按 frontmatter 的 `alwaysApply`、`globs`、`description` 自动或按文件匹配应用。
- **`AGENTS.md`**（或 **`CLAUDE.md`**）：项目根下的纯 Markdown，会被 CLI 自动当作规则加载。
- **`.cursorrules`**（legacy）：项目根下的 `.cursorrules` 仍被支持，但官方建议迁移到 `.cursor/rules` 或 `AGENTS.md`。

此外，`task.json` 中的 `instructions` 会被拼进 prompt 的 “Constraints” 段，可与上述规则同时使用。

---

## 3. 是否可以有 .cursorrules

可以。在 workspace 根目录（即 `--workspace` 指向的目录，或其中 `entrypoint` 子目录的根）放 `.cursorrules`，Cursor CLI 会读取并作为规则使用。新项目建议使用 `.cursor/rules` 或 `AGENTS.md`。

---

## 4. 模式（plan / ask / agent）

CLI 支持与编辑器相同的模式：

| 模式    | 说明                     | 用法           |
|---------|--------------------------|----------------|
| **agent** | 默认，全功能，可改代码、跑命令 | `--mode=agent` 或默认 |
| **plan**  | 先规划、可问澄清问题再执行   | `--mode=plan`  |
| **ask**   | 只读探索，不改文件         | `--mode=ask`   |

本项目从 `task.json` 的 `mode` 字段读取模式；未指定时默认为 **agent**。在任务 JSON 的 plan 中可为每个 agent 指定 `mode`，例如：

```json
"plan": [
  { "id": "agent-backend", "role": "backend", "mode": "agent" },
  { "id": "agent-test", "role": "tester", "mode": "ask" }
]
```

---

## 5. 多轮交互（以达到指定目标）

Cursor CLI 单次调用是「一发一答」：一次 `cursor run "prompt"` 只执行一个任务描述。要实现**经过多次交互达到指定目标**，在本项目中采用 **Broker 驱动的多轮**：

### 5.1 思路

- **单轮**：Broker 为当前轮生成/写入 `works/{run_id}/{plan_id}/task.json`（含 `objective`、`instructions`、`mode` 等）→ 启动 Agent 容器（传 `WORK_DIR_REL`）→ Agent 内执行一次 `cursor run -p --force "…"` → 容器退出，写出该子目录下的 `result.json` 和日志。
- **多轮**：Broker 将「指定目标」拆成多步（例如 JSON 中的 `steps`）。每一轮：
  1. Broker 根据当前步生成本轮的 `task.json`（可包含上轮结果摘要、本步目标、约束）。
  2. Broker 在 workspace 下写入 `works/{run_id}/{plan_id}/task.json`，然后启动 Agent 容器。
  3. Agent 执行单次 cursor-cli，结束后 Broker 读取该子目录下的 `result.json`。
  4. 若还有下一步，回到 1；否则结束。

这样 **cursor-cli 本身不需要支持多轮会话**，多轮由 Broker 通过「多轮写 task.json + 多轮起容器」完成。

### 5.2 数据流

```
Broker: 目标 + steps，获取 run_id（snowflake_id 或本地生成）
  → 第 1 轮: 写 works/{run_id}/{plan_id}/task.json (objective=step1, ...)
  → 起容器（WORK_DIR_REL=works/...）→ agent.py 读该子目录 task.json → cursor run "step1..."
  → 容器退出 → Broker 读该子目录 result.json
  → 第 2 轮: 写同一子目录 task.json (objective=step2, instructions=[..., "上轮结果: ..."])
  → 起容器 → ... 重复直到 steps 跑完或人工终止
```

### 5.3 任务描述中的多轮（JSON）

- **单目标单轮**：仅设 `task.objective`（及可选 `task.instructions`），Broker 写一次 `task.json`，跑一轮。
- **多轮**：在顶层增加 `steps`（字符串数组或对象数组），每元素为一轮的 `objective`；Broker 按顺序为每一轮写入 `task.json` 并启动**首个** Agent，上一轮的 `result.json` 会作为下一轮 `instructions` 的上下文摘要。

示例（单轮）：

```json
{
  "task": {
    "id": "my-task",
    "objective": "列出项目中的主要命令",
    "instructions": ["只读不写"]
  },
  "plan": [
    { "id": "agent-1", "mode": "agent" }
  ]
}
```

示例（多轮）：

```json
{
  "task": {
    "id": "my-task",
    "instructions": ["保持改动最小"]
  },
  "steps": [
    "第 1 步：列出 src/ 下所有 Python 文件",
    "第 2 步：在第一个文件中添加一行注释"
  ],
  "plan": [
    { "id": "agent-1", "mode": "agent" }
  ]
}
```

### 5.4 works 目录结构

- 路径：`/workspace/works/{run_id}/{plan_id}/`，内含 `task.json`、`result.json`、`agent.log`。
- `run_id` 为本次执行的 id（snowflake_id 或本地生成）；`plan_id` 为父上下文中的计划项 id（来自 plans 或 breakdown）。
- Broker 启动容器时设置环境变量 `WORK_DIR_REL=works/{run_id}/{plan_id}`，Agent 从该子目录读写 task.json、result.json、agent.log。

### 5.5 多步进度与恢复

- 多步任务（`steps`）的进度存宿主机 `.state/tasks/<task_id>/progress.json`（`task_id` 为 JSON 的 task.id），含已完成步下标、最后一轮结果摘要等。
- 容器异常退出或 Broker 重启后，再次执行同一任务（同一 task_id + workspace）时，Broker 会读取 progress 并**从上次已完成步骤之后**继续执行。
- 使用 `bro submit task.json --fresh`（或 `-f`）可清除该任务的进度，从第 0 步重新执行。

---

## 参考

- [Cursor CLI Overview](https://cursor.com/docs/cli/overview)
- [Using Agent in CLI](https://cursor.com/docs/cli/using)
- [Using Headless CLI](https://cursor.com/docs/cli/headless)
- [Agent Modes](https://cursor.com/docs/agent/modes)
- [Rules](https://cursor.com/docs/context/rules)
