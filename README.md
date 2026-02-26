# mimi-bro

AI Agent 工作任务执行系统。支持任务的分析、分拆、指派、实施、验收与综合。

---

## 系统目标与构建流程

### 1. 自举任务优先

- **自举任务**：`tasks/boost.json`
- 由 AI Agent（如 Cursor CLI）首先编写并维护
- 作用：构建系统本身（代码建设、能力迭代）
- 约束：遵循 `docs/DESIGN.md`，不修改核心安全逻辑，Agent 产出需标记 generated

### 2. 迭代执行自举任务

- 多次重复执行 `bro submit tasks/boost.json ...` 完成自举
- 自举任务采用**半瀑布半敏捷**：阶段门控 + 阶段内迭代
- 每完成一项必需能力后，执行项目以验证并产出价值

### 3. 一般任务产生业务价值

- **一般任务**：`tasks/` 下除 `boost.json` 外的任务（如 `demo-greetings.json` 等）
- 用于执行业务功能，持续运行以产生业务价值
- 任务格式：JSON，含 `task`、`plan`、可选 `steps`、`params`
- **任务来源**：当前为示例；实际业务任务由自举任务构建，构建时参考示例

### 4. 审计与修复闭环

```
一般任务执行 → 审计执行效果 → 若偏离业务需求 → 通过自举任务更新/修复系统
```

- 审计一般任务的执行效果
- 若执行效果偏离业务需求，通过自举任务（`boost.json`）对系统进行更新或修复
- **审计结果接入自举**：Phase 5.1 已实现；boost 运行时自动注入非 boost 任务的审计摘要到 task 指令，供自举迭代参考（见 docs/DESIGN.md §4.9）

---

## 审计与验收规则

### 审计触发

- **默认**：每次任务结束后自动执行
- **升级人工**：难以选择、难以判断、难以解决、或重试超过上限（默认 3 次）时
- **人工介入**：支持随时介入审计

### 验收条件

- 支持（不必须）事先在任务或规则中写明
- 未写明时由 AI 补充
- **验收结论中须注明**：验收条件来自「人工写明」或「AI 生成」

### 偏离判定

- 由自动化规则 / 测试用例判定
- 支持人工介入判定
- 可预定义期望结果或校验规则，不必须

---

## 快速开始

```bash
# 安装依赖
pip install -e .

# 本地运行一般任务（无需 Docker）
bro submit workers/demo-greetings.json -w docker/workspace -s src --local --arg person=John

# 本地运行自举任务
bro submit workers/boost.json -w docker/workspace -s src --local
```

`--local` 为本地开发模式，不走 Docker；正式部署架构请参考 `docs/DESIGN.md`。

### 输出格式与 TUI

`bro submit` 支持多种输出模式，通过 `--output-format` / `-o` 控制：

| 值 | 说明 |
|----|------|
| `auto`（默认） | TTY 时使用 TUI（任务树 + 日志监视 + 进度条），非 TTY 时使用 plain |
| `plain` | 精简行式输出（任务、指派、结果），适用于 CI 或管道 |
| `jsonl` | 机器可读 JSONL 事件流，供 IDE 插件解析 |

```bash
# 交互式 TUI（默认，需 TTY）
bro submit tasks/demo-greetings.json --local

# 强制 plain 模式（便于 CI 日志）
bro submit tasks/boost.json --local -o plain

# IDE 插件消费 JSONL
bro submit tasks/boost.json --local -o jsonl
```

TUI 功能：
- 任务树：展示父任务与子任务层级
- 日志监视：光标移到节点后按 Enter，在右侧查看该节点 `agent.log` 的 tail（约 10 行，可滚动）
- 进度条：父任务进度条（图形 + 数字）

退出 TUI 请按 **q**。在任务阻塞时若用 Ctrl+Q 退出，可能触发终端 I/O 错误，属已知问题。

---

## 目录结构

| 路径 | 说明 |
|------|------|
| `tasks/` | 任务定义。`boost.json` 为自举任务；其余为一般任务 |
| `src/broker/` | Broker 核心（CLI、planner、agent runner、state） |
| `docs/` | 设计文档、使用说明 |

详见 `docs/DESIGN.md`、`docs/CURSOR_CLI_USAGE.md`。
