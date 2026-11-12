"""Channel isolation: a local executor may not wear a real agent's name.

This file exists because of an actual incident on the demo board. The local
worker claimed a card, and the only enabled ``dev`` happened to be a *real*
Octop agent id, so the board showed an agent name next to a template file that
agent never produced. Splitting ``agent_role.channel`` fixes it structurally;
these tests pin the fix so it cannot regress silently.
"""

from __future__ import annotations

import asyncio

from octop.scrum.dispatcher import Dispatcher
from octop.scrum.store import BoardStore
from octop.scrum.transport import FakeTransport
from octop.scrum.worker import DemoExecutor, Worker

from .helpers import sprint_with_tasks


def _dispatcher(store: BoardStore) -> tuple[Dispatcher, FakeTransport]:
    transport = FakeTransport()
    return Dispatcher(store, transport, dry_run=False), transport


def test_dispatcher_never_wakes_a_local_only_identity(store, tmp_path):
    """A local demo id has no turn to wake, so it must not be scheduled."""
    _, _, tasks = sprint_with_tasks(store, count=1)
    # helpers seeds channel="any"; force this one back to local-only.
    store.register_agent("dev-1", "dev", channel="local")
    store.register_agent("dev-2", "dev", channel="local")
    store.register_agent("dev-3", "dev", channel="local")

    dispatcher, transport = _dispatcher(store)
    report = asyncio.run(dispatcher.tick())

    assert report.woken == []
    assert transport.wakes == []
    assert report.denied.get("no_slot") == 1
    assert store.get_task(tasks[0]).state == "queued"


def test_dispatcher_wakes_an_octop_identity(store, tmp_path):
    """The mirror case: an Octop-backed dev is exactly who should be woken."""
    _, _, tasks = sprint_with_tasks(store, count=1)
    store.register_agent("dev-1", "dev", channel="octop")
    store.register_agent("dev-2", "dev", channel="octop")
    store.register_agent("dev-3", "dev", channel="octop")

    dispatcher, transport = _dispatcher(store)
    report = asyncio.run(dispatcher.tick())

    assert report.woken == [tasks[0]]
    assert transport.wakes and transport.wakes[0][0] == "dev-1"


def test_local_worker_refuses_to_claim_an_octop_identity(store, tmp_path):
    """The failure that started this: a template file attributed to a real agent."""
    _, _, tasks = sprint_with_tasks(store, count=1)
    for agent_id in ("dev-1", "dev-2", "dev-3"):
        store.register_agent(agent_id, "dev", channel="octop")

    worker = Worker(store, DemoExecutor(artifacts=tmp_path), log=lambda _m: None)
    result = worker.step()

    assert result.action == "idle"
    assert result.detail == "no_slot"
    task = store.get_task(tasks[0])
    assert task is not None
    assert task.state == "queued"
    assert task.agent_id is None  # nobody wore the name
    assert not list(tmp_path.glob("*.md"))  # and nothing was written


def test_channel_filter_matches_the_documented_set(store):
    """``agents_by_role(channel=...)`` is the single source of truth for both sides."""
    store.register_agent("local-dev", "dev", channel="local")
    store.register_agent("real-dev", "dev", channel="octop")
    store.register_agent("test-dev", "dev", channel="any")

    local = store.agents_by_role("dev", channel="local")
    octop = store.agents_by_role("dev", channel="octop")

    assert "local-dev" in local and "real-dev" not in local
    assert "real-dev" in octop and "local-dev" not in octop
    assert "test-dev" in local and "test-dev" in octop  # the harness opt-in
    assert set(store.agents_by_role("dev")) >= {"local-dev", "real-dev", "test-dev"}


def test_channel_survives_registration_update(store):
    """Re-registering an id must move it, not leave a stale channel behind."""
    store.register_agent("dev-9", "dev", channel="local")
    store.register_agent("dev-9", "dev", channel="octop")

    assert "dev-9" in store.agents_by_role("dev", channel="octop")
    assert "dev-9" not in store.agents_by_role("dev", channel="local")
