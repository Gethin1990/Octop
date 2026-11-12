"""Role enforcement and the lease capability, at the HTTP boundary."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from octop.scrum.service import create_app

from .helpers import sprint_with_tasks


@pytest.fixture()
def client(store):
    return TestClient(create_app(store))


def _rpc(client, method: str, agent: str, **params):
    return client.post(
        f"/rpc/{method}",
        json={"params": params},
        headers={"X-Octop-Agent": agent},
    )


def test_unknown_agent_is_refused(client):
    resp = _rpc(client, "pbi_create", "ghost", title="x")
    assert resp.status_code == 403


def test_role_comes_from_the_board_not_the_payload(client):
    """A dev posting role='po' must not become PO."""
    resp = client.post(
        "/rpc/pbi_create",
        json={"params": {"title": "sneaky", "role": "po", "actor": "po"}},
        headers={"X-Octop-Agent": "dev-1"},
    )
    assert resp.status_code == 403


def test_po_can_create_and_reorder(client):
    created = _rpc(client, "pbi_create", "po", title="hello", acceptance=["works"])
    assert created.status_code == 200
    pbi_id = created.json()["data"]["pbi_id"]
    moved = _rpc(client, "pbi_reorder", "po", pbi_id=pbi_id, rank=5)
    assert moved.json()["ok"] is True


def test_dev_cannot_reorder_backlog(client):
    assert _rpc(client, "pbi_reorder", "dev-1", pbi_id="x", rank=1).status_code == 403


def test_claim_then_duplicate_claim_is_not_yours(client, store):
    _, _, tasks = sprint_with_tasks(store)
    first = _rpc(client, "task_claim", "dev-1", task_id=tasks[0]).json()["data"]
    assert first["status"] == "OK"
    assert first["lease_id"]
    # Same agent retrying the wake gets the same lease, not a second branch.
    again = _rpc(client, "task_claim", "dev-1", task_id=tasks[0]).json()["data"]
    assert again["status"] == "OK"
    assert again["lease_id"] == first["lease_id"]
    # Another agent must be told to stop.
    other = _rpc(client, "task_claim", "dev-2", task_id=tasks[0]).json()["data"]
    assert other["status"] == "NOT_YOURS"


def test_submit_requires_the_live_lease(client, store):
    _, _, tasks = sprint_with_tasks(store)
    _rpc(client, "task_claim", "dev-1", task_id=tasks[0])
    resp = _rpc(
        client,
        "task_submit",
        "dev-1",
        task_id=tasks[0],
        lease_id="forged",
        evidence={"summary": "s", "tests_output": "ok", "files_changed": "a.py"},
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "not_your_lease"


def test_dev_cannot_accept_its_own_work(client, store):
    _, _, tasks = sprint_with_tasks(store)
    resp = _rpc(client, "review_accept", "dev-1", task_id=tasks[0])
    assert resp.status_code == 403
    assert store.get_task(tasks[0]).state == "queued"


def test_reviewer_cannot_accept_before_dod_runs(client, store):
    _, _, tasks = sprint_with_tasks(store)
    claim = _rpc(client, "task_claim", "dev-1", task_id=tasks[0]).json()["data"]
    _rpc(
        client,
        "task_submit",
        "dev-1",
        task_id=tasks[0],
        lease_id=claim["lease_id"],
        evidence={"summary": "s", "tests_output": "1 passed", "files_changed": "a.py"},
    )
    blocked = _rpc(client, "review_accept", "reviewer", task_id=tasks[0]).json()
    assert blocked["ok"] is False and blocked["reason"] == "not_in_review"

    dod = _rpc(client, "dod_record", "sm", task_id=tasks[0],
               checks=[{"check_id": "unit", "ok": True}]).json()
    assert dod["data"]["all_passed"] is True
    assert store.get_task(tasks[0]).state == "in_review"

    accepted = _rpc(client, "review_accept", "reviewer", task_id=tasks[0],
                    dwell_seconds=90).json()
    assert accepted["ok"] is True
    assert store.get_task(tasks[0]).state == "done"


def test_metrics_surface_reports_the_denial_histogram(client, store):
    _, _, tasks = sprint_with_tasks(store, acceptance=())
    admit = store.admit(tasks[0])
    store.note_admission(tasks[0], admit.reason, admit.detail)
    body = client.get("/metrics").json()
    assert body["admit_denied"] == {"dor_fail": 1}


def test_unknown_rpc_method_returns_404(client):
    assert _rpc(client, "nope", "dev-1").status_code == 404


def test_board_page_renders(client, store):
    _, _, tasks = sprint_with_tasks(store)
    html = client.get("/").text
    assert "Queued" in html and "Review" in html
