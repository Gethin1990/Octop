"""The verification hop: submitted work is handed to a *different* agent.

Without this hop the board certified its own work — the process that submitted
also ran DoD, which is self-report wearing a checklist. These tests pin the
handoff: an SM wake, exactly one per cooldown window, and never to the executor.
"""

from __future__ import annotations

import asyncio

from octop.scrum.dispatcher import Dispatcher
from octop.scrum.store import BoardStore
from octop.scrum.transport import FakeTransport

from .helpers import sprint_with_tasks


def _submit_one(store: BoardStore, task_id: str) -> None:
    lease = store.claim(task_id, "dev-1")
    assert lease is not None
    store.ack(task_id, lease.lease_id)
    store.submit(
        task_id,
        lease.lease_id,
        evidence={"summary": "did it", "tests_output": "ok", "files_changed": "x.py"},
    )
    assert store.get_task(task_id).state == "submitted"


def _octop_team(store: BoardStore) -> None:
    """One real dev, two real verifiers — nothing local in the picture.

    The shared ``store`` fixture seeds harness identities on channel ``any``,
    which would also qualify as verifiers. Pushing them back to ``local`` keeps
    this test about the real handoff instead of about fixture ordering.
    """
    store.register_agent("sm", "sm", channel="local")
    store.register_agent("dev-1", "dev", channel="octop")
    store.register_agent("sm-1", "sm", channel="octop")
    store.register_agent("sm-2", "sm", channel="octop")


def test_submitted_work_is_handed_to_an_sm_not_back_to_the_dev(store):
    _octop_team(store)
    _, _, tasks = sprint_with_tasks(store, count=1)
    _submit_one(store, tasks[0])

    transport = FakeTransport()
    report = asyncio.run(Dispatcher(store, transport).tick())

    assert report.verify_woken == [tasks[0]]
    assert [wake[0] for wake in transport.wakes] == ["sm-1"]
    assert "VERIFY" in transport.wakes[0][1]
    # The executor must never be asked to certify its own increment.
    assert "dev-1" not in [wake[0] for wake in transport.wakes]


def test_the_verifier_is_not_woken_twice_inside_the_cooldown(store):
    _octop_team(store)
    _, _, tasks = sprint_with_tasks(store, count=1)
    _submit_one(store, tasks[0])

    transport = FakeTransport()
    dispatcher = Dispatcher(store, transport)
    asyncio.run(dispatcher.tick())
    second = asyncio.run(dispatcher.tick())

    assert second.verify_woken == []
    assert len(transport.wakes) == 1


def test_no_sm_means_no_verify_wake_rather_than_a_self_check(store):
    """Degrade to silence, not to self-certification."""
    store.register_agent("sm", "sm", channel="local")  # the harness identity
    store.register_agent("dev-1", "dev", channel="octop")
    store.register_agent("sm-1", "sm", channel="local")  # a local id cannot verify
    _, _, tasks = sprint_with_tasks(store, count=1)
    _submit_one(store, tasks[0])

    transport = FakeTransport()
    report = asyncio.run(Dispatcher(store, transport).tick())

    assert report.verify_woken == []
    assert transport.wakes == []
    assert store.get_task(tasks[0]).state == "submitted"  # waits for a human, visibly


def test_dry_run_never_asks_a_verifier(store):
    """A preview must not spend a real agent's turn."""
    _octop_team(store)
    _, _, tasks = sprint_with_tasks(store, count=1)
    _submit_one(store, tasks[0])

    transport = FakeTransport()
    report = asyncio.run(Dispatcher(store, transport, dry_run=True).tick())

    assert report.verify_woken == []
    assert transport.wakes == []
    assert store.get_task(tasks[0]).verify_wake_at is None
