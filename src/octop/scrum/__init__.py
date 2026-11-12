"""Scrum-over-Super-agent control plane for Octop (experimental).

This package is **not** wired into Octop's request path. It is a side-car
control plane that owns the one thing Octop deliberately does not model: a
durable task/commitment state machine ("board").

Why it lives outside the Octop process
--------------------------------------
``~/.octop/plugins/*`` cannot own a queue:

* plugin hot reload (``POST /api/plugins/reload``) re-executes the module and
  re-registers tools, silently dropping any background asyncio task it started;
* plugins cannot register HTTP routes or cron jobs;
* ``ask_agent``'s inbox is an in-memory dict that is pruned on terminal state,
  so a restart erases both queued and in-flight work with no failure record.

So the board + dispatcher run as their own process, and Octop is used purely as
an executor: the dispatcher pushes a *pointer* over the dashboard WebSocket and
the agent pulls its payload through ``board_*`` plugin tools.

Layout
------
``constants``  – states, classes of service, admission reasons (single source).
``schema.sql`` – the whole storage contract in one reviewable file.
``store``      – ``BoardStore``: the only writer, transaction-per-mutation,
                 every mutation appended to ``event_log``.
``policy``     – versioned, hash-checked policy (never edited in place).
``transport``  – ``Transport`` protocol + fakes used by tests.
``ws_transport`` – the real Octop implementation (login + dashboard WS).
``observe``    – read-only probe over ``octop.db`` (progress, not liveness).
``dispatcher`` – admit → claim → wake → confirm → stall/retry.
``reconcile``  – the six-step restart reconciliation.
``service``    – FastAPI RPC surface used by the plugin tools.
``cli``        – ``python -m octop.scrum ...``.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
