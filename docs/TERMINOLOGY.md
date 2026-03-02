# 项目专用术语

本文档定义 mimi-bro 项目的专用术语，便于团队与 AI 沟通时统一理解。

---

## 1. 控制台（TUI）

运行 `bro submit` 时，终端显示的多窗口 TUI 页面叫做**控制台**。

| 中文名 | 位置 | 说明 |
|--------|------|------|
| 任务树 | 控制台左上 | 树状结构，展示任务层级 |
| 日志窗口 | 控制台右上 | 日志显示区域 |
| 简讯窗口 | 控制台中下部 | 英文名 Brief Info，显示 broker 消息摘要 |
| 进度条 | 控制台底部 | 一组进度条（通常 1–2 个） |
| 当前状态 | 控制台最底部 | 单行文字，显示当前状态 |

---

## 2. 任务工作区

`workers/` 下的文件的 `worker`.`id` 字段的值，叫做**员工ID**。
`workspace/works/` 下的第一级文件夹，叫做**任务ID**。每个子任务运行时对应一个任务工作区目录。

---

## 3. 核心概念层级

| 概念 | 说明 | 文件位置 |
|------|------|----------|
| **Worker（员工）** | 预定义的员工配置，包含目标、指令、执行计划 | `workers/xxx.json` |
| **Plan（计划）** | Worker 中的 `plans` 字段，定义子任务及其依赖关系 | `workers/xxx.json` 中的 `plans` |
| **Plan Item** | `plans` 列表中的单个元素，可以是 skill/worker/inline 类型 | 同上 |
| **Task（任务）** | Worker 执行时产生的运行时实体 | `works/{task_id}/task.json` |
| **Breakdown** | Agent 运行时动态生成的子任务分解 | `works/{task_id}/breakdown.json` |

### 3.1 Plan Item 类型

Plan Item（计划项）有两种执行类型：

| 类型 | 字段 | 说明 |
|------|------|------|
| `skill` | `"skill": "skill-id"` | 调用已注册的 skill（包括 worker） |
| `inline` | 无 skill 字段，需 `mode` + `objective` | 直接启动 agent 执行 |

### 3.2 Worker 是一种 Skill

**重要概念**：Worker 是一种 Skill。区别在于 `invocation.type`：

| Skill 类型 | invocation.type | 说明 |
|------------|-----------------|------|
| 普通 skill | `agent_run` 等 | 单次 agent 执行 |
| Worker skill | `bro_submit` | 调用 `bro submit workers/xxx.json` |

Worker 通过 `bro worker register` 命令注册为 skill：

```bash
bro worker register workers/backend-dev.json
```

注册后，该 worker 可以像普通 skill 一样通过 `skill` 字段调用。

### 3.3 依赖关系

Plan Item 通过 `deps` 字段声明依赖：

```json
{
  "plans": [
    {"id": "api", "skill": "api-builder", "deps": []},
    {"id": "db", "skill": "db-builder", "deps": []},
    {"id": "test", "skill": "integration-test", "deps": ["api", "db"]}
  ]
}
```

- `api` 和 `db` 无依赖，可**并行执行**
- `test` 依赖前两者，在它们完成后执行
