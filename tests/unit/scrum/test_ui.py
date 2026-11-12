"""The human console: everything a browser can do, driven as a browser does it.

These tests exist because the board is useless if the only way to move a card is
to hand-write an RPC call. They walk the exact clicks a person makes, and the
walkthrough asserts the *same* state machine the agents use — a console that
takes shortcuts would prove nothing about the agent path.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from octop.scrum.service import create_app

from .helpers import drive_planning, evidence, sprint_with_tasks, to_review


@pytest.fixture()
def client(store):
    # follow_redirects off: the flash message lives in the Location header.
    return TestClient(create_app(store), follow_redirects=False)


def _new_card(client, title: str = "把看板变成可点的", **fields):
    data = {"title": title, "points": "3", "acceptance": "能点; 能流转", "actor": "po"}
    data.update(fields)
    resp = client.post("/ui/new_card", data=data)
    # A request now stops in the Backlog until PO refinement, team sizing and
    # the owner's Sprint approval have happened.
    drive_planning(client)
    return resp


def _act(client, method: str, task_id: str, **fields):
    data = {"method": method, "task_id": task_id}
    data.update(fields)
    return client.post("/ui/action", data=data)


def _only_queued(store) -> str:
    queued = store.tasks_in_state("queued")
    assert len(queued) == 1
    return queued[0].task_id


def test_new_card_builds_a_sprint_and_a_dispatchable_task(client, store):
    resp = _new_card(client)

    assert resp.status_code == 303
    assert "msg=" in resp.headers["location"]
    task_id = _only_queued(store)
    # Dispatchable means *admissible*, not merely "row exists": the card has to
    # sit in an active sprint with acceptance criteria or the dispatcher skips it.
    assert store.admit(task_id).ok
    assert store.get_task(task_id).pbi_id


def test_new_card_without_acceptance_is_held_before_any_sprint_exists(client, store):
    """DoR now fires at refinement, not after the Sprint is approved.

    The old behaviour promoted the request to ``ready`` anyway, so the owner
    approved a Sprint whose only card was guaranteed to bounce at admission.
    The card must now be held: no task, no Sprint proposal, and the owner is
    asked immediately.
    """
    _new_card(client, acceptance="")

    assert store.tasks_in_state("queued") == []
    assert store.planned_sprint() is None
    held = store.pbis_in_state("needs_revision")
    assert len(held) == 1
    assert held[0]["acceptance"] == []

    page = client.get("/").text
    assert "需求被打回" in page
    assert "待补验收条件" in page
    assert 'action="/ui/revise"' in page


def test_second_card_joins_the_running_sprint_as_scope_injection(client, store):
    _new_card(client, title="first")
    _new_card(client, title="second")

    assert len(store.tasks_in_state("queued")) == 2
    with store.tx() as conn:
        kinds = [
            r["kind"]
            for r in conn.execute("SELECT kind FROM event_log WHERE kind='scope_injected'")
        ]
    assert kinds == ["scope_injected"]  # exactly one: the first card bootstrapped


def test_claim_from_the_console_occupies_the_agent(client, store):
    _new_card(client)
    task_id = _only_queued(store)

    _act(client, "task_claim", task_id, actor="dev-2")

    task = store.get_task(task_id)
    assert task is not None
    assert task.state == "running"  # claim + ack are one user action here
    assert task.agent_id == "dev-2"
    assert store.occupying_agent_ids() == {"dev-2"}


def test_submit_needs_a_lease_and_borrows_the_owner(client, store):
    _new_card(client)
    task_id = _only_queued(store)

    rejected = _act(client, "task_submit", task_id)
    assert "msg=" in rejected.headers["location"]
    assert store.get_task(task_id).state == "queued"

    _act(client, "task_claim", task_id, actor="dev-1")
    _act(
        client,
        "task_submit",
        task_id,
        summary="做了",
        tests_output="1 passed",
        files_changed="a.py",
    )
    task = store.get_task(task_id)
    assert task.state == "submitted"
    assert task.evidence["tests_output"] == "1 passed"


def test_full_walkthrough_queued_to_done(client, store):
    """What a person actually clicks to prove the loop end to end."""
    _new_card(client)
    task_id = _only_queued(store)

    _act(client, "task_claim", task_id, actor="dev-1")
    _act(client, "task_submit", task_id, summary="s", tests_output="t", files_changed="f")
    _act(client, "dod_record", task_id, actor="sm")
    assert store.get_task(task_id).state == "in_review"

    _act(client, "review_accept", task_id, actor="reviewer")

    task = store.get_task(task_id)
    assert task.state == "done"
    assert store.replay_states(task_id)[-3:] == ["submitted", "in_review", "done"]


def test_reject_puts_the_card_back_in_the_pool(client, store):
    _new_card(client)
    task_id = _only_queued(store)

    _act(client, "task_claim", task_id, actor="dev-1")
    _act(client, "task_submit", task_id, summary="s", tests_output="t", files_changed="f")
    _act(client, "dod_record", task_id, actor="sm")
    _act(client, "review_reject", task_id, actor="reviewer", reason="rework")

    assert store.get_task(task_id).state == "queued"


def test_owner_page_has_two_entries_and_three_artifacts(client, store):
    """The owner is not in PO/SM/team: three entries, three artefacts, no roles."""
    _new_card(client)

    page = client.get("/").text

    assert 'action="/ui/new_card"' in page
    assert "① 提需求" in page
    assert "② 批准 Sprint 提案" in page
    assert "③ Review 验收" in page
    assert "Product Backlog" in page
    assert "Sprint Backlog" in page
    assert "Burndown chart" in page
    # No identity picker, no team-side moves, no acceptance buttons yet.
    assert 'name="actor"' not in page
    assert "领给 dev-1" not in page
    assert "调试 / 人工兜底" not in page
    assert 'class="acts own"' not in page
    assert "验收通过" not in page

    debug = client.get("/?debug=1").text
    assert "领给 dev-1" in debug
    assert "调试 / 人工兜底" in debug


def test_the_only_primary_button_is_accept_or_reject(client, store):
    """When a card reaches review, the owner gets exactly two choices."""
    _, _, tasks = sprint_with_tasks(store)
    to_review(store, tasks[0])

    page = client.get("/").text

    assert 'class="acts own"' in page
    assert "验收通过" in page
    assert "打回" in page
    assert f"/deliverable/{tasks[0]}" in page
    assert "调试 / 人工兜底" not in page  # nothing left for the team to do


def test_unknown_action_is_refused_without_touching_the_board(client, store):
    _new_card(client)
    task_id = _only_queued(store)
    before = store.event_count()

    resp = _act(client, "review_accept", task_id, actor="reviewer")

    assert resp.status_code == 303
    assert store.get_task(task_id).state == "queued"
    assert store.event_count() == before


def test_board_says_so_when_nothing_is_picking_up_work(client):
    """A frozen board must explain itself; silence looks like a bug in the board."""
    page = client.get("/").text

    assert "没有 worker" in page


def test_board_reports_a_live_worker(client, store):
    from octop.scrum.worker import heartbeat_path

    heartbeat_path(store).write_text("0", encoding="utf-8")

    assert "worker 在线" in client.get("/").text


def test_dor_rejected_request_is_visible_in_review_with_a_fix_form(client, store):
    """The gate hands a card back; the owner must be able to act on it here."""
    _, pbi_ids, tasks = sprint_with_tasks(store, acceptance=())
    store.request_revision(tasks[0], "dor_fail:missing_acceptance", actor="dor-gate")

    page = client.get("/").text

    assert "需求被打回" in page
    assert "验收条件" in page
    assert f'name="pbi_id" value="{pbi_ids[0]}"' in page
    assert "do " in page  # the task title travels with the card


def test_owner_completing_the_criteria_resubmits_the_request(client, store):
    _, pbi_ids, tasks = sprint_with_tasks(store, acceptance=())
    store.request_revision(tasks[0], "dor_fail:missing_acceptance", actor="dor-gate")

    resp = client.post(
        "/ui/revise",
        data={
            "pbi_id": pbi_ids[0],
            "title": "PBI 0",
            "body": "",
            "acceptance": "收盘价可核对",
            "note": "补齐验收条件",
        },
    )

    assert resp.status_code == 303
    assert store.get_task(tasks[0]).state == "queued"
    assert store.get_pbi(pbi_ids[0])["acceptance"] == ["收盘价可核对"]
    assert "需求被打回" not in client.get("/").text


def test_blocked_card_is_actionable_without_debug_mode(client, store):
    """``blocked`` used to be a dead end for the owner: both exits were debug-only.

    T-4c39edf4 stopped with ``tooling_blocked`` and the panel said nothing --
    the card was invisible outside the flow column, so a tool failure looked
    like the board being broken.
    """
    _, _, tasks = sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    store.submit(tasks[0], lease.lease_id, evidence=evidence())
    store.block(tasks[0], "tooling_blocked", actor="sm", detail="guard 拦截 execute")

    page = client.get("/").text

    assert "阻塞待裁决" in page
    assert "tooling_blocked" in page
    assert "guard 拦截 execute" in page
    assert 'value="task_reverify"' in page
    assert 'value="task_reopen"' in page


def test_owner_reverification_returns_the_card_to_the_sm(client, store):
    _, _, tasks = sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    store.submit(tasks[0], lease.lease_id, evidence=evidence())
    store.block(tasks[0], "tooling_blocked", actor="sm", detail="guard")

    resp = client.post(
        "/ui/action",
        data={"method": "task_reverify", "task_id": tasks[0], "note": "guard 已修"},
    )

    assert resp.status_code == 303
    assert store.get_task(tasks[0]).state == "submitted"
