# mimi-bro

[English](README.md)

AI Agent 编排系统，用于任务分析、分解、分配、执行、验证与合成。

## 快速开始

```bash
pip install -e .

# 运行任务（本地模式，无 Docker）
bro submit workers/test-greetings.json -w docker/workspace -s src --local

# 带模板参数
bro submit workers/test-greetings.json --local --arg person=John
```

## 使用方法

### 基本命令

```bash
bro submit <worker.json> [OPTIONS]
```

| 选项 | 说明 |
|------|------|
| `-w, --workspace` | 工作目录（task.json、日志、works/） |
| `-s, --source` | Agent 操作的源码路径 |
| `--local` | 使用本地 cursor-cli（不用 Docker） |
| `--auto` | 跳过确认（CI/无人值守模式） |
| `--fresh N` | 重执行控制：-1=继续，0=全部重新开始 |
| `-p, --parallel` | 启用并行子任务执行 |
| `-j, --max-workers` | 最大并行数（默认：4） |
| `-v, --verbose` | 显示详细日志 |

### 并行执行

并行模式（`-p`）使用 git worktree 隔离各子任务。所有子任务成功后，按拓扑顺序自动 cherry-pick 合并。

```bash
bro parallel status              # 查看执行状态
bro parallel worktree list       # 列出 git worktrees
```

#### 合并场景

| 场景 | 操作 |
|------|------|
| 全部成功 | 自动合并，无需操作 |
| 合并时冲突 | 手动解决后执行 `git cherry-pick --continue` |
| 部分任务失败 | 修复问题后执行 `bro parallel merge` |
| 合并到其他分支 | `bro parallel merge -t <branch>` |

#### 清理

```bash
bro parallel merge --cleanup     # 合并并清理 worktrees
bro parallel cleanup             # 仅清理（不合并）
bro parallel cleanup --force     # 强制清理（丢弃未提交的更改）
```

### 输出格式

```bash
bro submit <task> -o auto    # TTY 时用 TUI，否则纯文本（默认）
bro submit <task> -o plain   # 行式输出，适合 CI
bro submit <task> -o jsonl   # 机器可读，适合 IDE 插件
```

## TUI 控制

- **方向键**：导航任务树
- **Enter**：查看选中节点的 Agent 日志
- **q**：退出

## License

MIT
