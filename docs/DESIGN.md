# 多 Agent 自举式工程系统（Broker + Cursor CLI）设计文档

---

## 1. 背景与目标

随着 AI Agent 在软件工程中的能力增强，单一 Agent 已难以满足复杂工程对并行性、稳定性、可控性的要求。本设计旨在构建一个 **多 Agent 协作的软件工程系统**，以 **Broker** 为核心，通过管理多个 **Cursor CLI Agent（容器化）**，实现：

* 多任务并行开发
* 人在回路（Human-in-the-loop）到自动化的平滑演进
* 系统“自举式”演进（系统参与自身开发，但不失控）

最终目标不是“全自动写代码”，而是：

> **用工程化、可审计、可回滚的方式，最大化放大人的工程效率。**

---

## 2. 总体需求

### 2.1 功能需求

1. 支持多个 Cursor CLI 实例并行运行
2. Broker 统一接收用户任务，请求可来自 CLI / IDE / API
3. Broker 能将复杂任务拆分为多个子任务并分配给不同 Agent
4. Broker 管理 Agent Docker 容器的生命周期（启动 / 停止 / 清理）
5. Broker 汇总 Agent 结果并呈现给用户
6. 支持人工干预、覆盖、复审
7. 支持从人工主导 → 半自动 → 自动的平滑切换
8. 决策过程可记录、可回放、可学习

### 2.2 非功能需求

* **可控性**：任何自动行为必须可一键关闭
* **可回滚**：Agent 产物必须可审计、可撤销
* **可扩展性**：支持新增 Agent 类型、任务类型
* **轻量化**：初期不强依赖 Kubernetes 等重基础设施

---

## 3. 总体架构

```
┌────────────┐
│   Human    │  ← CLI / IDE Plugin / API
└─────┬──────┘
      │
┌─────▼───────────┐
│     Broker      │
│ ────────────── │
│ Task Manager    │
│ Decision Plane  │
│ Rule / Score    │
│ Container Ctrl  │
└─────┬───────────┘
      │
┌─────▼──────────────────────┐
│ Cursor CLI Agent Containers │  (N 个)
└────────────────────────────┘
```

Broker 是系统核心；Agent 是可替换、可扩展的执行单元。

---

## 4. 关键技术点与针对性设计

### 4.1 Agent 容器化与生命周期管理

**技术点**：

* 多 Agent 并行执行
* 隔离运行环境

**设计**：

* 每个 Cursor CLI Agent 运行于独立 Docker 容器
* Broker 通过 Docker SDK 管理生命周期
* Agent 以 Job 形式存在，非长期服务

**收益**：

* 隔离性强
* 易回收
* 易扩展到远程执行

**4.1.1 Broker 与 Agent 通信时序**

* Broker 在写入 `works/{{task_name}}-{{run_id}}-{{role}}/task.json` 后启动容器，调用 `container.wait()` **阻塞等待**直到容器进程退出。
* 无回调、无轮询；Agent「完成」= 容器进程退出，Broker 被 `wait()` 唤醒后继续（写 progress、下一轮或结束）。
* **开发阶段默认同步阻塞**；可通过环境变量 `BROKER_AGENT_ASYNC=1` 切换为异步非阻塞（若已实现）。
* **设计约定**：两种模式都支持对 agent 的监控与 kill；异步模式下需维护运行中容器的 registry，以便在停止或信号处理时遍历并 stop/kill 各容器。

---

### 4.2 Task 拆分与依赖建模

**技术点**：

* 复杂任务拆解
* 子任务依赖关系

**设计**：

* Task 抽象为 DAG（有向无环图）
* 使用图结构表示依赖与并行性
* 人工可参与 Task 拆分确认

---

### 4.3 Decision Plane（决策平面）

**核心思想**：

> Broker 不“替人决定”，只提供结构化选择

**决策三层模型**：

1. **Rules（硬规则）**

   * 禁止路径
   * 必须角色
   * 最大并行数

2. **Scores（软偏好）**

   * 历史成功率
   * 用户偏好
   * 复杂度估计

3. **Prompt Dictionary（策略库）**

   * 不同规划/执行风格

**人机关系**：

* 人工干预 = 高权重决策样本
* 所有决策记录 source（human / auto / rule）

---

### 4.4 人工 → 半自动 → 自动 的平滑切换

**设计原则**：

* 自动化是“跳过确认”，不是“取消人工权力”

**机制**：

* auto-mode 带阈值
* score 差距不足 → 强制人工确认
* 任意时刻可切回人工

---

### 4.5 自举式工程方法

**定义**：
系统在人的监督下，参与自身的开发。

**约束**：

* Agent 不得 merge
* Agent 产物必须标记 generated
* 系统核心安全逻辑永不自举

**适合自举模块**：

* 脚手架
* 文档
* 测试代码
* 重复性工具代码

---

### 4.6 多轮交互与 task.json 约定

**目标**：通过多次与 Cursor CLI 的「交互」（多轮执行）达到指定目标，而不要求 cursor-cli 本身支持会话式多轮。

**works 路径约定**：

* 路径：`/workspace/works/{{task_name}}-{{run_id}}-{{role}}/`，内含 `task.json`、`result.json`、`agent.log`。
* `task_name` 来自任务 JSON 的 `task.id`；`run_id` 为本次执行的 id（snowflake_id，可由接口获得或本地生成）；`role` 为当前 agent 的 role。
* Broker 写 task.json、读 result.json 均在该子目录；启动容器时通过环境变量 `WORK_DIR_REL` 告知 Agent 子目录路径。

**约定**：

