"""Restart reconciliation: adopt, release, or hold — never blindly re-dispatch."""

from __future__ import annotations

import pytest

from octop.scrum.reconcile import reconcile
from octop.scrum.transport import FakeTransport

from .helpers import evidence, sprint_with_tasks


class Clock:
    def __init__(self, start: float = 2_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


@pytest.fixture()
def clock():
    return Clock()


async def _running_task(store, clock, *, agent="dev-1"):
    _, _, tasks = sprint_with_tasks(store)
    lease = store.claim(tasks[0], agent)
    assert lease is not None
    assert store.ack(tasks[0], lease.lease_id)
    store.set_thread(tasks[0], "thr-1")
    return tasks[0], lease


async def test_adopts_a_task_that_is_genuinely_running(store, clock):
    store._now = clock  # noqa: SLF001
    task_id, _ = await _running_task(store, clock)
    transport = FakeTransport(probe_active=True, probe_last_event_at=clock() - 5)

    report = await reconcile(store, transport, now=clock())

    assert report.adopted == [task_id]
    task = store.get_task(task_id)
    assert task is not None
    assert task.state == "running"  # adopted, not re-dispatched
    assert task.lease_id


async def test_releases_a_lease_whose_thread_is_gone(store, clock):
    store._now = clock  # noqa: SLF001
    task_id, _ = await _running_task(store, clock)
    transport = FakeTransport(probe_active=False, probe_last_event_at=None)

    report = await reconcile(store, transport, now=clock())

    assert report.lease_lost == [task_id]
    task = store.get_task(task_id)
    assert task is not None
    assert task.state == "queued"
    assert task.agent_id is None


async def test_holds_a_zombie_instead_of_stealing_it(store, clock):
    """Alive but silent: let the hard deadline end it. Never race a live writer."""
    store._now = clock  # noqa: SLF001
    task_id, _ = await _running_task(store, clock)
    transport = FakeTransport(probe_active=True, probe_last_event_at=clock() - 7200)

    report = await reconcile(store, transport, now=clock())

    assert report.zombie == [task_id]
    assert report.lease_lost == []
    task = store.get_task(task_id)
    assert task is not None
    assert task.state == "running"
    assert transport.cancels == []


async def test_releases_a_task_whose_agent_is_gone(store, clock):
    store._now = clock  # noqa: SLF001
    task_id, _ = await _running_task(store, clock)
    store.register_agent("dev-1", "dev", enabled=False)
    transport = FakeTransport(probe_active=True, probe_last_event_at=clock())

    report = await reconcile(store, transport, now=clock())

    assert report.identity_drift == [task_id]
    assert store.get_task(task_id).state == "queued"


async def test_flags_submitted_tasks_without_dod_rows(store, clock):
    store._now = clock  # noqa: SLF001
    _, _, tasks = sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    store.submit(tasks[0], lease.lease_id, evidence=evidence())

    report = await reconcile(store, FakeTransport(), now=clock())

    assert report.needs_dod == [tasks[0]]


async def test_fixes_double_occupancy(store, clock):
    store._now = clock  # noqa: SLF001
    _, _, tasks = sprint_with_tasks(store, count=2)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    # Force a second live lease onto the same agent, as a crashed dispatcher would.
    with store.tx() as conn:
        conn.execute(
            "UPDATE task SET state='running', agent_id='dev-1', lease_id='ghost',"
            " lease_expires_at=?, acked_at=? WHERE task_id=?",
            (clock() + 9999, clock(), tasks[1]),
        )

    report = await reconcile(
        store, FakeTransport(probe_active=True, probe_last_event_at=clock()), now=clock()
    )

    assert report.occupancy_fixed == [tasks[1]]
    assert store.get_task(tasks[0]).state in ("leased", "running")
    assert store.get_task(tasks[1]).state == "queued"
    assert store.occupying_agent_ids() == {"dev-1"}


async def test_reconcile_always_leaves_a_record(store, clock):
    store._now = clock  # noqa: SLF001
    before = store.event_count()
    await reconcile(store, FakeTransport(), now=clock())
    assert store.event_count() == before + 1  # even a no-op reconcile is logged
