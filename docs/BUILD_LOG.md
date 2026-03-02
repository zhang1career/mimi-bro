# BUILD_LOG.md — Phase 3.2 Synthesis (Round 9 of 12)

**Generated.** Summary of what was built, what works, and known limits. Aligned with `docs/DESIGN.md`; core safety logic is not modified by agent.

---

## 1. What was built

### Broker CLI (`bro`)

- **`bro submit`** — Load task from JSON, build DAG from `worker.plans`, run Decision Plane (rules + scoring), select plan (human / auto / score-gap), record decision, run agents (Docker or local cursor-cli). Supports `--workspace`, `--source`, `--fresh`, `--local`, `--auto`, `--arg KEY=VALUE`.
- **`bro status`** — Reports: *"Status: v0.1 does not persist runtime state yet."*
- **`bro run`** — Run a single agent with `--objective`; writes workspace task.json and starts agent.
- **`bro stop`** — Placeholder: *"Stop: not implemented in v0.1"*.

### Task & planning

- **Task JSON** — Load from path (cwd / workspace / project_root); `{{key}}` template substitution via `substitute_task()` and `--set`.
- **DAG planner** — `task.plans` → NetworkX DAG (nodes: id, role, mode, objective; edges from `deps`). Cycle detection.
- **Progress** — `.state/tasks/<task_id>/progress.json` (completed_step_indices, last_round_result, updated_at). `--fresh` clears progress.

### Decision plane (DESIGN §4.3, §4.4)

- **Rules** — `forbidden_node_ids` (env `BROKER_FORBIDDEN_NODES`), `max_parallel` (env `BROKER_MAX_PARALLEL`). Applied before scoring.
- **Plans** — Parallel (topological batches) and serial (one agent per batch) variants; each plan has `score` (placeholder scoring).
- **Selection** — Single plan → auto; `--auto` → first plan; else score gap ≥ `BROKER_SCORE_GAP_THRESHOLD` (default 0.5) → auto; else human prompt.
- **Recording** — Decisions appended to `logs/decisions.jsonl` (event, source, choice, plan_summary).

### Agent execution

- **Local runner** — Invokes host cursor-cli (cursor-agent / agent / CURSOR_CLI_PATH); writes `task.json` in work dir; streams to `agent.log`; writes `result.json` on exit. Timeout 30 min. No autonomous merge.
- **Docker runner** — Optional; container lifecycle via Docker SDK; same work-dir contract.
- **Work dirs** — `workspace/works/{{task_id}}-{{run_id}}-{{role}}/` with `task.json`, `result.json`, `agent.log` (DESIGN §4.6).
- **Multi-step** — Steps with optional `validate_with`; broker can run validation sub-tasks directly (no agent shell). Progress used to skip completed steps.

### Validation & constraints

- Workspace validation before submit/run.
- Agent output marked generated; no autonomous merge; core safety logic not modified via agent (DESIGN §4.5).

---

## 2. What works

- **End-to-end local flow** — `bro submit tasks/<task>.json -w <workspace> --local [--auto]` loads task, plans DAG, proposes plans, selects (auto or prompt), runs agents in batch order, aggregates results from each work dir’s `result.json`.
- **Single-agent greeting** — Example run in `works/greetings-17715549945162046-backend/`: `task.json` + `agent.log` (cursor-cli session with success result).
- **Template params** — `--arg KEY=VALUE` and worker `params` fill `{{key}}` in worker.id, objective, instructions.
- **Fresh run** — `--fresh` clears progress and runs from step 0.
- **Decision log** — Plan choice and source written to `logs/decisions.jsonl`.

---

## 3. System state (from `bro status`)

```text
Status: v0.1 does not persist runtime state yet.
```

So: no persisted “current run” or “active agents” view in v0.1; progress is per-task in `.state/tasks/<task_id>/progress.json` only.

---

## 4. Known limits

- **`bro status`** — Does not report active runs or live agent state; placeholder message only.
- **`bro stop`** — Not implemented.
- **Scoring** — Plan scores are placeholder; no historical success or user-preference weighting yet.
- **Runtime state** — No global registry of running containers/jobs; async mode and stop/kill not wired to a central state.
- **Decision log path** — `logs/decisions.jsonl` is relative to process cwd (e.g. `src/`); may need explicit workspace-relative or absolute path in future.
- **Progress location** — Progress is under `PROJECT_ROOT` (broker host); when running from `src/`, `.state` is under repo root or cwd depending on path_util.

---

## 5. Phase / round context

- **Phase 3.2 — Synthesis.** Round 9 of 12. Previous round: status=success, exit_code=0.
- Semi-waterfall: phase gate before next; semi-agile: iterate within phase. After each local capability, run the project to validate.

---

*Document generated for Phase 3.2. Do not modify core safety logic via agent.*
