"""Board invariants. Each test names the failure it prevents."""

from __future__ import annotations

import json
import sqlite3

import pytest

from octop.scrum.constants import OCCUPYING_STATES
from octop.scrum.store import BoardStore, NotYourLease, TransitionRejected

from .helpers import sprint_with_tasks as _sprint_with_tasks
from .helpers import to_review as _to_review


def test_schema_is_idempotent(store):
    before = store.event_count()
    store.init_schema()
    store.init_schema()
    assert store.event_count() == before  # no duplicate rows on re-init
    assert store.policy().version == 0
    assert store.counts_by_state() == {}


def test_admission_requires_acceptance_criteria(store):
    """A card without AC is not Ready; admitting it produces reviewable garbage."""
    _, _, tasks = _sprint_with_tasks(store, acceptance=())
    assert store.admit(tasks[0]).reason == "dor_fail"


def test_admission_requires_active_sprint(store):
    sprint_id, pbi_ids, tasks = _sprint_with_tasks(store)
    store.close_sprint(sprint_id)
    assert store.admit(tasks[0]).reason == "no_sprint"


def test_review_backpressure_stops_admission(store):
    """Full review queue must stop admission rather than slow production down."""
    store.bump_policy({"review_queue_cap": 1}, note="tight cap")
    _, _, tasks = _sprint_with_tasks(store, count=2)
    _to_review(store, tasks[0])
    assert store.review_depth() == 1
    assert store.admit(tasks[1]).reason == "review_backpressure"


def test_pbi_wip_cap(store):
    store.bump_policy({"in_flight_pbi_cap": 1, "review_queue_cap": 50}, note="one slot")
    _, _, tasks = _sprint_with_tasks(store, count=2)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    assert store.admit(tasks[1]).reason == "pbi_wip"


def test_expedite_is_priced_not_forbidden(store):
    """The interruption budget degrades expedite to 'queued', it never crashes."""
    sprint_id = store.create_sprint("S1", capacity_pts=10.0, interrupt_ratio=0.1)  # budget = 1.0
    pbi = store.create_pbi("urgent", points=5.0, acceptance=["x"], class_of_service="expedite")
    store.commit_sprint(sprint_id, [pbi])
    task = store.create_task(pbi, "urgent work")
    admit = store.admit(task)
    assert not admit.ok
    assert admit.reason == "interrupt_budget_exhausted"
    assert admit.detail["budget"] == pytest.approx(1.0)


def test_interrupt_is_charged_on_ack(store):
    sprint_id = store.create_sprint("S1", capacity_pts=100.0, interrupt_ratio=0.2)
    pbi = store.create_pbi("urgent", points=5.0, acceptance=["x"], class_of_service="expedite")
    store.commit_sprint(sprint_id, [pbi])
    task = store.create_task(pbi, "urgent work")
    lease = store.claim(task, "dev-1")
    assert lease is not None
    assert store.ack(task, lease.lease_id)
    assert store.sprint_usage(sprint_id)["used"] == pytest.approx(5.0)


def test_claim_is_exclusive_per_agent(store):
    """One agent == one slot, because Octop serialises turns per session."""
    _, _, tasks = _sprint_with_tasks(store, count=2)
    first = store.claim(tasks[0], "dev-1")
    assert first is not None
    assert store.claim(tasks[1], "dev-1") is None  # dev-1 is busy
    second = store.claim(tasks[1], "dev-2")
    assert second is not None


def test_claim_rejects_wrong_role(store):
    _, _, tasks = _sprint_with_tasks(store)
    with pytest.raises(TransitionRejected) as exc:
        store.claim(tasks[0], "po")
    assert exc.value.reason == "unknown_agent"


def test_second_claim_of_same_task_returns_none(store):
    _, _, tasks = _sprint_with_tasks(store)
    assert store.claim(tasks[0], "dev-1") is not None
    assert store.claim(tasks[0], "dev-2") is None


def test_heartbeat_cannot_exceed_hard_deadline(store):
    store.bump_policy({"lease_ttl_s": 100, "lease_hard_cap_s": 150}, note="short caps")
    _, _, tasks = _sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    store.heartbeat(tasks[0], lease.lease_id)
    task = store.get_task(tasks[0])
    assert task is not None
    assert task.lease_expires_at <= lease.hard_deadline + 1


def test_foreign_lease_is_refused(store):
    _, _, tasks = _sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    with pytest.raises(NotYourLease):
        store.heartbeat(tasks[0], "not-the-lease")


def test_submit_requires_evidence(store):
    _, _, tasks = _sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    with pytest.raises(TransitionRejected) as exc:
        store.submit(tasks[0], lease.lease_id, evidence={"summary": "x"})
    assert exc.value.reason == "evidence_incomplete"


def test_submit_is_idempotent(store):
    _, _, tasks = _sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    evidence = {"summary": "s", "tests_output": "ok", "files_changed": "a.py"}
    first = store.submit(tasks[0], lease.lease_id, evidence=evidence, idem_key="k1")
    second = store.submit(tasks[0], lease.lease_id, evidence=evidence, idem_key="k1")
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert store.get_task(tasks[0]).state == "submitted"


