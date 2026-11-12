"""A control-plane demo is not an agent's increment, and must never look like one.

The owner asked a concrete question about a card in Review: where is the result,
and where is the file? The honest answer was "there is no result, only a template".
These tests pin that answer into the product so a placeholder cannot quietly be
accepted as delivered work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from octop.scrum.service import _provenance, create_app
from octop.scrum.store import BoardStore
from octop.scrum.worker import PLACEHOLDER_EXECUTOR, PLACEHOLDER_MARK, DemoExecutor

from .helpers import sprint_with_tasks


@pytest.fixture()
def env(store, board_dir):
    root = board_dir / "artifacts"
    root.mkdir(exist_ok=True)
    return store, root, TestClient(create_app(store, artifacts_root=root), follow_redirects=False)


def _deliver(
    store: BoardStore,
    root: Path,
    task_id: str,
    *,
    executor: str | None = None,
    artifact_body: str | None = None,
) -> Path:
    lease = store.claim(task_id, "dev-1")
    assert lease is not None
    store.ack(task_id, lease.lease_id)
    task = store.get_task(task_id)
    assert task is not None
    evidence = DemoExecutor(artifacts=root).run(task).evidence
    path = root / f"{task_id}.md"
    if artifact_body is not None:
        path.write_text(artifact_body, encoding="utf-8")
    if executor is not None:
        evidence["executor"] = executor
    store.submit(task_id, lease.lease_id, evidence=evidence)
    assert store.record_dod(task_id, [{"check_id": "artifact:exists", "ok": True}])
    return path


def test_demo_executor_declares_itself_as_a_placeholder(env):
    store, root, _ = env
    _, _, tasks = sprint_with_tasks(store, count=1)

    task = store.get_task(tasks[0])
    assert task is not None
    evidence = DemoExecutor(artifacts=root).run(task).evidence

    assert evidence["executor"] == PLACEHOLDER_EXECUTOR
    text = Path(str(evidence["artifact"])).read_text(encoding="utf-8")
    assert PLACEHOLDER_MARK in text
    assert "没有该需求的业务结果" in text
    assert "Produced by the local worker" not in text


def test_a_card_without_a_declared_executor_is_treated_as_unproven(env):
    store, root, _ = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    _deliver(store, root, tasks[0])
    task = store.get_task(tasks[0])
    assert task is not None
    stripped = {k: v for k, v in task.evidence.items() if k != "executor"}
    legacy = type("Legacy", (), {"evidence": stripped, "task_id": tasks[0]})()

    label, html = _provenance(legacy)

    assert "未声明" in label
    assert "未经 Octop agent 执行" in html


def test_a_declared_real_executor_gets_no_placeholder_warning(env):
    store, root, _ = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    _deliver(store, root, tasks[0], executor="octop-agent/dev-2")
    task = store.get_task(tasks[0])
    assert task is not None

    label, html = _provenance(task)

    assert label == "octop-agent/dev-2"
    assert "占位产物" not in html
    assert "执行主体" in html


def test_review_and_deliverable_pages_warn_and_show_the_absolute_path(env):
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    path = _deliver(store, root, tasks[0])

    review = client.get("/").text
    page = client.get(f"/deliverable/{tasks[0]}").text

    for html in (review, page):
        assert "占位产物" in html
        assert "未经 Octop agent 执行" in html
        assert "DemoExecutor" in html
        assert str(path) in html
    assert "产物绝对路径" in page
    assert "执行主体" in page


def test_a_faked_executor_declaration_is_exposed_by_artifact_content(env):
    """Self-reporting "I am a real agent" must lose to the file's own text."""
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    _deliver(store, root, tasks[0], executor="octop-agent/dev-2")

    page = client.get(f"/deliverable/{tasks[0]}").text

    assert "执行主体声明与产物内容矛盾" in page
    assert "没有 agent 真正执行过" in page
    assert "占位产物" in page  # the artefact itself still says PLACEHOLDER


def test_a_real_increment_has_no_placeholder_warning(env):
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    _deliver(
        store,
        root,
        tasks[0],
        executor="octop-agent/dev-2",
        artifact_body="## Result\n\n交付内容已由该 agent 生成（测试夹具）。\n",
    )

    page = client.get(f"/deliverable/{tasks[0]}").text

    assert "octop-agent/dev-2" in page
    assert "执行主体：" in page
    assert "占位产物" not in page
    assert "声明与产物矛盾" not in page


def test_guide_separates_the_two_channels_and_forbids_forging(env):
    """The glossary is the page an owner reads before trusting a card.

    It has to say three things: which channel ran the work, that the local
    channel produces no business result, and that hand-filling an answer is
    forging an execution result rather than a delivery.
    """
    _, _, client = env

    page = client.get("/guide").text

    assert "谁在真正执行" in page
    assert "agent_role.channel" in page          # the isolation is documented
    assert "L2（已跑通" in page                   # and so is the real channel
    assert "不联网、不调模型" in page               # the local caveat survives
    assert "伪造执行结果" in page
