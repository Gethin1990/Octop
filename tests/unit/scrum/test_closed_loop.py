"""Closed loop: dispatcher → pointer over "WS" → agent tools → review → done.

The point of this test is the *contract*, not the wiring: the pointer text must
contain everything an agent needs to pull its payload, and the payload pull must
go through the same RPC surface the plugin uses. If either side drifts, this
breaks before a real sprint does.
"""

from __future__ import annotations

import asyncio
import re

from fastapi.testclient import TestClient

from octop.scrum.dispatcher import Dispatcher
from octop.scrum.service import create_app
from octop.scrum.transport import WakeResult

from .helpers import sprint_with_tasks

_TASK_RE = re.compile(r"TASK (T-[0-9a-f]+)")
_VERIFY_RE = re.compile(r"VERIFY (T-[0-9a-f]+)")


class AgentLoopTransport:
    """Plays the agent: claim the task, then submit evidence."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.seen: list[tuple[str, str]] = []
        self.verifies: list[tuple[str, str]] = []

    def _rpc(self, method: str, agent: str, **params):
        return self.client.post(
            f"/rpc/{method}", json={"params": params}, headers={"X-Octop-Agent": agent}
        ).json()

    async def send_pointer(self, agent_id: str, text: str, thread_id: str | None):
        # Two kinds of wake now: "build this" (TASK) and "verify this" (VERIFY).
        # This double plays the developer, so it acknowledges verify wakes and
        # stays out of the verifier's lane.
        if verify := _VERIFY_RE.search(text):
            self.verifies.append((agent_id, verify.group(1)))
            return WakeResult(status="accepted", thread_id="thr-verify")
        match = _TASK_RE.search(text)
        assert match, f"pointer text has no task id: {text!r}"
        task_id = match.group(1)
        claim = self._rpc("task_claim", agent_id, task_id=task_id)["data"]
        self.seen.append((agent_id, claim["status"]))
        if claim["status"] != "OK":
            return WakeResult(status="accepted", thread_id="thr-1")  # NOOP path
        submit = self._rpc(
            "task_submit",
            agent_id,
            task_id=task_id,
            lease_id=claim["lease_id"],
            evidence={
                "summary": f"implemented {task_id}",
                "tests_output": "pytest -q: 12 passed",
                "files_changed": "src/x.py",
            },
        )
        assert submit["ok"], submit
        return WakeResult(status="accepted", thread_id="thr-1")

    async def probe(self, thread_id: str | None):
        raise AssertionError("not used in this scenario")

    async def cancel(self, thread_id: str) -> bool:
        return True


def test_pointer_pull_submit_review_closes_the_loop(store):
    client = TestClient(create_app(store))
    transport = AgentLoopTransport(client)
    _, _, tasks = sprint_with_tasks(store)

    report = asyncio.run(Dispatcher(store, transport).tick())
    assert report.woken == [tasks[0]]
    assert transport.seen == [("dev-1", "OK")]
    assert store.get_task(tasks[0]).state == "submitted"

    # DoD runner and reviewer are separate identities on purpose.
    dod = client.post(
        "/rpc/dod_record",
        json={"params": {"task_id": tasks[0], "checks": [{"check_id": "unit", "ok": True}]}},
        headers={"X-Octop-Agent": "sm"},
    ).json()
    assert dod["data"]["all_passed"] is True
    accept = client.post(
        "/rpc/review_accept",
        json={"params": {"task_id": tasks[0], "dwell_seconds": 75}},
        headers={"X-Octop-Agent": "reviewer"},
    ).json()
    assert accept["ok"] is True

    assert store.get_task(tasks[0]).state == "done"
    assert store.replay_states(tasks[0]) == [
        "queued",
        "leased",
        "running",
        "submitted",
        "in_review",
        "done",
    ]


def test_second_wake_of_a_submitted_task_is_refused(store):
    """The duplicate-wake guard: the agent is told NOT_QUEUED and must stop."""
    client = TestClient(create_app(store))
    transport = AgentLoopTransport(client)
    _, _, tasks = sprint_with_tasks(store)
    asyncio.run(Dispatcher(store, transport).tick())

    claim = client.post(
        "/rpc/task_claim",
        json={"params": {"task_id": tasks[0]}},
        headers={"X-Octop-Agent": "dev-1"},
    ).json()["data"]
    assert claim["status"] == "NOT_QUEUED"
