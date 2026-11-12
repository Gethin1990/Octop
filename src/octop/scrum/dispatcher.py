"""The dispatcher: admit → claim → wake → confirm, plus the stall sweep.

Shape of a tick (order is load-bearing):

1. **expire** leases past TTL — nothing else can be trusted while ghost leases
   hold agent slots;
2. **drop** wakes that were injected but never acknowledged;
3. **stall** running tasks whose thread shows neither heartbeat nor progress;
4. **wake** as many queued tasks as the policy allows, cheapest-first
   (expedite before standard, then PBI rank);
5. **report** — including why admission said no, because that histogram is the
   only thing that distinguishes a slow team from a broken spec.

The dispatcher is intentionally *not* an actor system. One process, one loop, one
database writer. Everything else (agents, turns, tools) lives in Octop and is
treated as unreliable by design.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any

from .store import BoardStore, Task
from .transport import Transport, pointer_text, verify_pointer_text

logger = logging.getLogger("octop.scrum.dispatcher")


@dataclass
class TickReport:
    expired: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    woken: list[str] = field(default_factory=list)
    #: ``submitted`` cards handed to a verifier this tick (SM channel only).
    verify_woken: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    #: Cards a DoR denial sent back to the owner's Review panel this tick.
    handed_back: list[str] = field(default_factory=list)
    denied: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Dry-run only: what a live tick would have done. Kept separate from
    # ``woken`` so nobody mistakes a preview for an actual dispatch.
    would_wake: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expired": self.expired,
            "dropped": self.dropped,
            "stalled": self.stalled,
            "woken": self.woken,
            "verify_woken": self.verify_woken,
            "released": self.released,
            "handed_back": self.handed_back,
            "denied": self.denied,
            "notes": self.notes,
            "would_wake": self.would_wake,
        }


class Dispatcher:
    def __init__(
        self,
        store: BoardStore,
        transport: Transport,
        *,
        now: Any = None,
        dry_run: bool = False,
    ) -> None:
        self.store = store
        self.transport = transport
        # A dry tick is a *read-only* preview. It must not claim a lease: a
        # lease taken by a diagnostic command looks exactly like a real agent
        # working, and only expires after ``ack_timeout_s``.
        self.dry_run = dry_run
        self._now = now or time.time

    # ── occupancy ───────────────────────────────────────────────────────────
    def _occupied_agents(self) -> set[str]:
        return self.store.occupying_agent_ids()

    def _free_agent(self, role: str, occupied: set[str]) -> str | None:
        # Only Octop-backed identities: this path wakes a real turn, and a local
        # demo id has no turn to wake.
        for agent_id in self.store.agents_by_role(role, channel="octop"):
            if agent_id not in occupied:
                return agent_id
        return None

    # ── one tick ────────────────────────────────────────────────────────────
    async def tick(self) -> TickReport:
        report = TickReport()
        policy = self.store.policy()

        if self.dry_run:
            # Preview the admission decision without mutating anything: no
            # lease, no event rows, no wake. Safe to run against production.
            self._preview_wake(report, policy)
            return report

        report.expired = self.store.release_expired()

        for task_id in self.store.dropped_wakes(float(policy["ack_timeout_s"])):
            state = self.store.release(task_id, "wake_dropped")
            report.dropped.append(task_id)
            report.released.append(task_id)
            if state == "blocked":
                report.notes.append(f"{task_id}: wake undeliverable, blocked")

        await self._sweep_stalls(report, float(policy["stall_window_s"]))
        await self._wake_ready(report, policy)
        await self._verify_ready(report)
        return report

    async def _sweep_stalls(self, report: TickReport, stall_window_s: float) -> None:
        now = self._now()
        for task in self.store.tasks_in_state("running"):
            if not task.acked_at:
                continue
            # Fast path: a fresh lease means the agent is still heartbeating.
            if (
                task.lease_expires_at
                and task.lease_expires_at > now
                and now - task.acked_at < stall_window_s
            ):
                continue
            probe = await self.transport.probe(task.thread_id)
            last = probe.last_event_at or 0.0
            # "Progress" = the lease was renewed OR the thread produced an event.
            # Liveness (process up, socket open) is explicitly not enough.
            if probe.active or (last and now - last < stall_window_s):
                continue
            if now - max(task.acked_at, last) < stall_window_s:
                continue
            if task.thread_id:
                await self.transport.cancel(task.thread_id)
            state = self.store.release(task.task_id, "stalled")
            report.stalled.append(task.task_id)
            report.released.append(task.task_id)
            report.notes.append(f"{task.task_id}: stalled -> {state}")

    def _preview_wake(self, report: TickReport, policy: Any) -> None:
        """Report ``(task, agent)`` pairs a live tick would dispatch.

        Mirrors ``_wake_ready`` exactly, minus every write. ``note_admission``
        is skipped on purpose: a diagnostic run should not inflate the denial
        histogram the dashboard reads.
        """
        budget = int(policy["wakes_per_tick"])
        candidates = self.store.next_candidates(limit=budget * 3)
        occupied = self._occupied_agents()
        for task in candidates:
            if budget <= 0:
                break
            admit = self.store.admit(task.task_id)
            if not admit.ok:
                report.denied[admit.reason] = report.denied.get(admit.reason, 0) + 1
                continue
            agent_id = self._free_agent(task.role, occupied)
            if agent_id is None:
                report.denied["no_slot"] = report.denied.get("no_slot", 0) + 1
                continue
            occupied.add(agent_id)
            budget -= 1
            report.would_wake.append(
                {"task_id": task.task_id, "agent_id": agent_id, "role": task.role}
            )

    async def _wake_ready(self, report: TickReport, policy: Any) -> None:
        budget = int(policy["wakes_per_tick"])
        candidates = self.store.next_candidates(limit=budget * 3)
        if not candidates:
            return
        occupied = self._occupied_agents()
        for task in candidates:
            if budget <= 0:
                break
            admit = self.store.admit(task.task_id)
            if not admit.ok:
                report.denied[admit.reason] = report.denied.get(admit.reason, 0) + 1
                if self.store.deny_admission(task.task_id, admit.reason, admit.detail):
                    report.handed_back.append(task.task_id)
                continue
            agent_id = self._free_agent(task.role, occupied)
            if agent_id is None:
                report.denied["no_slot"] = report.denied.get("no_slot", 0) + 1
                self.store.note_admission(task.task_id, "no_slot", {"role": task.role})
                continue
            lease = self.store.claim(task.task_id, agent_id)
            if lease is None:
                continue
            occupied.add(agent_id)
            budget -= 1
            await self.wake(task, lease, agent_id, report)

    # ── verification ────────────────────────────────────────────────────────
    async def _verify_ready(self, report: TickReport) -> None:
        """Ask a Scrum Master agent to verify each freshly submitted increment.

        This hop is the reason "done" means something. Without it the local
        process both submits and certifies, and DoD collapses into self-report —
        the exact failure the Scrum split is supposed to prevent. Only
        ``channel='octop'`` identities are asked, because a verifier has to be a
        different mind than the executor, not the same process under a new name.
        """
        verifiers = self.store.agents_by_role("sm", channel="octop")
        if not verifiers:
            return
        for task in self.store.tasks_awaiting_dod():
            agent_id = verifiers[0]
            # Written before the wake: a crash in between must not produce a
            # second ask on the next tick.
            self.store.mark_verify_woken(task.task_id, agent_id)
            result = await self.transport.send_pointer(
                agent_id, verify_pointer_text(task.task_id), None
            )
            if result.status == "accepted":
                report.verify_woken.append(task.task_id)
            else:
                report.notes.append(
                    f"{task.task_id}: verify wake {result.status}:{result.reason}"
                )

    # ── wake ────────────────────────────────────────────────────────────────
    async def wake(
        self,
        task: Task,
        lease: Any,
        agent_id: str,
        report: TickReport,
    ) -> None:
        text = pointer_text(
            task.task_id, lease.lease_id, task.attempt + 1, task.max_attempts
        )
        # Record the intent *before* touching the network. If the process dies
        # mid-injection, this timestamp is the only evidence that a wake may be
        # in flight — without it reconcile cannot tell "never sent" from "lost".
        self.store.mark_woken(task.task_id, thread_id=task.thread_id)
        result = await self.transport.send_pointer(agent_id, text, task.thread_id)
        if result.accepted:
            if result.thread_id and result.thread_id != task.thread_id:
                self.store.set_thread(task.task_id, result.thread_id)
            report.woken.append(task.task_id)
            return
        # Nothing ran: give the slot back without burning an attempt.
        self.store.release(task.task_id, "wake_rejected")
        report.notes.append(f"{task.task_id}: wake {result.status}:{result.reason}")

    async def run_forever(self, interval_s: float = 5.0) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # non-POSIX
                loop.add_signal_handler(sig, stop.set)
        while not stop.is_set():
            try:
                report = await self.tick()
                if any(report.as_dict().get(k) for k in ("woken", "stalled", "dropped", "expired")):
                    logger.info("scrum tick: %s", report.as_dict())
            except Exception:  # noqa: BLE001 - a tick must never kill the loop
                logger.exception("scrum tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                continue
