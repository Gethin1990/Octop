"""The three artefacts are projections, not a second source of truth."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from octop.scrum import reports
from octop.scrum.store import BoardStore

from .helpers import seed_agents, sprint_with_tasks, to_review


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def timed(tmp_board: Path):
    clock = Clock()
    store = BoardStore(tmp_board / "board.db", now=clock)
    store.init_schema()
    seed_agents(store)
    yield store, clock
    store.close()


@pytest.fixture()
def tmp_board():
    path = Path(tempfile.mkdtemp(prefix="octop-scrum-reports-"))
    yield path


def test_product_backlog_is_priority_ordered_and_counts_points(store):
    sprint_with_tasks(store, count=2, points=3.0)
    extra = store.create_pbi("未进 Sprint 的需求", points=5.0, acceptance=["a"])
    store.reorder_pbi(extra, 1.0)

    product = reports.product_backlog(store)

    assert [i["title"] for i in product["items"]][0] == "未进 Sprint 的需求"
    assert product["count"] == 3
    assert product["points_total"] == 11.0
    assert product["points_remaining"] == 11.0
    assert product["by_state"]["committed"] == 2
    assert product["by_state"]["ready"] == 1
    assert product["items"][0]["acceptance"] == ["a"]


def test_sprint_backlog_excludes_uncommitted_work_and_tracks_progress(store):
    sprint_id, _, tasks = sprint_with_tasks(store, count=2, points=4.0)
    store.create_pbi("还没承诺", points=9.0, acceptance=["x"])

    view = reports.sprint_backlog(store)

    assert view["sprint"]["sprint_id"] == sprint_id
    assert view["count"] == 2
    assert view["points_committed"] == 8.0
    assert view["tasks_total"] == 2
    assert view["progress_pct"] == 0.0
    assert all(len(i["tasks"]) == 1 for i in view["items"])

    to_review(store, tasks[0])
    store.accept(tasks[0], reviewer="owner")

    after = reports.sprint_backlog(store)
    assert after["points_done"] == 4.0
    assert after["points_remaining"] == 4.0
    assert after["progress_pct"] == 50.0


def test_sprint_backlog_without_a_sprint_is_an_explicit_empty(store):
    view = reports.sprint_backlog(store)

    assert view["sprint"] is None
    assert view["items"] == []
    assert view["points_committed"] == 0.0


def test_burndown_replays_scope_injection_and_completion(timed):
    store, clock = timed
    sprint_id, _, tasks = sprint_with_tasks(store, count=1, points=3.0)

    clock.advance(120)
    injected = store.create_pbi("中途插进来的", points=3.0, acceptance=["x"])
    store.add_pbi_to_sprint(injected)

    clock.advance(120)
    to_review(store, tasks[0])
    store.accept(tasks[0], reviewer="owner")

    chart = reports.burndown(store, now=clock() + 60)

    assert chart["sprint"]["sprint_id"] == sprint_id
    assert chart["baseline_pts"] == 3.0       # original commitment only
    assert chart["current_pts"] == 3.0        # +3 scope, −3 completed
    assert chart["events_used"] == 2
    assert len(chart["scope_changes"]) == 1
    assert chart["scope_changes"][0]["delta"] == 3.0
    remaining = [p["remaining"] for p in chart["actual"]]
    assert remaining[0] == 3.0
    assert 6.0 in remaining                  # scope creep is visible, not hidden
    assert remaining[-1] == 3.0


def test_burndown_ideal_line_starts_at_baseline_and_ends_at_zero(timed):
    store, clock = timed
    sprint_with_tasks(store, count=2, points=5.0)

    chart = reports.burndown(store, now=clock() + 300, horizon_s=3600.0)

    assert chart["adaptive"] is False
    assert chart["horizon_s"] == 3600.0
    assert chart["ideal"][0]["remaining"] == 10.0
    assert chart["ideal"][1]["remaining"] == 0.0
    assert chart["ideal"][1]["t"] == chart["ideal"][0]["t"] + 3600.0


def test_burndown_without_a_sprint_is_empty(store):
    chart = reports.burndown(store)

    assert chart["sprint"] is None
    assert chart["actual"] == []
    assert chart["baseline_pts"] == 0.0
    assert "还没有 Sprint" in reports.burndown_svg(chart)


def test_burndown_svg_is_self_contained_and_marks_scope_creep(timed):
    store, clock = timed
    sprint_with_tasks(store, count=1, points=3.0)
    injected = store.create_pbi("插单", points=2.0, acceptance=["x"])
    store.add_pbi_to_sprint(injected)

    svg = reports.burndown_svg(reports.burndown(store, now=clock() + 60))

    assert svg.startswith("<svg")
    assert "<polyline" in svg
    assert "stroke-dasharray" in svg
    assert "范围注入" in svg
    assert "剩余" in svg
    assert "<script" not in svg


def test_all_artifacts_returns_three_named_payloads(store):
    sprint_with_tasks(store, count=1, points=2.0)

    payload = reports.all_artifacts(store, horizon_s=3600.0)

    assert set(payload) == {"product_backlog", "sprint_backlog", "burndown"}
    assert payload["product_backlog"]["count"] == 1
    assert payload["sprint_backlog"]["points_committed"] == 2.0
    assert payload["burndown"]["baseline_pts"] == 2.0
