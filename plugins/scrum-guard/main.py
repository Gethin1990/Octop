"""Scrum Guard — scope gate on every tool call (kind=hook).

What this layer can and cannot do, honestly:

* **Can** stop the great majority of accidental scope creep: an agent that was
  told "don't touch main" but decides to run ``git commit -m ...`` anyway.
* **Can** keep read-only roles read-only, since the tool name is right there in
  ``request.tool_call``.
* **Cannot** be the security boundary. It sees tool calls at this agent's level
  only; a sub-agent, an MCP server, or a shell one-liner that shells out to
  another program can slip past any argument regex. That is why the board
  itself also refuses ``task_submit`` without a valid lease: the guard reduces
  noise, the server enforces.

Three failure modes are chosen deliberately:

* role lookup failure keeps the **last known role** for a grace window and only
  then fails open with a warning — a board outage must not brick every agent,
  but it also must not silently turn a read-only PO into a full-access one;
* a tool name we do not recognise is never denied (the deny lists are explicit,
  following deepagents' real tool names: ``write_file``/``edit_file``/``delete``
  /``execute``);
* the argument patterns fail **closed** — ``git push --force`` is never an
  accident worth risking.

The deny lists are keyed on the tool names the agent actually has. Checking a
name that does not exist is not a safety property, it is decoration: ``delete``
and ``execute`` were missing from the first draft and both can destroy work.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from octop_harness.plugins import AgentMiddleware, PluginContext

logger = logging.getLogger("octop.plugins.scrum_guard")

_RULES_PATH = Path(__file__).with_name("guard.json")
_DEFAULT_CACHE = "~/.octop/scrum-guard-roles.json"
_role_cache: dict[str, tuple[float, str | None]] = {}


def _rules() -> dict[str, Any]:
    try:
        return json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a broken config must not brick the hook
        logger.warning("scrum-guard: cannot read guard.json, using empty rules")
        return {}


def _ttl(rules: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(rules.get(key) or default)
    except (TypeError, ValueError):
        return default


def _cache_path() -> Path:
    return Path(os.environ.get("SCRUM_GUARD_CACHE", _DEFAULT_CACHE)).expanduser()


def _recall(agent_id: str) -> str | None:
    """Last known role from disk, so a board outage is not an amnesty."""
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent/garbled cache is not fatal
        return None
    entry = raw.get(agent_id)
    if not isinstance(entry, dict):
        return None
    role = entry.get("role")
    return str(role) if role else None


def _remember(agent_id: str, role: str, now: float) -> None:
    path = _cache_path()
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except Exception:  # noqa: BLE001
        existing = {}
    existing[agent_id] = {"role": role, "at": now}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - a read-only home must not break tools
        logger.debug("scrum-guard: cannot persist role cache: %s", exc)


def _context(request: Any = None) -> tuple[str, str]:
    """Resolve (agent_id, thread_id) from the graph config.

    ``langgraph.config.get_config()`` is the path harness middleware uses; the
    ToolCallRequest runtime config is the fallback for sync invocations that run
    outside a langgraph context.
    """
    cfg: dict[str, Any] = {}
    try:
        from langgraph.config import get_config

        cfg = (get_config() or {}).get("configurable") or {}
    except Exception:  # noqa: BLE001 - not every call site has a config context
        cfg = {}
    if not cfg.get("agent_id") and request is not None:
        runtime = getattr(request, "runtime", None)
        runtime_cfg = getattr(runtime, "config", None)
        if isinstance(runtime_cfg, dict):
            cfg = runtime_cfg.get("configurable") or cfg
    return str(cfg.get("agent_id") or ""), str(cfg.get("thread_id") or "")


def _board_url() -> str:
    return os.environ.get("SCRUM_BOARD_URL", "http://127.0.0.1:8099").rstrip("/")


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """Always talk to the board directly.

    The shell environment here carries ``HTTP_PROXY`` for outbound traffic; a
    loopback call must never be routed through it (that is the difference
    between "role unknown → fail open" and a working guard).
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_role(agent_id: str) -> str | None:
    body = json.dumps({"params": {}}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed loopback scheme
        f"{_board_url()}/rpc/whoami",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Octop-Agent": agent_id,
            "X-Scrum-Token": os.environ.get("SCRUM_TOKEN", ""),
        },
    )
    try:
        with _no_proxy_opener().open(request, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("scrum-guard: role lookup failed for %s: %s", agent_id, exc)
        return None
    role = (payload.get("data") or {}).get("role")
    return str(role) if role else None


def _role_for(agent_id: str) -> str | None:
    """Ask the board who this agent is; cache briefly, then fall back to disk."""
    if not agent_id:
        return None
    rules = _rules()
    ttl = _ttl(rules, "role_cache_ttl_s", 60.0)
    stale_ttl = _ttl(rules, "role_cache_stale_s", 900.0)
    now = time.time()
    cached = _role_cache.get(agent_id)
    if cached and now - cached[0] < ttl:
        return cached[1]
    role = _fetch_role(agent_id)
    if role is not None:
        _role_cache[agent_id] = (now, role)
        _remember(agent_id, role, now)
        return role
    # Board unreachable. Reuse the last known role inside the grace window
    # instead of silently granting full access to a read-only identity.
    try:
        entry = json.loads(_cache_path().read_text(encoding="utf-8")).get(agent_id) or {}
        age = now - float(entry.get("at") or 0)
    except Exception:  # noqa: BLE001
        age = float("inf")
    stale = _recall(agent_id)
    if stale and age <= stale_ttl:
        logger.warning(
            "scrum-guard: using cached role %s for %s (board unreachable, age %.0fs)",
            stale, agent_id, age,
        )
        _role_cache[agent_id] = (now, stale)
        return stale
    _role_cache[agent_id] = (now, None)
    return None


def _arg_strings(value: Any, depth: int = 0) -> list[str]:
    """Flatten an argument tree into strings for pattern matching."""
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_arg_strings(item, depth + 1))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_arg_strings(item, depth + 1))
        return out
    return []


