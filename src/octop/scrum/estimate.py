"""Sizing belongs to the agent that will do the work, not to the requester.

Asking the person who funds the work for a story point estimate is how a board
ends up with a number that measures confidence instead of effort. The estimate
is therefore produced here, attributed to a dev agent, and written to the event
log with a rationale so it can be audited (and invalidated when the request is
rewritten).

``HeuristicEstimator`` is the offline stand-in: deterministic, cheap, and
honest about being a heuristic. In a real Octop turn the same contract is
filled by ``board_estimate`` — a dev agent reads the request and calls the RPC.
The board does not care which one answered; it only cares that the actor is a
registered dev agent and that ``points`` came from the team.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .store import Task

#: Planning-poker scale. Story points are relative, so snapping a raw score to
#: a coarse scale is a feature: it prevents fake precision from being mistaken
#: for a measurement.
FIBONACCI: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0)


@dataclass(frozen=True, slots=True)
class Estimate:
    """A size plus the reasoning that produced it."""

    points: float
    rationale: str


class Estimator(Protocol):
    @property
    def name(self) -> str: ...

    def estimate(self, task: Task, pbi: dict[str, Any]) -> Estimate: ...


@dataclass(frozen=True, slots=True)
class HeuristicEstimator:
    """Estimate from the shape of the request: criteria, text size, risk words.

    A real agent would read the codebase too. This one deliberately does not
    pretend to: its rationale names the signals it actually used, so an owner
    can tell the difference between "the team sized it" and "a formula did".
    """

    name: str = "dev-agent"

    # Words that usually mean more surface area, not just more typing.
    _RISK: tuple[tuple[str, float], ...] = (
        ("迁移", 2.0), ("重构", 2.0), ("兼容", 1.5),
        ("安全", 1.5), ("权限", 1.5), ("支付", 2.0), ("并发", 1.5),
        ("性能", 1.0), ("审计", 1.0), ("全量", 1.5), ("回归", 1.0),
        ("migrate", 2.0), ("refactor", 2.0), ("security", 1.5),
        ("integration", 1.0), ("benchmark", 1.0),
    )

    def estimate(self, task: Task, pbi: dict[str, Any]) -> Estimate:
        parts = [
            str(pbi.get("title") or ""),
            str(pbi.get("body") or ""),
            str(task.spec or ""),
            str(task.title or ""),
        ]
        text = " ".join(parts)
        criteria = [a for a in (pbi.get("acceptance") or []) if str(a).strip()]
        hits = [(word, weight) for word, weight in self._RISK if word in text.lower()]
        score = (
            1.0
            + min(len(criteria), 6) * 0.5
            + min(len(text) / 300.0, 2.0)
            + sum(weight for _, weight in hits)
            + (0.5 if pbi.get("class_of_service") == "expedite" else 0.0)
        )
        points = min(FIBONACCI, key=lambda p: abs(p - score))
        reasons = [
            f"验收条件 {len(criteria)} 条",
            f"需求描述 {len(text)} 字",
        ]
        if hits:
            reasons.append("风险词 " + "、".join(word for word, _ in hits[:3]))
        return Estimate(
            points=points,
            rationale=f"团队估点依据：{'；'.join(reasons)}；按 {points:g} pt 刻度取值",
        )
