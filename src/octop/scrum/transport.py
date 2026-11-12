"""Transport contract: how the dispatcher reaches an agent, and what it can observe.

Two design notes that the rest of the system depends on:

**Push a pointer, pull the payload.** The wake text is three lines of ids. The
spec lives in the agent's workspace (``TASK.md``) and is fetched through
``board_claim``. That is what makes a re-sent wake harmless, keeps the context
window free for actual work, and lets a stale wake exit in a few hundred tokens
when ``board_claim`` answers ``NOT_YOURS``.

**Do not wait for the turn.** Octop explicitly keeps a turn running after the
WebSocket disconnects ("Disconnect must not cancel an in-flight turn"), so the
dispatcher waits for the *ack frame* and then closes. Progress is observed from
the board and ``octop.db``, never from the socket lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class WakeResult:
    status: str  # accepted | rejected | busy | error | timeout
    reason: str = ""
    thread_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True, slots=True)
class Probe:
    """What the outside world can tell about a thread right now."""

    active: bool
    last_event_at: float | None = None
    thread_status: str | None = None
    detail: str = ""


@runtime_checkable
class Transport(Protocol):
    async def send_pointer(
        self, agent_id: str, text: str, thread_id: str | None
    ) -> WakeResult: ...

    async def probe(self, thread_id: str | None) -> Probe: ...

    async def cancel(self, thread_id: str) -> bool: ...


def pointer_text(task_id: str, lease_id: str, attempt: int, max_attempts: int) -> str:
    """The wake payload. Deliberately boring: no spec, no history, no context.

    ``NOT_YOURS`` is the contract word: a duplicate wake (or a zombie worker)
    calls ``board_claim``, gets ``NOT_YOURS``, replies ``NOOP`` and stops.
    """
    return (
        f"TASK {task_id} / LEASE {lease_id} / ATTEMPT {attempt} of {max_attempts}\n"
        f"第一步：调用 board_claim(task_id=\"{task_id}\") 取规格。\n"
        "若返回 NOT_YOURS 或 NOT_QUEUED：立刻结束本轮，只回复 NOOP，不要做任何其他动作。"
    )


def verify_pointer_text(task_id: str) -> str:
    """Wake payload for the Scrum Master: verify, do not redo.

    Deliberately different from :func:`pointer_text`. A dev pointer says "claim
    and build"; a verifier pointer says "read what exists and judge it". Handing
    the SM the dev phrasing is how an SM ends up re-implementing the task instead
    of checking it.
    """
    return (
        f"VERIFY {task_id}\n"
        f"这是复验请求，不是执行请求。请依次：\n"
        f'1) 调用 board_view 或 task_get(task_id="{task_id}") 读 evidence 与 acceptance；\n'
        "2) 打开 evidence.artifact 指向的真实文件逐条核对，不要只看 summary；\n"
        "3) 调用 dod_record 写下逐项判定（通过或打回都要给依据）。\n"
        "产物不合格就判 false 并写明缺什么，不要替 dev 补结果。"
    )


class FakeTransport:
    """Scriptable transport for tests and for ``--dry-run``.

    ``script`` is consumed in order per agent; when exhausted the last entry
    repeats, so ``["accepted"]`` means "every wake lands".
    """

    def __init__(
        self,
        *,
        results: dict[str, list[str]] | None = None,
        default: str = "accepted",
        probe_active: bool = False,
        probe_last_event_at: float | None = None,
    ) -> None:
        self._results = {k: list(v) for k, v in (results or {}).items()}
        self._default = default
        self._probe_active = probe_active
        self._probe_last_event_at = probe_last_event_at
        self.wakes: list[tuple[str, str, str | None]] = []
        self.cancels: list[str] = []

    def _next(self, agent_id: str) -> str:
        queue = self._results.get(agent_id)
        if not queue:
            return self._default
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def send_pointer(self, agent_id: str, text: str, thread_id: str | None) -> WakeResult:
        self.wakes.append((agent_id, text, thread_id))
        status = self._next(agent_id)
        reason = "" if status == "accepted" else status
        return WakeResult(
            status=status,
            reason=reason,
            thread_id=thread_id or f"thr-{len(self.wakes)}",
            raw={"fake": True},
        )

    async def probe(self, thread_id: str | None) -> Probe:
        return Probe(
            active=self._probe_active,
            last_event_at=self._probe_last_event_at,
            thread_status="active" if self._probe_active else "idle",
            detail="fake",
        )

    async def cancel(self, thread_id: str) -> bool:
        self.cancels.append(thread_id)
        return True
