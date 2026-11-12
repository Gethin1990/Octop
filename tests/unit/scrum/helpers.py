"""Shared setup for the scrum board tests."""

from __future__ import annotations

from typing import Any

from octop.scrum.store import BoardStore


def seed_agents(store: BoardStore, *, devs: int = 3) -> BoardStore:
    # ``channel="any"`` because a test double stands in for both execution
    # channels: the worker tests drive these ids locally, the dispatcher tests
    # wake them through FakeTransport. A real install picks one side.
    for index in range(devs):
        store.register_agent(f"dev-{index + 1}", "dev", channel="any")
    store.register_agent("po", "po", channel="any")
    store.register_agent("sm", "sm", channel="any")
    store.register_agent("reviewer", "reviewer", channel="any")
    return store


def sprint_with_tasks(
    store: BoardStore,
    *,
    count: int = 1,
    points: float = 3.0,
    acceptance: tuple[str, ...] = ("works",),
    class_of_service: str = "standard",
    capacity: float = 100.0,
) -> tuple[str, list[str], list[str]]:
    sprint_id = store.create_sprint("S1", capacity_pts=capacity)
    pbi_ids = [
        store.create_pbi(
            f"PBI {i}",
            points=points,
            acceptance=list(acceptance),
            class_of_service=class_of_service,
        )
        for i in range(count)
    ]
    store.commit_sprint(sprint_id, pbi_ids)
    tasks = [
        store.create_task(pbi_id, f"do {pbi_id}", spec=f"spec for {pbi_id}") for pbi_id in pbi_ids
    ]
    return sprint_id, pbi_ids, tasks


def drive_planning(client: Any, *, approve: bool = True) -> None:
    """Browser-equivalent of the Planning gate.

    ``GET /`` runs one idempotent Planning tick (PO refinement, dev sizing,
    Sprint proposal). The owner's approval is what turns ``planned`` into
    ``active`` and creates the queued task, so tests that need a dispatchable
    card must do both -- the browser does exactly this.
    """
    client.get("/")
    if approve:
        client.post("/ui/approve_sprint")


def to_review(store: BoardStore, task_id: str, agent: str = "dev-1") -> None:
    lease = store.claim(task_id, agent)
    assert lease is not None
    store.submit(
        task_id,
        lease.lease_id,
        evidence={"summary": "done", "tests_output": "pytest: 3 passed", "files_changed": "a.py"},
    )
    assert store.record_dod(task_id, [{"check_id": "unit", "ok": True}])


def evidence() -> dict[str, Any]:
    return {"summary": "s", "tests_output": "ok", "files_changed": "a.py"}
