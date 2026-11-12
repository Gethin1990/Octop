"""Dispatcher behaviour: admission, wake accounting, and the stall sweep."""

from __future__ import annotations

import pytest

from octop.scrum.dispatcher import Dispatcher
from octop.scrum.transport import FakeTransport

from .helpers import sprint_with_tasks, to_review


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def wired(store, clock):
    store._now = clock  # noqa: SLF001 - test seam, the store takes an injectable clock
    transport = FakeTransport()
    dispatcher = Dispatcher(store, transport, now=clock)
    return store, transport, dispatcher


async def test_tick_wakes_one_task_with_a_pointer(wired):
    store, transport, dispatcher = wired
    _, _, tasks = sprint_with_tasks(store)
    report = await dispatcher.tick()

    assert report.woken == [tasks[0]]
    assert len(transport.wakes) == 1
    agent_id, text, thread_id = transport.wakes[0]
    assert agent_id == "dev-1"
    assert tasks[0] in text and "NOT_YOURS" in text
    assert thread_id is None  # first wake has no thread yet

    task = store.get_task(tasks[0])
    assert task is not None
    assert task.state == "leased"
    assert task.lease_id and task.last_wake_at


async def test_wake_is_not_repeated_while_leased(wired):
    store, transport, dispatcher = wired
    _, _, tasks = sprint_with_tasks(store)
    await dispatcher.tick()
    await dispatcher.tick()
    assert len(transport.wakes) == 1
    assert store.get_task(tasks[0]).state == "leased"


async def test_rejected_wake_returns_the_slot_without_burning_an_attempt(wired):
    store, transport, dispatcher = wired
    transport = FakeTransport(default="rejected")
    dispatcher = Dispatcher(store, transport, now=store._now)  # noqa: SLF001
    _, _, tasks = sprint_with_tasks(store)

    await dispatcher.tick()

    task = store.get_task(tasks[0])
    assert task is not None
    assert task.state == "queued"
    assert task.attempt == 0  # nothing ran, so nothing was spent
    assert task.wake_failures == 1
    assert store.occupying_agent_ids() == set()


async def test_repeated_wake_failures_end_in_blocked(wired, clock):
    store, _transport, _dispatcher = wired
    store.bump_policy({"max_wake_failures": 2}, note="two strikes")
    transport = FakeTransport(default="rejected")
    dispatcher = Dispatcher(store, transport, now=clock)
    _, _, tasks = sprint_with_tasks(store)

    await dispatcher.tick()
    clock.advance(700)  # past the backoff gate, otherwise the retry is not due
    await dispatcher.tick()
    clock.advance(700)
    await dispatcher.tick()

    task = store.get_task(tasks[0])
    assert task is not None
    assert task.state == "blocked"
    assert task.blocked_reason == "wake_rejected"


async def test_dropped_wake_is_released_after_ack_timeout(wired, clock):
    store, transport, dispatcher = wired
    _, _, tasks = sprint_with_tasks(store)
    await dispatcher.tick()
    assert store.get_task(tasks[0]).state == "leased"

    clock.advance(store.policy()["ack_timeout_s"] + 1)
    report = await dispatcher.tick()

    assert tasks[0] in report.dropped
    task = store.get_task(tasks[0])
    assert task is not None
    assert task.state == "queued"
    assert task.wake_failures == 1
    # Backoff: the next tick must not immediately retry a dropped injection.
    assert task.retry_after and task.retry_after > clock()
    again = await dispatcher.tick()
    assert again.woken == []


async def test_stall_uses_progress_not_liveness(store, clock):
    """A thread whose last event is an hour old is stalled even if it looks busy."""
    store._now = clock  # noqa: SLF001
    store.bump_policy({"lease_ttl_s": 3600, "stall_window_s": 10}, note="long lease, short stall")
    transport = FakeTransport(probe_active=False, probe_last_event_at=clock() - 3600)
    dispatcher = Dispatcher(store, transport, now=clock)
    _, _, tasks = sprint_with_tasks(store)

    await dispatcher.tick()
    lease = store.get_task(tasks[0]).lease_id
    assert lease is not None
    assert store.ack(tasks[0], lease)

    clock.advance(30)
    report = await dispatcher.tick()

    assert tasks[0] in report.stalled
    assert transport.cancels  # the turn is cancelled before the lease is dropped
    assert store.get_task(tasks[0]).state == "queued"


async def test_fresh_progress_prevents_the_stall_sweep(store, clock):
    store._now = clock  # noqa: SLF001
    store.bump_policy({"lease_ttl_s": 3600, "stall_window_s": 10}, note="long lease")
    transport = FakeTransport(probe_active=False, probe_last_event_at=clock() - 2)
    dispatcher = Dispatcher(store, transport, now=clock)
    _, _, tasks = sprint_with_tasks(store)
    await dispatcher.tick()
    lease = store.get_task(tasks[0]).lease_id
    assert lease and store.ack(tasks[0], lease)

    clock.advance(30)  # note: last event stays 32s old at evaluation time
    transport._probe_last_event_at = clock() - 2  # noqa: SLF001 - fresh again
    report = await dispatcher.tick()
    assert report.stalled == []


