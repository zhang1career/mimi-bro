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