1. **单轮**：Broker 在启动 Agent 容器**之前**，在对应 work 子目录写入 `task.json`，内容包含 `objective`、`instructions`（可选）、`mode`（可选）、`entrypoint`（可选）。

2. **多轮**：Broker 将「指定目标」拆成多步（例如 JSON 的 `task.steps`）。每一轮：
   * Broker 根据当前步和上一轮 result，生成本轮的 `objective`、`instructions`
   * 写入 work 子目录的 `task.json`，启动 Agent 容器
   * Agent 退出后写出该子目录下的 `result.json`
   * Broker 读取 `result.json`，决定下一轮内容或结束

   **Broker 直接调度子任务**（方案 A，避免 run_terminal_cmd 超时）：
   * 若 step 含 `validate_with`（任务路径），Broker 直接运行该子任务，不通过 Agent 的 shell
   * `validate_only: true` 时跳过 Agent，仅运行 `validate_with` 子任务
   * 生产环境：Broker 为子任务创建新容器；本地：Broker 直接调用 cursor-cli

3. **多步进度持久化与恢复**：进度存宿主机 `.state/tasks/<task_id>/progress.json`（`task_id` 为 JSON 的 task.id），字段含 `completed_step_indices`、可选 `last_round_result`、`updated_at`。多轮前读取 progress 并跳过已完成步骤；每轮容器**成功**退出后写 progress。默认可恢复；`bro submit ... --fresh` 可清除进度、从第 0 步重新执行。

4. **人在回路**：多轮之间可由 Broker CLI 暂停，展示本轮结果并询问是否继续，再继续执行。

详见 `docs/CURSOR_CLI_USAGE.md` 第 5 节。

---

### 4.7 Broker 验收 agent 工作成果

* Broker 验收 agent 工作成果时，可能由 **Broker 自身**完成，也可能引入**外部系统**（测试工具+测试用例，或人+设备）。Broker 需做好**接口统一**（验收触发、输入、输出抽象一致）。
* **测试工具 + 测试用例**：task 制定完成时即开始测试用例设计；task 后续有修改时，测试用例需相应修改。
* **人 + 设备**：task 制定完成时即开始制定测试方案和设备预检方案。

### 4.8 Post-run 自动审计（Phase 4.1 / 4.2）

* **每轮结束后**自动执行审计（规则/可选 AI 补充）；**结论来源**记录为 human 或 AI。
* **升级条件**：结果不明确（ambiguous）、非零退出（hard）、或本步重试次数 >3 时升级，**人工可介入**（接受并继续 / 重试本步）。
* **Phase 4.2**：任务/步骤可设可选 **expected_results**（如 `status`、`exit_code`）；实际结果与预期不符时按规则升级，人工可接受或重试。
* 审计记录存 `.state/tasks/<task_id>/audit/audit.json`；进度中可选 `retry_counts`（每步尝试次数）。标准不强制；AI 仅作补充。

### 4.9 Audit-to-bootstrap（Phase 5.1）

* **目标**：一般任务执行 → 审计 → 若偏离业务需求 → 通过自举任务（boost）更新/修复系统；审计结果需接入自举。
* **实现**：Broker 在运行 **bootstrap** 任务（含多轮步骤或单轮）时，从 `.state/tasks/<task_id>/audit/` 汇总**非 boost** 任务的近期审计记录（优先 escalated / 非 pass），生成简短摘要字符串。
* **注入方式**：该摘要作为本轮的 `instructions` 一条注入到 `task.json`，供 boost Agent 阅读；Agent 可据此在系统层面做修复或迭代，不改变核心安全逻辑。
* **接口**：`broker.audit.store` 提供 `list_task_ids_with_audits()`、`get_audit_summary_for_boost(exclude_task_id="boost", max_per_task=10)`；runner 在构建 bootstrap 的 task  payload 时调用并传入 `audit_context`。

---

## 5. 实施方法（推荐路径）

### Phase 0：最小可运行系统

* Broker CLI
* Docker 管理
* 单 Agent 执行

### Phase 1：多 Agent 并行

* DAG 任务模型
* 结果汇总

### Phase 2：Decision Plane

* Rule + Score
* 人工确认流程

### Phase 3：自举支持

* 系统开发任务类型
* 生成代码标记

### Phase 4：IDE 插件

* VS Code / IDEA
* 一屏多 Agent 交互

---

## 6. 可复用的开源软件与工具

### 6.1 容器与执行

* Docker Engine
* docker-py / dockerode

### 6.2 DAG / 任务建模

* NetworkX (Python)
* Apache Airflow（可选）
* Argo Workflows（K8s 环境）

### 6.3 Agent / 协作框架（参考）

* AutoGen
* OpenHands
* SWE-Agent

### 6.4 CLI / UI

* Typer / Click
* tmux / tmuxinator
* VS Code Extension API

### 6.5 规则 / 决策

* durable_rules
* 自定义 scoring 模块

### 6.6 可观测性

* OpenTelemetry
* Prometheus + Grafana
* Sentry

---

## 7. 注意事项与风险控制

1. **禁止 Agent 自主重构系统核心**
2. **自动 merge = 高风险操作，默认关闭**
3. **必须有全局停机开关**
4. **所有自动行为必须可回滚**
5. **避免早期引入复杂 ML，先用规则和统计**

---

## 8. 结语

该系统不是一个“AI 写代码工具”，而是一个：

> **以人类工程师为中心的、多 Agent 协作的工程操作系统。**

它通过严格的工程纪律，让 AI 成为可扩展的“劳动力”，而不是不可控的“自治体”。

---

（文档版本：v0.1，适用于原型与早期实现）

