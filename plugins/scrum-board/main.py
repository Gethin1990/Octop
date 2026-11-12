"""Scrum Board — the agent's side of the control plane (kind=tool).

The tools here are deliberately thin: the board is the authority, this plugin is
a client. Three constraints from the harness/plugin contract drive the shape of
this file:

* every tool returns ``str`` — no exceptions escape, because an exception would
  abort the whole turn instead of letting the model read the reason;
* the agent identity comes from ``configurable.agent_id`` (injected by Octop),
  never from an argument, so a model cannot claim to be someone else;
* ``NOT_YOURS`` is a first-class answer. A duplicate wake must exit in a few
  hundred tokens, not by re-implementing the task.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from octop_harness.plugins import PluginContext, get_tool_config

_DEFAULT_URL = "http://127.0.0.1:8099"


def _context() -> tuple[str, str]:
    """Return ``(agent_id, thread_id)`` from Octop's runnable config."""
    try:
        from langgraph.config import get_config

        cfg = get_config().get("configurable") or {}
        return str(cfg.get("agent_id") or ""), str(cfg.get("thread_id") or "")
    except Exception:  # noqa: BLE001 - never break a turn over telemetry
        return "", ""


def _settings() -> tuple[str, str]:
    url = os.environ.get("SCRUM_BOARD_URL") or _DEFAULT_URL
    token = os.environ.get("SCRUM_TOKEN") or ""
    try:  # outside a runnable context (tests, CLI probes) get_config raises
        cfg = get_tool_config("board_claim") or {}
        url = str(cfg.get("board_url") or url)
        token = str(cfg.get("board_token") or token)
    except Exception:  # noqa: BLE001
        pass
    return url.rstrip("/"), token


def _rpc(method: str, **params: Any) -> dict[str, Any]:
    url, token = _settings()
    agent_id, _ = _context()
    body = json.dumps({"params": params}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed localhost scheme
        f"{url}/rpc/{method}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Octop-Agent": agent_id,
            "X-Scrum-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "reason": f"http_{exc.code}", "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "board_unreachable", "detail": str(exc)[:300]}


