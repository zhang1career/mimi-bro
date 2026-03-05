#!/bin/bash
# Diagnose exit 137 (SIGKILL) with OOMKilled=false.
# Run from project root after building: ./scripts/diagnose-137.sh
# Usage: ./scripts/diagnose-137.sh [-s|--steps N[,N,...]] [workspace]
#   -s 7          run only step 7
#   -s 3,5,7      run steps 3, 5, 7
#   (no -s)       run all steps in order

set -e
PROJ_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJ_ROOT"

# Parse -s/--steps and workspace (workspace: first arg that looks like a path)
STEPS=""
WS_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--steps) STEPS="$2"; shift 2 ;;
    *)
      if [[ -z "$WS_ARG" && ("$1" == /* || "$1" == .* || "$1" == */*) ]]; then
        WS_ARG="$1"
      fi
      shift
      ;;
  esac
done
WS="${WS_ARG:-$(pwd)/docker/workspace}"

run_step() {
  [[ -z "$STEPS" ]] && return 0
  [[ ",${STEPS}," == *",${1},"* ]]
}

if run_step 1; then
echo "=== Step 1: Minimal container (sleep 60) ==="
echo "If this gets 137, the issue is Docker/VM environment, not agent."
if docker run --rm cursor-agent:latest python -c "import time; time.sleep(60); print('OK')"; then
  echo "[PASS] Minimal container ran 60s successfully"
else
  echo "[FAIL] Minimal container got 137 - check Docker Desktop, VM limits, host"
  exit 1
fi
fi

if run_step 2; then
echo ""
echo "=== Step 2: Agent with trivial task (will invoke cursor-cli) ==="
echo "If this gets 137, the issue is agent or cursor-cli."
mkdir -p "$WS/diag-137"
echo '{"objective":"say hello","instructions":[],"entrypoint":".","mode":"plan"}' > "$WS/diag-137/task.json"
if docker run --rm \
  -v "$WS:/workspace" \
  -e WORKSPACE=/workspace \
  -e SOURCE=/workspace \
  -e WORK_DIR_REL=diag-137 \
  -e CURSOR_API_KEY="${CURSOR_API_KEY:-}" \
  --shm-size=1g \
  cursor-agent:latest python /src/agent.py 2>&1 | tail -30; then
  echo "[PASS] Agent completed (or failed with non-137)"
else
  code=$?
  echo "[FAIL] Agent exited with $code (137 = SIGKILL)"
  exit $code
fi
fi

if run_step 3; then
echo ""
echo "=== Step 3: cursor-cli DIRECTLY (no Python agent) ==="
echo "If this gets 137, cursor-cli or container is the cause; our agent is not involved."
mkdir -p "$WS"
if docker run --rm \
  -v "$WS:/workspace" \
  -e CURSOR_API_KEY="${CURSOR_API_KEY:-}" \
  --shm-size=1g \
  cursor-agent:latest cursor run -p -f --output-format stream-json --mode plan --workspace /workspace "say hello" 2>&1 | tail -25; then
  echo "[PASS] cursor-cli ran directly without 137"
else
  code=$?
  echo "[FAIL] cursor-cli exited $code (137=SIGKILL) - cursor-cli or Docker, not our agent"
fi
fi

if run_step 4; then
echo ""
echo "=== Step 4: run_container via Python (same as bro run) ==="
echo "If this gets 137, the issue is Python docker API or run_container."
WS="$(cd "$WS" && pwd)"
mkdir -p "$WS/diag-py"
echo '{"objective":"say hello","instructions":[],"entrypoint":".","mode":"plan"}' > "$WS/diag-py/task.json"
export CURSOR_API_KEY="${CURSOR_API_KEY:-}"
export DIAG_WS="$WS"
if ( python - << 'PYEOF'
import os
from pathlib import Path
import sys
sys.path.insert(0, os.getcwd())
from broker.agent.docker import run_container
ws = Path(os.environ["DIAG_WS"]).resolve()
ws.mkdir(parents=True, exist_ok=True)
(ws / "diag-py").mkdir(exist_ok=True)
(ws / "diag-py" / "task.json").write_text(
    '{"objective":"say hello","instructions":[],"entrypoint":".","mode":"plan"}'
)
run_container("agent-1", "diag", task_id="diag", workspace=ws, work_dir_rel="diag-py", source=ws)
PYEOF
) 2>&1; then
  echo "[PASS] run_container completed"
else
  code=$?
  echo "[FAIL] run_container exited $code (137=Python run_container/docker API)"
fi
fi

if run_step 5; then
echo ""
echo "=== Step 5: bro run (same as user command) ==="
echo "If this gets 137, the issue is in bro run / run_agents flow (not run_container)."
if bro run agent-1 -w "$WS" -s "$WS" -o "say hello" --api-key "${CURSOR_API_KEY:-}" 2>&1; then
  echo "[PASS] bro run completed"
