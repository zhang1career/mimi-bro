"""
技能注册表：从知识库 API /knowledge/some_like 加载技能，提供「任务可指派给谁」「任务如何指派」「如何调用」的查询接口。

知识库条目结构：
- title: skill_id
- description: 完整 skill JSON（{ executors, invocation }）
- source_type: "mimi-bro"

环境变量 KNOW_API_URL 必须在 .env 中配置（如 http://localhost:8000/api/know）。
API 不可用或返回空时抛出错误退出，不再使用本地 data/skills.json。
"""
from __future__ import annotations

import json
import os
import shlex
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from broker.utils.env_util import load_dotenv_from_dir

_SkillRegistry = dict[str, dict[str, Any]]
_registry: _SkillRegistry | None = None
_console_callback: Callable[[str], None] | None = None


def set_console_callback(cb: Callable[[str], None] | None) -> None:
    """Deprecated: 请改用 load_skill_registry(..., on_message=...) 显式传入回调，避免全局状态。"""
    global _console_callback
    _console_callback = cb


def _console(msg: str) -> None:
    if _console_callback:
        try:
            _console_callback(msg)
        except Exception:
            pass


def _project_root() -> Path:
    """项目根目录（与 data/skills.json 曾用路径一致）"""
    return Path(__file__).resolve().parents[3]


