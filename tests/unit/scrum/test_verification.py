"""DoD re-verification window: a FAIL is evidence, not a terminal verdict.

The failure these tests prevent is the one the real L2 run exposed: the SM
misread an acceptance criterion, recorded ``acceptance_met=0``, and by the time
it wanted to correct itself the card had already left ``submitted``. Two more
automatic reworks later it was ``blocked`` and no human had been asked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from octop.scrum.service import create_app
from octop.scrum.store import TransitionRejected

from .helpers import evidence, sprint_with_tasks


def _submit(store, task_id: str, agent: str = "dev-1") -> None:
    lease = store.claim(task_id, agent)
    assert lease is not None
    store.submit(task_id, lease.lease_id, evidence=evidence())


def _fail(store, task_id: str) -> None:
    assert not store.record_dod(
        task_id,
        [
            {"check_id": "acceptance_met", "ok": False, "output_tail": "criterion not read"},
            {"check_id": "artifact:exists", "ok": True},
        ],
    )


def test_a_failed_round_opens_the_window_instead_of_requeueing(store):
    _, _, tasks = sprint_with_tasks(store)
    _submit(store, tasks[0])

    _fail(store, tasks[0])

    task = store.get_task(tasks[0])
    assert task.state == "verifying"
    assert task.attempt == 0  # nothing was retried, so nothing was spent
    assert task.blocked_reason == "acceptance_met"


def test_the_verifier_can_correct_its_own_verdict_in_a_new_round(store):
    _, _, tasks = sprint_with_tasks(store)
    _submit(store, tasks[0])
    _fail(store, tasks[0])

    # The SM re-reads the task and records a second, corrected round.
    assert store.record_dod(
        tasks[0],
        [{"check_id": "acceptance_met", "ok": True}],
    )
    assert store.get_task(tasks[0]).state == "in_review"

    # History is append-only: the original FAIL is still visible for the audit.
    rounds = store.dod_rounds(tasks[0])
    assert [int(r["check_round"]) for r in rounds] == [1, 2]
    assert rounds[0]["failed"] == "acceptance_met"
    checks = store.dod_checks(tasks[0])
    assert len(checks) == 3
    assert {int(c["check_round"]) for c in checks} == {1, 2}


def test_accept_counts_only_the_latest_round(store):
    """Counting every historical row would make a corrected verdict un-acceptable."""
    _, _, tasks = sprint_with_tasks(store)
    _submit(store, tasks[0])
    _fail(store, tasks[0])
    assert store.record_dod(tasks[0], [{"check_id": "acceptance_met", "ok": True}])

    store.accept(tasks[0], reviewer="owner")

    assert store.get_task(tasks[0]).state == "done"


def test_rework_resets_the_attempt_budget_and_requeues(store):
    _, _, tasks = sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    store.ack(tasks[0], lease.lease_id)
    store.submit(tasks[0], lease.lease_id, evidence=evidence())
    _fail(store, tasks[0])
    assert store.get_task(tasks[0]).attempt == 1

    state = store.resolve_verification(
        tasks[0], "rework", actor="owner", note="acceptance criterion was never met"
    )

    assert state == "queued"
    task = store.get_task(tasks[0])
    assert task.state == "queued"
    assert task.attempt == 0
    assert task.wake_failures == 0
    assert task.agent_id is None and task.lease_id is None
    assert task.blocked_reason is None


def test_recheck_rearms_the_verifier_wake(store):
    _, _, tasks = sprint_with_tasks(store)
    _submit(store, tasks[0])
    _fail(store, tasks[0])
    store.mark_verify_woken(tasks[0], "sm")
    assert tasks[0] not in [t.task_id for t in store.tasks_awaiting_dod()]

    state = store.resolve_verification(tasks[0], "recheck", actor="owner")

    assert state == "submitted"
    assert tasks[0] in [t.task_id for t in store.tasks_awaiting_dod()]


def test_escalate_makes_blocking_an_explicit_human_decision(store):
    _, _, tasks = sprint_with_tasks(store)
    _submit(store, tasks[0])
    _fail(store, tasks[0])

    assert store.resolve_verification(
        tasks[0], "escalate", actor="owner", note="output is fabricated"
    ) == "blocked"

    task = store.get_task(tasks[0])
    assert task.state == "blocked"
    assert task.blocked_reason == "output is fabricated"


def test_resolve_requires_the_verifying_state(store):
    _, _, tasks = sprint_with_tasks(store)
    with pytest.raises(TransitionRejected):
        store.resolve_verification(tasks[0], "rework")


def test_reopen_revives_a_blocked_card_with_a_fresh_budget(store):
    _, _, tasks = sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    store.ack(tasks[0], lease.lease_id)
    store.block(tasks[0], "executor_failed")

    assert store.reopen(tasks[0], actor="owner", note="fixed the executor")

    task = store.get_task(tasks[0])
    assert task.state == "queued"
    assert task.attempt == 0
    assert task.blocked_reason is None
    assert store.replay_states(tasks[0])[-1] == "queued"


def test_the_owner_sees_the_window_and_can_rework_it_from_the_page(store):
    """The fix is only real if the failure is visible where the owner decides."""
    _, _, tasks = sprint_with_tasks(store)
    _submit(store, tasks[0])
    _fail(store, tasks[0])
    client = TestClient(create_app(store), follow_redirects=False)

    page = client.get("/").text

    assert "DoD 复核窗口" in page
    assert "acceptance_met" in page
    assert 'name="decision" value="rework"' in page

    resp = client.post(
        "/ui/action",
        data={
            "method": "verification_resolve",
            "task_id": tasks[0],
            "decision": "rework",
            "note": "输出与验收条件不符",
        },
    )

    assert resp.status_code == 303
    assert store.get_task(tasks[0]).state == "queued"


def test_the_deliverable_page_refuses_to_accept_a_failed_round(store):
    _, _, tasks = sprint_with_tasks(store)
    _submit(store, tasks[0])
    _fail(store, tasks[0])
    client = TestClient(create_app(store), follow_redirects=False)

    page = client.get(f"/deliverable/{tasks[0]}").text

    assert "此时不能验收" in page
    assert 'value="review_accept"' not in page


def test_review_reject_does_not_consume_the_retry_budget(store):
    """An owner's second review must not strand a card they are actively editing."""
    _, _, tasks = sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    store.ack(tasks[0], lease.lease_id)
    store.submit(tasks[0], lease.lease_id, evidence=evidence())
    assert store.record_dod(tasks[0], [{"check_id": "unit", "ok": True}])
    assert store.get_task(tasks[0]).attempt == 1

    store.reject(tasks[0], reviewer="owner", note="not what I asked")

    task = store.get_task(tasks[0])
    assert task.state == "queued"
    assert task.attempt == 0
    assert task.verify_wake_at is None


