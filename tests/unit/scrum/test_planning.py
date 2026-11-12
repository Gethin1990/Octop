"""The approval gate: owner asks, PO refines, dev sizes, human approves.

These tests pin down the boundary the design note cares about most: a request
is not a commitment. Nothing may become a task until the owner has approved the
PO's Sprint proposal, and every earlier step must be attributed to the role
that owns it rather than to the requester.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from octop.scrum.service import OWNER_ACTOR, create_app
from octop.scrum.store import BoardStore


def _events(store: BoardStore, kind: str) -> list[dict[str, Any]]:
    with store.tx() as conn:
        rows = conn.execute("SELECT * FROM event_log WHERE kind=? ORDER BY id", (kind,)).fetchall()
    return [dict(r) for r in rows]


def _ask(client: TestClient, store: BoardStore, title: str) -> str:
    before = {str(p["pbi_id"]) for p in store.all_pbis()}
    resp = client.post(
        "/ui/new_card",
        data={"title": title, "acceptance": "能打开; 能流转", "points": "99", "actor": "po"},
    )
    assert resp.status_code == 303
    added = [p for p in store.all_pbis() if str(p["pbi_id"]) not in before]
    assert len(added) == 1
    return str(added[0]["pbi_id"])


def test_request_stays_in_backlog_until_owner_approves(store: BoardStore):
    client = TestClient(create_app(store), follow_redirects=False)

    pbi_id = _ask(client, store, "把看板变成可点的")

    pbi = store.get_pbi(pbi_id)
    assert pbi is not None
    assert pbi["state"] == "new"
    assert pbi["points"] is None
    assert store.active_sprint_id() is None
    assert store.planned_sprint() is None
    assert store.tasks_in_state("queued") == []
    assert _events(store, "pbi_created")[0]["actor"] == OWNER_ACTOR

    # One render tick = one Planning pass. It stops at `planned`.
    page = client.get("/").text
    pbi = store.get_pbi(pbi_id)
    assert pbi is not None
    assert pbi["state"] == "ready"
    assert pbi["readiness"]["refined_by"] == "po"
    assert pbi["points"] is not None
    assert str(pbi["readiness"]["estimated_by"]).startswith("dev-")
    assert "② 批准 Sprint 提案" in page
    assert "批准开工" in page

    planned = store.planned_sprint()
    assert planned is not None
    assert planned["state"] == "planned"
    assert store.active_sprint_id() is None
    assert store.tasks_in_state("queued") == []
    assert store.candidates_for(str(planned["sprint_id"])) == [pbi_id]
    assert _events(store, "pbi_refined")[0]["actor"] == "po"
    assert _events(store, "pbi_estimated")[0]["actor"] == pbi["readiness"]["estimated_by"]
    assert _events(store, "sprint_proposed")[0]["actor"] == "po"
    assert _events(store, "capacity_checked")[0]["actor"] == "sm"
    assert _events(store, "sprint_committed") == []

    # Idempotent: repeated renders must not mint a second proposal or estimate.
    client.get("/")
    client.get("/")
    assert len(_events(store, "pbi_estimated")) == 1
    assert len(_events(store, "sprint_proposed")) == 1
    assert store.tasks_in_state("queued") == []

    resp = client.post("/ui/approve_sprint", data={"sprint_id": planned["sprint_id"]})
    assert resp.status_code == 303
    assert store.active_sprint_id() == planned["sprint_id"]
    assert len(store.tasks_in_state("queued")) == 1
    assert _events(store, "sprint_committed")[0]["actor"] == OWNER_ACTOR
    assert store.get_pbi(pbi_id)["state"] == "committed"


def test_second_request_is_a_po_scope_injection(store: BoardStore):
    client = TestClient(create_app(store), follow_redirects=False)

    _ask(client, store, "第一张卡")
    client.get("/")  # Planning tick drafts the candidate
    planned = store.planned_sprint()
    assert planned is not None
    sprint_id = str(planned["sprint_id"])
    assert client.post("/ui/approve_sprint").status_code == 303

    _ask(client, store, "第二张卡")
    client.get("/")  # Planning tick sees the running Sprint

    assert len(store.tasks_in_state("queued")) == 2
    injected = _events(store, "scope_injected")
    assert len(injected) == 1
    assert injected[0]["actor"] == "po"
    assert store.planned_sprint() is None
    assert store.active_sprint_id() == sprint_id


# ── DoR misfires before the commitment, not after it ────────────────────────
def _ask_without_acceptance(client: TestClient, store: BoardStore, title: str) -> str:
    before = {str(p["pbi_id"]) for p in store.all_pbis()}
    resp = client.post("/ui/new_card", data={"title": title, "acceptance": ""})
    assert resp.status_code == 303
    added = [p for p in store.all_pbis() if str(p["pbi_id"]) not in before]
    assert len(added) == 1
    return str(added[0]["pbi_id"])


def test_request_without_acceptance_is_held_not_proposed(store: BoardStore):
    """The PO must not draft a Sprint around a card that cannot be admitted.

    The old flow promoted the request straight to ``ready`` and recorded
    ``dor_fail`` as a note, so the owner approved a Sprint and only then found
    out the single card would bounce at admission.
    """
    client = TestClient(create_app(store), follow_redirects=False)
    pbi_id = _ask_without_acceptance(client, store, "没说清楚的需求")

    client.get("/")

    pbi = store.get_pbi(pbi_id)
    assert pbi is not None
    assert pbi["state"] == "needs_revision"
    assert pbi["points"] is None  # never sized: sizing is wasted on a hold
    assert pbi["readiness"]["refined_by"] == "po"
    assert store.planned_sprint() is None
    assert store.tasks_in_state("queued") == []
    assert _events(store, "pbi_estimated") == []
    assert _events(store, "sprint_proposed") == []
    assert _events(store, "dor_fail")[0]["actor"] == "po"

    # The owner is asked in the panel, with the form to fix it.
    page = client.get("/").text
    assert "需求被打回" in page
    assert "待补验收条件" in page
    assert 'action="/ui/revise"' in page


def test_hold_is_idempotent_across_renders(store: BoardStore):
    client = TestClient(create_app(store), follow_redirects=False)
    _ask_without_acceptance(client, store, "反复渲染")

    for _ in range(3):
        client.get("/")

    assert len(_events(store, "pbi_refined")) == 1
    assert len(_events(store, "dor_fail")) == 1
    assert store.planned_sprint() is None


def test_released_hold_is_sized_and_proposed(store: BoardStore):
    client = TestClient(create_app(store), follow_redirects=False)
    pbi_id = _ask_without_acceptance(client, store, "补上验收条件")
    client.get("/")

    resp = client.post(
        "/ui/revise",
        data={
            "pbi_id": pbi_id,
            "title": "补上验收条件",
            "body": "",
            "acceptance": "能打开; 能流转",
            "note": "补齐",
        },
    )
    assert resp.status_code == 303
    assert store.get_pbi(pbi_id)["state"] == "ready"

    client.get("/")

    pbi = store.get_pbi(pbi_id)
    assert pbi is not None
    assert pbi["points"] is not None
    assert pbi["readiness"]["estimated_by"].startswith("dev-")
    planned = store.planned_sprint()
    assert planned is not None
    assert store.candidates_for(str(planned["sprint_id"])) == [pbi_id]
    assert _events(store, "dor_fail")[0]["actor"] == "po"  # history kept