def test_done_only_through_accept(store):
    """An agent cannot self-declare completion: done is a review outcome."""
    _, _, tasks = _sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    store.submit(
        tasks[0],
        lease.lease_id,
        evidence={"summary": "s", "tests_output": "ok", "files_changed": "a.py"},
    )
    with pytest.raises(TransitionRejected):
        store.accept(tasks[0], reviewer="reviewer")  # no DoD rows yet
    store.record_dod(tasks[0], [{"check_id": "unit", "ok": True}])
    assert store.get_task(tasks[0]).state == "in_review"
    store.accept(tasks[0], reviewer="reviewer", dwell_seconds=120)
    assert store.get_task(tasks[0]).state == "done"


def test_failed_dod_opens_a_verification_window_without_marking_done(store):
    """A FAIL is evidence, not a verdict: it must not silently requeue or block."""
    _, _, tasks = _sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    assert store.ack(tasks[0], lease.lease_id)
    store.submit(
        tasks[0],
        lease.lease_id,
        evidence={"summary": "s", "tests_output": "boom", "files_changed": "a.py"},
    )
    assert not store.record_dod(tasks[0], [{"check_id": "unit", "ok": False}])
    task = store.get_task(tasks[0])
    assert task.state == "verifying"
    assert task.attempt == 1  # the FAIL does not burn another attempt
    assert task.blocked_reason == "unit"
    # The verifier may correct itself: a second round is accepted and can pass.
    assert store.record_dod(tasks[0], [{"check_id": "unit", "ok": True}])
    assert store.get_task(tasks[0]).state == "in_review"


def test_close_sprint_is_destructive(store):
    sprint_id, _, tasks = _sprint_with_tasks(store, count=2)
    assert store.claim(tasks[0], "dev-1") is not None
    counts = store.close_sprint(sprint_id)
    assert counts["requeued"] == 2
    assert store.occupying_agent_ids() == set()


def test_replay_matches_projection(store):
    """The event log must be able to rebuild the state column, or it is folklore."""
    _, _, tasks = _sprint_with_tasks(store)
    _to_review(store, tasks[0])
    store.accept(tasks[0], reviewer="reviewer")
    states = store.replay_states(tasks[0])
    assert states[-1] == "done"
    assert store.get_task(tasks[0]).state == "done"
    assert "in_review" in states


def test_release_blocks_after_max_attempts(store):
    store.bump_policy({"max_attempts": 1}, note="one shot")
    _, _, tasks = _sprint_with_tasks(store)
    lease = store.claim(tasks[0], "dev-1")
    assert lease is not None
    assert store.ack(tasks[0], lease.lease_id)
    assert store.release(tasks[0], "expired") == "blocked"


def test_admit_denied_histogram_is_queryable(store):
    _, _, tasks = _sprint_with_tasks(store, acceptance=())
    admit = store.admit(tasks[0])
    store.note_admission(tasks[0], admit.reason, admit.detail)
    assert store.admit_denied_histogram() == {"dor_fail": 1}


def test_ceremony_window_is_idempotent(store):
    first = store.ceremony_start("daily", "2026-11-11")
    second = store.ceremony_start("daily", "2026-11-11", trigger_src="catchup")
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert not store.ceremony_window_done("daily", "2026-11-11")
    store.ceremony_finish(first["run_id"], status="ok")
    assert store.ceremony_window_done("daily", "2026-11-11")


def test_policy_bump_is_append_only_and_validated(store):
    store.bump_policy({"review_queue_cap": 3}, note="first")
    store.bump_policy({"review_queue_cap": 2}, note="second")
    assert store.policy().version == 2
    assert store.policy()["review_queue_cap"] == 2
    with pytest.raises(ValueError):
        store.bump_policy({"review_queue_cap": 0}, note="disable a gate")


def test_event_log_payload_is_json(store):
    _, _, tasks = _sprint_with_tasks(store)
    with store.tx() as conn:
        rows = conn.execute("SELECT payload_json FROM event_log").fetchall()
    for row in rows:
        json.loads(row["payload_json"])
    assert OCCUPYING_STATES == ("leased", "running")


# ── schema migration: pbi.state gains `needs_revision` ──────────────────────
def test_old_pbi_table_is_rebuilt_to_accept_the_new_state(board_dir):
    """A board created before the PO-side hold must not need a wipe.

    SQLite cannot widen a CHECK, so the table is rebuilt -- and the event log,
    which *is* the history, must survive untouched.
    """
    path = board_dir / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE pbi (
            pbi_id          TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            body            TEXT NOT NULL DEFAULT '',
            state           TEXT NOT NULL CHECK (state IN
                                ('new', 'refining', 'ready', 'committed', 'done', 'parked')),
            points          REAL,
            priority_rank   REAL NOT NULL,
            class_of_service TEXT NOT NULL DEFAULT 'standard',
            sprint_id       TEXT,
            acceptance_json TEXT NOT NULL DEFAULT '[]',
            readiness_json  TEXT NOT NULL DEFAULT '{}',
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );
        INSERT INTO pbi (pbi_id, title, state, priority_rank, points,
                         acceptance_json, created_at, updated_at)
        VALUES ('PBI-old', '老需求', 'ready', 100.0, 3.0, '[]', 1.0, 1.0);
        """
    )
    conn.commit()
    conn.close()

    store = BoardStore(path)
    store.init_schema()

    assert store.meta_get("schema_version") == "4"
    kept = store.get_pbi("PBI-old")
    assert kept is not None
    assert kept["title"] == "老需求"
    assert kept["points"] == 3.0  # rows copied, not defaulted away

    # The widened CHECK now accepts what refinement needs to write.
    store.refine_pbi("PBI-old")
    assert store.get_pbi("PBI-old")["state"] == "needs_revision"
    store.close()
