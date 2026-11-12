"""Sizing is team work, and a DoR rejection is an owner action, not a hang.

This file covers the two rules the owner asked for:
  1. they never supply story points — a dev agent does, with an auditable actor;
  2. a request that fails DoR comes back into Review so it can be edited and
     resubmitted, instead of sitting in Queued pretending to be healthy.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from octop.scrum import reports
from octop.scrum.estimate import FIBONACCI, HeuristicEstimator
from octop.scrum.service import OWNER_ACTOR, create_app
from octop.scrum.store import _SCHEMA_PATH, BoardStore
from octop.scrum.worker import DemoExecutor, Worker

from .helpers import drive_planning, sprint_with_tasks


@pytest.fixture()
def client(store):
    return TestClient(create_app(store), follow_redirects=False)


def _events(store: BoardStore, kind: str) -> list[dict]:
    with store.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE kind=? ORDER BY id", (kind,)
        ).fetchall()
    return [dict(r) for r in rows]


def _pbi(store: BoardStore) -> dict:
    items = store.all_pbis()
    assert len(items) == 1
    return items[0]


# ── 1. the owner does not estimate ──────────────────────────────────────────
def test_estimator_snaps_to_a_coarse_scale_and_names_its_signals():
    estimator = HeuristicEstimator()
    small = estimator.estimate(
        SimpleNamespace(title="改一行文案", spec=""),
        {"title": "改一行文案", "body": "", "acceptance": ["文案变了"]},
    )
    large = estimator.estimate(
        SimpleNamespace(title="迁移数据库并重构权限", spec="全量回归"),
        {"title": "迁移数据库并重构权限", "body": "兼容旧数据，涉及安全审计",
         "acceptance": ["a", "b", "c", "d", "e"]},
    )
    assert small.points in FIBONACCI and large.points in FIBONACCI
    assert large.points > small.points
    assert "验收条件" in large.rationale and "风险词" in large.rationale


def test_request_form_has_no_points_field_and_the_dev_agent_sizes_it(client, store):
    page = client.get("/").text
    assert 'name="points"' not in page
    assert "估点由" in page

    resp = client.post("/ui/new_card", data={
        "title": "给看板加说明页", "points": "99",  # forged / ignored
        "acceptance": "能看到 DoR; 能看到 DoD", "actor": "po",
    })
    assert resp.status_code == 303
    drive_planning(client)
    pbi = _pbi(store)
    assert pbi["points"] not in (None, 99.0)
    assert pbi["readiness"]["estimated_by"] in {"dev-1", "dev-2", "dev-3"}

    created = _events(store, "pbi_created")
    assert created and '"points": null' in created[0]["payload_json"]
    estimated = _events(store, "pbi_estimated")
    assert len(estimated) == 1
    assert estimated[0]["actor"] == pbi["readiness"]["estimated_by"]
    assert estimated[0]["entity_id"] == pbi["pbi_id"]


def test_estimates_are_visible_in_all_three_artefacts(client, store):
    client.post("/ui/new_card", data={"title": "导出清单", "acceptance": "能导出"})
    drive_planning(client)

    product = reports.product_backlog(store)["items"][0]
    assert product["points_estimated"] is True
    assert product["estimated_by"]
    sprint = reports.sprint_backlog(store)["items"][0]
    assert sprint["points_estimated"] is True
    chart = reports.burndown(store)
    assert chart["baseline_pts"] == product["points"]  # sized before commitment


def test_rpc_estimate_is_dev_only(client, store):
    pbi_id = store.create_pbi("需要估点", acceptance=["x"])
    denied = client.post(
        "/rpc/pbi_estimate", headers={"X-Octop-Agent": "po"},
        json={"params": {"pbi_id": pbi_id, "points": 8}},
    )
    assert denied.status_code == 403

    allowed = client.post(
        "/rpc/pbi_estimate", headers={"X-Octop-Agent": "dev-2"},
        json={"params": {"pbi_id": pbi_id, "points": 5, "rationale": "三个模块"}},
    ).json()
    assert allowed["ok"] is True
    assert allowed["data"]["points"] == 5.0
    assert store.get_pbi(pbi_id)["readiness"]["estimated_by"] == "dev-2"


def test_worker_sizes_rpc_created_cards_before_pulling_them(store, board_dir):
    sprint_id, pbi_ids, tasks = sprint_with_tasks(store, points=None)
    worker = Worker(store, DemoExecutor(artifacts=board_dir / "artifacts"),
                    log=lambda _m: None)

    result = worker.step()

    assert result.action == "reviewed"
    pbi = store.get_pbi(pbi_ids[0])
    assert pbi["points"] is not None
    assert pbi["readiness"]["estimated_by"] in {"dev-1", "dev-2", "dev-3"}
    assert store.get_task(tasks[0]).agent_id == pbi["readiness"]["estimated_by"]


# ── 2. DoR rejection returns to Review ──────────────────────────────────────
def test_missing_acceptance_is_held_by_the_po_before_a_sprint_is_proposed(client, store):
    """The gate fires at refinement, so no Sprint is built around a bad card.

    Previously the request went new -> ready -> Sprint proposal -> owner
    approval -> task -> admission denial -> needs_revision. Everything after
    "ready" was wasted work, and the owner had already approved a commitment
    that could not start.
    """
    client.post("/ui/new_card", data={"title": "一个没写验收条件的需求", "acceptance": ""})
    drive_planning(client)

    assert store.tasks_in_state("queued") == []
    assert store.planned_sprint() is None
    held = store.pbis_in_state("needs_revision")
    assert len(held) == 1
    pbi_id = held[0]["pbi_id"]

    page = client.get("/").text
    assert "需求被打回" in page
    assert 'action="/ui/revise"' in page
    assert 'name="acceptance"' in page

    revised = client.post("/ui/revise", data={
        "pbi_id": pbi_id, "title": "一个说清楚验收条件的需求",
        "body": "补充：要能打开页面", "acceptance": "页面能打开; 产物可下载",
        "note": "补齐验收条件",
    })
    assert revised.status_code == 303
    assert store.get_pbi(pbi_id)["state"] == "ready"
    assert store.get_pbi(pbi_id)["acceptance"] == ["页面能打开", "产物可下载"]

    # Next tick sizes it and drafts the Sprint; approval creates the task.
    drive_planning(client)
    task = store.tasks_in_state("queued")[0]
    assert store.get_task(task.task_id).pbi_id == pbi_id
    assert store.get_pbi(pbi_id)["points"] is not None


def test_resubmitting_without_acceptance_keeps_the_request_held(client, store):
    client.post("/ui/new_card", data={"title": "还没写清楚", "acceptance": ""})
    drive_planning(client)
    pbi_id = store.pbis_in_state("needs_revision")[0]["pbi_id"]

    resp = client.post("/ui/revise", data={
        "pbi_id": pbi_id, "title": "还是没写清楚", "acceptance": "",
    })
    assert resp.status_code == 303
    assert "留在 Review" in unquote(resp.headers["location"])
    assert store.get_pbi(pbi_id)["state"] == "needs_revision"


def test_revision_is_attributed_to_the_owner(client, store):
    client.post("/ui/new_card", data={"title": "被打回的需求", "acceptance": ""})
    drive_planning(client)
    pbi_id = store.pbis_in_state("needs_revision")[0]["pbi_id"]

    client.post("/ui/revise", data={
        "pbi_id": pbi_id, "title": "改好了", "acceptance": "能跑",
    })

    revised = _events(store, "pbi_revised")
    assert revised and revised[0]["actor"] == OWNER_ACTOR
    # The hold itself stays PO work, so the trail shows who did what.
    refined = _events(store, "pbi_refined")
    assert refined and refined[0]["actor"] == "po"


# ── 3. burndown accounting and schema compatibility ────────────────────────
def test_burndown_replays_estimate_and_invalidation(store):
    _, pbi_ids, tasks = sprint_with_tasks(store, points=None)
    pbi_id = pbi_ids[0]
    assert reports.burndown(store)["current_pts"] == 0.0

    store.estimate_pbi(pbi_id, 3.0, actor="dev-1", rationale="两个模块")
    chart = reports.burndown(store)
    assert chart["current_pts"] == 3.0
    assert any("团队估点" in c["label"] for c in chart["scope_changes"])

    store.request_revision(tasks[0], "dor_fail:missing_acceptance")
    store.revise_request(pbi_id, title="改写", body="", acceptance=["能跑"], actor="owner")
    assert reports.burndown(store)["current_pts"] == 0.0

    store.estimate_pbi(pbi_id, 5.0, actor="dev-2", rationale="范围变大")
    assert reports.burndown(store)["current_pts"] == 5.0


def test_a_pre_revision_database_is_migrated_without_losing_rows(board_dir):
    """SQLite cannot ALTER a CHECK constraint; rebuild must preserve history."""
    db = board_dir / "legacy.db"
    legacy_ddl = _SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "'needs_revision', ", ""
    ).replace("'schema_version', '2'", "'schema_version', '1'")
    raw = sqlite3.connect(db)
    raw.executescript(legacy_ddl)
    raw.execute(
        "INSERT INTO task (task_id, pbi_id, sprint_id, title, state, created_at, updated_at)"
        " VALUES ('T-old', 'PBI-old', 'SPR-old', '迁移前就存在的卡', 'queued', 1.0, 1.0)"
    )
    raw.commit()
    raw.close()

    store = BoardStore(db)
    store.init_schema()
    ddl = store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task'"
    ).fetchone()["sql"]
    assert "needs_revision" in ddl
    assert store.get_task("T-old") is not None  # history preserved

    store.request_revision("T-old", "dor_fail:missing_acceptance")
    assert store.get_task("T-old").state == "needs_revision"
    store.close()


# ── 4. the glossary page ────────────────────────────────────────────────────
def test_guide_button_is_in_every_page_header_and_the_page_explains_the_terms(client, store):
    board = client.get("/").text
    assert 'class="guide-btn" href="/guide"' in board

    page = client.get("/guide")
    assert page.status_code == 200
    text = page.text
    for term in ("DoR", "DoD", "WIP", "lease", "故事点", "needs_revision",
                 "Burndown", "event_log", "状态流转图"):
        assert term in text
    assert 'id="dor"' in text and 'id="dod"' in text
    assert "不由提需求的人填" in text
    assert "<script" not in text

    _, _, tasks = sprint_with_tasks(store)
    deliverable = client.get(f"/deliverable/{tasks[0]}").text
    assert 'href="/guide"' in deliverable
