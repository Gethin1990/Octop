"""Planning boundary between "asked for" and "committed".

The design note draws a hard line: the owner states what they want, the PO
refines and orders the Product Backlog, a dev sizes the work, the PO drafts a
Sprint and the SM checks capacity -- and only then does a human approve the
commitment. ``advance_planning`` is one idempotent tick that moves a request up
to (but never past) that approval gate; ``approve_sprint`` is the gate itself.

This module is an orchestrator, not an actor system. Each transition is
attributed to the role that owns it in ``event_log`` (po / dev-N / sm / owner),
so replacing any step with a real Octop expert agent does not change the state
machine or the board's contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .estimate import Estimator
from .store import BoardStore, TransitionRejected

DEFAULT_CAPACITY_PTS = 100.0
SPRINT_NAME = "Sprint 1"
SPRINT_GOAL = "承接 owner 提出的需求"


def _draft(pbi: dict[str, Any]) -> Any:
    """The estimator only reads title/spec; a draft PBI is enough for it."""
    return SimpleNamespace(
        task_id="(draft)",
        pbi_id=pbi["pbi_id"],
        title=str(pbi.get("title") or ""),
        spec=str(pbi.get("body") or ""),
    )


def _is_candidate(pbi: dict[str, Any]) -> bool:
    readiness = pbi.get("readiness")
    return not (isinstance(readiness, dict) and readiness.get("planned_sprint_id"))


def advance_planning(
    store: BoardStore,
    sizer: Estimator,
    *,
    capacity_pts: float = DEFAULT_CAPACITY_PTS,
) -> dict[str, Any]:
    """Refine -> size -> propose, or inject into an already active Sprint.

    Safe to call on every page render: every step is guarded by the current
    state, so a second tick does nothing. It never activates a Sprint.
    """
    devs = store.agents_by_role("dev") or ["dev-1"]
    sms = store.agents_by_role("sm") or ["sm"]
    report: dict[str, Any] = {
        "refined": [],
        "held": [],
        "estimated": [],
        "injected": [],
        "proposed": None,
    }

    # PO refinement: new -> refining -> ready, or -> needs_revision when the
    # requester left acceptance criteria empty. A held request never reaches
    # sizing or the Sprint proposal: the owner is asked before the commitment,
    # not after it. The PO stamp in readiness_json is the idempotency key,
    # because create_pbi already calls a request with criteria "ready".
    for pbi in store.all_pbis():
        readiness = pbi.get("readiness")
        already_refined = isinstance(readiness, dict) and readiness.get("refined_by")
        if pbi["state"] in ("new", "refining", "ready") and not already_refined:
            result = store.refine_pbi(str(pbi["pbi_id"]), actor="po")
            report["refined"].append(str(pbi["pbi_id"]))
            if result.get("state") == "needs_revision":
                report["held"].append(str(pbi["pbi_id"]))

    # Team sizing is a dev decision, never the requester's.
    for pbi in store.all_pbis():
        if pbi["state"] != "ready" or pbi.get("points") is not None:
            continue
        sizing = sizer.estimate(_draft(pbi), pbi)
        store.estimate_pbi(
            str(pbi["pbi_id"]),
            sizing.points,
            actor=devs[0],
            rationale=f"[{devs[0]}] {sizing.rationale}",
        )
        report["estimated"].append(str(pbi["pbi_id"]))

    ready = [
        pbi
        for pbi in store.all_pbis()
        if pbi["state"] == "ready"
        and pbi.get("points") is not None
        and not pbi.get("sprint_id")
        and _is_candidate(pbi)
    ]
    active = store.active_sprint_id()
    if active is not None:
        # Scope injection into a running Sprint: the PO owns the move and the
        # event log records it as scope_injected, not as a quiet append.
        for pbi in ready:
            pbi_id = str(pbi["pbi_id"])
            store.add_pbi_to_sprint(pbi_id, active, actor="po")
            store.create_task(pbi_id, str(pbi["title"]), actor="po")
            report["injected"].append(pbi_id)
        return report

    if store.planned_sprint() is not None or not ready:
        return report

    sprint_id = store.create_sprint(SPRINT_NAME, capacity_pts=capacity_pts, goal=SPRINT_GOAL)
    proposal = store.mark_candidates(
        sprint_id,
        [str(pbi["pbi_id"]) for pbi in ready],
        actor="po",
        sm=sms[0],
    )
    report["proposed"] = sprint_id
    report["proposal"] = proposal
    return report


def approve_sprint(
    store: BoardStore,
    sprint_id: str | None = None,
    *,
    actor: str = "owner",
) -> dict[str, Any]:
    """Human approval is the only path from ``planned`` to ``active``."""
    if not sprint_id:
        planned = store.planned_sprint()
        if planned is None:
            raise TransitionRejected("no_planned_sprint", "")
        sprint_id = str(planned["sprint_id"])
    pbi_ids = store.candidates_for(sprint_id)
    if not pbi_ids:
        raise TransitionRejected("empty_sprint_candidate", sprint_id)
    committed = store.commit_sprint(sprint_id, pbi_ids, actor=actor)
    # Decomposition is PO work; the approval above remains attributed to owner.
    task_ids = [
        store.create_task(
            pbi_id,
            str((store.get_pbi(pbi_id) or {}).get("title") or pbi_id),
            actor="po",
        )
        for pbi_id in pbi_ids
    ]
    return {
        "sprint_id": sprint_id,
        "pbi_ids": pbi_ids,
        "task_ids": task_ids,
        "committed_pts": committed,
    }
