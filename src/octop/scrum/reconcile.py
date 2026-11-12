"""Restart reconciliation — six steps, order matters.

Run this **before** the dispatcher starts serving. A dispatcher that begins
waking tasks without reconciling will re-dispatch work that is still running,
and two agents editing one branch costs more to repair than the whole sprint.

The interesting decision is what to do with a lease whose thread still looks
active but has produced nothing for a long time ("zombie"). We do **not** steal
it. We let it run until its hard deadline and let the lease expire on its own.
That wastes at most one TTL and removes the most dangerous class of bug: two
concurrent writers on one branch. Wasting work is cheap; merging is not.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .store import BoardStore, Task
from .transport import Transport

logger = logging.getLogger("octop.scrum.reconcile")


@dataclass
class ReconcileReport:
    adopted: list[str] = field(default_factory=list)
    lease_lost: list[str] = field(default_factory=list)
    zombie: list[str] = field(default_factory=list)
    needs_dod: list[str] = field(default_factory=list)
    identity_drift: list[str] = field(default_factory=list)
    occupancy_fixed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"reconcile adopted={len(self.adopted)} lost={len(self.lease_lost)} "
            f"zombie={len(self.zombie)} dod={len(self.needs_dod)} "
            f"drift={len(self.identity_drift)} occ={len(self.occupancy_fixed)}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": self.adopted,
            "lease_lost": self.lease_lost,
            "zombie": self.zombie,
            "needs_dod": self.needs_dod,
            "identity_drift": self.identity_drift,
            "occupancy_fixed": self.occupancy_fixed,
            "notes": self.notes,
        }


async def reconcile(
    store: BoardStore,
    transport: Transport,
    *,
    now: float | None = None,
    actor: str = "reconcile",
) -> ReconcileReport:
    now = now or time.time()
    policy = store.policy()
    stall_window = float(policy["stall_window_s"])
    ack_timeout = float(policy["ack_timeout_s"])
    report = ReconcileReport()

    # R1 — take the snapshot of anything that claims to own an agent slot.
    live: list[Task] = store.tasks_in_state("leased", "running")

    # R5 (before R2) — drop tasks whose agent no longer exists or lost the role.
    # Adopting a task for a deleted agent would just keep the slot occupied.
    for task in live:
        if task.state == "queued" or not task.agent_id:
            continue
        if store.role_of(task.agent_id) != task.role:
            store.release(task.task_id, "identity_drift", actor=actor)
            report.identity_drift.append(task.task_id)
            report.notes.append(f"{task.task_id}: agent {task.agent_id} gone/role changed")

    live = store.tasks_in_state("leased", "running")

    # R2 — classify each remaining lease by observation, not by assumption.
    for task in live:
        probe = await transport.probe(task.thread_id)
        last = probe.last_event_at or 0.0
        fresh = bool(last) and (now - last) < stall_window
        if probe.active and (fresh or task.state == "leased"):
            store.adopt(task.task_id, actor=actor)
            report.adopted.append(task.task_id)
            continue
        if probe.active:
            # Alive but not progressing: hold, let the hard deadline end it.
            report.zombie.append(task.task_id)
            report.notes.append(f"{task.task_id}: zombie, held until hard deadline")
            continue
        store.release(task.task_id, "expired", actor=actor)
        report.lease_lost.append(task.task_id)

    # R3 — rebuild occupancy: at most one in-flight task per agent identity.
    seen: dict[str, str] = {}
    for task in store.tasks_in_state("leased", "running"):
        if not task.agent_id:
            continue
        if task.agent_id in seen:
            store.release(task.task_id, "human", actor=actor)
            report.occupancy_fixed.append(task.task_id)
            report.notes.append(f"{task.task_id}: second lease for {task.agent_id}, released")
        else:
            seen[task.agent_id] = task.task_id

    # R4 — wakes that were recorded but never acknowledged.
    for task_id in store.dropped_wakes(ack_timeout):
        store.release(task_id, "wake_dropped", actor=actor)
        report.lease_lost.append(task_id)

    # R2b — submitted without any DoD rows: the runner died mid-check.
    for task in store.tasks_in_state("submitted"):
        if not store.has_dod_checks(task.task_id):
            report.needs_dod.append(task.task_id)

    # R6 — record the reconciliation itself. "Nothing to do" must also be logged,
    # otherwise a silent reconcile is indistinguishable from a crashed one.
    with store.tx() as conn:
        store.log(
            conn,
            "reconcile",
            "system",
            uuid.uuid4().hex[:8],
            actor,
            adopted=len(report.adopted),
            lease_lost=len(report.lease_lost),
            zombie=len(report.zombie),
            identity_drift=len(report.identity_drift),
        )
    logger.info("scrum %s", report.summary())
    return report
