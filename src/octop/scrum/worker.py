"""The thing that makes the board move without a human in the loop.

Scrum says a developer pulls work, does it, and reports an increment. A person
clicking "claim / submit" is not a developer — that is the board's user doing the
team's job. This module is the team: it sizes requests, pulls admissible cards,
executes them, submits evidence, and runs DoD, so the owner only ever sees a card
appear and later arrive in ``in_review``.

Three deliberate properties:

* **Sizing is team work.** The owner is never asked for story points. A dev
  agent estimates every unsized request before it can be pulled, and the same
  agent is preferred when the card is claimed.
* **Execution is pluggable.** The executor returns an :class:`Outcome` carrying
  real evidence. ``DemoExecutor`` writes an artefact and then *verifies it* — the
  ``ok`` flag comes from inspecting output, not from a hard-coded ``True``. Point
  ``--exec`` at a real command to make it do the real work.
* **Rejection is visible, not a spinner.** A request that fails DoR is sent back
  to the owner's Review panel as ``needs_revision``; a card that cannot be
  executed is ``blocked`` with a reason. Neither sits in ``queued`` pretending
  to be healthy, and neither retries forever.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .estimate import Estimator, HeuristicEstimator
from .store import BoardStore, Task

#: Evidence keys ``submit`` refuses to accept without. Mirrors ``policy.dod``.
DEFAULT_REQUIRED = ("summary", "tests_output", "files_changed")

#: A worker older than this is presumed dead. The board shows it, because a
#: frozen board with no explanation is the worst possible failure mode.
STALE_AFTER_S = 15.0


def heartbeat_path(store: BoardStore) -> Path:
    """Where the worker advertises that it is alive (next to the database)."""
    return store.path.parent / (store.path.name + ".worker")


def read_heartbeat(store: BoardStore) -> float | None:
    """Seconds since the worker last checked in, or ``None`` if it never has."""
    path = heartbeat_path(store)
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


@dataclass
class Outcome:
    """What an executor hands back: did it work, and what proves it."""

    ok: bool
    evidence: dict[str, Any]
    note: str = ""


class Executor(Protocol):
    name: str

    def run(self, task: Task) -> Outcome: ...


#: Declared in ``evidence`` so the board can say who actually did the work.
#: A card without this field is treated as unproven rather than as real work.
PLACEHOLDER_EXECUTOR = "demo/placeholder"
PLACEHOLDER_MARK = "PLACEHOLDER: not executed by an Octop agent"


@dataclass
class DemoExecutor:
    """Control-plane smoke test, NOT an agent and NOT a deliverable.

    It writes a markdown skeleton and checks that the file exists. That is enough
    to prove the state machine, lease and DoD wiring move a card, and it is *not*
    enough to answer the owner's request. Every artefact therefore says so in
    plain text and the evidence declares ``executor=demo/placeholder``. Real
    increments must come from an Octop agent turn (see ``ws_transport.py``).
    """

    artifacts: Path
    name: str = "demo"

    def run(self, task: Task) -> Outcome:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        path = self.artifacts / f"{task.task_id}.md"
        body = (
            f"# {task.title}\n\n"
            f"- task: {task.task_id}\n"
            f"- branch: {task.branch}\n"
            f"- attempt: {task.attempt}\n\n"
            "## Spec\n\n"
            f"{task.spec or '(no extra spec)'}\n\n"
            "## Result\n\n"
            f"> **⚠ 占位产物 / PLACEHOLDER** — {PLACEHOLDER_MARK}.\n"
            "> 本地演示执行器只写了这个模板文件，没有调用任何 Octop dev agent，\n"
            "> 因此这里**没有该需求的业务结果**，不能作为验收依据。\n"
            "> 要拿到真实结果，需接通 L2：真实 agent turn + 插件工具。\n"
        )
        path.write_text(body, encoding="utf-8")

        text = path.read_text(encoding="utf-8")
        checks = {
            "file_written": path.exists(),
            "non_empty": len(text) > 0,
            "has_spec_section": "## Spec" in text,
        }
        ok = all(checks.values())
        return Outcome(
            ok=ok,
            evidence={
                "summary": f"写入 {path.name}",
                "tests_output": " ".join(
                    f"{name}={'ok' if passed else 'FAIL'}" for name, passed in checks.items()
                ),
                "files_changed": str(path),
                "artifact": str(path),
                "executor": PLACEHOLDER_EXECUTOR,
            },
            note="placeholder, no agent ran",
        )


@dataclass
class ShellExecutor:
    """Runs one shell command per card, in ``cwd``, with the card in the env."""

    command: str
    cwd: Path
    timeout_s: float = 300.0
    name: str = "shell"

    def run(self, task: Task) -> Outcome:
        env = {
            **os.environ,
            "SCRUM_TASK_ID": task.task_id,
            "SCRUM_TASK_TITLE": task.title,
            "SCRUM_SPEC": task.spec or "",
            "SCRUM_BRANCH": task.branch or "",
        }
        started = time.monotonic()
        try:
            # shell=True on purpose: a task spec is a command line, pipes included.
            proc = subprocess.run(  # noqa: S602
                self.command,
                shell=True,
                cwd=str(self.cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Outcome(
                ok=False,
                evidence={"summary": "命令超时", "tests_output": "", "files_changed": ""},
                note=f"timeout after {self.timeout_s}s",
            )
        elapsed = time.monotonic() - started
        tail = (proc.stdout + proc.stderr).strip()[-1500:]
        return Outcome(
            ok=proc.returncode == 0,
            evidence={
                "summary": f"`{self.command}` exit={proc.returncode} in {elapsed:.1f}s",
                "tests_output": tail or "(no output)",
                "files_changed": "(run git status in cwd to see the delta)",
                "executor": "shell",
            },
            note=f"exit={proc.returncode}",
        )


@dataclass
class DodRunner:
    """Re-checks the world instead of trusting the caller.

    ``submit`` already refuses an evidence dict with missing keys, so repeating
    that check would be theatre. What this adds is the part a lying or confused
    executor cannot fake: if the evidence claims an artefact, the file has to be
    on disk and non-empty.
    """

    required: tuple[str, ...] = DEFAULT_REQUIRED

    def check(self, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = [
            {"check_id": f"evidence:{key}", "ok": bool(evidence.get(key))}
            for key in self.required
        ]
        claimed = evidence.get("artifact")
        if claimed:
            path = Path(str(claimed))
            ok = path.exists() and path.stat().st_size > 0
            checks.append({"check_id": "artifact:exists", "ok": ok, "output_tail": str(path)})
        return checks


@dataclass
class StepResult:
    """One card, one action. Returned so the CLI and tests can assert on it."""

    task_id: str | None = None
    action: str = "idle"
    detail: str = ""
    state: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)


class Worker:
    """Pull → execute → submit → DoD. One card at a time, per free agent."""

    def __init__(
        self,
        store: BoardStore,
        executor: Executor,
        *,
        step_delay: float = 0.0,
        dod: DodRunner | None = None,
        estimator: Estimator | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.store = store
        self.executor = executor
        self.step_delay = step_delay
        self.dod = dod or DodRunner()
        self.estimator = estimator or HeuristicEstimator()
        self.log = log
        self.denied: dict[str, int] = {}
        #: Sizing is attributed to the agent that will do the work; it is also
        #: a preference, not a reservation — the agent may be busy.
        self.estimated_by: dict[str, str] = {}
        #: Why the last ``run`` stopped — ``no_slot`` vs ``nothing_admissible``
        #: is the difference between "hire agents" and "write better cards".
        self.last_idle: StepResult | None = None
        self._beat()

    def _beat(self) -> None:
        """Touch the heartbeat file. Cheap, and it is the only liveness the
        board can actually trust — a worker that crashed leaves a stale file."""
        with contextlib.suppress(OSError):  # never let telemetry break the loop
            heartbeat_path(self.store).write_text(str(time.time()), encoding="utf-8")

    def _free_agent(self, role: str, *, prefer: str | None = None) -> str | None:
        taken = self.store.occupying_agent_ids()
        # Local channel only. A real Octop agent id must never be claimed by this
        # process: it would attach a template artefact to an identity that never
        # ran, which is exactly the fake-delivery defect this board exists to
        # prevent.
        free = [
            a
            for a in self.store.agents_by_role(role, channel="local")
            if a not in taken
        ]
        if prefer and prefer in free:
            return prefer
        return free[0] if free else None

    def _estimate_pending(self) -> int:
        """Refinement pass: no request enters execution without a team size.

        The owner's form has no points field. If a PBI arrives without one (via
        RPC, or because its request was just rewritten and the old estimate was
        invalidated), this is where a dev agent produces it. Estimate before
        admission so interrupt pricing and the sprint commitment see the real
        number rather than zero.
        """
        done = 0
        for task in self.store.tasks_needing_estimate():
            pbi = self.store.get_pbi(task.pbi_id)
            if pbi is None:
                continue
            local_devs = self.store.agents_by_role("dev", channel="local")
            agent = self._free_agent("dev") or (local_devs or ["dev-1"])[0]
            estimate = self.estimator.estimate(task, pbi)
            self.store.estimate_pbi(
                task.pbi_id, estimate.points, actor=agent,
                rationale=f"[{agent}] {estimate.rationale}",
            )
            self.estimated_by[task.task_id] = agent
            self.log(f"[{agent}] estimated {task.pbi_id}: {estimate.points:g} pt")
            done += 1
        return done

    def step(self) -> StepResult:
        self._beat()
        self._estimate_pending()
        for task in self.store.next_candidates(limit=20):
            admit = self.store.admit(task.task_id)
            if not admit.ok:
                # Not an error: a card waiting on review capacity is doing what
                # the gate is for. A *spec* failure is different — it is the
                # owner's missing input, so hand the request back instead of
                # re-checking it forever in a column the owner does not read.
                # ``deny_admission`` owns that decision so the L1 worker and the
                # L2 dispatcher cannot drift apart again.
                self.denied[admit.reason] = self.denied.get(admit.reason, 0) + 1
                if self.store.deny_admission(task.task_id, admit.reason, admit.detail):
                    self.log(f"[dor] {task.task_id} -> needs_revision (owner 需补齐验收条件)")
                continue
            pbi = self.store.get_pbi(task.pbi_id) or {}
            readiness = pbi.get("readiness") or {}
            prefer = self.estimated_by.get(task.task_id) or (
                readiness.get("estimated_by") if isinstance(readiness, dict) else None
            )
            agent = self._free_agent(task.role, prefer=prefer)
            if agent is None:
                return StepResult(action="idle", detail="no_slot")
            lease = self.store.claim(task.task_id, agent)
            if lease is None:
                continue
            self.store.ack(task.task_id, lease.lease_id)
            self.log(f"[{agent}] claimed {task.task_id}: {task.title}")

            if self.step_delay:
                time.sleep(self.step_delay)

            fresh = self.store.get_task(task.task_id) or task
            outcome = self.executor.run(fresh)

            if not outcome.ok:
                self.store.block(
                    task.task_id, "executor_failed", actor=agent, detail=outcome.note
                )
                self.log(f"[{agent}] blocked {task.task_id}: {outcome.note}")
                return StepResult(
                    task_id=task.task_id,
                    action="blocked",
                    detail=outcome.note,
                    state=self._state_of(task.task_id),
                )

            self.store.submit(
                task.task_id,
                lease.lease_id,
                evidence=outcome.evidence,
                idem_key=f"{task.task_id}:{lease.lease_id}",
            )
            self.log(f"[{agent}] submitted {task.task_id}")

            if self.step_delay:
                time.sleep(self.step_delay * 0.5)

            checks = self.dod.check(outcome.evidence)
            passed = self.store.record_dod(task.task_id, checks)
            if not passed:
                # ``record_dod`` already opened the ``verifying`` window and
                # wrote the failed checks. Blocking here would be a second,
                # local verdict that closes the window before the SM can
                # re-verify — exactly the bug this window exists to fix.
                failed = [c["check_id"] for c in checks if not c.get("ok")]
                self.log(f"[dod] {task.task_id} -> verifying: {failed}")
                return StepResult(
                    task_id=task.task_id,
                    action="dod_failed",
                    detail=",".join(failed),
                    state=self._state_of(task.task_id),
                    checks=checks,
                )
            self.log(f"[dod] {task.task_id} -> in_review")
            return StepResult(
                task_id=task.task_id,
                action="reviewed",
                detail=outcome.note,
                state=self._state_of(task.task_id),
                checks=checks,
            )
        return StepResult(action="idle", detail="nothing_admissible")

    def run(self, *, max_steps: int = 0) -> list[StepResult]:
        """Work until nothing is admissible. Only performed steps are returned."""
        results: list[StepResult] = []
        while not max_steps or len(results) < max_steps:
            result = self.step()
            if result.action == "idle":
                self.last_idle = result
                break
            results.append(result)
        return results

    def run_forever(self, *, interval: float = 3.0) -> None:
        """Stay resident so a card created five minutes from now still moves.

        A one-shot ``run`` is fine for a demo but wrong for a board: the owner
        adds work whenever they feel like it, and nobody is going to restart the
        worker each time.
        """
        while True:
            self.run()
            time.sleep(interval)

    def _state_of(self, task_id: str) -> str:
        task = self.store.get_task(task_id)
        return task.state if task is not None else "?"
