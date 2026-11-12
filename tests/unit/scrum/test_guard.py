"""The scope gate is a policy file plus a role lookup; both are testable alone.

These tests deliberately avoid a live board: the interesting failures are
"board is down" and "the agent never had a role", which are exactly the paths a
green-path integration test never touches.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PLUGIN = Path(__file__).resolve().parents[3] / "plugins" / "scrum-guard" / "main.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("scrum_guard_main", _PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrum_guard_main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def guard(tmp_path, monkeypatch):
    module = _load_guard()
    monkeypatch.setenv("SCRUM_GUARD_CACHE", str(tmp_path / "roles.json"))
    monkeypatch.setenv("SCRUM_BOARD_URL", "http://127.0.0.1:9/")
    module._role_cache.clear()
    return module


def _rules(guard):
    return json.loads((_PLUGIN.parent / "guard.json").read_text(encoding="utf-8"))


# ── role gate ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tool", ["write_file", "edit_file", "delete", "execute"])
def test_po_cannot_call_any_mutating_tool(guard, tool):
    reason = guard.block_reason(tool, {"path": "a.md"}, "po", _rules(guard))
    assert reason and "只读" in reason


def test_po_cannot_spawn_a_subagent_to_escape_the_gate(guard):
    """`task` runs another agent whose tool calls this middleware never sees."""
    reason = guard.block_reason("task", {"description": "写个脚本"}, "po", _rules(guard))
    assert reason and "只读" in reason


def test_sm_cannot_write_artifacts_it_is_supposed_to_verify(guard):
    reason = guard.block_reason("write_file", {"path": "result.md"}, "sm", _rules(guard))
    assert reason and "不能调用 write_file" in reason


def test_sm_keeps_execute_so_dod_can_actually_be_verified(guard):
    """Regression: denying the SM ``execute`` made every review impossible.

    T-4c39edf4 went ``blocked / tooling_blocked`` because DoD verification is
    nothing but shell commands and RPC calls; the SM could read the board but
    not the evidence. ``deny_tools_by_role`` is now applied directly instead of
    hiding behind ``read_only_roles``, so "may not edit product files" and "may
    run verification" are separate decisions.
    """
    assert guard.block_reason(
        "execute", {"command": "curl -s /rpc/task_get"}, "sm", _rules(guard)
    ) is None
    # Execute access is still not a licence to commit or push.
    assert guard.block_reason(
        "execute", {"command": "git commit -m wip"}, "sm", _rules(guard)
    ) is not None


def test_reading_is_never_blocked_by_role(guard):
    assert guard.block_reason("read_file", {"path": "a.md"}, "po", _rules(guard)) is None
    assert guard.block_reason("board_view", {}, "sm", _rules(guard)) is None


def test_dev_keeps_full_tool_access(guard):
    assert guard.block_reason("execute", {"command": "pytest -q"}, "dev", _rules(guard)) is None


# ── destructive command patterns ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "command",
    ["git commit -m wip", "git push origin main", "git rebase -i HEAD~3",
     "git reset --hard HEAD", "pytest --no-verify", "deploy --force"],
)
def test_dev_cannot_commit_or_push_even_though_it_can_execute(guard, command):
    reason = guard.block_reason("execute", {"command": command}, "dev", _rules(guard))
    assert reason and "禁止模式" in reason


def test_honest_evidence_mentioning_git_commit_is_not_rejected(guard):
    """Regression: a global substring match rejected truthful DoD reports."""
    evidence = {
        "summary": "实现完成，未执行 git commit（按约束）",
        "tests_output": "12 passed",
    }
    assert guard.block_reason("board_submit", evidence, "dev", _rules(guard)) is None


# ── protected paths ──────────────────────────────────────────────────────────
def test_write_into_git_dir_is_refused(guard):
    reason = guard.block_reason(
        "write_file", {"path": "/repo/.git/config"}, "dev", _rules(guard)
    )
    assert reason and "保护范围" in reason


def test_reading_ssh_keys_is_refused_too(guard):
    reason = guard.block_reason(
        "read_file", {"path": "/Users/x/.ssh/id_rsa"}, "dev", _rules(guard)
    )
    assert reason and "保护范围" in reason


def test_nested_arguments_are_inspected(guard):
    reason = guard.block_reason(
        "execute", {"args": {"cmd": ["sh", "-c", "git push --force"]}}, "dev", _rules(guard)
    )
    assert reason is not None


# ── role lookup: board down must not become an amnesty ───────────────────────
def _fake_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return body

    return _Resp()


def test_role_is_cached_to_disk_and_survives_a_board_outage(guard, monkeypatch):
    monkeypatch.setattr(guard, "_fetch_role", lambda agent_id: "po")
    assert guard._role_for("TYD8PH") == "po"
    assert Path(guard.os.environ["SCRUM_GUARD_CACHE"]).exists()

    # Board dies: the cached role still applies inside the grace window.
    guard._role_cache.clear()
    monkeypatch.setattr(guard, "_fetch_role", lambda agent_id: None)
    assert guard._role_for("TYD8PH") == "po"


def test_stale_cache_beyond_grace_window_fails_open(guard, monkeypatch):
    path = Path(guard.os.environ["SCRUM_GUARD_CACHE"])
    path.write_text(json.dumps({"TYD8PH": {"role": "po", "at": 0}}), encoding="utf-8")
    guard._role_cache.clear()
    monkeypatch.setattr(guard, "_fetch_role", lambda agent_id: None)
    assert guard._role_for("TYD8PH") is None


def test_unknown_agent_is_unconstrained(guard, monkeypatch):
    monkeypatch.setattr(guard, "_fetch_role", lambda agent_id: None)
    assert guard._role_for("") is None
    assert guard.block_reason("write_file", {"path": "a"}, None, _rules(guard)) is None


def test_lookup_ignores_the_outbound_proxy(guard, monkeypatch):
    """Loopback must bypass HTTP_PROXY, or every lookup fails and fails open."""
    seen: dict[str, object] = {}

    class _Opener:
        def open(self, request, timeout=3):
            seen["url"] = request.full_url
            seen["agent"] = request.get_header("X-octop-agent")
            return _fake_response({"ok": True, "data": {"role": "sm"}})

    monkeypatch.setattr(guard, "_no_proxy_opener", lambda: _Opener())
    assert guard._fetch_role("327P52") == "sm"
    assert seen["url"].endswith("/rpc/whoami")
    assert seen["agent"] == "327P52"


# ── context resolution ───────────────────────────────────────────────────────
def test_agent_id_falls_back_to_the_request_runtime(guard, monkeypatch):
    """Sync invocations have no langgraph config; the request still carries it."""
    import types

    fake_config = types.ModuleType("langgraph.config")

    def _no_context():
        raise RuntimeError("no langgraph context")

    fake_config.get_config = _no_context
    monkeypatch.setitem(sys.modules, "langgraph.config", fake_config)
    request = SimpleNamespace(
        runtime=SimpleNamespace(config={"configurable": {"agent_id": "NE09TY"}})
    )
    agent_id, _ = guard._context(request)
    assert agent_id == "NE09TY"


# ── registration ─────────────────────────────────────────────────────────────
def test_setup_registers_one_middleware_outside_the_host_guards(guard):
    registered: list[tuple[object, int]] = []

    class _Ctx:
        def middleware(self, instance, *, priority=100):
            registered.append((instance, priority))

    guard.setup(_Ctx())
    assert len(registered) == 1
    assert registered[0][1] == 50
    assert isinstance(registered[0][0], guard.GuardMiddleware)
