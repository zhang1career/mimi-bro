# 测试场景文档

本文档描述 workers 目录下各测试员工的使用方法和验证步骤。

---

## Git Worktree 并行执行测试

测试 git worktree 在并行执行场景下的正确性、清理、合并（cherry-pick）和冲突处理。

### 相关文件

| 文件 | 说明 |
|------|------|
| `test_worktree_manager.json` | Manager 员工，包含并行子任务 |
| `test_worktree_worker.json` | 简单文件修改员工 |
| `tests/test_worktree_parallel.py` | pytest 单元测试 |
| `scripts/verify_worktree_test.sh` | 验证脚本 |
| `tests/fixtures/worktree/` | 测试夹具目录 |

### 测试目标

1. **Worktree 创建**：验证并行任务各自在独立 worktree 中运行
2. **隔离性**：验证各 worktree 之间文件修改互不干扰
3. **Cherry-pick 合并**：验证拓扑顺序的 cherry-pick 合并
4. **冲突检测**：验证同文件修改时的冲突检测
5. **清理**：验证 worktree 和分支的正确清理

### 依赖图

```
setup-fixture
     │
     ├──────────────┬──────────────┐
     ▼              ▼              ▼
worker-alpha  worker-beta  worker-gamma
     │              │              │
     └──────────────┴──────────────┘
                    │
                    ▼
           verify-worktrees
```

- `setup-fixture`: 创建初始测试文件
- `worker-alpha/beta/gamma`: 并行执行，修改同一文件
- `verify-worktrees`: 验证 worktree 状态

### 集成测试步骤

#### 1. 执行 Manager 员工

```bash
# 基本用法
bro submit workers/test-worktree-manager.json

# 指定源目录（推荐用于隔离测试）
bro submit workers/test-worktree-manager.json --source /path/to/test/repo
```

#### 2. 观察控制台

执行期间观察：

- **任务树**：应显示 5 个子任务
- **执行顺序**：
  1. `setup-fixture` 先执行
  2. `worker-alpha`、`worker-beta`、`worker-gamma` 并行执行
  3. `verify-worktrees` 最后执行
- **进度条**：显示并行任务进度

#### 3. 检查 Worktree 状态

```bash
# 列出所有 worktree
git worktree list

# 如果指定了 --source，需要在对应目录执行
cd /path/to/test/repo && git worktree list
```

预期输出示例：
```
/path/to/repo                 abc1234 [main]
/path/to/repo-worktrees/...   def5678 [test-worktree-manager-xxx/worker-alpha]
/path/to/repo-worktrees/...   ghi9012 [test-worktree-manager-xxx/worker-beta]
/path/to/repo-worktrees/...   jkl3456 [test-worktree-manager-xxx/worker-gamma]
```

#### 4. 尝试合并（预期冲突）

```bash
# 查看最近的 run-id
ls -la .state/parallel/

# 执行合并
bro parallel merge <RUN_ID>
```

**预期结果**：在合并 `worker-beta` 时遇到冲突，因为 `worker-alpha` 和 `worker-beta` 都修改了 `shared_config.json` 的 `name` 字段。

冲突内容示例：
```
<<<<<<< HEAD
  "name": "alpha-modified",
=======
  "name": "beta-modified",
>>>>>>> <commit>
```

#### 5. 运行验证脚本

```bash
# 当前目录
./scripts/verify_worktree_test.sh

# 指定源目录
./scripts/verify_worktree_test.sh --source /path/to/test/repo
```

验证脚本检查：
- 当前 Git 状态
- 所有 worktree 列表
- 测试相关分支
- 测试夹具文件
- 冲突状态
- 孤立 worktree 条目

#### 6. 清理

```bash
# 使用 bro 命令清理
bro parallel cleanup --run-id <RUN_ID> --force

# 或手动清理
git worktree prune
git branch -D $(git branch | grep "test-worktree-manager")
```

### 预期结果

| 检查项 | 预期结果 |
|--------|----------|
| Worktree 创建 | 每个并行子任务在独立 worktree |
| 文件隔离 | 各 worktree 修改互不影响 |
| 冲突检测 | `shared_config.json` 报告冲突 |
| 非冲突文件 | `*_only.txt` 文件正常合并 |
| 清理 | worktree 和分支正确删除 |

### 单元测试

除集成测试外，可单独运行 pytest 单元测试：

```bash
# 运行所有 worktree 测试
pytest tests/test_worktree_parallel.py -v

# 运行特定测试类
pytest tests/test_worktree_parallel.py::TestParallelConflict -v

# 运行特定测试
pytest tests/test_worktree_parallel.py::TestParallelConflict::test_same_file_different_changes_causes_conflict -v
```

### 冲突处理选项

遇到冲突时的处理方式：

| 方式 | 命令 |
|------|------|
| 手动解决 | 编辑文件 → `git add <file>` → `git cherry-pick --continue` |
| 跳过提交 | `git cherry-pick --skip` |
| 中止合并 | `git cherry-pick --abort` |

---

## 并行执行测试（无冲突）

测试并行执行的基本功能，任务间无依赖。

### 相关文件

| 文件 | 说明 |
|------|------|
| `test-parallel.json` | 两个独立并行任务 |

### 执行

```bash
bro submit workers/test-parallel.json --requirement "Test parallel execution"
```

### 预期结果

- `agent-a` 和 `agent-b` 同时执行
- 无冲突，两个任务独立完成

---

## 并行执行测试（有依赖）

测试带依赖关系的并行执行。

### 相关文件

| 文件 | 说明 |
|------|------|
| `test-parallel-deps.json` | 带依赖的并行任务 |

### 依赖图

```
agent-first
     │
     ├─────────────┐
     ▼             ▼
agent-parallel-a  agent-parallel-b
     │             │
     └──────┬──────┘
            ▼
       agent-final
```

### 执行

```bash
bro submit workers/test-parallel-deps.json --requirement "Test parallel with deps"
```

### 预期结果

1. `agent-first` 先执行
2. `agent-parallel-a` 和 `agent-parallel-b` 并行执行
3. `agent-final` 最后执行
