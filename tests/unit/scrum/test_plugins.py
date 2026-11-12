"""Plugin contracts: manifest shape, tool registration, and the guard's verdicts.

These run without an Octop server: the plugin ``setup(ctx)`` contract only needs
an object with ``tool()``/``middleware()``, and the guard's decision function is
pure. That is deliberate — a plugin that can only be tested by booting the whole
product will only be tested by accident.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

PLUGINS = Path(__file__).resolve().parents[3] / "plugins"


class RecordingCtx:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}
        self.middlewares: list[tuple[object, int]] = []

    def tool(self, name, fn, *, description="", config_fields=None):
        self.tools[name] = {"fn": fn, "description": description,
                            "config_fields": config_fields or []}

    def middleware(self, instance, *, priority=100):
        self.middlewares.append((instance, priority))


def _load(plugin_id: str) -> ModuleType:
    entry = PLUGINS / plugin_id / "main.py"
    spec = importlib.util.spec_from_file_location(f"scrum_test_{plugin_id.replace('-', '_')}", entry)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_both_plugins_declare_a_valid_manifest():
    for plugin_id, kind in (("scrum-board", "tool"), ("scrum-guard", "hook")):
        text = (PLUGINS / plugin_id / "plugin.yaml").read_text(encoding="utf-8")
        assert f"id: {plugin_id}" in text
        assert f"kind: {kind}" in text
        assert "entry: main.py" in text


def test_board_plugin_registers_the_agent_tools():
    module = _load("scrum-board")
    ctx = RecordingCtx()
    module.setup(ctx)
    assert set(ctx.tools) == {
        "board_claim",
        "board_heartbeat",
        "board_submit",
        "board_block",
        "board_view",
        "board_estimate",
        "board_backlog_add",
    }
    assert ctx.tools["board_claim"]["config_fields"]  # board url + token


async def test_board_claim_tells_a_duplicate_wake_to_stop(monkeypatch):
    module = _load("scrum-board")
    monkeypatch.setattr(module, "_rpc", lambda *a, **k: {"ok": True, "data": {"status": "NOT_YOURS"}})
    text = await module.board_claim("T-1")
    assert "NOT_YOURS" in text
    assert "NOOP" in text


async def test_board_claim_returns_branch_and_rules(monkeypatch):
    module = _load("scrum-board")
    monkeypatch.setattr(
        module,
        "_rpc",
        lambda *a, **k: {
            "ok": True,
            "data": {
                "status": "OK",
                "task_id": "T-1",
                "lease_id": "abc",
                "branch": "scrum/T-1",
                "attempt": 1,
                "spec": "改一下 README",
            },
        },
    )
    text = await module.board_claim("T-1")
    assert "scrum/T-1" in text
    assert "不要 git commit" in text or "不要碰 main" in text
    assert "board_submit" in text


async def test_board_tools_degrade_instead_of_raising(monkeypatch):
    """A dead board must produce a readable reason, not break the agent's turn."""
    module = _load("scrum-board")

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(module.urllib.request, "urlopen", boom)
    payload = module._rpc("task_claim", task_id="T-1")
    assert payload["ok"] is False
    assert payload["reason"] == "board_unreachable"
    text = await module.board_claim("T-1")
    assert text.startswith("BOARD_ERROR")


def test_guard_blocks_commit_and_push():
    """The experiment's hard rule: no commit, no push, on any agent."""
    module = _load("scrum-guard")
    rules = json.loads((PLUGINS / "scrum-guard" / "guard.json").read_text(encoding="utf-8"))
    reason = module.block_reason("execute", {"command": "git commit -m 'x'"}, "dev", rules)
    assert reason and "禁止模式" in reason
    reason = module.block_reason("execute", {"command": "git push origin main"}, "dev", rules)
    assert reason is not None


def test_guard_keeps_read_only_roles_read_only():
    module = _load("scrum-guard")
    rules = json.loads((PLUGINS / "scrum-guard" / "guard.json").read_text(encoding="utf-8"))
    assert module.block_reason("write_file", {"path": "a.py"}, "po", rules)
    assert module.block_reason("edit_file", {"path": "a.py"}, "reviewer", rules)
    assert module.block_reason("read_file", {"path": "a.py"}, "reviewer", rules) is None
    assert module.block_reason("write_file", {"path": "a.py"}, "dev", rules) is None


def test_guard_protects_credential_paths():
    module = _load("scrum-guard")
    rules = json.loads((PLUGINS / "scrum-guard" / "guard.json").read_text(encoding="utf-8"))
    assert module.block_reason("read_file", {"path": "/home/x/.ssh/id_rsa"}, "dev", rules)
    assert module.block_reason("read_file", {"path": "/repo/.git/config"}, "dev", rules)


def test_guard_middleware_is_registered_with_high_priority():
    module = _load("scrum-guard")
    ctx = RecordingCtx()
    module.setup(ctx)
    assert len(ctx.middlewares) == 1
    instance, priority = ctx.middlewares[0]
    assert priority == 50  # outside Octop's own guards
    assert hasattr(instance, "awrap_tool_call")


async def test_backlog_add_stops_at_the_sprint_gate(monkeypatch):
    """If the PBI cannot enter a sprint, say so — and say what already exists."""
    module = _load("scrum-board")
    calls: list[str] = []

    def fake(method, **params):
        calls.append(method)
        if method == "pbi_create":
            return {"ok": True, "data": {"pbi_id": "PBI-1"}}
        return {"ok": False, "reason": "no_active_sprint", "detail": ""}

    monkeypatch.setattr(module, "_rpc", fake)
    out = await module.board_backlog_add("加张卡", "能跑")

    assert out.startswith("ADD_REJECTED no_active_sprint")
    assert calls == ["pbi_create", "pbi_add_to_sprint"]  # never reaches task_create
    assert "PBI-1" in out


async def test_backlog_add_reports_the_task_id(monkeypatch):
    module = _load("scrum-board")
    replies = {
        "pbi_create": {"ok": True, "data": {"pbi_id": "PBI-9"}},
        "pbi_add_to_sprint": {"ok": True, "data": {"sprint_id": "S-1"}},
        "task_create": {"ok": True, "data": {"task_id": "T-9"}},
    }
    monkeypatch.setattr(module, "_rpc", lambda method, **p: replies[method])
    out = await module.board_backlog_add("加张卡", "能跑")

    assert out == "ADDED T-9 pbi=PBI-9 已进入派单池，不要自己认领。"


async def test_backlog_add_never_sends_points_from_the_requester(monkeypatch):
    """The PO-side tool must not smuggle a story point into the create call."""
    module = _load("scrum-board")
    seen: dict[str, object] = {}

    def fake(method, **params):
        if method == "pbi_create":
            seen.update(params)
            return {"ok": True, "data": {"pbi_id": "PBI-1"}}
        if method == "pbi_add_to_sprint":
            return {"ok": True, "data": {"sprint_id": "S-1"}}
        return {"ok": True, "data": {"task_id": "T-1"}}

    monkeypatch.setattr(module, "_rpc", fake)
    await module.board_backlog_add("加张卡", "能跑")

    assert seen["points"] is None


async def test_board_estimate_reports_the_delta(monkeypatch):
    module = _load("scrum-board")
    monkeypatch.setattr(
        module, "_rpc",
        lambda method, **p: {"ok": True, "data": {"pbi_id": "PBI-1", "points": 5.0, "delta": 5.0}},
    )
    out = await module.board_estimate("PBI-1", 5.0, "涉及迁移脚本")
    assert out.startswith("ESTIMATED PBI-1 5 pt")
    assert "delta 5" in out
