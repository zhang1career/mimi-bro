# mimi-bro

[中文](README_zh.md)

AI Agent orchestration system for task analysis, decomposition, assignment, execution, validation, and synthesis.

## Quick Start

```bash
pip install -e .

# Run a task (local mode, no Docker)
bro submit workers/test-greetings.json -w docker/workspace -s src --local

# With template parameters
bro submit workers/test-greetings.json --local --arg person=John
```

## Usage

### Basic Commands

```bash
bro submit <worker.json> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-w, --workspace` | Work directory (task.json, logs, works/) |
| `-s, --source` | Source path for agent to operate on |
| `--local` | Use local cursor-cli instead of Docker |
| `--auto` | Skip confirmations (CI/unattended mode) |
| `--fresh N` | Re-execution control: -1=continue, 0=restart all |
| `-p, --parallel` | Enable parallel subtask execution |
| `-j, --max-workers` | Max parallel workers (default: 4) |
| `-v, --verbose` | Show detailed logs |

### Parallel Execution

Parallel mode (`-p`) uses git worktree to isolate each subtask. After all subtasks succeed, results are auto-merged via cherry-pick in topological order.

```bash
bro parallel status              # View execution status
bro parallel worktree list       # List git worktrees
```

#### Merge Scenarios

| Scenario | Action |
|----------|--------|
| All succeeded | Auto-merged, no action needed |
| Conflict during merge | Resolve manually, then `git cherry-pick --continue` |
| Some tasks failed | Fix issues, then `bro parallel merge` |
| Merge to different branch | `bro parallel merge -t <branch>` |

#### Cleanup

```bash
bro parallel merge --cleanup     # Merge and cleanup worktrees
bro parallel cleanup             # Cleanup only (no merge)
bro parallel cleanup --force     # Force cleanup (discard uncommitted changes)
```

### Output Formats

```bash
bro submit <task> -o auto    # TUI when TTY, plain otherwise (default)
bro submit <task> -o plain   # Line-based output for CI
bro submit <task> -o jsonl   # Machine-readable for IDE plugins
```

## TUI Controls

- **Arrow keys**: Navigate task tree
- **Enter**: View agent log for selected node
- **q**: Quit

## License

MIT
