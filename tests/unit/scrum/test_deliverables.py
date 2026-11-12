"""Acceptance means seeing the increment, not trusting a status column."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from octop.scrum.service import OWNER_ACTOR, create_app
from octop.scrum.store import BoardStore
from octop.scrum.worker import DemoExecutor

from .helpers import drive_planning, sprint_with_tasks


@pytest.fixture()
def env(store, board_dir):
    root = board_dir / "artifacts"
    root.mkdir(exist_ok=True)
    return store, root, TestClient(create_app(store, artifacts_root=root), follow_redirects=False)


def _deliver(store: BoardStore, root: Path, task_id: str, *, artifact: str | None = None) -> None:
    lease = store.claim(task_id, "dev-1")
    assert lease is not None
    store.ack(task_id, lease.lease_id)
    task = store.get_task(task_id)
    assert task is not None
    evidence = DemoExecutor(artifacts=root).run(task).evidence
    if artifact is not None:
        evidence["artifact"] = artifact
    store.submit(task_id, lease.lease_id, evidence=evidence)
    assert store.record_dod(task_id, [{"check_id": "artifact:exists", "ok": True}])


def test_deliverable_page_shows_the_real_increment(env):
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1, points=3.0)
    _deliver(store, root, tasks[0])

    page = client.get(f"/deliverable/{tasks[0]}").text

    assert "当初提的需求" in page
    assert "works" in page                       # acceptance criteria
    assert "交付证据" in page
    assert "artifact:exists" in page and "PASS" in page
    assert "产物全文" in page
    assert "## Spec" in page                    # real file content, not a stub
    assert tasks[0] in page
    assert "验收通过" in page


def test_raw_artifact_is_readable_as_text(env):
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    _deliver(store, root, tasks[0])

    resp = client.get(f"/artifact/raw?task_id={tasks[0]}")

    assert resp.status_code == 200
    assert "## Result" in resp.text
    assert "utf-8" in resp.headers["content-type"]


def test_artifact_outside_the_registered_root_is_refused(env, board_dir):
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    outside = board_dir / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    _deliver(store, root, tasks[0], artifact=str(outside))

    assert client.get(f"/artifact/raw?task_id={tasks[0]}").status_code == 404
    page = client.get(f"/deliverable/{tasks[0]}").text
    assert "secret" not in page
    assert "没有登记可读产物" in page


def test_owner_acceptance_records_owner_note_and_review_dwell(env):
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    _deliver(store, root, tasks[0])

    resp = client.post("/ui/action", data={
        "method": "review_accept", "task_id": tasks[0], "note": "页面能打开，产物可读",
    })

    assert resp.status_code == 303
    assert store.get_task(tasks[0]).state == "done"
    decision = store.review_decision(tasks[0])
    assert decision is not None
    assert decision["reviewer"] == OWNER_ACTOR
    assert decision["note"] == "页面能打开，产物可读"
    assert decision["dwell_seconds"] is not None and decision["dwell_seconds"] >= 0
    accepted = [e for e in store.recent_events(20) if e["kind"] == "task_accepted"]
    assert accepted and accepted[0]["actor"] == OWNER_ACTOR


def test_owner_cannot_impersonate_a_scrum_role_when_asking_for_work(env):
    store, _, client = env

    resp = client.post("/ui/new_card", data={
        "title": "给系统加个导出", "points": "3", "acceptance": "能导出; 有文件名",
        "actor": "po",  # forged: the server must ignore it
    })

    assert resp.status_code == 303
    drive_planning(client)
    created = [e for e in store.recent_events(20) if e["kind"] == "pbi_created"]
    assert created and created[0]["actor"] == OWNER_ACTOR
    assert store.get_task(store.tasks_in_state("queued")[0].task_id) is not None


def test_api_artifacts_returns_all_three_named_artefacts(env):
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1, points=5.0)
    _deliver(store, root, tasks[0])

    payload = client.get("/api/artifacts?horizon=3600").json()

    assert set(payload) == {"product_backlog", "sprint_backlog", "burndown"}
    assert payload["product_backlog"]["points_total"] == 5.0
    assert payload["sprint_backlog"]["points_committed"] == 5.0
    assert payload["burndown"]["horizon_s"] == 3600.0


def test_rpc_deliverable_is_a_read_only_view_for_known_agents(env):
    store, root, client = env
    _, _, tasks = sprint_with_tasks(store, count=1)
    _deliver(store, root, tasks[0])

    resp = client.post(
        "/rpc/deliverable",
        headers={"X-Octop-Agent": "dev-1"},
        json={"params": {"task_id": tasks[0]}},
    ).json()

    assert resp["ok"] is True
    assert resp["data"]["found"] is True
    assert resp["data"]["pbi"]["acceptance"] == ["works"]
    assert resp["data"]["evidence"]["summary"]
    assert resp["data"]["dod_checks"]