def _matches_any(texts: list[str], patterns: list[str]) -> str | None:
    for pattern in patterns:
        try:
            regex = re.compile(pattern)
        except re.error:
            continue
        for text in texts:
            if regex.search(text):
                return pattern
    return None


def block_reason(tool_name: str, args: Any, role: str | None, rules: dict[str, Any]) -> str | None:
    """Return a human-readable denial, or ``None`` to let the call through."""
    # 1. Per-role tool denials. ``deny_tools_by_role`` is the whole table: a role
    #    that is missing simply has no blanket denial (dev). It used to be gated
    #    behind ``read_only_roles``, which meant "read-only" and "may not write
    #    product files" were the same switch -- and the SM, which must run
    #    verification commands, ended up unable to read evidence at all. A
    #    blocked DoD is worse than a noisy shell: see task T-4c39edf4.
    denied = set((rules.get("deny_tools_by_role") or {}).get(role) or [])
    if tool_name in denied:
        if role in (rules.get("read_only_roles") or []):
            return (
                f"scrum-guard 拒绝执行：角色 {role} 是只读的，不能调用 {tool_name}。\n"
                "你需要改动的东西请通过 board 提成卡片，交给 dev 执行。"
            )
        return (
            f"scrum-guard 拒绝执行：角色 {role} 不能调用 {tool_name}。\n"
            "这一角色可以读和验证，但不能改产品文件；判定结果请通过 board_* 工具记录。"
        )
    # 2. Destructive command patterns, scoped to the tools that can run them.
    #    Evidence text on board_submit legitimately mentions "git commit"; a
    #    global substring match would reject honest reports.
    pattern_tools = set(rules.get("arg_pattern_tools") or [])
    if not pattern_tools or tool_name in pattern_tools:
        hit = _matches_any(_arg_strings(args), list(rules.get("deny_arg_patterns") or []))
        if hit:
            return (
                f"scrum-guard 拒绝执行：参数命中禁止模式 /{hit}/。\n"
                "本项目的约束是「只在任务分支上工作，不 commit、不 push」。"
                "如果你认为需要这个操作，请调用 board_block 说明原因交人工裁决。"
            )
    # 3. Protected paths, for tools that can read or write them.
    path_tools = set(rules.get("path_guard_tools") or [])
    if not path_tools or tool_name in path_tools:
        texts = _arg_strings(args)
        for path in rules.get("protected_paths") or []:
            if any(path in text for text in texts):
                return f"scrum-guard 拒绝执行：目标路径 {path} 属于保护范围。"
    return None


def _deny(request: Any, reason: str) -> Any:
    from langchain_core.messages import ToolMessage

    tool_call = getattr(request, "tool_call", None) or {}
    return ToolMessage(
        content=reason,
        tool_call_id=str(tool_call.get("id") or ""),
        status="error",
    )


class GuardMiddleware(AgentMiddleware[Any, Any]):
    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = str(tool_call.get("name") or "")
        agent_id, _ = _context(request)
        role = _role_for(agent_id)
        reason = block_reason(tool_name, tool_call.get("args") or {}, role, _rules())
        if reason is None:
            return await handler(request)
        logger.warning("scrum-guard blocked %s for agent=%s role=%s", tool_name, agent_id, role)
        return _deny(request, reason)

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        # Sync path: same decision. Skipping the role lookup here would make the
        # read-only roles unenforced whenever a caller uses ``invoke()``.
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = str(tool_call.get("name") or "")
        agent_id, _ = _context(request)
        role = _role_for(agent_id)
        reason = block_reason(tool_name, tool_call.get("args") or {}, role, _rules())
        if reason is None:
            return handler(request)
        logger.warning("scrum-guard blocked %s for agent=%s role=%s", tool_name, agent_id, role)
        return _deny(request, reason)


def setup(ctx: PluginContext) -> None:
    # Priority 50: runs outside Octop's own guards so our refusal happens first.
    ctx.middleware(GuardMiddleware(), priority=50)