async def test_dor_failure_hands_the_request_back(store, clock):
    """A missing acceptance list is the owner's input, not a retry loop.

    Regression: the L2 dispatcher recorded ``admit_denied`` and moved on, so
    the card stayed ``queued`` forever - re-checked every tick, and invisible in
    the owner's Review panel where the fix has to happen.
    """
    store._now = clock  # noqa: SLF001
    transport = FakeTransport()
    dispatcher = Dispatcher(store, transport, now=clock)
    _, pbi_ids, tasks = sprint_with_tasks(store, acceptance=())

    report = await dispatcher.tick()

    assert report.denied.get("dor_fail") == 1
    assert report.handed_back == [tasks[0]]
    assert transport.wakes == []
    task = store.get_task(tasks[0])
    assert task.state == "needs_revision"
    assert task.blocked_reason == "dor_fail:missing_acceptance"
    assert store.get_pbi(pbi_ids[0])["state"] == "committed"


async def test_handed_back_request_is_not_denied_again(store, clock):
    store._now = clock  # noqa: SLF001
    dispatcher = Dispatcher(store, FakeTransport(), now=clock)
    _, _, tasks = sprint_with_tasks(store, acceptance=())
    await dispatcher.tick()

    second = await dispatcher.tick()

    # Out of the queue means out of the denial histogram: the owner sees one
    # card in Review, not an event every five seconds.
    assert second.denied.get("dor_fail") is None
    assert second.handed_back == []
    assert store.get_task(tasks[0]).state == "needs_revision"


async def test_revision_criteria_put_the_card_back_in_the_queue(store, clock):
    store._now = clock  # noqa: SLF001
    transport = FakeTransport()
    dispatcher = Dispatcher(store, transport, now=clock)
    _, pbi_ids, tasks = sprint_with_tasks(store, acceptance=())
    await dispatcher.tick()

    result = store.revise_request(
        pbi_ids[0], title="PBI 0", body="", acceptance=["收盘价正确"],
        actor="owner", note="补验收条件",
    )

    assert result["resubmitted"] is True
    assert store.get_task(tasks[0]).state == "queued"
    report = await dispatcher.tick()
    assert report.woken == [tasks[0]]


async def test_review_backpressure_is_reported_not_silent(store, clock):
    store._now = clock  # noqa: SLF001
    store.bump_policy({"review_queue_cap": 1}, note="cap")
    transport = FakeTransport()
    dispatcher = Dispatcher(store, transport, now=clock)
    _, _, tasks = sprint_with_tasks(store, count=2)
    to_review(store, tasks[0])

    report = await dispatcher.tick()

    assert report.denied.get("review_backpressure") == 1
    assert transport.wakes == []
    assert store.admit_denied_histogram().get("review_backpressure") == 1


async def test_expedite_is_woken_before_standard(store, clock):
    store._now = clock  # noqa: SLF001
    sprint_id = store.create_sprint("S1", capacity_pts=100.0, interrupt_ratio=1.0)
    normal = store.create_pbi("normal", points=1, acceptance=["x"], priority_rank=1)
    urgent = store.create_pbi("urgent", points=1, acceptance=["x"], priority_rank=99,
                              class_of_service="expedite")
    store.commit_sprint(sprint_id, [normal, urgent])
    normal_task = store.create_task(normal, "normal work")
    urgent_task = store.create_task(urgent, "urgent work")

    report = await Dispatcher(store, FakeTransport(), now=clock).tick()

    # Both fit in the two free slots, but the expedite card must be woken first.
    assert report.woken[0] == urgent_task
    assert normal_task in report.woken


async def test_no_slot_when_every_agent_is_busy(store, clock):
    store._now = clock  # noqa: SLF001
    store.register_agent("dev-2", "dev", enabled=False)
    store.register_agent("dev-3", "dev", enabled=False)
    _, _, tasks = sprint_with_tasks(store, count=2)
    report = await Dispatcher(store, FakeTransport(), now=clock).tick()

    assert report.woken == [tasks[0]]
    assert report.denied.get("no_slot") == 1


async def test_dry_run_previews_without_touching_the_board(store, clock):
    """A dry tick is a read-only preview — it must not claim or log."""
    store._now = clock  # noqa: SLF001
    _, _, tasks = sprint_with_tasks(store)
    events_before = store.event_count()
    transport = FakeTransport()

    report = await Dispatcher(store, transport, now=clock, dry_run=True).tick()

    task = store.get_task(tasks[0])
    assert task is not None
    assert task.state == "queued"  # no lease taken
    assert task.lease_id is None
    assert task.agent_id is None
    assert store.event_count() == events_before  # nothing written
    assert transport.wakes == []  # nothing sent
    assert report.woken == []
    assert report.would_wake == [
        {"task_id": tasks[0], "agent_id": "dev-1", "role": "dev"}
    ]


async def test_dry_run_still_reports_admission_denials(store, clock):
    store._now = clock  # noqa: SLF001
    for agent_id in ("dev-1", "dev-2", "dev-3"):
        store.register_agent(agent_id, "dev", enabled=False)
    _, _, tasks = sprint_with_tasks(store, count=2)
    events_before = store.event_count()

    report = await Dispatcher(store, FakeTransport(), now=clock, dry_run=True).tick()

    assert report.denied.get("no_slot") == 2
    assert report.would_wake == []
    # Preview must not write to the denial histogram it is reporting.
    assert store.event_count() == events_before
