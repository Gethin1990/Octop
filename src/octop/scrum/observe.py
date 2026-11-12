"""Read-only observation of ``octop.db``.

The watchdog question from the Super-agent paper is not "is the process alive"
but "is the task moving". Both signals we need for that live in Octop's own
database, so this module opens it ``mode=ro`` and never writes.

A missing table or a locked database must degrade to "unknown", not raise: the
board must keep working when Octop is down, otherwise a restart of one process
takes out the other.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .transport import Probe

#: Values of ``thread_history_projection.status`` that mean "a turn is running".
_ACTIVE_STATUSES: Final[frozenset[str]] = frozenset(
    {"running", "pending", "streaming", "busy", "in_progress", "tool_calls"}
)

_TERMINAL: Final[frozenset[str]] = frozenset({"ready", "done", "idle", "error", "failed"})


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    thread_id: str
    status: str | None
    last_event_at: float | None
    last_kind: str | None
    last_is_error: bool


class OctopObserver:
    """Probe helper. Cheap enough to call once per task per tick."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection | None:
        if not self.db_path.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def snapshot(self, thread_id: str | None) -> TurnSnapshot:
        if not thread_id:
            return TurnSnapshot("", None, None, None, False)
        conn = self._connect()
        if conn is None:
            return TurnSnapshot(thread_id, None, None, None, False)
        try:
            status_row = conn.execute(
                "SELECT status FROM thread_history_projection WHERE thread_id=?", (thread_id,)
            ).fetchone()
            event_row = conn.execute(
                "SELECT ts, kind, is_error FROM trajectory_events"
                " WHERE thread_id=? ORDER BY seq DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        except sqlite3.Error:
            return TurnSnapshot(thread_id, None, None, None, False)
        finally:
            conn.close()
        return TurnSnapshot(
            thread_id=thread_id,
            status=str(status_row["status"]) if status_row else None,
            last_event_at=float(event_row["ts"]) if event_row else None,
            last_kind=str(event_row["kind"]) if event_row else None,
            last_is_error=bool(event_row["is_error"]) if event_row else False,
        )

    def probe(self, thread_id: str | None, *, stall_window_s: float, now: float) -> Probe:
        snap = self.snapshot(thread_id)
        fresh = snap.last_event_at is not None and snap.last_event_at > now - stall_window_s
        status_active = snap.status is not None and snap.status not in _TERMINAL
        if snap.status is None and snap.last_event_at is None:
            return Probe(active=False, detail="unknown")
        return Probe(
            active=bool(fresh or (status_active and snap.status in _ACTIVE_STATUSES)),
            last_event_at=snap.last_event_at,
            thread_status=snap.status,
            detail=snap.last_kind or "",
        )
