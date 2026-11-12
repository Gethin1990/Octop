"""The three Scrum artefacts the owner is actually allowed to look at.

Scrum does not ask the person who funded the work to watch a kanban of agent
internals. It asks for three artefacts: an ordered Product Backlog, a committed
Sprint Backlog, and a burndown. Everything here is a **read-only projection** of
``pbi`` / ``sprint`` / ``task`` / ``event_log`` — there is no second backlog
table that could drift from the board.

The burndown is replayed from the event log rather than sampled into a metrics
table. That matters: a sampled table answers "what did the chart say at 10:00",
while a replay answers "what *had happened* by 10:00", and can be recomputed
after the fact when scope was injected or a review was reversed.
"""

from __future__ import annotations

import html
import time
from typing import Any

from .constants import TASK_STATES
from .store import BoardStore, Task

#: Default timebox for the ideal line when the board has not been told one.
#: A real sprint would store this on the sprint row; the HTTP surface exposes
#: ``?horizon=`` so the same chart can show an hourly demo or a two-week sprint
#: without rewriting history.
DEFAULT_HORIZON_S = 3600.0

_STATE_LABELS: dict[str, str] = {
    "new": "待梳理",
    "refining": "梳理中",
    "needs_revision": "待补验收条件",
    "ready": "就绪",
    "committed": "已承诺",
    "planned": "待批准",
    "done": "已完成",
    "parked": "搁置",
}

_TASK_LABELS: dict[str, str] = {
    "queued": "排队",
    "leased": "已派单",
    "running": "执行中",
    "submitted": "已提交",
    "verifying": "DoD 复核中",
    "in_review": "待验收",
    "needs_revision": "待改需求",
    "blocked": "阻塞",
    "parked": "搁置",
    "done": "完成",
}


def state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state)


def task_label(state: str) -> str:
    return _TASK_LABELS.get(state, state)