def _get_know_api_url() -> str:
    load_dotenv_from_dir(_project_root())
    url = (os.environ.get("KNOW_API_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError(
            "KNOW_API_URL 未配置。请在 .env 中设置 KNOW_API_URL（如 http://localhost:8000/api/know）"
        )
    return url


def _fetch_some_like(summary: str) -> list[dict[str, Any]]:
    """
    调用 GET /knowledge/some_like?summary=... 返回知识列表。

    注意：此接口仅用于 main.py 中的业务命令（语义搜索）。
    后台命令（如 skill sync）应使用 _fetch_by_title() 按 title 精确查询。
    """
    base = _get_know_api_url()
    q = summary.strip()
    if not q:
        return []
    encoded = urllib.parse.quote(q, safe="")
    url = f"{base}/knowledge/some_like?summary={encoded}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise RuntimeError(f"知识库 API 调用失败: {url} - {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"知识库 API 响应解析失败: {e}") from e

    if data.get("errorCode") != 0:
        raise RuntimeError(
            f"知识库 API 返回错误: {data.get('message', 'unknown')}"
        )
    items = data.get("data")
    if not isinstance(items, list):
        return []
    return items


def _fetch_by_title(title: str) -> list[dict[str, Any]]:
    """
    调用 GET /knowledge?title=... 按标题前缀匹配查询。

    用于后台命令（如 skill sync, skill list）按 skill_id 精确查询。
    title 参数会进行左对齐前缀匹配。
    """
    base = _get_know_api_url()
    t = title.strip()
    if not t:
        return []
    encoded = urllib.parse.quote(t, safe="")
    url = f"{base}/knowledge?title={encoded}&limit=100"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise RuntimeError(f"知识库 API 调用失败: {url} - {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"知识库 API 响应解析失败: {e}") from e

    if data.get("errorCode") != 0:
        raise RuntimeError(
            f"知识库 API 返回错误: {data.get('message', 'unknown')}"
        )
    result = data.get("data")
    if isinstance(result, dict):
        items = result.get("data")
        if isinstance(items, list):
            return items
    return []


def _api_request(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发起知识库 API 请求。path 不含前缀 /api/know。"""
    base = _get_know_api_url()
    url = f"{base}{path}"
    data_bytes = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url,
        data=data_bytes,
        method=method,
        headers={"Content-Type": "application/json"} if data_bytes else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            err_data = json.loads(body)
            msg = err_data.get("message", body) or str(e)
        except json.JSONDecodeError:
            msg = body or str(e)
        raise RuntimeError(f"知识库 API 调用失败 ({method} {path}): {msg}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"知识库 API 调用失败: {url} - {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"知识库 API 响应解析失败: {e}") from e


def create_skill(
        title: str,
        description: str | None = None,
        content: str | None = None,
        source_type: str = "mimi-bro",
) -> dict[str, Any]:
    """创建技能（对应 POST /knowledge）。无需人工确认。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("title 不能为空")
    if len(title) > 512:
        raise ValueError("title 最大 512 字符")
    if source_type and len(source_type) > 64:
        raise ValueError("source_type 最大 64 字符")
    payload: dict[str, Any] = {"title": title}
    if description is not None:
        payload["description"] = description
    if content is not None:
        payload["content"] = content
    if source_type:
        payload["source_type"] = source_type
    data = _api_request("POST", "/knowledge", body=payload)
    if data.get("errorCode") != 0:
        raise RuntimeError(
            f"知识库 API 返回错误: {data.get('message', 'unknown')}"
        )
    return data.get("data") or {}


def update_skill(
        entity_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        content: str | None = None,
        source_type: str | None = None,
) -> dict[str, Any]:
    """更新技能（对应 PUT /knowledge/{entity_id}）。调用前需人工确认。"""
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if description is not None:
        payload["description"] = description
    if content is not None:
        payload["content"] = content
    if source_type is not None:
        payload["source_type"] = source_type
    if not payload:
        raise ValueError("至少提供一个要更新的字段: title, description, content, source_type")
    data = _api_request("PUT", f"/knowledge/{entity_id}", body=payload)
    if data.get("errorCode") != 0:
        raise RuntimeError(
            f"知识库 API 返回错误: {data.get('message', 'unknown')}"
        )
    return data.get("data") or {}


def get_skill_entity_id(skill_id: str) -> int:
    """按 skill_id（title）查询，返回对应的知识 entity_id。"""
    items = _fetch_some_like(skill_id)
    for k in items:
        title = k.get("title")
        if title and str(title).strip() == skill_id.strip():
            eid = k.get("id")
            if eid is not None and isinstance(eid, (int, float)):
                return int(eid)
            raise SkillNotFoundError(
                f"技能 '{skill_id}' 在知识库中找到但无有效 id"
            )
    raise SkillNotFoundError(f"技能 '{skill_id}' 未在知识库中找到")


def _knowledge_to_entry(knowledge: dict) -> dict[str, Any]:
    """
    从知识条目解析出 skill entry。

    knowledge 字段映射（见 docs/SKILL.md）：
    - title → skill.id
    - content → skill.description（文字描述）
    - description → JSON {match_rules, invocation, executors}

    返回合并后的 entry：{
        "id": str,
        "description": str,  # 文字描述
        "match_rules": dict | None,
        "invocation": dict | None,
        "executors": dict | None,
    }
    """
    skill_id = knowledge.get("title") or ""
    text_description = knowledge.get("content") or ""

    desc = knowledge.get("description")
    parsed: dict[str, Any] = {}
    if desc and isinstance(desc, str):
        try:
            parsed = json.loads(desc)
            if not isinstance(parsed, dict):
                parsed = {}
        except json.JSONDecodeError:
            parsed = {}

    return {
        "id": skill_id,
        "description": text_description,
        "match_rules": parsed.get("match_rules"),
        "invocation": parsed.get("invocation"),
        "executors": parsed.get("executors"),
    }


class SkillNotFoundError(RuntimeError):
    """技能未在知识库中找到"""


def load_skill_registry(
        skill_refs: list[str] | None = None,
        on_message: Callable[[str], None] | None = None,
) -> _SkillRegistry:
    """从知识库 API 加载技能，初始化内部缓存。
    skill_refs: 要加载的技能 ID 列表，按 summary 查询（如 [backend-dev]）；缺省时用 mimi-bro 拉全量（兼容旧行为）。
    on_message: 加载过程中的消息回调（如 API 结果摘要）；优先于 set_console_callback。显式传入可避免全局状态。
    path 参数已废弃，保留仅为兼容。"""
    global _registry
    log = on_message or _console_callback or (lambda m: None)
    if skill_refs:
        summaries_to_fetch = list(skill_refs)
    else:
        summaries_to_fetch = ["mimi-bro"]
    all_items: list[tuple[str, dict[str, Any]]] = []
    seen_titles: set[str] = set()
    for summary in summaries_to_fetch:
        try:
            items = _fetch_some_like(summary)
        except Exception as e:
            log(f"Skill API error (summary={summary!r}): {e}")
            raise
        if not items:
            log(f"Skill API returned no results (summary={summary!r}).")
            if skill_refs:
                raise RuntimeError(
                    f"知识库 API 返回空结果（summary={summary!r}）。请确认 KNOW_API_URL 正确且知识库中已有对应技能。"
                )
            continue
        log(f"Skill API: {len(items)} entries for {summary!r}.")
        for k in items:
            title = k.get("title")
            if not title or not isinstance(title, str):
                continue
            skill_id = str(title).strip()
            if not skill_id or skill_id in seen_titles:
                continue
            seen_titles.add(skill_id)
            try:
                entry = _knowledge_to_entry(k)
            except ValueError:
                continue
            all_items.append((skill_id, entry))
    if not all_items:
        if skill_refs:
            raise RuntimeError(
                "知识库 API 未返回有效技能数据。请确认 KNOW_API_URL 正确且知识库中已有对应技能。"
            )
        raise RuntimeError(
            "知识库 API 返回空结果（summary=mimi-bro）。请确认 KNOW_API_URL 正确且知识库中已有 mimi-bro 技能数据。"
        )
    _registry = {}
    for skill_id, entry in all_items:
        _registry[skill_id] = entry
    return _registry


def _ensure_loaded() -> _SkillRegistry:
    if _registry is None:
        load_skill_registry()
    return _registry or {}


def _ensure_skill(skill_id: str) -> dict[str, Any]:
    """确保 skill_id 在缓存中；若不在则从 API 按 skill_id 查询并合并。"""
    reg = _ensure_loaded()
    if skill_id in reg:
        return reg[skill_id]
    try:
        items = _fetch_some_like(skill_id)
    except Exception as e:
        _console(f"Skill API error for '{skill_id}': {e}")
        raise
    if not items:
        _console(f"Skill '{skill_id}': API returned no results.")
        raise SkillNotFoundError(f"技能 '{skill_id}' 未在知识库中找到")
    if len(items) > 1:
        _console(f"Skill '{skill_id}': API returned {len(items)} results, using first match.")
    for k in items:
        title = k.get("title")
        if not title:
            continue
        sid = str(title).strip()
        if sid == skill_id:
            entry = _knowledge_to_entry(k)
            reg[sid] = entry
            return entry
    _console(f"Skill '{skill_id}': no exact match in {len(items)} result(s).")
    raise SkillNotFoundError(f"技能 '{skill_id}' 未在知识库中找到")


def list_skills() -> list[str]:
    """列出已缓存的技能 ID。"""
    return list(_ensure_loaded().keys())


def get_skill_info(skill_id: str) -> dict[str, Any]:
    """
    获取 skill 的完整信息。

    Returns:
        {
            "id": str,
            "description": str,  # 文字描述
            "match_rules": dict | None,
            "invocation": dict | None,
            "executors": dict | None,
        }
    """
    return _ensure_skill(skill_id)


def get_all_skill_infos() -> dict[str, dict[str, Any]]:
    """
    获取所有已加载 skill 的完整信息。

    Returns:
        {skill_id: skill_info, ...}
    """
    return dict(_ensure_loaded())


def _executors_for_skill(skill: str) -> dict[str, Any]:
    """获取技能对应的执行者映射。"""
    entry = _ensure_skill(skill)
    return entry.get("executors") or {}


def get_assignees(skill: str) -> list[dict[str, Any]]:
    """
    查询某技能可指派给谁。返回列表，每项为 {executor, mode, method, ...}。
    """
    executors = _executors_for_skill(skill)
    result = []
    for executor_id, cfg in executors.items():
        if isinstance(cfg, dict) and ("mode" in cfg or "method" in cfg):
            result.append({"executor": executor_id, **(cfg or {})})
    return result


def get_assignment_how(skill: str, executor: str) -> dict[str, Any] | None:
    """查询某技能、某执行者的指派方法。返回 {mode, method, ...} 或 None。"""
    executors = _executors_for_skill(skill)
    cfg = executors.get(executor)
    if cfg is None or not isinstance(cfg, dict):
        return None
    return dict(cfg)


def _substitute_placeholders(val: Any, ctx: dict[str, str]) -> Any:
    """递归替换字符串中的 {key} 占位符"""
    if isinstance(val, dict):
        return {k: _substitute_placeholders(v, ctx) for k, v in val.items()}
    if isinstance(val, list):
        return [_substitute_placeholders(item, ctx) for item in val]
    if isinstance(val, str):
        for k, v in ctx.items():
            val = val.replace("{" + k + "}", str(v))
        return val
    return val


def get_invocation(
        skill: str,
        src_path: str = "src",
        **params: str,
) -> str | dict[str, Any] | None:
    """
    查询某技能的调用方法。返回类型因 invocation.type 而异：
    - bro_submit / shell: 返回完整命令字符串
    - http: 返回请求描述 dict {method, url, headers?, body?}
    - 无 invocation: 返回 None
    """
    entry = _ensure_skill(skill)
    inv = entry.get("invocation")
    if not inv or not isinstance(inv, dict):
        return None

    ctx: dict[str, str] = {
        "src_path": src_path,
        "skill_id": skill,
        "params_json": json.dumps(params, ensure_ascii=False),
        **params,
    }
    inv_type = inv.get("type", "bro_submit")

    if inv_type == "bro_submit":
        return _parse_bro_submit(ctx, inv, params)

    if inv_type == "shell":
        return _parse_shell(ctx, inv)

    if inv_type == "http":
        return _parse_http(ctx, inv)

    return None


def _parse_bro_submit(ctx, inv, params):
    task_file = inv.get("task_file")
    if not task_file:
        return None
    parts = ["bro", "submit", task_file]
    if inv.get("local"):
        parts.append("--local")
    ws = inv.get("workspace")
    if ws:
        parts.extend(["-w", _substitute_placeholders(ws, ctx)])
    res = _substitute_placeholders(inv.get("source", "{src_path}"), ctx)
    parts.extend(["-s", res])
    for k, v in params.items():
        parts.extend(["--arg", f"{k}={shlex.quote(v)}"])
    return " ".join(parts)


def _parse_shell(ctx, inv):
    template = inv.get("template")
    if not template:
        return None
    return _substitute_placeholders(template, ctx)


def _parse_http(ctx, inv):
    method = inv.get("method", "GET")
    url = inv.get("url")
    if not url:
        return None
    result: dict[str, Any] = {
        "method": method,
        "url": _substitute_placeholders(url, ctx),
    }
    if "headers" in inv:
        result["headers"] = _substitute_placeholders(dict(inv["headers"]), ctx)
    if "body" in inv:
        body = inv["body"]
        if isinstance(body, (dict, list)):
            result["body"] = _substitute_placeholders(body, ctx)
        else:
            result["body"] = _substitute_placeholders(str(body), ctx)
    return result
