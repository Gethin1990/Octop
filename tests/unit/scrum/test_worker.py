"""The worker is the part that lets the owner stay out of the loop.

Every assertion here is about the *owner's* experience: a card goes in, a card
arrives in review, and nothing in between requires a human click. If a test here
needs a manual step, the worker is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from octop.scrum.worker import DemoExecutor, DodRunner, Outcome, Worker

from .helpers import sprint_with_tasks


class _Failing:
    name = "failing"

    def run(self, task):  # noqa: ANN001, ANN201 - test double
        return Outcome(
            ok=False,
            evidence={"summary": "boom", "tests_output": "", "files_changed": ""},
            note="pytest exited 1",
        )


@dataclass
class _Liar:
    """Claims an artefact it never wrote. Only a real DoD catches this."""

    name: str = "liar"

    def run(self, task):  # noqa: ANN001, ANN201
        return Outcome(
            ok=True,
            evidence={
                "summary": "pretended",
                "tests_output": "3 passed",
                "files_changed": "a.py",
                "artifact": "/nonexistent/artefact.md",
            },
            note="",
        )


def _worker(store, executor=None, tmp: Path | None = None) -> Worker:
    return Worker(
        store,
        executor or DemoExecutor(artifacts=(tmp or Path("/tmp")) / "artifacts"),
        log=lambda _msg: None,
    )


def test_one_step_takes_a_card_from_queued_to_review(store, tmp_path):
    _, _, tasks = sprint_with_tasks(store)
    worker = _worker(store, tmp=tmp_path)

    result = worker.step()

    assert result.task_id == tasks[0]
    assert result.action == "reviewed"
    assert result.state == "in_review"
    assert store.get_task(tasks[0]).state == "in_review"


def test_the_run_loop_finishes_every_admissible_card(store, tmp_path):
    _, _, tasks = sprint_with_tasks(store, count=2)
    results = _worker(store, tmp=tmp_path).run()

    assert [r.action for r in results] == ["reviewed", "reviewed"]
    assert all(store.get_task(t).state == "in_review" for t in tasks)


def test_evidence_comes_from_the_executor_not_from_a_constant(store, tmp_path):
    """The submitted evidence must describe the artefact that was actually made."""
    _, _, tasks = sprint_with_tasks(store)
    worker = _worker(store, tmp=tmp_path)

    worker.step()

    evidence = store.get_task(tasks[0]).evidence
    artefact = Path(evidence["artifact"])
    assert artefact.exists()
    assert evidence["tests_output"].count("=ok") == 3
    assert "has_spec_section=ok" in evidence["tests_output"]


def test_a_failing_executor_blocks_instead_of_retrying_forever(store, tmp_path):
    _, _, tasks = sprint_with_tasks(store)
    worker = _worker(store, executor=_Failing())

    result = worker.step()

    assert result.action == "blocked"
    assert store.get_task(tasks[0]).state == "blocked"
    assert store.get_task(tasks[0]).blocked_reason == "executor_failed"
    # A blocked card must stop costing money: the next step must not pick it up.
    assert worker.step().action == "idle"


def test_dod_catches_an_artefact_that_was_never_written(store):
    """The executor saying "ok" is not proof; the gate has to check the disk."""
    _, _, tasks = sprint_with_tasks(store)
    worker = _worker(store, executor=_Liar())

    result = worker.step()

    assert result.action == "dod_failed"
    assert result.detail == "artifact:exists"
    # Visible in the owner's DoD re-verification window, not silently parked in
    # Submitted where nobody looks, and not terminal-blocked before the SM can
    # correct a misjudgement.
    task = store.get_task(tasks[0])
    assert task.state == "verifying"
    assert task.blocked_reason == "artifact:exists"


def test_dor_sends_an_unready_request_back_to_the_owner(store, tmp_path):
    """A DoR failure is the owner's missing input, not a card to re-check forever."""
    _, _, tasks = sprint_with_tasks(store, acceptance=())
    worker = _worker(store, tmp=tmp_path)

    result = worker.step()

    assert result.action == "idle"
    assert worker.denied == {"dor_fail": 1}
    task = store.get_task(tasks[0])
    assert task.state == "needs_revision"
    assert "missing_acceptance" in (task.blocked_reason or "")
    assert store.replay_states(tasks[0])[-1] == "needs_revision"
    # Not silently re-admitted on the next tick: it stays with the owner.
    assert worker.step().action == "idle"


def test_agents_are_reused_rather_than_multiplied(store, tmp_path):
    """Five cards, three agents: the pool drains by reuse, not by hiring.

    Occupancy is ``leased|running`` only, so an agent is free the moment its card
    reaches review. That is the behaviour that keeps a small team affordable.
    """
    _, _, tasks = sprint_with_tasks(store, count=5)
    results = _worker(store, tmp=tmp_path).run()

    assert len([r for r in results if r.action == "reviewed"]) == 5
    assert store.occupying_agent_ids() == set()  # nobody ended up wedged
    assert all(store.get_task(t).state == "in_review" for t in tasks)


def test_dod_runner_reports_one_check_per_required_key():
    checks = DodRunner().check({"summary": "s", "tests_output": ""})
    by_id = {c["check_id"]: c["ok"] for c in checks}

    assert by_id == {
        "evidence:summary": True,
        "evidence:tests_output": False,
        "evidence:files_changed": False,
    }
