"""BoardStore — the only writer of the board database.

Invariants enforced here (not by convention, by transaction):

* one task per agent slot at a time (``claim`` refuses while another task of the
  same agent is ``leased``/``running``);
* ``claim`` is a compare-and-swap on ``state='queued' AND lease_id IS NULL``;
* ``done`` is reachable only from ``in_review`` via ``accept`` — an agent
  cannot self-declare completion;
* every mutation appends to ``event_log`` inside the same transaction, so the
  log can be replayed to rebuild any projection.

The store never talks to Octop. It has no network code and no clock beyond
``time.time()``, which is what makes the whole thing testable without a server.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from . import policy as policy_mod
from .constants import (
    OCCUPYING_STATES,
    REVIEW_STATES,
    TERMINAL_TASK_STATES,
)

_SCHEMA_PATH: Final[Path] = Path(__file__).with_name("schema.sql")


class BoardError(RuntimeError):
    """Base class for board-level rejections (all carry a machine-readable reason)."""

    reason: str = "error"


class NotYourLease(BoardError):
    """The caller's lease_id does not own this task (or the lease already moved on)."""

    reason = "not_your_lease"


class TransitionRejected(BoardError):
    """The requested transition is illegal for the current state."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


def now_ts() -> float:
    return time.time()


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    pbi_id: str
    sprint_id: str
    title: str
    spec: str
    state: str
    role: str
    agent_id: str | None
    lease_id: str | None
    attempt: int
    max_attempts: int
    wake_failures: int
    lease_expires_at: float | None
    lease_hard_deadline: float | None
    last_wake_at: float | None
    acked_at: float | None
    thread_id: str | None
    branch: str | None
    blocked_reason: str | None
    retry_after: float | None
    verify_wake_at: float | None
    evidence: dict[str, Any]
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        return cls(
            task_id=row["task_id"],
            pbi_id=row["pbi_id"],
            sprint_id=row["sprint_id"],
            title=row["title"],
            spec=row["spec"],
            state=row["state"],
            role=row["role"],
            agent_id=row["agent_id"],
            lease_id=row["lease_id"],
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            wake_failures=int(row["wake_failures"]),
            lease_expires_at=row["lease_expires_at"],
            lease_hard_deadline=row["lease_hard_deadline"],
            last_wake_at=row["last_wake_at"],
            acked_at=row["acked_at"],
            thread_id=row["thread_id"],
            branch=row["branch"],
            blocked_reason=row["blocked_reason"],
            retry_after=row["retry_after"],
            verify_wake_at=row["verify_wake_at"],
            evidence=evidence if isinstance(evidence, dict) else {},
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class Lease:
    task_id: str
    lease_id: str
    agent_id: str
    expires_at: float
    hard_deadline: float
    spec: str
    branch: str


@dataclass(frozen=True, slots=True)
class Admit:
    ok: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


class BoardStore:
    """SQLite-backed board. One instance per process; call it from one thread."""

    def __init__(self, path: str | Path, *, now: Any = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or now_ts
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ── plumbing ────────────────────────────────────────────────────────────
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init_schema(self) -> None:
        ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock:
            self.conn.executescript(ddl)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '4')"
            )
            self._migrate_task_states(ddl)
            self._migrate_pbi_states(ddl)
            self._migrate_agent_channel()
            self._migrate_task_verify_columns()
            self._migrate_dod_round()

    def _migrate_task_verify_columns(self) -> None:
        """Add the SM-verify bookkeeping columns to an older board.

        ``ALTER TABLE ADD COLUMN`` is enough here (no CHECK to widen), so this
        never rebuilds the table — the event log stays untouched.
        """
        wanted = {
            "verify_wake_at": "REAL",
            "verify_agent_id": "TEXT",
        }
        with self._lock:
            cols = {
                str(r["name"])
                for r in self.conn.execute("PRAGMA table_info(task)").fetchall()
            }
            missing = {k: v for k, v in wanted.items() if k not in cols}
            if not missing:
                return
            with self.tx() as conn:
                for name, sqltype in missing.items():
                    conn.execute(f"ALTER TABLE task ADD COLUMN {name} {sqltype}")

    def _migrate_agent_channel(self) -> None:
        """Add ``agent_role.channel`` to boards created before it existed.

        Everything already in the table was a local harness identity (the real
        ones did not exist yet), so ``local`` is both the column default and the
        correct backfill. Existing rows therefore keep working unchanged and the
        dispatcher still cannot wake them.
        """
        with self._lock:
            cols = {
                str(r["name"])
                for r in self.conn.execute("PRAGMA table_info(agent_role)").fetchall()
            }
            if "channel" in cols:
                return
            with self.tx() as conn:
                conn.execute(
                    "ALTER TABLE agent_role ADD COLUMN channel TEXT NOT NULL DEFAULT 'local'"
                )

    def _migrate_dod_round(self) -> None:
        """Older boards recorded one flat list of checks; add the round column.

        ``ALTER TABLE ADD COLUMN`` with a constant DEFAULT is enough (no CHECK
        to widen), so existing rows become round 1 and stay readable. The
        history is never rewritten.
        """
        with self._lock:
            cols = {
                str(r["name"])
                for r in self.conn.execute("PRAGMA table_info(dod_check)").fetchall()
            }
            if "check_round" in cols:
                return
            with self.tx() as conn:
                conn.execute(
                    "ALTER TABLE dod_check ADD COLUMN check_round INTEGER NOT NULL DEFAULT 1"
                )

    def _migrate_task_states(self, ddl: str) -> None:
        """Widen ``task.state`` CHECK on boards created before ``verifying``.

        SQLite has no ``ALTER CONSTRAINT``. Rebuilding the table is the only way,
        and deleting the board would be worse: the event log *is* the history.
        Rows are copied by the columns the legacy table actually has, so columns
        added later (e.g. ``verify_wake_at``) simply take their DEFAULT instead
        of breaking an old board with a column-count mismatch. Indexes are
        recreated by re-running the (idempotent) DDL.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='task'"
            ).fetchone()
            ddl_of_task = str(row["sql"] or "")
            if row is None or all(
                state in ddl_of_task for state in ("needs_revision", "verifying")
            ):
                return
            start = ddl.index("CREATE TABLE IF NOT EXISTS task (")
            end = ddl.index("\n);", start) + len("\n);")
            create = ddl[start:end].replace(
                "CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1
            )
            with self.tx() as conn:
                conn.execute("DROP TABLE IF EXISTS task_legacy")
                conn.execute("ALTER TABLE task RENAME TO task_legacy")
                conn.execute(create)
                legacy_cols = [
                    col_row["name"]
                    for col_row in conn.execute("PRAGMA table_info(task_legacy)").fetchall()
                ]
                cols = ", ".join(f'"{name}"' for name in legacy_cols)
                conn.execute(f"INSERT INTO task ({cols}) SELECT {cols} FROM task_legacy")
                conn.execute("DROP TABLE task_legacy")
            self.conn.executescript(ddl)
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4')"
            )

    def _migrate_pbi_states(self, ddl: str) -> None:
        """Widen ``pbi.state`` CHECK on boards created before ``needs_revision``.

        Same constraint as the task table: SQLite cannot alter a CHECK, so the
        table is rebuilt and each row copied by the columns it actually has.
        There is no foreign key pointing at ``pbi``, so the rename/drop dance
        cannot cascade into ``task``. Indexes are recreated by the idempotent
        DDL that runs after the swap.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='pbi'"
            ).fetchone()
            ddl_of_pbi = str(row["sql"] or "") if row is not None else ""
            if row is None or "needs_revision" in ddl_of_pbi:
                return
            start = ddl.index("CREATE TABLE IF NOT EXISTS pbi (")
            end = ddl.index("\n);", start) + len("\n);")
            create = ddl[start:end].replace(
                "CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1
            )
            with self.tx() as conn:
                conn.execute("DROP TABLE IF EXISTS pbi_legacy")
                conn.execute("ALTER TABLE pbi RENAME TO pbi_legacy")
                conn.execute(create)
                legacy_cols = [
                    col_row["name"]
                    for col_row in conn.execute("PRAGMA table_info(pbi_legacy)").fetchall()
                ]
                cols = ", ".join(f'"{name}"' for name in legacy_cols)
                conn.execute(f"INSERT INTO pbi ({cols}) SELECT {cols} FROM pbi_legacy")
                conn.execute("DROP TABLE pbi_legacy")
            self.conn.executescript(ddl)
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4')"
            )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction. ``BEGIN IMMEDIATE`` so concurrent claimers serialise."""
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def log(
        self,
        conn: sqlite3.Connection,
        kind: str,
        entity: str,
        entity_id: str | None,
        actor: str,
        **payload: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO event_log (ts, kind, entity, entity_id, actor, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                self._now(),
                kind,
                entity,
                entity_id,
                actor,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )

    # ── policy ──────────────────────────────────────────────────────────────
    def policy(self) -> policy_mod.Policy:
        with self._lock:
            return policy_mod.load(self.conn)

    def bump_policy(self, changes: dict[str, Any], *, note: str, actor: str = "human") -> policy_mod.Policy:
        with self.tx() as conn:
            updated = policy_mod.bump(conn, changes, note=note, now=self._now())
            self.log(conn, "policy_bumped", "system", str(updated.version), actor, note=note,
                     policy_hash=updated.hash, changes=changes)
            return updated

    # ── agents ──────────────────────────────────────────────────────────────
    def register_agent(
        self,
        agent_id: str,
        role: str,
        *,
        enabled: bool = True,
        channel: str = "local",
    ) -> None:
        """Bind an identity to a role and to the one channel allowed to drive it.

        ``channel`` is not decoration: it is what stops a placeholder executor
        from claiming work under a real agent's name. Default ``local`` keeps
        every pre-existing registration (and every test harness) on the local
        side, where the demo worker lives.
        """
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO agent_role (agent_id, role, channel, enabled, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(agent_id) DO UPDATE SET role=excluded.role,"
                " channel=excluded.channel, enabled=excluded.enabled,"
                " updated_at=excluded.updated_at",
                (agent_id, role, channel, 1 if enabled else 0, self._now()),
            )
            self.log(conn, "agent_registered", "agent", agent_id, "human", role=role,
                     enabled=enabled, channel=channel)

    def role_of(self, agent_id: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT role, enabled FROM agent_role WHERE agent_id=?", (agent_id,)
            ).fetchone()
        if row is None or not row["enabled"]:
            return None
        return str(row["role"])

    def agent_channel(self, agent_id: str | None) -> str | None:
        """Which executor drives *agent_id* (``local`` / ``octop`` / ``any``)."""
        if not agent_id:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT channel FROM agent_role WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return None if row is None else str(row["channel"])

    def agents_by_role(self, role: str, *, channel: str | None = None) -> list[str]:
        """Enabled identities for *role*, optionally restricted to one channel.

        ``channel='octop'`` matches rows whose channel is ``octop`` or ``any``;
        likewise for ``local``. ``None`` keeps the old behaviour (all of them)
        for callers that are only asking "who could hold this role".
        """
        sql = "SELECT agent_id FROM agent_role WHERE role=? AND enabled=1"
        params: list[Any] = [role]
        if channel is not None:
            sql += " AND channel IN (?, 'any')"
            params.append(channel)
        sql += " ORDER BY agent_id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [str(r["agent_id"]) for r in rows]

    # ── backlog ─────────────────────────────────────────────────────────────
    def create_pbi(
        self,
        title: str,
        *,
        body: str = "",
        points: float | None = None,
        priority_rank: float | None = None,
        class_of_service: str = "standard",
        acceptance: list[str] | None = None,
        actor: str = "po",
        state: str | None = None,
    ) -> str:
        pbi_id = f"PBI-{uuid.uuid4().hex[:8]}"
        acceptance = acceptance or []
        # The owner's request always starts as ``new``: readiness is a PO
        # decision, not a side effect of filling in a form field. Other callers
        # (RPC, demo seeding) keep the old convenience default.
        state = state or ("ready" if acceptance else "new")
        with self.tx() as conn:
            rank = priority_rank
            if rank is None:
                row = conn.execute("SELECT COALESCE(MAX(priority_rank), 0) AS r FROM pbi").fetchone()
                rank = float(row["r"]) + 100.0
            ts = self._now()
            conn.execute(
                "INSERT INTO pbi (pbi_id, title, body, state, points, priority_rank,"
                " class_of_service, acceptance_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pbi_id,
                    title,
                    body,
                    state,
                    points,
                    rank,
                    class_of_service,
                    json.dumps(acceptance, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
            self.log(conn, "pbi_created", "pbi", pbi_id, actor, title=title, points=points,
                     class_of_service=class_of_service, state=state)
        return pbi_id

    def get_pbi(self, pbi_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM pbi WHERE pbi_id=?", (pbi_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["acceptance"] = json.loads(item.pop("acceptance_json") or "[]")
        item["readiness"] = json.loads(item.pop("readiness_json") or "{}")
        return item

    def estimate_pbi(
        self,
        pbi_id: str,
        points: float,
        *,
        actor: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Record the team's size estimate — never the owner's guess.

        Sizing is a developer activity in Scrum: the person who will do the work
        is the only one who knows how big it is. The event carries ``delta`` so a
        re-estimate after the PBI entered a sprint can be replayed into the
        burndown instead of silently inflating the current projection.
        """
        try:
            value = float(points)
        except (TypeError, ValueError) as exc:
            raise TransitionRejected("invalid_points", str(points)) from exc
        if not math.isfinite(value) or value <= 0:
            raise TransitionRejected("invalid_points", str(points))
        value = round(value, 2)
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM pbi WHERE pbi_id=?", (pbi_id,)).fetchone()
            if row is None:
                raise TransitionRejected("unknown_pbi", pbi_id)
            if row["state"] == "done":
                raise TransitionRejected("pbi_already_done", pbi_id)
            previous = float(row["points"] or 0.0)
            delta = round(value - previous, 2)
            try:
                readiness = json.loads(row["readiness_json"] or "{}")
            except json.JSONDecodeError:
                readiness = {}
            readiness.update({
                "estimated_by": actor,
                "estimate_rationale": rationale,
                "estimated_at": self._now(),
            })
            conn.execute(
                "UPDATE pbi SET points=?, readiness_json=?, updated_at=? WHERE pbi_id=?",
                (value, json.dumps(readiness, ensure_ascii=False), self._now(), pbi_id),
            )
            sprint_id = str(row["sprint_id"] or "") or None
            if sprint_id and delta:
                conn.execute(
                    "UPDATE sprint SET committed_pts=committed_pts+? WHERE sprint_id=?",
                    (delta, sprint_id),
                )
            self.log(conn, "pbi_estimated", "pbi", pbi_id, actor,
                     sprint_id=sprint_id, points=value, previous_points=previous,
                     delta=delta, rationale=rationale)
        return {"pbi_id": pbi_id, "points": value, "previous_points": previous,
                "delta": delta, "sprint_id": sprint_id}

    def tasks_needing_estimate(self) -> list[Task]:
        """Queued cards whose PBI has no team estimate yet."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT t.* FROM task t JOIN pbi p ON p.pbi_id=t.pbi_id"
                " WHERE p.points IS NULL AND t.state='queued' ORDER BY t.created_at"
            ).fetchall()
        return [Task.from_row(r) for r in rows]

    def reorder_pbi(self, pbi_id: str, rank: float, *, actor: str = "po") -> None:
        with self.tx() as conn:
            cursor = conn.execute(
                "UPDATE pbi SET priority_rank=?, updated_at=? WHERE pbi_id=?",
                (rank, self._now(), pbi_id),
            )
            if cursor.rowcount == 0:
                raise TransitionRejected("unknown_pbi", pbi_id)
            self.log(conn, "pbi_reordered", "pbi", pbi_id, actor, rank=rank)

    def refine_pbi(self, pbi_id: str, *, actor: str = "po") -> dict[str, Any]:
        """PO refinement: ``new -> refining -> ready | needs_revision``.

        The intermediate state is short-lived but real: both transitions are
        written to the event log, so a later reader can tell a refined request
        from one that was never looked at.

        A request the owner left without acceptance criteria is *held* in
        ``needs_revision`` instead of ``ready``. It used to be promoted to ready
        anyway and only rejected later at admission, which meant the owner
        approved a Sprint whose cards were guaranteed to bounce -- the review
        panel stayed empty until then.
        """
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM pbi WHERE pbi_id=?", (pbi_id,)).fetchone()
            if row is None:
                raise TransitionRejected("unknown_pbi", pbi_id)
            if row["state"] not in ("new", "refining", "ready", "needs_revision"):
                raise TransitionRejected("pbi_not_refinable", str(row["state"]))
            try:
                acceptance = json.loads(row["acceptance_json"] or "[]")
            except json.JSONDecodeError:
                acceptance = []
            try:
                readiness = json.loads(row["readiness_json"] or "{}")
            except json.JSONDecodeError:
                readiness = {}
            # ``create_pbi`` marks a request with acceptance criteria as
            # ``ready`` immediately, so refinement is keyed off the PO's stamp
            # rather than off the state alone.
            if readiness.get("refined_by"):
                raise TransitionRejected("pbi_already_refined", pbi_id)
            ts = self._now()
            conn.execute(
                "UPDATE pbi SET state='refining', updated_at=? WHERE pbi_id=?",
                (ts, pbi_id),
            )
            self.log(conn, "pbi_refining", "pbi", pbi_id, actor, phase="refining")
            readiness.update({
                "refined_by": actor,
                "refined_at": ts,
                "acceptance_count": len(acceptance),
            })
            target = "ready" if acceptance else "needs_revision"
            conn.execute(
                "UPDATE pbi SET state=?, readiness_json=?, updated_at=? WHERE pbi_id=?",
                (target, json.dumps(readiness, ensure_ascii=False), ts, pbi_id),
            )
            self.log(
                conn, "pbi_refined", "pbi", pbi_id, actor,
                acceptance_count=len(acceptance),
                priority_rank=float(row["priority_rank"]),
                to_state=target,
            )
            if not acceptance:
                # Held before sizing and before the Sprint proposal: the owner
                # is asked now, not after approving a commitment.
                self.log(conn, "dor_fail", "pbi", pbi_id, actor,
                         reason="missing_acceptance", held_in="needs_revision")
        return {"pbi_id": pbi_id, "state": target, "acceptance_count": len(acceptance)}

    # ── sprint ──────────────────────────────────────────────────────────────
    def create_sprint(
        self,
        name: str,
        *,
        capacity_pts: float,
        goal: str = "",
        interrupt_ratio: float | None = None,
    ) -> str:
        sprint_id = f"SPR-{uuid.uuid4().hex[:8]}"
        current = self.policy()
        ratio = current["interrupt_ratio"] if interrupt_ratio is None else interrupt_ratio
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO sprint (sprint_id, name, goal, state, capacity_pts, committed_pts,"
                " interrupt_budget_pts, policy_version, created_at)"
                " VALUES (?, ?, ?, 'planned', ?, 0, ?, ?, ?)",
                (
                    sprint_id,
                    name,
                    goal,
                    capacity_pts,
                    capacity_pts * float(ratio),
                    current.version,
                    self._now(),
                ),
            )
            self.log(conn, "sprint_created", "sprint", sprint_id, "po", name=name,
                     capacity_pts=capacity_pts, interrupt_budget_pts=capacity_pts * float(ratio))
        return sprint_id

    def commit_sprint(self, sprint_id: str, pbi_ids: list[str], *, actor: str = "po") -> float:
        """Freeze the working set. Returns committed points."""
        with self.tx() as conn:
            sprint = conn.execute("SELECT * FROM sprint WHERE sprint_id=?", (sprint_id,)).fetchone()
            if sprint is None:
                raise TransitionRejected("unknown_sprint", sprint_id)
            if sprint["state"] != "planned":
                raise TransitionRejected("sprint_not_planned", str(sprint["state"]))
            total = 0.0
            for pbi_id in pbi_ids:
                row = conn.execute("SELECT * FROM pbi WHERE pbi_id=?", (pbi_id,)).fetchone()
                if row is None:
                    raise TransitionRejected("unknown_pbi", pbi_id)
                if row["state"] not in ("ready", "refining", "new"):
                    raise TransitionRejected("pbi_not_committable", f"{pbi_id}:{row['state']}")
                total += float(row["points"] or 0.0)
                conn.execute(
                    "UPDATE pbi SET state='committed', sprint_id=?, updated_at=? WHERE pbi_id=?",
                    (sprint_id, self._now(), pbi_id),
                )
            conn.execute(
                "UPDATE sprint SET state='active', committed_pts=?, started_at=? WHERE sprint_id=?",
                (total, self._now(), sprint_id),
            )
            self.log(conn, "sprint_committed", "sprint", sprint_id, actor,
                     pbi_ids=pbi_ids, committed_pts=total)
            return total

    def active_sprint_id(self) -> str | None:
        """The one sprint that is currently accepting work, if any."""
        with self._lock:
            row = self.conn.execute(
                "SELECT sprint_id FROM sprint WHERE state='active'"
                " ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return str(row["sprint_id"]) if row is not None else None

    def planned_sprint(self) -> dict[str, Any] | None:
        """The Sprint candidate waiting for the owner's approval, if any."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM sprint WHERE state='planned'"
                " ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def planned_sprint_id(self) -> str | None:
        sprint = self.planned_sprint()
        return str(sprint["sprint_id"]) if sprint is not None else None

    def mark_candidates(
        self,
        sprint_id: str,
        pbi_ids: list[str],
        *,
        actor: str = "po",
        sm: str = "sm",
    ) -> dict[str, Any]:
        """PO drafts the Sprint; SM checks capacity. Nothing is committed yet.

        Candidates are marked in ``readiness_json`` rather than ``pbi.sprint_id``:
        a proposed item is still in the Product Backlog, and moving the foreign
        key early would make ``revise_request`` treat it as already committed.
        """
        with self.tx() as conn:
            sprint = conn.execute(
                "SELECT * FROM sprint WHERE sprint_id=?", (sprint_id,)
            ).fetchone()
            if sprint is None:
                raise TransitionRejected("unknown_sprint", sprint_id)
            if sprint["state"] != "planned":
                raise TransitionRejected("sprint_not_planned", str(sprint["state"]))
            total = 0.0
            for pbi_id in pbi_ids:
                row = conn.execute(
                    "SELECT * FROM pbi WHERE pbi_id=?", (pbi_id,)
                ).fetchone()
                if row is None:
                    raise TransitionRejected("unknown_pbi", pbi_id)
                try:
                    readiness = json.loads(row["readiness_json"] or "{}")
                except json.JSONDecodeError:
                    readiness = {}
                readiness["planned_sprint_id"] = sprint_id
                conn.execute(
                    "UPDATE pbi SET readiness_json=?, updated_at=? WHERE pbi_id=?",
                    (json.dumps(readiness, ensure_ascii=False), self._now(), pbi_id),
                )
                total += float(row["points"] or 0.0)
            capacity = float(sprint["capacity_pts"])
            self.log(conn, "sprint_proposed", "sprint", sprint_id, actor,
                     pbi_ids=pbi_ids, proposed_pts=total)
            self.log(conn, "capacity_checked", "sprint", sprint_id, sm,
                     capacity_pts=capacity, proposed_pts=total, fits=total <= capacity)
        return {"sprint_id": sprint_id, "pbi_ids": pbi_ids,
                "proposed_pts": round(total, 2), "capacity_pts": capacity,
                "fits": total <= capacity}

    def candidates_for(self, sprint_id: str) -> list[str]:
        """PBI ids proposed for a planned Sprint, in priority order."""
        return [
            str(pbi["pbi_id"])
            for pbi in self.all_pbis()
            if pbi["state"] in ("new", "refining", "ready")
            and isinstance(pbi.get("readiness"), dict)
            and pbi["readiness"].get("planned_sprint_id") == sprint_id
        ]

    def add_pbi_to_sprint(
        self, pbi_id: str, sprint_id: str | None = None, *, actor: str = "po"
    ) -> str:
        """Inject an item into a *running* sprint.

        ``commit_sprint`` only accepts planned sprints, which is the right default:
        a Sprint Goal you can silently append to is not a commitment. But scope
        does get injected in real life, so the move is allowed and made expensive
        to ignore — it writes ``scope_injected`` to the event log and grows
        ``committed_pts`` so the burndown shows the bump instead of hiding it.
        """
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM pbi WHERE pbi_id=?", (pbi_id,)).fetchone()
            if row is None:
                raise TransitionRejected("unknown_pbi", pbi_id)
            if row["sprint_id"]:
                raise TransitionRejected("pbi_already_committed", str(row["sprint_id"]))
            if sprint_id is None:
                active = conn.execute(
                    "SELECT sprint_id FROM sprint WHERE state='active'"
                    " ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                if active is None:
                    raise TransitionRejected("no_active_sprint", "")
                sprint_id = str(active["sprint_id"])
            sprint = conn.execute("SELECT * FROM sprint WHERE sprint_id=?", (sprint_id,)).fetchone()
            if sprint is None:
                raise TransitionRejected("unknown_sprint", sprint_id)
            if sprint["state"] != "active":
                raise TransitionRejected("sprint_not_active", str(sprint["state"]))
            ts = self._now()
            points = float(row["points"] or 0.0)
            conn.execute(
                "UPDATE pbi SET state='committed', sprint_id=?, updated_at=? WHERE pbi_id=?",
                (sprint_id, ts, pbi_id),
            )
            conn.execute(
                "UPDATE sprint SET committed_pts=committed_pts+? WHERE sprint_id=?",
                (points, sprint_id),
            )
            self.log(conn, "scope_injected", "pbi", pbi_id, actor,
                     sprint_id=sprint_id, points=points)
        return sprint_id

    def close_sprint(self, sprint_id: str) -> dict[str, int]:
        """Destructive boundary: no lease survives it, no task stays running.

        This is the whole point of a timebox. A ``running`` task that spans the
        boundary forever is what flattens a burndown chart into a straight line.
        """
        counts = {"requeued": 0, "blocked": 0}
        with self.tx() as conn:
            sprint = conn.execute("SELECT state FROM sprint WHERE sprint_id=?", (sprint_id,)).fetchone()
            if sprint is None:
                raise TransitionRejected("unknown_sprint", sprint_id)
            rows = conn.execute(
                "SELECT task_id, state FROM task WHERE sprint_id=? AND state IN ('queued','leased','running')",
                (sprint_id,),
            ).fetchall()
            ts = self._now()
            for row in rows:
                conn.execute(
                    "UPDATE task SET state='queued', agent_id=NULL, lease_id=NULL,"
                    " lease_expires_at=NULL, lease_hard_deadline=NULL, retry_after=NULL,"
                    " updated_at=? WHERE task_id=?",
                    (ts, row["task_id"]),
                )
                self.log(conn, "task_released", "task", row["task_id"], "sm",
                         reason="sprint_closed", carryover=True, from_state=row["state"])
                counts["requeued"] += 1
            conn.execute(
                "UPDATE sprint SET state='closed', closed_at=? WHERE sprint_id=?",
                (ts, sprint_id),
            )
            self.log(conn, "sprint_closed", "sprint", sprint_id, "sm", **counts)
        return counts

    def sprint_usage(self, sprint_id: str) -> dict[str, float]:
        with self._lock:
            row = self.conn.execute(
                "SELECT interrupt_budget_pts, interrupts_used_pts FROM sprint WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()
        if row is None:
            return {"budget": 0.0, "used": 0.0}
        return {"budget": float(row["interrupt_budget_pts"]), "used": float(row["interrupts_used_pts"])}

    # ── tasks ───────────────────────────────────────────────────────────────
    def create_task(
        self,
        pbi_id: str,
        title: str,
        *,
        spec: str = "",
        role: str = "dev",
        actor: str = "po",
    ) -> str:
        with self.tx() as conn:
            pbi = conn.execute("SELECT * FROM pbi WHERE pbi_id=?", (pbi_id,)).fetchone()
            if pbi is None:
                raise TransitionRejected("unknown_pbi", pbi_id)
            if not pbi["sprint_id"]:
                raise TransitionRejected("pbi_not_committed", pbi_id)
            task_id = f"T-{uuid.uuid4().hex[:8]}"
            ts = self._now()
            conn.execute(
                "INSERT INTO task (task_id, pbi_id, sprint_id, title, spec, state, role,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (task_id, pbi_id, pbi["sprint_id"], title, spec, role, ts, ts),
            )
            self.log(conn, "task_created", "task", task_id, actor, pbi_id=pbi_id, role=role)
        return task_id

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        return Task.from_row(row) if row is not None else None

    def _require_task(self, conn: sqlite3.Connection, task_id: str) -> Task:
        row = conn.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise TransitionRejected("unknown_task", task_id)
        return Task.from_row(row)

    def set_thread(self, task_id: str, thread_id: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE task SET thread_id=?, updated_at=? WHERE task_id=?",
                (thread_id, self._now(), task_id),
            )
            self.log(conn, "task_thread_bound", "task", task_id, "dispatcher", thread_id=thread_id)

    # ── admission ───────────────────────────────────────────────────────────
    def admit(self, task_id: str) -> Admit:
        """Decide whether this task may enter execution right now.

        The reason order is the product: ``review_backpressure`` before
        ``pbi_wip`` means the dashboard answers "are we short on agents or on
        eyeballs?" differently for the two cases.
        """
        p = self.policy()
        with self._lock:
            task = self._require_task(self.conn, task_id)
            if task.state != "queued":
                return Admit(False, "not_queued", {"state": task.state})

            sprint = self.conn.execute(
                "SELECT * FROM sprint WHERE sprint_id=?", (task.sprint_id,)
            ).fetchone()
            if sprint is None or sprint["state"] != "active":
                return Admit(False, "no_sprint", {"sprint_state": sprint["state"] if sprint else None})

            pbi = self.conn.execute("SELECT * FROM pbi WHERE pbi_id=?", (task.pbi_id,)).fetchone()
            if pbi is None or pbi["sprint_id"] != task.sprint_id:
                return Admit(False, "not_in_sprint", {"pbi_id": task.pbi_id})

            acceptance = json.loads(pbi["acceptance_json"] or "[]")
            if not acceptance and pbi["class_of_service"] != "intangible":
                return Admit(False, "dor_fail", {"pbi_id": task.pbi_id})

            review_depth = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS c FROM task WHERE state IN ({})".format(
                        ",".join("?" * len(REVIEW_STATES))
                    ),
                    REVIEW_STATES,
                ).fetchone()["c"]
            )
            if review_depth >= int(p["review_queue_cap"]):
                return Admit(False, "review_backpressure", {"depth": review_depth,
                                                            "cap": p["review_queue_cap"]})

            inflight = int(
                self.conn.execute(
                    "SELECT COUNT(DISTINCT pbi_id) AS c FROM task WHERE state IN ({})".format(
                        ",".join("?" * len(OCCUPYING_STATES))
                    ),
                    OCCUPYING_STATES,
                ).fetchone()["c"]
            )
            if inflight >= int(p["in_flight_pbi_cap"]):
                return Admit(False, "pbi_wip", {"inflight": inflight, "cap": p["in_flight_pbi_cap"]})

            if pbi["class_of_service"] == "expedite":
                used = float(sprint["interrupts_used_pts"])
                budget = float(sprint["interrupt_budget_pts"])
                cost = float(pbi["points"] or 0.0)
                if used + cost > budget:
                    return Admit(
                        False,
                        "interrupt_budget_exhausted",
                        {"used": used, "budget": budget, "cost": cost},
                    )

            return Admit(True, "ok", {"review_depth": review_depth, "inflight": inflight})

    def next_candidates(self, limit: int = 8) -> list[Task]:
        ts = self._now()
        with self._lock:
            rows = self.conn.execute(
                "SELECT t.* FROM task t JOIN pbi p ON p.pbi_id = t.pbi_id"
                " WHERE t.state='queued'"
                "   AND (t.retry_after IS NULL OR t.retry_after <= ?)"
                " ORDER BY CASE WHEN p.class_of_service='expedite' THEN 0 ELSE 1 END,"
                "          p.priority_rank, t.created_at LIMIT ?",
                (ts, limit),
            ).fetchall()
        return [Task.from_row(r) for r in rows]

    def tasks_awaiting_dod(self, *, cooldown_s: float = 90.0) -> list[Task]:
        """``submitted`` cards whose verifier has not been woken recently.

        The cooldown is what stops a two-second tick loop from firing an SM wake
        every two seconds while the verifier is still reading the artefact: one
        ask per card per cooldown window, not one per tick.
        """
        now = self._now()
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task WHERE state='submitted'"
                "   AND (verify_wake_at IS NULL OR verify_wake_at <= ?)"
                " ORDER BY created_at",
                (now - cooldown_s,),
            ).fetchall()
        return [Task.from_row(r) for r in rows]

    def mark_verify_woken(self, task_id: str, agent_id: str) -> None:
        """Record that *agent_id* was asked to verify, before the wake is sent.

        Written first on purpose: a crash between "wake sent" and "note written"
        would otherwise re-wake the verifier on every restart.
        """
        with self.tx() as conn:
            conn.execute(
                "UPDATE task SET verify_wake_at=?, verify_agent_id=?, updated_at=?"
                " WHERE task_id=?",
                (self._now(), agent_id, self._now(), task_id),
            )
            self.log(conn, "verify_woken", "task", task_id, "dispatcher", agent=agent_id)

    def tasks_in_state(self, *states: str) -> list[Task]:
        if not states:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task WHERE state IN ({}) ORDER BY created_at".format(
                    ",".join("?" * len(states))
                ),
                states,
            ).fetchall()
        return [Task.from_row(r) for r in rows]

    def occupying_agent_ids(self) -> set[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT agent_id FROM task WHERE state IN ({}) AND agent_id IS NOT NULL".format(
                    ",".join("?" * len(OCCUPYING_STATES))
                ),
                OCCUPYING_STATES,
            ).fetchall()
        return {str(r["agent_id"]) for r in rows}

    def note_admission(self, task_id: str, reason: str, detail: dict[str, Any]) -> None:
        """Record a denial. This histogram is the single most useful metric here."""
        with self.tx() as conn:
            self.log(conn, "admit_denied", "task", task_id, "dispatcher",
                     reason=reason, detail=detail)

    def deny_admission(self, task_id: str, reason: str, detail: dict[str, Any]) -> bool:
        """Record a denial and act on the ones that need a human.

        ``review_backpressure`` and ``no_slot`` are self-clearing: the next tick
        will admit the card when a slot frees up. ``dor_fail`` is not — nothing
        the system can do makes acceptance criteria appear, so leaving it in
        ``queued`` produces a card that looks healthy while being re-checked
        every tick forever, and an owner who never sees it.

        Both executors must call this, not ``note_admission``: the L2 dispatcher
        used to record the denial and move on, so a card with an empty
        acceptance list never reached the owner's Review panel at all.

        Returns ``True`` when the request was handed back to the owner.
        """
        self.note_admission(task_id, reason, detail)
        if reason != "dor_fail":
            return False
        try:
            return self.request_revision(
                task_id,
                "dor_fail:missing_acceptance",
                actor="dor-gate",
                detail="验收条件为空，请补齐后重新提交",
            )
        except TransitionRejected:
            # The card moved on between the admit and the hand-back; the next
            # tick will see the new state and decide again.
            return False

    # ── leases ──────────────────────────────────────────────────────────────
    def claim(self, task_id: str, agent_id: str) -> Lease | None:
        """CAS claim. Returns ``None`` when the task moved on or the agent is busy."""
        p = self.policy()
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state != "queued":
                return None
            role = conn.execute(
                "SELECT role, enabled FROM agent_role WHERE agent_id=?", (agent_id,)
            ).fetchone()
            if role is None or not role["enabled"] or role["role"] != task.role:
                raise TransitionRejected("unknown_agent", agent_id)

            busy = conn.execute(
                "SELECT COUNT(*) AS c FROM task WHERE agent_id=? AND state IN ({})".format(
                    ",".join("?" * len(OCCUPYING_STATES))
                ),
                (agent_id, *OCCUPYING_STATES),
            ).fetchone()["c"]
            if int(busy) > 0:
                return None

            lease_id = uuid.uuid4().hex
            ts = self._now()
            expires = ts + float(p["lease_ttl_s"])
            hard = ts + float(p["lease_hard_cap_s"])
            branch = task.branch or f"scrum/{task.task_id}"
            cursor = conn.execute(
                "UPDATE task SET state='leased', agent_id=?, lease_id=?, lease_expires_at=?,"
                " lease_hard_deadline=?, branch=?, retry_after=NULL, updated_at=?"
                " WHERE task_id=? AND state='queued' AND lease_id IS NULL",
                (agent_id, lease_id, expires, hard, branch, ts, task_id),
            )
            if cursor.rowcount != 1:  # another writer won the race
                return None
            self.log(conn, "task_claimed", "task", task_id, agent_id,
                     lease_id=lease_id, branch=branch, attempt=task.attempt)
            return Lease(
                task_id=task_id,
                lease_id=lease_id,
                agent_id=agent_id,
                expires_at=expires,
                hard_deadline=hard,
                spec=task.spec,
                branch=branch,
            )

    def mark_woken(self, task_id: str, *, thread_id: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE task SET last_wake_at=?, thread_id=COALESCE(?, thread_id), updated_at=?"
                " WHERE task_id=?",
                (self._now(), thread_id, self._now(), task_id),
            )
            self.log(conn, "task_woken", "task", task_id, "dispatcher", thread_id=thread_id)

    def ack(self, task_id: str, lease_id: str) -> bool:
        """Agent acknowledged the wake. This is when ``attempt`` is charged."""
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.lease_id != lease_id:
                raise NotYourLease(task_id)
            if task.state != "leased":
                return task.state == "running"
            ts = self._now()
            cursor = conn.execute(
                "UPDATE task SET state='running', acked_at=?, attempt=attempt+1, updated_at=?"
                " WHERE task_id=? AND lease_id=? AND state='leased'",
                (ts, ts, task_id, lease_id),
            )
            if cursor.rowcount != 1:
                return False
            self.log(conn, "task_acked", "task", task_id, task.agent_id or "agent",
                     attempt=task.attempt + 1)
            self._charge_interrupt(conn, task.pbi_id)
            return True

    def _charge_interrupt(self, conn: sqlite3.Connection, pbi_id: str) -> None:
        """Price the interruption instead of forbidding it."""
        row = conn.execute(
            "SELECT p.class_of_service, p.points, p.sprint_id FROM pbi p WHERE p.pbi_id=?", (pbi_id,)
        ).fetchone()
        if row is None or row["class_of_service"] != "expedite" or not row["sprint_id"]:
            return
        conn.execute(
            "UPDATE sprint SET interrupts_used_pts = interrupts_used_pts + ? WHERE sprint_id=?",
            (float(row["points"] or 0.0), row["sprint_id"]),
        )
        self.log(conn, "interrupt_charged", "sprint", row["sprint_id"], "dispatcher",
                 pbi_id=pbi_id, points=float(row["points"] or 0.0))

    def heartbeat(self, task_id: str, lease_id: str) -> bool:
        p = self.policy()
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.lease_id != lease_id or task.state not in OCCUPYING_STATES:
                raise NotYourLease(task_id)
            ts = self._now()
            hard = task.lease_hard_deadline or ts
            new_expiry = min(ts + float(p["lease_ttl_s"]), hard)
            conn.execute(
                "UPDATE task SET lease_expires_at=?, updated_at=? WHERE task_id=?",
                (new_expiry, ts, task_id),
            )
            self.log(conn, "task_heartbeat", "task", task_id, task.agent_id or "agent",
                     extends_to=new_expiry)
            return new_expiry > ts

    def release(self, task_id: str, reason: str, *, actor: str = "dispatcher") -> str:
        """Give a task back. Returns the resulting state."""
        p = self.policy()
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state in TERMINAL_TASK_STATES:
                return task.state
            attempts = task.attempt
            wake_failures = task.wake_failures + (
                1 if reason in ("wake_rejected", "wake_dropped") else 0
            )
            exhausted = attempts >= int(p["max_attempts"]) or wake_failures >= int(
                p["max_wake_failures"]
            )
            state = "blocked" if exhausted else "queued"
            ts = self._now()
            # Exponential backoff. Without this a rejected wake is retried on the
            # very next tick, which turns one busy agent into a busy-loop of
            # rejected injections (each one costs a WS connect and a frame).
            retry_after = None
            if state == "queued" and reason in (
                "wake_rejected",
                "wake_dropped",
                "expired",
                "stalled",
            ):
                exponent = max(1, wake_failures + attempts)
                backoff = min(
                    float(p["backoff_s"]) * (2 ** (exponent - 1)),
                    float(p["backoff_max_s"]),
                )
                retry_after = ts + backoff
            conn.execute(
                "UPDATE task SET state=?, agent_id=NULL, lease_id=NULL, lease_expires_at=NULL,"
                " lease_hard_deadline=NULL, wake_failures=?, blocked_reason=?, retry_after=?,"
                " updated_at=? WHERE task_id=?",
                (state, wake_failures, reason if exhausted else None, retry_after, ts, task_id),
            )
            self.log(conn, "task_released", "task", task_id, actor, reason=reason,
                     to_state=state, attempt=attempts, wake_failures=wake_failures,
                     retry_after=retry_after)
            return state

    def adopt(self, task_id: str, *, actor: str = "reconcile") -> bool:
        """Take ownership of a lease whose thread is genuinely still working.

        Adoption extends the lease; it does not re-dispatch. The whole point of
        reconciling before serving is that "probably still running" must default
        to "leave it alone", not "send it again".
        """
        p = self.policy()
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state not in OCCUPYING_STATES:
                return False
            ts = self._now()
            hard = task.lease_hard_deadline or (ts + float(p["lease_hard_cap_s"]))
            new_expiry = min(ts + float(p["lease_ttl_s"]), hard)
            conn.execute(
                "UPDATE task SET lease_expires_at=?, updated_at=? WHERE task_id=?",
                (new_expiry, ts, task_id),
            )
            self.log(conn, "task_adopted", "task", task_id, actor, extends_to=new_expiry)
            return True

    def has_dod_checks(self, task_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM dod_check WHERE task_id=?", (task_id,)
            ).fetchone()
        return int(row["c"] or 0) > 0

    def release_expired(self) -> list[str]:
        ts = self._now()
        with self._lock:
            rows = self.conn.execute(
                "SELECT task_id FROM task WHERE state IN ({}) AND lease_expires_at IS NOT NULL"
                " AND lease_expires_at < ?".format(",".join("?" * len(OCCUPYING_STATES))),
                (*OCCUPYING_STATES, ts),
            ).fetchall()
        released: list[str] = []
        for row in rows:
            self.release(str(row["task_id"]), "expired")
            released.append(str(row["task_id"]))
        return released

    def dropped_wakes(self, ack_timeout_s: float) -> list[str]:
        """Leased long enough without an ack: the injection never landed."""
        ts = self._now()
        with self._lock:
            rows = self.conn.execute(
                "SELECT task_id FROM task WHERE state='leased' AND last_wake_at IS NOT NULL"
                " AND acked_at IS NULL AND last_wake_at < ?",
                (ts - ack_timeout_s,),
            ).fetchall()
        return [str(r["task_id"]) for r in rows]

    # ── execution outcomes ──────────────────────────────────────────────────
    def submit(
        self,
        task_id: str,
        lease_id: str,
        *,
        evidence: dict[str, Any],
        idem_key: str | None = None,
    ) -> dict[str, Any]:
        """Record an increment. Idempotent on ``idem_key``."""
        p = self.policy()
        required = list(p["dod"]["min_evidence_keys"])
        missing = [k for k in required if not evidence.get(k)]
        if missing:
            raise TransitionRejected("evidence_incomplete", ",".join(missing))
        branch_hint = evidence.get("git_sha") or evidence.get("branch")
        if p["dod"]["require_git_sha"] and not branch_hint:
            # The branch the lease minted counts as the reference: agents in this
            # experiment are forbidden from committing, so a sha cannot exist yet.
            existing = self.get_task(task_id)
            if existing is None:
                raise TransitionRejected("unknown_task", task_id)
            if not existing.branch:
                raise TransitionRejected("evidence_incomplete", "git_sha|branch")

        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if idem_key:
                prior = conn.execute(
                    "SELECT task_id, state, evidence_json FROM task WHERE idem_key=?", (idem_key,)
                ).fetchone()
                if prior is not None:
                    return {
                        "task_id": prior["task_id"],
                        "state": prior["state"],
                        "duplicate": True,
                    }
            if task.lease_id != lease_id:
                raise NotYourLease(task_id)
            if task.state not in ("running", "leased"):
                raise TransitionRejected("not_running", task.state)
            ts = self._now()
            conn.execute(
                "UPDATE task SET state='submitted', evidence_json=?, idem_key=?, updated_at=?"
                " WHERE task_id=?",
                (json.dumps(evidence, ensure_ascii=False, sort_keys=True), idem_key, ts, task_id),
            )
            self.log(conn, "task_submitted", "task", task_id, task.agent_id or "agent",
                     evidence_keys=sorted(evidence), idem_key=idem_key)
            return {"task_id": task_id, "state": "submitted", "duplicate": False}

    def record_dod(self, task_id: str, checks: list[dict[str, Any]]) -> bool:
        """Persist one *round* of DoD results; FAIL opens a re-verification window.

        The old behaviour released the card the instant any check failed. That
        was a one-way door: the state left ``submitted``, so the verifier could
        not correct a misread acceptance criterion, and three automatic
        reworks later the card was ``blocked`` with no human ever consulted.
        A DoD verdict is evidence, not a sentence — so a FAIL now parks the card
        in ``verifying`` where the SM may re-run the checks (a new round) and the
        owner may re-verify, rework or escalate. Rounds are append-only and only
        the latest one may gate ``accept``.
        """
        if not checks:
            raise TransitionRejected("dod_empty", task_id)
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state not in ("submitted", "verifying"):
                raise TransitionRejected("not_submitted", task.state)
            ts = self._now()
            row = conn.execute(
                "SELECT COALESCE(MAX(check_round), 0) AS r FROM dod_check WHERE task_id=?",
                (task_id,),
            ).fetchone()
            round_no = int(row["r"] or 0) + 1
            for check in checks:
                conn.execute(
                    "INSERT INTO dod_check (task_id, check_id, ok, exit_code, duration_ms,"
                    " output_tail, ran_at, check_round) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        str(check.get("check_id", "unknown")),
                        1 if check.get("ok") else 0,
                        check.get("exit_code"),
                        check.get("duration_ms"),
                        str(check.get("output_tail", ""))[:4000],
                        ts,
                        round_no,
                    ),
                )
                self.log(conn, "dod_check", "task", task_id, "dod-runner",
                         check_id=check.get("check_id"), ok=bool(check.get("ok")),
                         check_round=round_no)
            all_ok = all(c.get("ok") for c in checks)
            failed = [str(c.get("check_id")) for c in checks if not c.get("ok")]
            self.log(conn, "dod_round", "task", task_id, "dod-runner",
                     check_round=round_no, all_ok=all_ok, failed=failed,
                     to_state="in_review" if all_ok else "verifying")
            if all_ok:
                conn.execute(
                    "UPDATE task SET state='in_review', blocked_reason=NULL, updated_at=?"
                    " WHERE task_id=?", (ts, task_id)
                )
                self.log(conn, "task_review_queued", "task", task_id, "dod-runner",
                         check_round=round_no)
                return True
            conn.execute(
                "UPDATE task SET state='verifying', blocked_reason=?, updated_at=?"
                " WHERE task_id=?", (",".join(failed)[:200], ts, task_id)
            )
            self.log(conn, "task_verification_opened", "task", task_id, "dod-runner",
                     check_round=round_no, failed=failed, to_state="verifying")
            return False

    def dod_rounds(self, task_id: str) -> list[dict[str, Any]]:
        """One row per verification round, oldest first, for the audit trail."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT check_round, COUNT(*) AS total, SUM(ok) AS passed,"
                " MAX(ran_at) AS ran_at, GROUP_CONCAT(CASE WHEN ok=0 THEN check_id END) AS failed"
                " FROM dod_check WHERE task_id=? GROUP BY check_round ORDER BY check_round",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_verification(
        self, task_id: str, decision: str, *, actor: str = "owner", note: str = ""
    ) -> str:
        """Owner's verdict on a failed DoD round. Returns the resulting state.

        ``recheck``  -> back to ``submitted`` with the verifier wake cleared, so
                        the dispatcher asks the SM to look again;
        ``rework``   -> back to ``queued`` with the attempt counter reset, since
                        a human decision starts a new round of work, not a
                        continuation of an unattended retry loop;
        ``escalate`` -> ``blocked``, explicitly, with the owner's note as reason.
        """
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state != "verifying":
                raise TransitionRejected("not_verifying", task.state)
            ts = self._now()
            if decision == "recheck":
                state = "submitted"
                conn.execute(
                    "UPDATE task SET state=?, verify_wake_at=NULL, verify_agent_id=NULL,"
                    " blocked_reason=NULL, updated_at=? WHERE task_id=?",
                    (state, ts, task_id),
                )
                kind = "task_verification_recheck"
            elif decision == "rework":
                state = "queued"
                conn.execute(
                    "UPDATE task SET state=?, agent_id=NULL, lease_id=NULL,"
                    " lease_expires_at=NULL, lease_hard_deadline=NULL, attempt=0,"
                    " wake_failures=0, retry_after=NULL, idem_key=NULL,"
                    " verify_wake_at=NULL, verify_agent_id=NULL, blocked_reason=NULL,"
                    " updated_at=? WHERE task_id=?",
                    (state, ts, task_id),
                )
                kind = "task_verification_rework"
            elif decision == "escalate":
                state = "blocked"
                conn.execute(
                    "UPDATE task SET state=?, agent_id=NULL, lease_id=NULL,"
                    " lease_expires_at=NULL, lease_hard_deadline=NULL,"
                    " blocked_reason=?, updated_at=? WHERE task_id=?",
                    (state, note or "verification_escalated", ts, task_id),
                )
                kind = "task_verification_escalated"
            else:
                raise TransitionRejected("unknown_decision", decision)
            self.log(conn, kind, "task", task_id, actor, decision=decision,
                     to_state=state, note=note)
            return state

    def request_reverification(
        self, task_id: str, *, actor: str = "owner", note: str = ""
    ) -> bool:
        """Re-open the DoD step without asking the dev to redo the work.

        ``blocked`` covers two very different failures: the artefact is bad, or
        the tooling to inspect it was. ``reopen`` (back to ``queued``) is right
        for the first. Sending a correctly-executed card back through the agent
        for the second burns a turn and changes the artefact being judged -- so
        this path keeps the evidence and asks the SM to look again.

        This was a real dead end: T-4c39edf4 was blocked with
        ``tooling_blocked`` when the scope gate denied the SM ``execute``, and
        there was no way back to ``submitted`` except re-running the work.
        """
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state not in ("blocked", "parked", "submitted"):
                raise TransitionRejected("not_reverifiable", task.state)
            ts = self._now()
            conn.execute(
                "UPDATE task SET state='submitted', verify_wake_at=NULL,"
                " verify_agent_id=NULL, blocked_reason=NULL, updated_at=?"
                " WHERE task_id=?",
                (ts, task_id),
            )
            self.log(conn, "task_reverification_requested", "task", task_id, actor,
                     from_state=task.state, to_state="submitted", note=note)
            return True

    def reopen(self, task_id: str, *, actor: str = "owner", note: str = "") -> bool:
        """Owner puts a terminal card back in the queue as a *fresh* round.

        Without resetting ``attempt`` the card would be re-claimed once and
        immediately blocked again: the retry cap exists to stop an unattended
        loop, and a human decision is the opposite of unattended. The old
        rounds stay in ``dod_check`` and the event log.
        """
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state not in ("blocked", "parked"):
                raise TransitionRejected("not_reopenable", task.state)
            ts = self._now()
            conn.execute(
                "UPDATE task SET state='queued', agent_id=NULL, lease_id=NULL,"
                " lease_expires_at=NULL, lease_hard_deadline=NULL, attempt=0,"
                " wake_failures=0, retry_after=NULL, idem_key=NULL,"
                " verify_wake_at=NULL, verify_agent_id=NULL, blocked_reason=NULL,"
                " updated_at=? WHERE task_id=?",
                (ts, task_id),
            )
            self.log(conn, "task_reopened", "task", task_id, actor,
                     from_state=task.state, to_state="queued", note=note)
            return True

    def accept(
        self,
        task_id: str,
        *,
        reviewer: str,
        dwell_seconds: float | None = None,
        note: str = "",
    ) -> None:
        """``done`` is reachable only here. An agent cannot call this."""
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state != "in_review":
                raise TransitionRejected("not_in_review", task.state)
            # Only the latest verification round can open the door. Earlier
            # failed rounds stay in the table as evidence; counting them here
            # would make a corrected verdict permanently un-acceptable.
            checks = conn.execute(
                "SELECT COUNT(*) AS total, SUM(ok) AS passed FROM dod_check"
                " WHERE task_id=? AND check_round = ("
                "   SELECT MAX(check_round) FROM dod_check WHERE task_id=?)",
                (task_id, task_id),
            ).fetchone()
            if not checks["total"] or int(checks["passed"] or 0) != int(checks["total"]):
                raise TransitionRejected("dod_incomplete", task_id)
            ts = self._now()
            conn.execute(
                "UPDATE task SET state='done', updated_at=? WHERE task_id=?", (ts, task_id)
            )
            conn.execute(
                "INSERT OR REPLACE INTO review_decision (task_id, decision, reviewer,"
                " dwell_seconds, note, decided_at) VALUES (?, 'accept', ?, ?, ?, ?)",
                (task_id, reviewer, dwell_seconds, note, ts),
            )
            self.log(conn, "task_accepted", "task", task_id, reviewer,
                     dwell_seconds=dwell_seconds, note=note)
            self._close_pbi_if_done(conn, task.pbi_id)

    def reject(
        self,
        task_id: str,
        *,
        reviewer: str,
        dwell_seconds: float | None = None,
        note: str = "",
    ) -> None:
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state != "in_review":
                raise TransitionRejected("not_in_review", task.state)
            ts = self._now()
            conn.execute(
                "INSERT OR REPLACE INTO review_decision (task_id, decision, reviewer,"
                " dwell_seconds, note, decided_at) VALUES (?, 'reject', ?, ?, ?, ?)",
                (task_id, reviewer, dwell_seconds, note, ts),
            )
            # A human rejection starts a new round of work. Charging it to the
            # automatic retry budget meant two owner decisions could strand a
            # card in ``blocked`` while the owner was still actively reviewing.
            conn.execute(
                "UPDATE task SET state='queued', agent_id=NULL, lease_id=NULL,"
                " lease_expires_at=NULL, lease_hard_deadline=NULL, attempt=0,"
                " wake_failures=0, retry_after=NULL, idem_key=NULL,"
                " verify_wake_at=NULL, verify_agent_id=NULL, blocked_reason=NULL,"
                " updated_at=? WHERE task_id=?",
                (ts, task_id),
            )
            self.log(conn, "task_rejected", "task", task_id, reviewer,
                     dwell_seconds=dwell_seconds, note=note, to_state="queued")

    def block(self, task_id: str, reason: str, *, actor: str = "agent", detail: str = "") -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE task SET state='blocked', agent_id=NULL, lease_id=NULL,"
                " lease_expires_at=NULL, lease_hard_deadline=NULL, blocked_reason=?, updated_at=?"
                " WHERE task_id=?",
                (reason, self._now(), task_id),
            )
            self.log(conn, "task_blocked", "task", task_id, actor, reason=reason, detail=detail)

    # ── request revision (DoR rejection) ───────────────────────────────────
    def request_revision(
        self, task_id: str, reason: str, *, actor: str = "dor-gate", detail: str = ""
    ) -> bool:
        """Send an unready request back to the owner's Review panel.

        A DoR rejection that leaves the card in ``queued`` is a silent failure:
        the worker will re-check it forever and the owner sees a card that looks
        healthy. ``needs_revision`` makes the missing information *the owner's*
        next action, with the reason attached.
        """
        with self.tx() as conn:
            task = self._require_task(conn, task_id)
            if task.state == "needs_revision":
                return False
            if task.state != "queued":
                raise TransitionRejected("not_queued", task.state)
            conn.execute(
                "UPDATE task SET state='needs_revision', blocked_reason=?, updated_at=?"
                " WHERE task_id=?",
                (reason, self._now(), task_id),
            )
            self.log(conn, "task_revision_requested", "task", task_id, actor,
                     reason=reason, detail=detail, pbi_id=task.pbi_id,
                     to_state="needs_revision")
        return True

    def revise_request(
        self,
        pbi_id: str,
        *,
        title: str,
        body: str,
        acceptance: list[str],
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Save the owner's edit and resubmit, or keep it in revision.

        Editing a request changes its size, so the previous team estimate is
        invalidated (logged as a negative delta) and the team re-estimates it on
        the next worker tick. A request still without acceptance criteria is
        *not* resubmitted — that would just bounce it straight back.
        """
        with self.tx() as conn:
            pbi = conn.execute("SELECT * FROM pbi WHERE pbi_id=?", (pbi_id,)).fetchone()
            if pbi is None:
                raise TransitionRejected("unknown_pbi", pbi_id)
            if pbi["state"] == "done":
                raise TransitionRejected("pbi_already_done", pbi_id)
            ts = self._now()
            criteria = [str(a).strip() for a in acceptance if str(a).strip()]
            sprint_id = str(pbi["sprint_id"] or "") or None
            # A request held by the PO for missing criteria stays held until the
            # owner supplies them -- bouncing it back to ``new`` would just make
            # the next planning tick hold it again under a different state name.
            if criteria:
                new_state = "committed" if sprint_id else "ready"
            elif pbi["state"] == "needs_revision":
                new_state = "needs_revision"
            else:
                new_state = "new"
            old_points = float(pbi["points"] or 0.0)
            try:
                readiness = json.loads(pbi["readiness_json"] or "{}")
            except json.JSONDecodeError:
                readiness = {}
            readiness["acceptance_count"] = len(criteria)
            if criteria:
                # The PO's hold is released; sizing may now run again.
                readiness["dor_released_at"] = ts
            estimate_invalidated = False
            if old_points > 0:
                readiness["estimate_invalidated_at"] = ts
                for key in ("estimated_by", "estimate_rationale", "estimated_at"):
                    readiness.pop(key, None)
                estimate_invalidated = True
                if sprint_id:
                    conn.execute(
                        "UPDATE sprint SET committed_pts=committed_pts-? WHERE sprint_id=?",
                        (old_points, sprint_id),
                    )
                    self.log(conn, "estimate_invalidated", "pbi", pbi_id, actor,
                             sprint_id=sprint_id, delta=-old_points,
                             previous_points=old_points,
                             reason="request_revised")
            conn.execute(
                "UPDATE pbi SET title=?, body=?, state=?, points=NULL,"
                " acceptance_json=?, readiness_json=?, updated_at=? WHERE pbi_id=?",
                (title.strip(), body.strip(), new_state,
                 json.dumps(criteria, ensure_ascii=False),
                 json.dumps(readiness, ensure_ascii=False), ts, pbi_id),
            )
            self.log(conn, "pbi_revised", "pbi", pbi_id, actor,
                     sprint_id=sprint_id, acceptance=criteria, note=note,
                     estimate_invalidated=estimate_invalidated,
                     from_state=str(pbi["state"]), to_state=new_state)

            rows = conn.execute(
                "SELECT task_id FROM task WHERE pbi_id=? AND state='needs_revision'",
                (pbi_id,),
            ).fetchall()
            task_ids = [str(r["task_id"]) for r in rows]
            resubmitted = bool(criteria) and bool(task_ids)
            for task_id in task_ids:
                if criteria:
                    conn.execute(
                        "UPDATE task SET state='queued', blocked_reason=NULL, updated_at=?"
                        " WHERE task_id=?",
                        (ts, task_id),
                    )
                    self.log(conn, "task_resubmitted", "task", task_id, actor,
                             to_state="queued", pbi_id=pbi_id, note=note)
                else:
                    conn.execute(
                        "UPDATE task SET blocked_reason=?, updated_at=? WHERE task_id=?",
                        ("dor_fail:missing_acceptance", ts, task_id),
                    )
        return {"pbi_id": pbi_id, "resubmitted": resubmitted,
                "task_ids": task_ids, "acceptance": criteria,
                "estimate_invalidated": estimate_invalidated}

    def _close_pbi_if_done(self, conn: sqlite3.Connection, pbi_id: str) -> None:
        row = conn.execute(
            "SELECT COUNT(*) AS open FROM task WHERE pbi_id=? AND state != 'done'", (pbi_id,)
        ).fetchone()
        if int(row["open"] or 0) == 0:
            points_row = conn.execute(
                "SELECT points FROM pbi WHERE pbi_id=?", (pbi_id,)
            ).fetchone()
            points = float(points_row["points"] or 0.0) if points_row else 0.0
            conn.execute(
                "UPDATE pbi SET state='done', updated_at=? WHERE pbi_id=?", (self._now(), pbi_id)
            )
            self.log(conn, "pbi_done", "pbi", pbi_id, "sm", points=points)

    # ── ceremonies ──────────────────────────────────────────────────────────
    def ceremony_start(
        self,
        name: str,
        window_key: str,
        *,
        trigger_src: str = "cron",
        actor: str = "sm",
    ) -> dict[str, Any]:
        """Claim a ceremony window. Idempotent on ``(name, window_key)``.

        ``cron_jobs.last_status`` is not evidence a ceremony ran — APScheduler
        drops overlapping ticks and skips misfires after 60s. This row is.
        """
        run_id = f"CER-{uuid.uuid4().hex[:8]}"
        with self.tx() as conn:
            existing = conn.execute(
                "SELECT id, status FROM ceremony_run WHERE name=? AND window_key=?",
                (name, window_key),
            ).fetchone()
            if existing is not None:
                return {"run_id": existing["id"], "status": existing["status"], "duplicate": True}
            conn.execute(
                "INSERT INTO ceremony_run (id, name, window_key, trigger_src, status, started_at)"
                " VALUES (?, ?, ?, ?, 'running', ?)",
                (run_id, name, window_key, trigger_src, self._now()),
            )
            self.log(conn, "ceremony_started", "ceremony", run_id, actor,
                     name=name, window_key=window_key, trigger_src=trigger_src)
            return {"run_id": run_id, "status": "running", "duplicate": False}

    def ceremony_finish(self, run_id: str, *, status: str = "ok", note: str = "") -> dict[str, Any]:
        with self.tx() as conn:
            row = conn.execute("SELECT name FROM ceremony_run WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise TransitionRejected("unknown_ceremony", run_id)
            conn.execute(
                "UPDATE ceremony_run SET status=?, finished_at=?, note=? WHERE id=?",
                (status, self._now(), note, run_id),
            )
            self.log(conn, "ceremony_finished", "ceremony", run_id, "sm", status=status, note=note)
            return {"run_id": run_id, "status": status}

    def ceremony_window_done(self, name: str, window_key: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT status FROM ceremony_run WHERE name=? AND window_key=?",
                (name, window_key),
            ).fetchone()
        return row is not None and row["status"] in ("ok", "skipped")

    def ceremonies_missing(self, name: str, expected_windows: list[str]) -> list[str]:
        """Windows that should have run and have no record. Drives catch-up."""
        return [w for w in expected_windows if not self.ceremony_window_done(name, w)]

    # ── projections / metrics ───────────────────────────────────────────────
    def review_depth(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM task WHERE state IN ({})".format(
                    ",".join("?" * len(REVIEW_STATES))
                ),
                REVIEW_STATES,
            ).fetchone()
        return int(row["c"])

    def counts_by_state(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT state, COUNT(*) AS c FROM task GROUP BY state"
            ).fetchall()
        return {str(r["state"]): int(r["c"]) for r in rows}

    def admit_denied_histogram(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT json_extract(payload_json, '$.reason') AS reason, COUNT(*) AS c"
                " FROM event_log WHERE kind='admit_denied' GROUP BY reason"
            ).fetchall()
        return {str(r["reason"]): int(r["c"]) for r in rows if r["reason"]}

    def recent_events(self, limit: int = 12) -> list[dict[str, Any]]:
        """Newest-first slice of the log, for the "what just happened" strip.

        The board columns say where work *is*; this says how it got there, which
        is the difference between watching a system and watching a screenshot.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, kind, entity, entity_id, actor FROM event_log"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts": float(r["ts"]),
                "kind": str(r["kind"]),
                "entity": str(r["entity"]),
                "entity_id": str(r["entity_id"] or ""),
                "actor": str(r["actor"] or ""),
            }
            for r in rows
        ]

    def replay_states(self, task_id: str) -> list[str]:
        """Rebuild the state sequence from ``event_log`` alone (projection check)."""
        events = {
            "task_created": "queued",
            "task_claimed": "leased",
            "task_acked": "running",
            "task_submitted": "submitted",
            "task_review_queued": "in_review",
            "task_revision_requested": "needs_revision",
            "task_resubmitted": "queued",
            "task_accepted": "done",
            "task_blocked": "blocked",
        }
        with self._lock:
            rows = self.conn.execute(
                "SELECT kind, payload_json FROM event_log WHERE entity='task' AND entity_id=?"
                " ORDER BY id",
                (task_id,),
            ).fetchall()
        states: list[str] = []
        for row in rows:
            kind = str(row["kind"])
            if kind in events:
                states.append(events[kind])
            elif kind in (
                "task_released", "task_rejected", "task_rework",
                "task_verification_opened", "task_verification_recheck",
                "task_verification_rework", "task_verification_escalated",
                "task_reopened",
            ):
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                states.append(str(payload.get("to_state") or "queued"))
        return states

    def event_count(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM event_log").fetchone()
        return int(row["c"])

    # ── owner-facing artefacts (read-only) ──────────────────────────────────
    # The three Scrum artefacts are projections, never tables of their own.
    # A second "backlog" table would be a second source of truth, and the first
    # thing to drift when scope is injected mid-sprint.
    def all_pbis(self) -> list[dict[str, Any]]:
        """Every PBI in priority order, with acceptance criteria decoded."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM pbi ORDER BY priority_rank, created_at"
            ).fetchall()
        return [self._pbi_dict(r) for r in rows]

    def pbis_in_state(self, *states: str) -> list[dict[str, Any]]:
        """PBIs in the given states, priority order.

        Same read-only projection as ``all_pbis``. Used by the owner's Review
        panel to surface a request the PO held *before* it ever became a task,
        which is the whole point of holding it at refinement.
        """
        if not states:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM pbi WHERE state IN ({})"
                " ORDER BY priority_rank, created_at".format(",".join("?" * len(states))),
                states,
            ).fetchall()
        return [self._pbi_dict(r) for r in rows]

    @staticmethod
    def _pbi_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["acceptance"] = json.loads(item.pop("acceptance_json") or "[]")
        item["readiness"] = json.loads(item.pop("readiness_json") or "{}")
        return item

    def get_sprint(self, sprint_id: str | None = None) -> dict[str, Any] | None:
        """Resolve a sprint; ``None`` means the active one, else the most recent."""
        with self._lock:
            if sprint_id is not None:
                row = self.conn.execute(
                    "SELECT * FROM sprint WHERE sprint_id=?", (sprint_id,)
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM sprint ORDER BY"
                    " CASE state WHEN 'active' THEN 0 WHEN 'planned' THEN 1 ELSE 2 END,"
                    " COALESCE(started_at, created_at) DESC LIMIT 1"
                ).fetchone()
        return dict(row) if row is not None else None

    def tasks_for_sprint(self, sprint_id: str) -> list[Task]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task WHERE sprint_id=? ORDER BY created_at", (sprint_id,)
            ).fetchall()
        return [Task.from_row(r) for r in rows]

    def burndown_events(self) -> list[dict[str, Any]]:
        """Point-accounting events, oldest first.

        ``sprint_committed`` fixes the baseline; ``scope_injected`` raises it;
        ``pbi_done`` lowers it. ``pbi_estimated`` / ``estimate_invalidated``
        adjust it when the team sizes a request (or throws that size away
        because the owner rewrote the request). Task-state events are
        deliberately absent: in Scrum only a finished PBI burns down.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, kind, entity, entity_id, payload_json FROM event_log"
                " WHERE kind IN ('sprint_committed','scope_injected','pbi_done',"
                "                'pbi_estimated','estimate_invalidated')"
                " ORDER BY id"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            out.append({
                "ts": float(row["ts"]),
                "kind": str(row["kind"]),
                "entity_id": str(row["entity_id"] or ""),
                "payload": payload if isinstance(payload, dict) else {},
            })
        return out

    def dod_checks(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT check_id, ok, output_tail, ran_at, check_round FROM dod_check"
                " WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def review_decision(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM review_decision WHERE task_id=?", (task_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def meta_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def meta_set(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
