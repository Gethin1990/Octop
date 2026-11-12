"""Versioned policy: the numbers that decide how the board behaves.

Two rules make this worth a module rather than a config file:

* **Never edit in place.** A change produces a new row with a new hash, so
  "the DoD got easier in sprint 7" is a fact you can query, not a suspicion.
* **Agents read a generated snapshot, not the database.** The snapshot carries
  the hash; on wake an agent can compare hashes and notice it is running on a
  stale contract (context rot is what makes long tasks quietly violate rules
  they were told once, 40 minutes ago).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

#: Starting values. Every one of these is a guess until the first sprint's P50
#: lands; ``policy.bump`` exists so the guess can be corrected with a record of
#: who corrected it and why.
DEFAULT_POLICY: Final[dict[str, Any]] = {
    "in_flight_pbi_cap": 4,  # <= number of dev agents (see dispatcher I1)
    "review_queue_cap": 6,  # back-pressure: stop admitting, do not slow down
    "interrupt_ratio": 0.12,  # share of capacity that may be consumed by expedite
    "max_attempts": 3,
    "max_wake_failures": 5,
    "ack_timeout_s": 120,  # wake -> ack; longer means the injection was dropped
    "stall_window_s": 600,  # no heartbeat AND no new progress
    "lease_ttl_s": 900,
    "lease_hard_cap_s": 3600,  # heartbeats cannot extend past this
    "backoff_s": 30,
    "backoff_max_s": 600,
    "wakes_per_tick": 4,
    "dod": {
        "min_evidence_keys": ["summary", "tests_output", "files_changed"],
        "require_git_sha": True,
    },
}

#: Keys whose value must be a positive number; a zero here silently disables a
#: gate, which is exactly the failure mode this module is meant to prevent.
_POSITIVE_KEYS: Final[tuple[str, ...]] = (
    "in_flight_pbi_cap",
    "review_queue_cap",
    "max_attempts",
    "max_wake_failures",
    "ack_timeout_s",
    "stall_window_s",
    "lease_ttl_s",
    "lease_hard_cap_s",
    "backoff_s",
    "backoff_max_s",
    "wakes_per_tick",
)


class PolicyError(ValueError):
    """Raised when a policy body would disable a gate or is malformed."""


def canonical(body: dict[str, Any]) -> str:
    """Stable JSON used for hashing (sorted keys, no whitespace variance)."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Policy:
    version: int
    body: dict[str, Any]
    hash: str

    def __getitem__(self, key: str) -> Any:
        return self.body[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)


def validate(body: dict[str, Any]) -> dict[str, Any]:
    """Merge over defaults and reject anything that would silently disable a gate."""
    merged = {**DEFAULT_POLICY, **body}
    for key in _POSITIVE_KEYS:
        value = merged.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise PolicyError(f"policy.{key} must be a positive number, got {value!r}")
    ratio = merged.get("interrupt_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 <= ratio <= 1:
        raise PolicyError(f"policy.interrupt_ratio must be within [0, 1], got {ratio!r}")
    dod = merged.get("dod")
    if not isinstance(dod, dict):
        raise PolicyError("policy.dod must be an object")
    return merged


def load(conn: sqlite3.Connection) -> Policy:
    """Return the newest policy row, or the built-in default when the table is empty."""
    row = conn.execute(
        "SELECT version, body_json, body_hash FROM policy ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        body = validate({})
        return Policy(version=0, body=body, hash=digest(body))
    body = validate(json.loads(row["body_json"]))
    return Policy(version=int(row["version"]), body=body, hash=str(row["body_hash"]))


def bump(
    conn: sqlite3.Connection,
    changes: dict[str, Any],
    *,
    note: str,
    now: float,
) -> Policy:
    """Append a new policy version. ``changes`` is merged over the current body."""
    current = load(conn)
    body = validate({**current.body, **changes})
    new_hash = digest(body)
    cursor = conn.execute(
        "INSERT INTO policy (body_json, body_hash, note, created_at) VALUES (?, ?, ?, ?)",
        (canonical(body), new_hash, note, now),
    )
    return Policy(version=int(cursor.lastrowid or 0), body=body, hash=new_hash)
