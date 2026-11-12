"""States, classes of service and admission reasons.

Everything the board can say about a task lives here so that the store, the
dispatcher, the RPC layer and the reading UIs cannot drift apart. Adding a state
means editing this module, ``schema.sql`` (the CHECK constraint) and the
transition table in ``store.py`` — deliberately annoying, because a state that
nobody transitions to is worse than no state at all.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

# ── task lifecycle ───────────────────────────────────────────────────────────
# done is reachable only through review (see store.accept), never through an
# agent's self-report: "I finished" costs no tool call, so no guard can catch it.
TaskState = Literal[
    "queued",  # waiting for admission / a free slot
    "leased",  # a dispatcher handed it to one agent; nothing has run yet
    "running",  # the agent acknowledged the wake and is executing
    "submitted",  # evidence recorded; DoD checks pending
    "verifying",  # DoD reported a FAIL: re-verification window, not a verdict
    "in_review",  # DoD passed, waiting for a human decision
    "needs_revision",  # DoR failed: the request is back with the owner to fix
    "done",
    "blocked",  # the only state that requires a human
    "parked",  # deliberately set aside (dependency / spec gap / no owner)
]
TASK_STATES: Final[tuple[str, ...]] = get_args(TaskState)

#: States that occupy an agent slot. One agent == one concurrency unit, because
#: Octop serialises turns per session and a session binds exactly one thread.
OCCUPYING_STATES: Final[tuple[str, ...]] = ("leased", "running")

#: States that occupy the review queue (this is what back-pressure throttles).
#: ``needs_revision`` is deliberately absent: it is the owner's *input* queue,
#: not the team's acceptance queue. Counting it would let an unedited request
#: stop all production, which is a different failure than review overload.
REVIEW_STATES: Final[tuple[str, ...]] = ("in_review",)

#: Everything that belongs in the owner's Review panel, in display order.
#: ``verifying`` is here because a failed DoD used to be a silent, terminal
#: rework: the verifier could not correct its own mistake and the owner never
#: saw the card. A FAIL now opens a window in which the verifier may re-run the
#: checks, or the owner may re-verify / rework / escalate.
OWNER_REVIEW_STATES: Final[tuple[str, ...]] = ("needs_revision", "verifying", "in_review")

PBIState = Literal["new", "refining", "ready", "committed", "done", "parked"]

SprintState = Literal["planned", "active", "closed"]

ClassOfService = Literal["expedite", "standard", "fixed_date", "intangible"]
CLASSES_OF_SERVICE: Final[tuple[str, ...]] = get_args(ClassOfService)

AgentRole = Literal["po", "sm", "dev", "reviewer", "host"]

# ── admission ────────────────────────────────────────────────────────────────
# Order matters: the dispatcher reports the *first* matching reason, and the
# whole point of the reason is to tell a human which knob to turn.
#
#   review_backpressure  -> the bottleneck is acceptance, not production
#   pbi_wip              -> the bottleneck is agent slots
#   dor_fail             -> the bottleneck is the spec
#
# Collapsing these into a single "busy" would erase the only signal that
# distinguishes "agents are slow" from "we cannot review what agents produce".
AdmitReason = Literal[
    "ok",
    "not_queued",
    "no_sprint",  # the task's sprint is not active
    "not_in_sprint",  # pbi not committed to the active sprint
    "dor_fail",  # acceptance criteria missing
    "review_backpressure",  # review queue at cap -> stop admitting, do not slow down
    "pbi_wip",  # in-flight PBI cap reached
    "interrupt_budget_exhausted",  # expedite work priced, budget spent
    "no_slot",  # target agent already has a task in flight
    "unknown_agent",
]

ReleaseReason = Literal[
    "expired",  # lease TTL passed
    "wake_rejected",  # the agent was busy; nothing ran, so no attempt is burned
    "wake_dropped",  # injected but never acknowledged
    "stalled",  # heartbeat and progress both silent
    "sprint_closed",  # boundary is destructive: no running task survives it
    "identity_drift",  # agent deleted / disabled / role changed
    "human",
]

# ── wake results ─────────────────────────────────────────────────────────────
WakeStatus = Literal[
    "accepted",  # the server queued the turn
    "rejected",  # delivery=follow_up while busy -> retry later
    "busy",  # turn.busy (shared turn)
    "error",
    "timeout",
]

# ── ceremony ─────────────────────────────────────────────────────────────────
Ceremony = Literal["refinement", "planning", "daily", "review", "retro"]
CEREMONIES: Final[tuple[str, ...]] = get_args(Ceremony)

#: ``trigger_src`` values. ``catchup`` exists because APScheduler drops missed
#: recurring ticks after a 60s grace window — a laptop that slept through 09:30
#: would otherwise skip Daily with no record anywhere.
TriggerSrc = Literal["cron", "event", "catchup", "human", "api"]

TERMINAL_TASK_STATES: Final[frozenset[str]] = frozenset({"done", "blocked", "parked"})