def _points(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _estimate(pbi: dict[str, Any]) -> dict[str, Any]:
    """Who sized this request, and why — the owner must not be asked to guess."""
    readiness = pbi.get("readiness") or {}
    by = readiness.get("estimated_by") if isinstance(readiness, dict) else None
    return {
        "points_estimated": pbi.get("points") is not None,
        "estimated_by": by,
        "estimate_rationale": (
            str(readiness.get("estimate_rationale") or "")
            if isinstance(readiness, dict) else ""
        ),
        "estimate_invalidated": bool(
            isinstance(readiness, dict) and readiness.get("estimate_invalidated_at")
        ),
    }


def _task_brief(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "state": task.state,
        "state_label": task_label(task.state),
        "agent_id": task.agent_id,
        "attempt": task.attempt,
        "blocked_reason": task.blocked_reason,
        "revision_reason": task.blocked_reason if task.state == "needs_revision" else None,
        "verification_reason": (
            task.blocked_reason if task.state == "verifying" else None
        ),
        "summary": str(task.evidence.get("summary") or ""),
        "has_artifact": bool(task.evidence.get("artifact")),
        "updated_at": task.updated_at,
    }


def _tasks_by_pbi(store: BoardStore) -> dict[str, list[Task]]:
    grouped: dict[str, list[Task]] = {}
    for task in store.tasks_in_state(*TASK_STATES):
        grouped.setdefault(task.pbi_id, []).append(task)
    return grouped


def product_backlog(store: BoardStore) -> dict[str, Any]:
    """Ordered Product Backlog: every PBI, its readiness and where it landed."""
    grouped = _tasks_by_pbi(store)
    items: list[dict[str, Any]] = []
    for pbi in store.all_pbis():
        tasks = grouped.get(str(pbi["pbi_id"]), [])
        items.append({
            "pbi_id": str(pbi["pbi_id"]),
            "title": str(pbi["title"]),
            "state": str(pbi["state"]),
            "state_label": state_label(str(pbi["state"])),
            "points": _points(pbi["points"]),
            "priority_rank": _points(pbi["priority_rank"]),
            "class_of_service": str(pbi["class_of_service"]),
            "acceptance": list(pbi["acceptance"]),
            "sprint_id": pbi["sprint_id"],
            "task_count": len(tasks),
            "task_states": [task_label(t.state) for t in tasks],
            **_estimate(pbi),
        })
    by_state: dict[str, int] = {}
    for item in items:
        by_state[item["state"]] = by_state.get(item["state"], 0) + 1
    return {
        "items": items,
        "count": len(items),
        "points_total": round(sum(i["points"] for i in items), 2),
        "points_remaining": round(
            sum(i["points"] for i in items if i["state"] != "done"), 2
        ),
        "by_state": by_state,
    }


def sprint_backlog(store: BoardStore, sprint_id: str | None = None) -> dict[str, Any]:
    """The active sprint's commitment, decomposed into deliverable tasks."""
    sprint = store.get_sprint(sprint_id)
    if sprint is None:
        return {
            "sprint": None,
            "items": [],
            "count": 0,
            "points_committed": 0.0,
            "points_done": 0.0,
            "points_remaining": 0.0,
            "progress_pct": 0.0,
            "task_counts": {},
        }
    sid = str(sprint["sprint_id"])
    tasks = store.tasks_for_sprint(sid)
    grouped: dict[str, list[Task]] = {}
    for task in tasks:
        grouped.setdefault(task.pbi_id, []).append(task)

    items: list[dict[str, Any]] = []
    for pbi in store.all_pbis():
        if pbi["sprint_id"] != sid:
            continue
        owned = grouped.get(str(pbi["pbi_id"]), [])
        items.append({
            "pbi_id": str(pbi["pbi_id"]),
            "title": str(pbi["title"]),
            "state": str(pbi["state"]),
            "state_label": state_label(str(pbi["state"])),
            "points": _points(pbi["points"]),
            "acceptance": list(pbi["acceptance"]),
            "tasks": [_task_brief(t) for t in owned],
            **_estimate(pbi),
        })

    task_counts: dict[str, int] = {}
    for task in tasks:
        task_counts[task.state] = task_counts.get(task.state, 0) + 1
    done = round(sum(i["points"] for i in items if i["state"] == "done"), 2)
    remaining = round(sum(i["points"] for i in items if i["state"] != "done"), 2)
    committed = round(done + remaining, 2)
    return {
        "sprint": {
            "sprint_id": sid,
            "name": str(sprint["name"]),
            "goal": str(sprint["goal"]),
            "state": str(sprint["state"]),
            "capacity_pts": _points(sprint["capacity_pts"]),
            "started_at": sprint["started_at"],
            "closed_at": sprint["closed_at"],
        },
        "items": items,
        "count": len(items),
        "points_committed": committed,
        "points_done": done,
        "points_remaining": remaining,
        "progress_pct": round(100.0 * done / committed, 1) if committed else 0.0,
        "task_counts": task_counts,
        "tasks_total": len(tasks),
    }


def _deltas(
    store: BoardStore,
    sprint_id: str,
    baseline_fallback: float,
) -> tuple[float, list[tuple[float, float, str]]]:
    """Replay the log into (timestamp, point delta, label) triples."""
    pbi_points = {
        str(p["pbi_id"]): _points(p["points"])
        for p in store.all_pbis()
        if p["sprint_id"] == sprint_id
    }
    baseline: float | None = None
    deltas: list[tuple[float, float, str]] = []
    for event in store.burndown_events():
        kind = event["kind"]
        payload = event["payload"]
        if kind == "sprint_committed" and event["entity_id"] == sprint_id:
            baseline = _points(payload.get("committed_pts"))
        elif kind == "scope_injected" and payload.get("sprint_id") == sprint_id:
            pts = _points(payload.get("points"))
            # The owner no longer supplies points, so an injection made before
            # the team has sized it records 0. A zero step is not a fact.
            if pts:
                deltas.append((event["ts"], pts, f"范围注入 +{pts:g}pt"))
        elif kind in ("pbi_estimated", "estimate_invalidated"):
            if payload.get("sprint_id") != sprint_id:
                continue
            delta = _points(payload.get("delta"))
            if not delta:
                continue
            label = (
                f"团队估点 +{delta:g}pt" if delta > 0
                else f"需求改写，估点作废 {delta:g}pt"
            )
            deltas.append((event["ts"], delta, label))
        elif kind == "pbi_done" and event["entity_id"] in pbi_points:
            # Prefer the size recorded at completion: a later re-estimate must
            # not retroactively rewrite how much burned down that day.
            pts = _points(payload.get("points")) or pbi_points[event["entity_id"]]
            deltas.append((event["ts"], -pts, f"PBI 完成 −{pts:g}pt"))
    if baseline is None:
        # Older boards (or a sprint created by an import) may lack the event.
        # Subtract every injection so the replay still starts at the original
        # commitment instead of double-counting scope creep.
        baseline = baseline_fallback - sum(d for _, d, _ in deltas if d > 0)
    return baseline, sorted(deltas, key=lambda d: d[0])


def burndown(
    store: BoardStore,
    sprint_id: str | None = None,
    *,
    now: float | None = None,
    horizon_s: float | None = None,
    samples: int = 64,
) -> dict[str, Any]:
    """Remaining points over time, plus the ideal line for a chosen timebox.

    ``horizon_s=None`` adapts the timebox to how long the sprint has actually
    been running. A fixed two-week line on a five-minute-old demo board is a
    horizontal line that hides every event; adaptive scaling keeps the shape
    visible while still labelling itself as a display choice, not a commitment.
    """
    sprint = store.get_sprint(sprint_id)
    empty: dict[str, Any] = {
        "sprint": None, "started_at": 0.0, "now": now or time.time(),
        "horizon_s": horizon_s or 0.0, "baseline_pts": 0.0, "current_pts": 0.0,
        "actual": [], "ideal": [], "scope_changes": [], "adaptive": False,
        "events_used": 0,
    }
    if sprint is None:
        return empty
    sid = str(sprint["sprint_id"])
    current_time = now if now is not None else time.time()
    t0 = float(sprint["started_at"] or sprint["created_at"] or current_time)
    baseline, deltas = _deltas(store, sid, _points(sprint["committed_pts"]))

    adaptive = horizon_s is None
    horizon = max(
        60.0,
        (max(current_time - t0, 60.0) * 1.2) if adaptive else float(horizon_s or 0),
    )
    t_end = max(t0 + horizon, current_time + (current_time - t0) * 0.02)

    # Event boundaries get a point immediately before and at the event, so a
    # scope injection renders as a vertical step rather than a fake ramp.
    grid = [t0 + (t_end - t0) * i / max(samples - 1, 1) for i in range(max(samples, 2))]
    marks = {min(max(t, t0), t_end) for t in grid}
    for ts, _, _ in deltas:
        marks.add(min(max(ts - 0.001, t0), t_end))
        marks.add(min(max(ts, t0), t_end))
    marks.add(min(current_time, t_end))

    actual: list[dict[str, float]] = []
    for t in sorted(m for m in marks if m <= current_time + 1e-6):
        remaining = baseline + sum(d for ts, d, _ in deltas if ts <= t)
        actual.append({"t": round(t, 3), "remaining": round(max(remaining, 0.0), 2)})
    current = actual[-1]["remaining"] if actual else round(max(baseline, 0.0), 2)

    ideal = [
        {"t": round(t0, 3), "remaining": round(baseline, 2)},
        {"t": round(min(t0 + horizon, t_end), 3), "remaining": 0.0},
    ]
    scope_changes = [
        {"t": round(ts, 3), "delta": round(d, 2), "label": label,
         "remaining": round(baseline + sum(x for s, x, _ in deltas if s <= ts), 2)}
        for ts, d, label in deltas if d > 0
    ]
    return {
        "sprint": {"sprint_id": sid, "name": str(sprint["name"]),
                   "state": str(sprint["state"])},
        "started_at": t0,
        "now": current_time,
        "t_end": t_end,
        "horizon_s": horizon,
        "baseline_pts": round(baseline, 2),
        "current_pts": current,
        "actual": actual,
        "ideal": ideal,
        "scope_changes": scope_changes,
        "adaptive": adaptive,
        "events_used": len(deltas),
    }


def burndown_svg(report: dict[str, Any], *, width: int = 660, height: int = 230) -> str:
    """Self-contained SVG. No CDN, no JS — the artefact must survive offline."""
    left, right, top, bottom = 46.0, 18.0, 16.0, 34.0
    plot_w = width - left - right
    plot_h = height - top - bottom
    t0 = float(report["started_at"])
    t_end = float(report.get("t_end") or t0 + float(report["horizon_s"]))
    span = max(t_end - t0, 1.0)
    y_values = [float(report["baseline_pts"]), 1.0]
    y_values.extend(float(p["remaining"]) for p in report["actual"])
    y_max = max(y_values) * 1.15

    def x(t: float) -> float:
        return left + (float(t) - t0) / span * plot_w

    def y(v: float) -> float:
        return top + plot_h - max(float(v), 0.0) / y_max * plot_h

    def esc(text: Any) -> str:
        return html.escape(str(text), quote=True)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        'aria-label="Sprint 燃尽图" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#141821" rx="10"/>',
    ]
    for i in range(5):
        value = y_max * i / 4
        gy = y(value)
        parts.append(
            f'<line x1="{left:.1f}" y1="{gy:.1f}" x2="{width - right:.1f}" y2="{gy:.1f}" '
            'stroke="#262d3d" stroke-width="1"/>'
            f'<text x="{left - 7:.1f}" y="{gy + 3:.1f}" fill="#7d879c" font-size="10" '
            f'text-anchor="end">{value:.0f}</text>'
        )
    parts.append(
        f'<line x1="{left:.1f}" y1="{top:.1f}" x2="{left:.1f}" '
        f'y2="{top + plot_h:.1f}" stroke="#3a4358" stroke-width="1"/>'
    )

    ideal = report["ideal"]
    if len(ideal) == 2:
        parts.append(
            f'<line x1="{x(ideal[0]["t"]):.1f}" y1="{y(ideal[0]["remaining"]):.1f}" '
            f'x2="{x(ideal[1]["t"]):.1f}" y2="{y(ideal[1]["remaining"]):.1f}" '
            'stroke="#6f7688" stroke-width="1.5" stroke-dasharray="5 4"/>'
        )
    actual = report["actual"]
    if actual:
        points = " ".join(f"{x(p['t']):.1f},{y(p['remaining']):.1f}" for p in actual)
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="#6ea8fe" '
            'stroke-width="2.2" stroke-linejoin="round"/>'
        )
        last = actual[-1]
        parts.append(
            f'<circle cx="{x(last["t"]):.1f}" cy="{y(last["remaining"]):.1f}" r="4" '
            'fill="#6ea8fe" stroke="#0e1118" stroke-width="1.5"/>'
            f'<text x="{min(x(last["t"]) + 8, width - 62):.1f}" '
            f'y="{max(y(last["remaining"]) - 8, top + 10):.1f}" fill="#cfe0ff" '
            f'font-size="11" font-weight="600">剩余 {last["remaining"]:g} pt</text>'
        )
    for change in report["scope_changes"]:
        cx, cy = x(change["t"]), y(change["remaining"])
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="#e0a25a"/>'
            f'<text x="{cx + 5:.1f}" y="{cy - 6:.1f}" fill="#e0a25a" '
            f'font-size="9">{esc(change["label"])}</text>'
        )

    def clock(ts: float) -> str:
        return time.strftime("%m-%d %H:%M", time.localtime(ts))

    parts.append(
        f'<text x="{left:.1f}" y="{height - 12:.1f}" fill="#7d879c" font-size="10">'
        f'{esc(clock(t0))}</text>'
        f'<text x="{width - right:.1f}" y="{height - 12:.1f}" fill="#7d879c" '
        f'font-size="10" text-anchor="end">{esc(clock(t_end))}</text>'
        f'<text x="{left + 6:.1f}" y="{top + 12:.1f}" fill="#cfe0ff" font-size="10">'
        '━ 实际剩余</text>'
        f'<text x="{left + 82:.1f}" y="{top + 12:.1f}" fill="#6f7688" font-size="10">'
        '┄ 理想线</text>'
    )
    if not actual:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{height / 2:.1f}" fill="#7d879c" '
            'font-size="12" text-anchor="middle">还没有 Sprint，提一个需求就会开始记录</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def all_artifacts(
    store: BoardStore,
    *,
    horizon_s: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """One JSON payload for the whole owner-facing view."""
    product = product_backlog(store)
    sprint = sprint_backlog(store)
    chart = burndown(store, horizon_s=horizon_s, now=now)
    return {"product_backlog": product, "sprint_backlog": sprint, "burndown": chart}
