import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Debug: Print Python path and working directory
print(f"[agent][init] Python version: {sys.version}", flush=True)
print(f"[agent][init] Python path: {sys.path}", flush=True)
print(f"[agent][init] Working directory: {os.getcwd()}", flush=True)

try:
    from common.consts.response_const import RET_ERR, RET_INVALID_PARAM, RET_RESOURCE_NOT_FOUND, RET_THREAD_TIMEOUT, \
        RET_THREAD_ERROR
    print("[agent][init] Successfully imported common.consts.response_const", flush=True)
except ImportError as e:
    print(f"[agent][init] Failed to import common.consts.response_const: {e}", flush=True)
    print(f"[agent][init] sys.path: {sys.path}", flush=True)
    raise


def log(msg: str):
    print(msg, flush=True)


def fatal(msg: str, code=RET_ERR):
    log(f'[agent][fatal] {msg}')
    sys.exit(code)


def main():
    # ---- 读取环境变量 ----
    # WORKSPACE = 工作路径（task.json, agent.log, works/）；SOURCE = 源代码路径（供 cursor --workspace 使用）
    agent_id = os.getenv('AGENT_ID', 'unknown')
    role = os.getenv('AGENT_ROLE', 'generic')
    task_id = os.getenv('TASK_ID', 'unknown')
    workspace = Path(os.getenv('WORKSPACE', '/workspace'))
    source = Path(os.getenv('SOURCE', str(workspace)))
    
    # Cursor API 认证信息（通过环境变量传递）
    cursor_api_key = os.getenv('CURSOR_API_KEY')
    if cursor_api_key:
        # 设置 cursor-agent 使用的环境变量
        os.environ['CURSOR_API_KEY'] = cursor_api_key
        log('[agent] Cursor API key configured from environment variable')
    else:
        log('[agent] warning: CURSOR_API_KEY not set, cursor-agent may require authentication')
    
    log(f'[agent] id={agent_id} role={role} task_id={task_id}')
    log(f'[agent] workspace (work)={workspace} source={source}')

    agents_dir = workspace / 'agents'

    # Prefer workspace/agents/cursor (DESIGN); fallback to image PATH for Docker when workspace has no agents/cursor
    cursor_bin = agents_dir / 'cursor'
    if not cursor_bin.exists():
        for name in ('cursor-agent', 'cursor'):
            found = shutil.which(name)
            if found:
                cursor_bin = Path(found)
                log(f'[agent] using cursor from PATH: {cursor_bin}')
                break
        else:
            fatal(f'cursor cli not found: {agents_dir / "cursor"} (and not in PATH)', code=RET_INVALID_PARAM)

    missing_deps = []
    if missing_deps:
        fatal(f'cursor cli dependencies missing: {", ".join(missing_deps)}', code=RET_INVALID_PARAM)

    # --- 准备工作目录（Broker 可传 WORK_DIR_REL 如 works/{{task_id}}-{{run_id}}-{{role}}） ----
    work_dir_rel = os.getenv('WORK_DIR_REL', '').strip()
    if work_dir_rel:
        work_dir = workspace / work_dir_rel
    else:
        work_dir = workspace / 'works'
    work_dir.mkdir(parents=True, exist_ok=True)

    # --- 读取 task ----
    task_file = work_dir / 'task.json'
    result_file = work_dir / 'result.json'
    log_file = work_dir / 'agent.log'

    task = {}
    if task_file.exists():
        task = json.loads(task_file.read_text())
        log(f'[agent] objective: {task.get("objective")}')
    if not task:
        fatal(f'task file not found or empty: {task_file}', code=RET_RESOURCE_NOT_FOUND)

    # ---- 解析 task ----
    objective = task.get('objective', '')
    instructions = task.get('instructions', [])
    entrypoint = task.get('entrypoint', '.')
    mode = task.get('mode', 'agent')
    task_type = task.get('type', '')
    # Bootstrap: DESIGN §4.5 — constraints in payload; result must be marked generated (done below)
    if task_type == 'bootstrap':
        log('[agent] task type=bootstrap (no core modification, generated marker, no autonomous merge)')
    # Cursor CLI supports plan/ask only; map agent -> plan (execution allowed)
    if mode == 'agent':
        mode = 'plan'

    # ---- 组装 prompt ----
    prompt = objective
    if instructions:
        prompt += '\n\nConstraints:\n'
        for i in instructions:
            prompt += f'- {i}\n'

    log('[agent] invoking cursor cli')
    log(f'[agent] prompt: {objective}')

    # 输出格式：stream-json = 每条事件一行 NDJSON（会看到时断时续、大量 JSON，因 tool_call 等事件可能带完整文件内容）
    # text = 只在结束时输出最终回答，控制台中间无输出。通过 AGENT_OUTPUT_FORMAT 切换。
    output_fmt = os.getenv('AGENT_OUTPUT_FORMAT', 'stream-json').strip().lower()
    if output_fmt not in ('text', 'stream-json'):
        output_fmt = 'stream-json'
    # cursor-cli --workspace 使用源代码路径，工作路径仅用于 task.json/agent.log/works/
    source_entry = source / entrypoint
    cmd = [
        str(cursor_bin),
        'run',
        '-p',
        '-f',
        '--output-format', output_fmt,
        '--mode', mode,
        '--workspace', str(source_entry),
        prompt
    ]
    log(f'[agent] cmd: {" ".join(cmd)}')

    # --- 调用 cursor cli（完整日志写 agent.log；控制台仅输出 type=="result" 的 result 字段） ----
    # 避免 PIPE：stdout 直写文件，消除 pipe buffer 满导致 cursor 阻塞、进而被 kill 的可能
    CURSOR_CLI_TIMEOUT = 1800  # 30 min
    proc = None

    def tail_and_emit_results():
        with log_file.open('r') as f:
            while True:
                line = f.readline()
                if line:
                    try:
                        obj = json.loads(line.strip())
                        if obj.get('type') == 'result':
                            out = obj.get('result')
                            if out is not None:
                                text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                                sys.stdout.write(text if text.endswith('\n') else text + '\n')
                                sys.stdout.flush()
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif proc.poll() is not None:
                    break
                else:
                    time.sleep(0.05)

    try:
        with log_file.open('w') as log_handle:
            proc = subprocess.Popen(
                cmd,
                cwd=str(source),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        reader = threading.Thread(target=tail_and_emit_results, daemon=True)
        reader.start()
        proc.wait(timeout=CURSOR_CLI_TIMEOUT)
    except KeyboardInterrupt:
        log('[agent] interrupted by user (Ctrl+C), terminating cursor cli...')
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        raise SystemExit(130)  # 128 + SIGINT
    except subprocess.TimeoutExpired:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()
        fatal('cursor cli timeout', code=RET_THREAD_TIMEOUT)
    except Exception as _e:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log(f'[agent] cursor cli exception: {_e}')
        if log_file.exists():
            log_content = log_file.read_text()
            if log_content:
                log('[agent] cursor cli output:')
                log(log_content)
        raise

    log(f'[agent] cursor exit code: {proc.returncode}')
    
    # If cursor cli failed, read and display the log
    if proc.returncode != 0:
        if log_file.exists():
            log_content = log_file.read_text()
            if log_content:
                log(f'[agent] cursor cli error output:')
                log(log_content)

    # --- 记录结果（日志约定：与 result.json 同目录的 agent.log） ----
    # token_usage: 当前 Cursor CLI stream-json 未暴露用量，预留字段；日后可填 {"input": n, "output": n} 等
    # generated: DESIGN §4.5 — agent 产物必须标记 generated
    result = {
        'agent_id': agent_id,
        'role': role,
        'status': 'success' if proc.returncode == 0 else 'failed',
        'code': proc.returncode,
        'token_usage': None,
        'generated': True,
    }
    # --- 写入结果文件 ----
    result_file.write_text(json.dumps(result, indent=2))
    if proc.returncode == 0:
        sys.exit(0)
    else:
        sys.exit(RET_THREAD_ERROR)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        fatal(str(e), code=RET_ERR)