def _fmt_claim(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return f"BOARD_ERROR {payload.get('reason')} {payload.get('detail', '')}".strip()
    data = payload.get("data") or {}
    status = str(data.get("status") or "")
    if status != "OK":
        return (
            f"{status}\n"
            "结束本轮：不要修改任何文件，只回复 NOOP。若 status 是 NOT_QUEUED，"
            "说明这张卡已经不在执行队列（可能已完成或被人接管）。"
        )
    spec = str(data.get("spec") or "").strip() or "(无附加规格，按卡片标题执行)"
    return (
        f"OK {data.get('task_id')} lease={data.get('lease_id')} attempt={data.get('attempt')}\n"
        f"branch: {data.get('branch')}\n"
        "--- SPEC ---\n"
        f"{spec}\n"
        "--- RULES ---\n"
        "1) 只在上面这条 branch 上提交，不要碰 main，不要 git commit，不要 git push。\n"
        "2) 长任务每完成一个阶段调用 board_heartbeat 续租。\n"
        "3) 全部做完调用 board_submit，贴原始测试输出；不要自己宣布完成。\n"
        "4) 做不下去调用 board_block，说明卡在哪，不要硬撑。"
    )


async def board_claim(task_id: str) -> str:
    """认领任务并取回规格。

    Args:
        task_id: 唤醒消息里给出的 TASK id。
    """
    return _fmt_claim(_rpc("task_claim", task_id=task_id))


async def board_heartbeat(task_id: str, lease_id: str) -> str:
    """续租，证明任务还在推进。

    Args:
        task_id: 任务 id。
        lease_id: board_claim 返回的 lease。
    """
    payload = _rpc("task_heartbeat", task_id=task_id, lease_id=lease_id)
    if not payload.get("ok"):
        return f"HEARTBEAT_REJECTED {payload.get('reason')} {payload.get('detail', '')}".strip()
    alive = bool((payload.get("data") or {}).get("alive"))
    return "HEARTBEAT_OK" if alive else "LEASE_EXPIRED 立刻停止工作并回复 NOOP"


async def board_submit(
    task_id: str,
    lease_id: str,
    summary: str,
    tests_output: str,
    files_changed: str,
    git_sha: str = "",
) -> str:
    """提交增量，附带证据包。提交后任务进入 DoD 检查，不是完成。

    Args:
        task_id: 任务 id。
        lease_id: 当前租约。
        summary: 你改了什么、为什么这么改。
        tests_output: 原始测试输出（命令 + 结果），不要写"测试通过"四个字。
        files_changed: 变更文件清单，逗号分隔。
        git_sha: 提交 sha；不许 commit 时留空，用分支名代替。
    """
    evidence = {
        "summary": summary,
        "tests_output": tests_output,
        "files_changed": files_changed,
    }
    if git_sha:
        evidence["git_sha"] = git_sha
    payload = _rpc(
        "task_submit",
        task_id=task_id,
        lease_id=lease_id,
        evidence=evidence,
        idem_key=f"{task_id}:{lease_id}",
    )
    if not payload.get("ok"):
        return f"SUBMIT_REJECTED {payload.get('reason')} {payload.get('detail', '')}".strip()
    data = payload.get("data") or {}
    if data.get("duplicate"):
        return "SUBMIT_DUPLICATE 已经收到过这次提交，不要重复提交，回复 NOOP。"
    return "SUBMITTED 已记录证据，等待 DoD 检查与人工验收。不要重复提交。"


async def board_block(task_id: str, reason: str, detail: str = "") -> str:
    """弃权：说明为什么这张卡做不下去。

    Args:
        task_id: 任务 id。
        reason: 归类，如 spec_unclear / dependency_missing / tests_broken / out_of_scope。
        detail: 具体现象，一两句。
    """
    payload = _rpc("task_block", task_id=task_id, reason=reason, detail=detail)
    if not payload.get("ok"):
        return f"BLOCK_REJECTED {payload.get('reason')}".strip()
    return "BLOCKED 已上报。停止本轮，等人工裁决。"


async def board_view(limit: int = 15) -> str:
    """查看当前看板（只读）。

    Args:
        limit: 最多返回多少条。
    """
    payload = _rpc("board_view", limit=limit)
    if not payload.get("ok"):
        return f"BOARD_ERROR {payload.get('reason')}"
    data = payload.get("data") or {}
    lines = [f"{t['state']:<10} {t['task_id']:<12} {t['agent_id'] or '-':<8} {t['title'][:40]}"
             for t in (data.get("tasks") or [])]
    counts = json.dumps(data.get("counts") or {}, ensure_ascii=False)
    return "\n".join([*lines, f"counts={counts} review_depth={data.get('review_depth')}"])


async def board_estimate(pbi_id: str, points: float, rationale: str = "") -> str:
    """以 dev 身份给需求估故事点（只有干活的 agent 能调）。

    估点是团队职责，不是提需求的人填的表单字段。给出依据，方便审计与重估。

    Args:
        pbi_id: 需求 id。
        points: 故事点，建议用 1/2/3/5/8/13 刻度。
        rationale: 估算依据，例如「涉及 3 个模块 + 迁移脚本」。
    """
    payload = _rpc("pbi_estimate", pbi_id=pbi_id, points=points, rationale=rationale)
    if not payload.get("ok"):
        return f"ESTIMATE_REJECTED {payload.get('reason')} {payload.get('detail', '')}".strip()
    data = payload.get("data") or {}
    return (
        f"ESTIMATED {pbi_id} {data.get('points'):g} pt "
        f"(delta {data.get('delta'):g}) 已记账到燃尽图。"
    )


async def board_backlog_add(title: str, acceptance: str = "") -> str:
    """把一条需求建成可派单的卡（只有 PO 身份能调）。

    走三步：建 PBI（不估点）→ 注入当前 Sprint → 生成 task。故事点由 dev agent
    通过 board_estimate 评估，PO 不填。中间任一步被拒都会原样返回原因。

    Args:
        title: 用户故事或任务标题。
        acceptance: 验收条件，用 ; 分隔；为空会被 DoR 闸门打回给需求方修改。
    """
    criteria = [a.strip() for a in acceptance.replace("；", ";").split(";") if a.strip()]
    created = _rpc(
        "pbi_create",
        title=title,
        points=None,
        acceptance=criteria,
    )
    if not created.get("ok"):
        return f"ADD_REJECTED {created.get('reason')} {created.get('detail', '')}".strip()
    pbi_id = (created.get("data") or {}).get("pbi_id")

    injected = _rpc("pbi_add_to_sprint", pbi_id=pbi_id)
    if not injected.get("ok"):
        return (
            f"ADD_REJECTED {injected.get('reason')} {injected.get('detail', '')} "
            f"(pbi={pbi_id} 已建，但没有生效中的 Sprint)"
        ).strip()

    task = _rpc("task_create", pbi_id=pbi_id, title=title)
    if not task.get("ok"):
        return f"ADD_REJECTED {task.get('reason')} {task.get('detail', '')}".strip()
    task_id = (task.get("data") or {}).get("task_id")
    return f"ADDED {task_id} pbi={pbi_id} 已进入派单池，不要自己认领。"


def setup(ctx: PluginContext) -> None:
    ctx.tool(
        "board_claim",
        board_claim,
        description="认领任务并取回规格；返回 NOT_YOURS / NOT_QUEUED 时必须立刻结束本轮",
        config_fields=[
            {"name": "board_url", "label": "Board URL", "type": "text",
             "placeholder": "http://127.0.0.1:8099"},
            {"name": "board_token", "label": "Board token", "type": "password"},
        ],
    )
    ctx.tool("board_heartbeat", board_heartbeat, description="续租，证明任务仍在推进")
    ctx.tool("board_submit", board_submit, description="提交增量与证据包，进入 DoD 检查")
    ctx.tool("board_block", board_block, description="弃权并说明原因，交人工裁决")
    ctx.tool("board_view", board_view, description="查看看板（只读）")
    ctx.tool(
        "board_estimate",
        board_estimate,
        description="以 dev 身份给需求估故事点并写明依据（提需求的人不估点）",
    )
    ctx.tool(
        "board_backlog_add",
        board_backlog_add,
        description="以 PO 身份建卡（建 PBI → 进 Sprint → 生成 task），故事点留给 dev 估",
    )