else
  code=$?
  echo "[FAIL] bro run exited $code"
fi
fi

if run_step 6; then
echo ""
echo "=== Step 6: bro submit (minimal worker, say hello) ==="
echo "If this gets 137, the issue is in bro submit flow (TUI/threading or submit logic)."
if bro submit workers/diag-say-hello.json --auto -w "$WS" -s "$WS" --output-format plain 2>&1; then
  echo "[PASS] bro submit completed"
else
  code=$?
  echo "[FAIL] bro submit exited $code (137 = submit/TUI path)"
fi
fi

if run_step 7a; then
echo ""
echo "=== Step 7a: Two containers in parallel (sleep 30, mem=512m each) ==="
echo "If 137, generic parallel container issue. If PASS, 137 is bro-subtask/cursor-agent specific."
cleanup_7a() { docker rm -f diag-7a-a diag-7a-b 2>/dev/null || true; }
trap cleanup_7a EXIT
docker run -d --name diag-7a-a --memory=512m cursor-agent:latest sleep 30
docker run -d --name diag-7a-b --memory=512m cursor-agent:latest sleep 30
CODE_A=$(docker wait diag-7a-a)
CODE_B=$(docker wait diag-7a-b)
if [[ "$CODE_A" == "0" && "$CODE_B" == "0" ]]; then
  trap - EXIT
  cleanup_7a
  echo "[PASS] Two parallel containers (512m each) completed"
else
  echo "[FAIL] diag-7a-a exit=$CODE_A diag-7a-b exit=$CODE_B"
  [[ "$CODE_A" != "0" ]] && docker inspect diag-7a-a --format 'diag-7a-a OOMKilled={{.State.OOMKilled}}' 2>/dev/null || true
  [[ "$CODE_B" != "0" ]] && docker inspect diag-7a-b --format 'diag-7a-b OOMKilled={{.State.OOMKilled}}' 2>/dev/null || true
  cleanup_7a
  trap - EXIT
  exit 1
fi
fi

if run_step 7; then
echo ""
echo "=== Step 7: bro submit -p (parallel, host creates cursor-agent directly) ==="
if bro submit workers/diag-say-hello-parallel.json --auto -p -w "$WS" -s "$PROJ_ROOT" --output-format plain 2>&1; then
  echo "[PASS] bro submit -p completed"
else
  code=$?
  echo "[FAIL] bro submit -p exited $code"
fi
fi

if run_step 8; then
echo ""
echo "=== Step 8: bro submit with TUI (default, needs TTY) ==="
echo "If 137, TUI/threading may be the cause. Runs with pseudo-TTY via script."
if script -q /dev/null bro submit workers/diag-say-hello.json --auto -w "$WS" -s "$WS" <<< "" 2>&1; then
  echo "[PASS] bro submit (TUI) completed"
else
  code=$?
  echo "[FAIL] bro submit (TUI) exited $code"
fi
fi

if run_step 9; then
echo ""
echo "=== Step 9: bro submit test-worktree-manager (no -p) ==="
echo "If 137, test-worktree-manager worker or its structure is the trigger."
if bro submit workers/test-worktree-manager.json --auto --fresh 0 -w "$WS" -s "$PROJ_ROOT" --output-format plain 2>&1; then
  echo "[PASS] bro submit test-worktree-manager completed"
else
  code=$?
  echo "[FAIL] bro submit test-worktree-manager exited $code"
fi
fi

if run_step 10; then
echo ""
echo "=== Step 10: bro submit with -s /tmp/empty-source ==="
echo "If 137, source path /tmp/empty-source or its handling is the trigger."
mkdir -p /tmp/empty-source
if bro submit workers/diag-say-hello.json --auto -w "$WS" -s /tmp/empty-source --output-format plain 2>&1; then
  echo "[PASS] bro submit -s /tmp/empty-source completed"
else
  code=$?
  echo "[FAIL] bro submit -s /tmp/empty-source exited $code"
fi
fi

if run_step 11; then
echo ""
echo "=== Step 11: full user command (test-worktree-manager -p -s /tmp/empty-source) ==="
echo "If 137, this replicates the original failing scenario."
mkdir -p /tmp/empty-source
if bro submit workers/test-worktree-manager.json --fresh 0 -p -w "$WS" -s /tmp/empty-source --output-format plain 2>&1; then
  echo "[PASS] full user command completed"
else
  code=$?
  echo "[FAIL] full user command exited $code"
fi
fi

echo ""
echo "=== WORKAROUND: Run without Docker (--local): ==="
echo "  bro run -w $WS -s $WS -o 'say hello' --local --api-key \$CURSOR_API_KEY"
