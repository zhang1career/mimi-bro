# Git Worktree Parallel Execution Test

测试 git worktree 在并行执行场景下的正确性、清理、合并（cherry-pick）和冲突处理。

## 测试目标

1. **Worktree 创建**：验证并行任务各自在独立 worktree 中运行
2. **隔离性**：验证各 worktree 之间文件修改互不干扰
3. **Cherry-pick 合并**：验证拓扑顺序的 cherry-pick 合并
4. **冲突检测**：验证同文件修改时的冲突检测
5. **清理**：验证 worktree 和分支的正确清理

## 测试员工

### test_worktree_manager.json

Manager 员工，包含以下并行计划：

```
setup-fixture
     ↓
  ┌──┴──┐──────────┐
  ↓     ↓          ↓
worker-alpha  worker-beta  worker-gamma
  ↓     ↓          ↓
  └──┬──┘──────────┘
     ↓
verify-worktrees
```

- **setup-fixture**: 创建初始测试文件 `shared_config.json`
- **worker-alpha**: 修改 `name` 为 `alpha-modified`，`timeout` 为 60
- **worker-beta**: 修改 `name` 为 `beta-modified`，`retries` 为 5
- **worker-gamma**: 修改 `version` 为 2，添加 `modified_by` 字段
- **verify-worktrees**: 验证 worktree 列表

### test_worktree_worker.json

简单的文件修改员工，可被 manager 调用。

## 预期冲突

`worker-alpha` 和 `worker-beta` 都修改 `shared_config.json` 的 `name` 字段，
会在 cherry-pick 合并时产生冲突：

```
<<<<<<< HEAD
  "name": "alpha-modified",
=======
  "name": "beta-modified",
>>>>>>> <commit>
```

## 测试步骤

### 1. 运行 Manager 测试

```bash
bro submit workers/test-worktree-manager.json
```

### 2. 观察控制台

- 任务树应显示 5 个子任务
- `setup-fixture` 先执行
- `worker-alpha`、`worker-beta`、`worker-gamma` 并行执行
- `verify-worktrees` 最后执行

### 3. 检查 Worktree 状态

```bash
git worktree list
```

应看到为每个并行任务创建的 worktree。

### 4. 手动合并测试

```bash
bro parallel merge --run-id <RUN_ID>
```

预期在 `worker-beta` 时遇到冲突。

### 5. 验证

```bash
./scripts/verify_worktree_test.sh
```

或运行 pytest：

```bash
pytest tests/test_worktree_parallel.py -v
```

## 清理

测试完成后清理 worktree：

```bash
bro parallel cleanup --run-id <RUN_ID> --force
```

或手动：

```bash
git worktree prune
git branch -D <test-branches>
```

## 测试场景覆盖

| 场景 | 测试类 | 验证点 |
|------|--------|--------|
| 创建 worktree | `TestWorktreeCreation` | 新分支、多 worktree、隔离性 |
| 并行冲突 | `TestParallelConflict` | 同文件冲突、不同文件无冲突 |
| 清理 | `TestWorktreeCleanup` | 删除目录、强制删除、prune |
| 合并顺序 | `TestMergeOrderDependency` | 拓扑顺序影响 |

## 文件结构

```
tests/fixtures/worktree/
├── README.md                 # 本文档
├── shared_config.json        # 共享配置（冲突测试目标）
├── alpha_only.txt           # alpha worker 创建（执行后出现）
├── beta_only.txt            # beta worker 创建（执行后出现）
└── gamma_only.txt           # gamma worker 创建（执行后出现）
```

## 冲突处理选项

当遇到冲突时，可以：

1. **手动解决**：编辑冲突文件，然后 `git add && git cherry-pick --continue`
2. **跳过该提交**：`git cherry-pick --skip`
3. **中止合并**：`git cherry-pick --abort`
4. **使用回调**：在代码中设置 `conflict_callback` 自动处理