# ── blocked is not a dead end ───────────────────────────────────────────────
def test_blocked_card_can_return_to_verification_without_redoing_the_work(store):
    """A tooling failure must not force the dev to re-run a correct turn.

    T-4c39edf4 was blocked with ``tooling_blocked`` when the scope gate denied
    the SM ``execute``; the only escape was ``reopen``, which resets attempts and
    re-runs the agent. Re-verification keeps the evidence and just wakes the SM.
    """
    _, _, tasks = sprint_with_tasks(store)
    task_id = tasks[0]
    lease = store.claim(task_id, "dev-1")
    assert lease is not None
    store.submit(task_id, lease.lease_id, evidence=evidence())
    before_task = store.get_task(task_id)
    before = before_task.evidence
    before_attempt = before_task.attempt
    store.block(task_id, "tooling_blocked", actor="sm", detail="guard 拦截 execute")

    assert store.request_reverification(task_id, actor="owner", note="guard 已修")

    after = store.get_task(task_id)
    assert after.state == "submitted"
    assert after.blocked_reason is None
    assert after.evidence == before  # the artefact being judged is unchanged
    assert after.attempt == before_attempt  # a human decision, not a retry


def test_only_a_stopped_card_can_be_reverified(store):
    _, _, tasks = sprint_with_tasks(store)
    with pytest.raises(TransitionRejected):
        store.request_reverification(tasks[0], actor="owner")
