# mimi-bro

[中文](README_zh.md)

AI Agent orchestration system: task analysis, decomposition, assignment, execution, validation, and synthesis.

**Main command: `bro submit`**

## Quick Start

```bash
pip install -e .

# Local mode (no Docker)
bro submit workers/test-greetings.json -w docker/workspace -s src --local

# With template parameter
bro submit workers/test-greetings.json --local -a person=John
```

![confirm](assets/screenshoot-confirm-dependencies.png)
![main pannel](assets/screenshoot-main-pannel.png)

## Examples

### Parallel execution

When `-s`/`--source` points to a git repository, `-p` enables parallel subtask execution via git worktree isolation.

```bash
bro submit workers/test-worktree-manager.json --fresh 0 -p -w docker/workspace -s /tmp/empty-source -a requirement="say 'hello'"
```

`workers/test-worktree-manager.json` is a test worker for validating git worktree parallel execution, merge, and conflict handling.

### Output formats

```bash
bro submit <worker> -o auto    # TUI when TTY, plain otherwise (default)
bro submit <worker> -o plain   # Line-based, for CI
bro submit <worker> -o jsonl   # Machine-readable, for IDE plugins
```

## Command reference

```bash
bro submit <worker.json> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-w, --workspace` | Work directory (task.json, logs, works/) |
| `-s, --source` | Source path for agent to operate on (git repo for `-p`) |
| `-p, --parallel` | Enable parallel subtasks (requires git source) |
| `-j, --max-workers` | Max parallel workers (default: 4) |
| `--fresh N` | Re-execution: -1=continue, 0=restart all |
| `-a, --arg` | Template param KEY=VALUE (repeatable) |
| `--local` | Use local cursor-cli instead of Docker |
| `--auto` | Skip confirmations (CI/unattended) |
| `-v, --verbose` | Show detailed logs |

## Parallel mode

With `-p`, each subtask runs in an isolated git worktree branch. After all succeed, results are auto-merged via cherry-pick in topological order.

```bash
bro parallel status              # View execution status
bro parallel worktree list       # List git worktrees
bro parallel merge --cleanup     # Merge and cleanup worktrees
bro parallel cleanup             # Cleanup only (no merge)
bro parallel cleanup --force     # Force cleanup (discard uncommitted)
```

| Scenario | Action |
|----------|--------|
| All succeeded | Auto-merged |
| Merge conflict | Resolve manually, then `git cherry-pick --continue` |
| Some failed | Fix issues, then `bro parallel merge` |
| Merge to different branch | `bro parallel merge -t <branch>` |

## TUI controls

- **Arrow keys**: Navigate task tree
- **Enter**: View agent log for selected node
- **q**: Quit

## License

MIT
