"""HTTP surface for the board (RPC + the owner's three Scrum artefacts).

Two rules make this a safe boundary rather than a convenience API:

* **Role comes from the credential, not the payload.** Every call carries
  ``X-Octop-Agent``; the server looks the role up in ``agent_role`` and ignores
  any ``role``/``actor`` field the caller supplies. A model cannot escalate by
  describing itself as PO.
* **``lease_id`` is the capability token.** ``task_submit`` is only accepted from
  a caller holding the current lease, so a stale worker that wakes up twice
  cannot overwrite a newer increment.

The page at ``/`` exists because Octop's dashboard is a compiled bundle:
plugins can add a chat card and a drawer, but not a page. The person paying for
the work is deliberately **not** a PO/SM/team member here: they get exactly
three entry points — submit a request, approve the PO's Sprint proposal, and
accept/reject the delivered increment — plus the three Scrum artefacts to
observe progress. Refinement, sizing, capacity checks and decomposition are
attributed to po / dev / sm in the event log, never to the owner. Every
team-side control is hidden behind ``?debug=1`` and is not part of the intended
workflow.
"""

from __future__ import annotations

import html
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import planning, reports
from .estimate import Estimator, HeuristicEstimator
from .store import BoardError, BoardStore, TransitionRejected
from .worker import PLACEHOLDER_EXECUTOR, PLACEHOLDER_MARK, STALE_AFTER_S, read_heartbeat

#: The person who funds and accepts the work is not a Scrum role in this setup.
#: Keeping a distinct actor id makes it obvious in the event log who acted.
OWNER_ACTOR = "owner"

#: Artifact reads are confined to this many bytes and these suffixes. The board
#: binds loopback, but "loopback" is not an access control for the filesystem.
_MAX_ARTIFACT_BYTES = 512 * 1024
_ARTIFACT_SUFFIXES = frozenset({
    ".md", ".txt", ".log", ".json", ".yaml", ".yml", ".csv", ".py",
    ".js", ".ts", ".html", ".htm", ".css", ".sql", ".sh", ".toml",
})

#: method -> roles allowed. ``None`` means "any authenticated agent".
_ROLES: dict[str, tuple[str, ...] | None] = {
    "whoami": None,
    "board_view": None,
    "metrics": None,
    "task_get": None,
    "product_backlog": None,
    "sprint_backlog": None,
    "burndown": None,
    "deliverable": None,
    "pbi_create": ("po",),
    "pbi_estimate": ("dev",),
    "pbi_reorder": ("po",),
    "pbi_add_to_sprint": ("po", "sm"),
    "task_create": ("po",),
    "sprint_create": ("po",),
    "sprint_commit": ("po",),
    "sprint_close": ("sm",),
    "policy_bump": ("sm",),
    "ceremony_start": ("sm",),
    "ceremony_finish": ("sm",),
    "task_claim": ("dev",),
    "task_ack": ("dev",),
    "task_heartbeat": ("dev",),
    "task_submit": ("dev",),
    "task_block": ("dev", "sm"),
    "dod_record": ("sm",),
    "verification_resolve": ("reviewer", "sm"),
    "task_reopen": ("reviewer", "sm"),
    "task_reverify": ("reviewer", "sm"),
    "review_accept": ("reviewer", "sm"),
    "review_reject": ("reviewer", "sm"),
    "task_release": ("sm",),
}


class RpcBody(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


def _token() -> str:
    return os.environ.get("SCRUM_TOKEN", "")


def create_app(
    store: BoardStore,
    *,
    artifacts_root: Path | None = None,
    estimator: Estimator | None = None,
) -> FastAPI:
    app = FastAPI(title="Octop Scrum Board", version="0.1.0")
    # More than one root, because a real agent writes into its own Octop
    # workspace, not into the board's demo folder. ``os.pathsep`` keeps the
    # single-root case unchanged. The confinement check below is what makes a
    # list safe: every root is resolved and compared, never trusted as a prefix
    # string.
    roots: list[Path] = []
    raw_roots = artifacts_root or os.environ.get(
        "SCRUM_ARTIFACTS", str(store.path.parent / "artifacts")
    )
    for entry in str(raw_roots).split(os.pathsep):
        if entry.strip():
            roots.append(Path(entry.strip()).expanduser().resolve())
    sizer = estimator or HeuristicEstimator()
    handlers: dict[str, Callable[[dict[str, Any], str], Any]] = {
        **_grant(store),
        "board_view": _view(store),
        "whoami": _h_whoami(store),
        "metrics": _h_metrics(store),
        "product_backlog": lambda _p, _a: reports.product_backlog(store),
        "sprint_backlog": lambda p, _a: reports.sprint_backlog(store, p.get("sprint_id")),
        "burndown": lambda p, _a: reports.burndown(
            store, p.get("sprint_id"), horizon_s=p.get("horizon_s")
        ),
        "deliverable": lambda p, _a: _deliverable_json(store, str(p.get("task_id") or "")),
    }

    def auth(
        x_scrum_token: str | None = Header(default=None),
        x_octop_agent: str | None = Header(default=None),
    ) -> tuple[str, str | None]:
        expected = _token()
        if expected and x_scrum_token != expected:
            raise HTTPException(status_code=401, detail="bad scrum token")
        return (x_octop_agent or ""), (x_scrum_token or None)

    def guard(method: str, agent_id: str) -> str:
        role = store.role_of(agent_id) if agent_id else None
        allowed = _ROLES.get(method, ())
        if allowed is None:
            # Any authenticated identity may read, but only a *known* one:
            # an unregistered caller is not "trusted by default".
            if role is None and method != "whoami":
                raise HTTPException(status_code=403, detail=f"unknown agent: {agent_id!r}")
            return role or "unknown"
        if role is None:
            raise HTTPException(status_code=403, detail=f"unknown agent: {agent_id!r}")
        if role not in allowed:
            raise HTTPException(status_code=403, detail=f"role {role} may not call {method}")
        return role

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "tasks": store.counts_by_state()}

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        return {
            "review_depth": store.review_depth(),
            "counts": store.counts_by_state(),
            "admit_denied": store.admit_denied_histogram(),
            "policy_version": store.policy().version,
        }

    @app.post("/rpc/{method}")
    def rpc(
        method: str,
        body: RpcBody,
        creds: tuple[str, str | None] = Depends(auth),
    ) -> dict[str, Any]:
        agent_id, _ = creds
        if method not in _ROLES:
            raise HTTPException(status_code=404, detail=f"unknown method: {method}")
        guard(method, agent_id)
        handler = handlers.get(method)
        if handler is None:
            raise HTTPException(status_code=501, detail=f"not wired: {method}")
        try:
            return {"ok": True, "data": handler(body.params, agent_id)}
        except TransitionRejected as exc:
            # Rejections are answers, not failures: the agent needs the reason
            # to decide between "stop" and "fix the input".
            return {"ok": False, "reason": exc.reason, "detail": str(exc)}
        except BoardError as exc:
            return {"ok": False, "reason": exc.reason, "detail": str(exc)}

    # ── owner console ────────────────────────────────────────────────────────
    # Two entry points only: submit a request, and accept/reject a delivered
    # increment. The actor is fixed by the server — the owner is never asked to
    # pick a PO/SM/dev identity, and cannot do so by editing the form.
    # Team-side actions remain reachable under ?debug=1 purely to unstick a
    # wedged card; they are not part of the intended workflow.

    def back(message: str, *, debug: bool = False) -> RedirectResponse:
        suffix = "&debug=1" if debug else ""
        return RedirectResponse(url=f"/?msg={quote(message)}{suffix}", status_code=303)

    @app.post("/ui/new_card")
    async def ui_new_card(request: Request) -> RedirectResponse:
        """Owner states a request. Nothing else happens in this turn.

        The blueprint separates "asked for" from "committed": the PO refines
        and orders the Backlog, a dev sizes it, and a human approves the Sprint
        before a task may exist. See ``planning.py`` for those later stages.
        """
        form = await _form(request)
        title = (form.get("title") or "").strip()
        if not title:
            return back("标题不能为空")
        raw_acceptance = (form.get("acceptance") or "").replace("；", ";")
        acceptance = [a.strip() for a in raw_acceptance.split(";") if a.strip()]
        # ``points`` and ``actor`` in the payload are ignored. Sizing belongs to
        # a dev; refinement and commitment belong to the PO and the owner.
        try:
            pbi_id = store.create_pbi(
                title,
                body=form.get("body") or "",
                points=None,
                class_of_service=form.get("class_of_service") or "standard",
                acceptance=acceptance,
                actor=OWNER_ACTOR,
                state="new",
            )
        except (BoardError, TransitionRejected) as exc:
            return back(f"建卡被拒：{exc.reason} {exc}")
        return back(f"需求已受理 {pbi_id}；PO 细化后会生成 Sprint 提案待你批准")

    @app.post("/ui/approve_sprint")
    async def ui_approve_sprint(request: Request) -> RedirectResponse:
        """The one human decision between a proposed and a live Sprint."""
        form = await _form(request)
        sprint_id = (form.get("sprint_id") or "").strip() or None
        try:
            result = planning.approve_sprint(
                store, sprint_id, actor=OWNER_ACTOR
            )
        except (BoardError, TransitionRejected) as exc:
            return back(f"批准被拒：{exc.reason} {exc}")
        return back(
            f"Sprint {result['sprint_id']} 已批准开工 · 承诺 "
            f"{result['committed_pts']:g} pt · 拆出 {len(result['task_ids'])} 张任务卡"
        )

    @app.post("/ui/revise")
    async def ui_revise(request: Request) -> RedirectResponse:
        """Owner edits a DoR-rejected request and resubmits it."""
        form = await _form(request)
        pbi_id = (form.get("pbi_id") or "").strip()
        title = (form.get("title") or "").strip()
        if not pbi_id or not title:
            return back("需求编号与标题不能为空")
        raw = (form.get("acceptance") or "").replace("；", ";")
        acceptance = [a.strip() for a in raw.split(";") if a.strip()]
        try:
            result = store.revise_request(
                pbi_id,
                title=title,
                body=form.get("body") or "",
                acceptance=acceptance,
                actor=OWNER_ACTOR,
                note=form.get("note") or "",
            )
        except (BoardError, TransitionRejected) as exc:
            return back(f"修改被拒：{exc.reason} {exc}")
        if not result["acceptance"]:
            return back("已保存，但验收条件仍为空，需求留在 Review 等继续修改")
        if not result["resubmitted"]:
            # A request held at refinement has no task yet: releasing it means
            # it re-enters sizing and then either joins the active Sprint or the
            # next proposal -- not a requeue.
            return back(
                "需求已补齐验收条件，进入团队估点；有进行中的 Sprint 会按优先级排入，"
                "否则进入下一轮 Sprint 提案"
            )
        extra = "，原估点作废，团队将重新估点" if result["estimate_invalidated"] else ""
        return back(f"需求已重新提交，团队会重新接单{extra}")

    @app.post("/ui/action")
    async def ui_action(request: Request) -> RedirectResponse:
        form = await _form(request)
        method = form.get("method") or ""
        task_id = form.get("task_id") or ""
        debug = form.get("debug") == "1"
        # Owner actions are always attributed to the owner. Only the debug
        # console may supply a team identity, and even then only for team moves.
        actor = OWNER_ACTOR
        handler = handlers.get(method)
        if handler is None:
            return back(f"未知动作：{method}", debug=debug)
        params: dict[str, Any] = {"task_id": task_id}
        if method == "task_claim":
            if not form.get("actor"):
                return back("认领需要一个 agent 身份", debug=debug)
            actor = form["actor"]
        elif method == "task_submit":
            task = store.get_task(task_id)
            if task is None or not task.lease_id:
                return back("这张卡没有有效租约，不能提交", debug=debug)
            actor = task.agent_id or form.get("actor") or "dev-1"
            params["lease_id"] = task.lease_id
            params["evidence"] = {
                "summary": form.get("summary") or "",
                "tests_output": form.get("tests_output") or "",
                "files_changed": form.get("files_changed") or "",
            }
        elif method == "dod_record":
            params["checks"] = [{"check_id": "human_review", "ok": True}]
            actor = form.get("actor") or "sm"
        elif method in ("review_accept", "review_reject"):
            # review_lead_time is the metric that tells a rubber stamp from a
            # review. Measure it from when the card entered in_review, not from
            # when the page was opened (the owner may have been away for hours).
            task = store.get_task(task_id)
            if task is not None:
                params["dwell_seconds"] = max(0.0, time.time() - task.updated_at)
            params["note"] = form.get("note") or ""
        elif method == "verification_resolve":
            params["decision"] = form.get("decision") or "recheck"
            params["note"] = form.get("note") or ""
        elif method in ("task_reopen", "task_reverify"):
            params["note"] = form.get("note") or ""
        elif method in ("task_release", "task_block"):
            params["reason"] = form.get("reason") or "human"
        try:
            handler(params, actor)
        except (BoardError, TransitionRejected) as exc:
            return back(f"{method} 被拒：{exc.reason} {exc}", debug=debug)
        return back(f"{method} 完成（{task_id}）", debug=debug)

    # ── three artefacts + delivered increment ───────────────────────────────
    @app.get("/api/artifacts")
    def api_artifacts(horizon: float | None = None) -> dict[str, Any]:
        return reports.all_artifacts(store, horizon_s=horizon)

    def read_artifact(task_id: str) -> tuple[str, str] | None:
        """Return (text, path) for a task's registered artefact, confined to roots."""
        task = store.get_task(task_id)
        if task is None:
            return None
        claimed = task.evidence.get("artifact") or task.evidence.get("files_changed") or ""
        first = str(claimed).split(",")[0].strip()
        if not first:
            return None
        path = Path(first).expanduser().resolve()
        inside = any(
            os.path.commonpath([str(base), str(path)]) == str(base) for base in roots
        )
        if not inside:
            return None
        if path.suffix.lower() not in _ARTIFACT_SUFFIXES or not path.is_file():
            return None
        if path.stat().st_size > _MAX_ARTIFACT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace"), str(path)

    @app.get("/artifact/raw")
    def artifact_raw(task_id: str = "") -> PlainTextResponse:
        payload = read_artifact(task_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="no readable artefact for this task")
        text, _ = payload
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    @app.get("/deliverable/{task_id}", response_class=HTMLResponse)
    def deliverable_page(task_id: str) -> str:
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="unknown task")
        payload = read_artifact(task_id)
        return _render_deliverable(store, task, payload)

    @app.get("/", response_class=HTMLResponse)
    def board_page(request: Request) -> str:
        # One idempotent Planning tick per render. The page already polls every
        # 4s, so a freshly submitted request advances through PO refinement and
        # sizing without a separate scheduler. The tick deliberately stops at a
        # `planned` Sprint: activation is the owner's approval, never a GET.
        planning.advance_planning(store, sizer)
        q = request.query_params
        horizon: float | None = None
        if q.get("horizon"):
            try:
                horizon = float(q["horizon"])
            except ValueError:
                horizon = None
        return _render_board(
            store,
            msg=q.get("msg", ""),
            horizon=horizon,
            debug=q.get("debug") == "1",
        )

    @app.get("/guide", response_class=HTMLResponse)
    def guide_page() -> str:
        return _render_guide()

    return app


async def _form(request: Request) -> dict[str, str]:
    """Parse a urlencoded body without pulling in ``python-multipart``."""
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items() if v}


# ── method implementations ───────────────────────────────────────────────────
def _h_whoami(store: BoardStore) -> Callable[[dict[str, Any], str], Any]:
    def handler(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
        # The guard plugin asks this to learn its role. One source of truth.
        return {"agent_id": agent_id, "role": store.role_of(agent_id)}

    return handler


def _view(store: BoardStore) -> Callable[[dict[str, Any], str], Any]:
    def handler(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
        limit = int(params.get("limit", 50))
        return {
            "tasks": [_task_json(t) for t in store.tasks_in_state(
                "queued", "leased", "running", "submitted", "verifying",
                "in_review", "blocked"
            )[:limit]],
            "counts": store.counts_by_state(),
            "review_depth": store.review_depth(),
        }

    return handler


def _h_metrics(store: BoardStore) -> Callable[[dict[str, Any], str], Any]:
    def handler(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return {
            "review_depth": store.review_depth(),
            "counts": store.counts_by_state(),
            "admit_denied": store.admit_denied_histogram(),
            "policy_version": store.policy().version,
        }

    return handler


def _task_json(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "pbi_id": task.pbi_id,
        "title": task.title,
        "state": task.state,
        "agent_id": task.agent_id,
        "attempt": task.attempt,
        "branch": task.branch,
        "thread_id": task.thread_id,
    }


def _deliverable_json(store: BoardStore, task_id: str) -> dict[str, Any]:
    """Everything a reviewer needs, in one machine-readable payload."""
    task = store.get_task(task_id)
    if task is None:
        return {"found": False, "task_id": task_id}
    pbi = store.get_pbi(task.pbi_id)
    return {
        "found": True,
        "task": _task_json(task),
        "spec": task.spec,
        "pbi": {
            "pbi_id": task.pbi_id,
            "title": pbi["title"] if pbi else task.title,
            "body": pbi["body"] if pbi else "",
            "points": pbi["points"] if pbi else None,
            "acceptance": pbi["acceptance"] if pbi else [],
        },
        "evidence": task.evidence,
        "dod_checks": store.dod_checks(task_id),
        "review_decision": store.review_decision(task_id),
    }


def _grant(store: BoardStore) -> dict[str, Callable[..., Any]]:
    def pbi_create(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        pbi_id = store.create_pbi(
            p["title"],
            body=p.get("body", ""),
            points=p.get("points"),
            class_of_service=p.get("class_of_service", "standard"),
            acceptance=p.get("acceptance") or [],
            actor=agent_id,
        )
        return {"pbi_id": pbi_id}

    def pbi_estimate(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        """A dev agent sizes a request. Requesters cannot call this method."""
        try:
            points = float(p["points"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TransitionRejected("invalid_points", str(p.get("points"))) from exc
        return store.estimate_pbi(
            p["pbi_id"], points, actor=agent_id, rationale=p.get("rationale", "")
        )

    def pbi_reorder(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        store.reorder_pbi(p["pbi_id"], float(p["rank"]), actor=agent_id)
        return {"pbi_id": p["pbi_id"], "rank": p["rank"]}

    def pbi_add_to_sprint(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        """Scope injection into a running sprint. Logged as ``scope_injected``."""
        sprint_id = store.add_pbi_to_sprint(
            p["pbi_id"], p.get("sprint_id"), actor=agent_id
        )
        return {"pbi_id": p["pbi_id"], "sprint_id": sprint_id}

    def sprint_create(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return {
            "sprint_id": store.create_sprint(
                p["name"], capacity_pts=float(p["capacity_pts"]), goal=p.get("goal", "")
            )
        }

    def sprint_commit(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        committed = store.commit_sprint(p["sprint_id"], list(p["pbi_ids"]), actor=agent_id)
        return {"sprint_id": p["sprint_id"], "committed_pts": committed}

    def sprint_close(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return store.close_sprint(p["sprint_id"])

    def task_create(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return {
            "task_id": store.create_task(
                p["pbi_id"], p["title"], spec=p.get("spec", ""), role=p.get("role", "dev")
            )
        }

    def task_get(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        task = store.get_task(p["task_id"])
        if task is None:
            return {"found": False}
        return {"found": True, "task": _task_json(task), "spec": task.spec,
                "lease_id": task.lease_id, "evidence": task.evidence}

    def task_claim(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        """Pull the payload. This is where a duplicate wake exits cheaply."""
        task_id = p["task_id"]
        task = store.get_task(task_id)
        if task is None:
            return {"status": "UNKNOWN_TASK"}
        if task.state not in ("queued", "leased", "running"):
            return {"status": "NOT_QUEUED", "state": task.state}
        if task.agent_id and task.agent_id != agent_id:
            return {"status": "NOT_YOURS", "detail": "owned_by_other"}
        if task.state == "queued":
            lease = store.claim(task_id, agent_id)
            if lease is None:
                # Lost the CAS or the agent already holds another task.
                return {"status": "NOT_YOURS", "detail": "claim_lost"}
            store.ack(task_id, lease.lease_id)
            fresh = store.get_task(task_id)
            return {
                "status": "OK",
                "task_id": task_id,
                "spec": lease.spec,
                "branch": lease.branch,
                "lease_id": lease.lease_id,
                "attempt": max(fresh.attempt, 1) if fresh else 1,
            }
        # Already leased to this agent (the dispatcher pre-claimed before waking):
        # the pull *is* the acknowledgement, so charge the attempt here and hand
        # back the same lease. A retried wake therefore never mints a second branch.
        if task.lease_id:
            store.ack(task_id, task.lease_id)
        current = store.get_task(task_id) or task
        return {
            "status": "OK",
            "task_id": task_id,
            "spec": current.spec,
            "branch": current.branch,
            "lease_id": current.lease_id,
            "attempt": max(current.attempt, 1),
        }

    def task_ack(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return {"acked": store.ack(p["task_id"], p["lease_id"])}

    def task_heartbeat(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return {"alive": store.heartbeat(p["task_id"], p["lease_id"])}

    def task_submit(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        # Stamp the executor here, not in the payload. A model that could write
        # ``executor`` itself could label a template "octop-agent/xyz"; the RPC
        # knows which credential arrived and what channel that identity belongs
        # to, so the field is overwritten with the truth before it is stored.
        evidence = dict(p.get("evidence") or {})
        task = store.get_task(p["task_id"])
        channel = store.agent_channel(task.agent_id) if task is not None else None
        if channel == "octop":
            evidence["executor"] = f"octop-agent/{task.agent_id if task else agent_id}"
        elif channel == "local":
            evidence["executor"] = PLACEHOLDER_EXECUTOR
        return store.submit(
            p["task_id"],
            p["lease_id"],
            evidence=evidence,
            idem_key=p.get("idem_key"),
        )

    def task_block(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        store.block(p["task_id"], p.get("reason", "unspecified"), actor=agent_id,
                    detail=p.get("detail", ""))
        return {"blocked": True}

    def task_release(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return {"state": store.release(p["task_id"], p.get("reason", "human"), actor=agent_id)}

    def task_reverify(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        """Owner: the artefacts are fine, the *inspection* failed -- look again."""
        store.request_reverification(
            p["task_id"], actor=agent_id, note=p.get("note") or ""
        )
        return {"state": store.get_task(p["task_id"]).state}

    def dod_record(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        """The DoD runner's write path. A FAIL opens a re-verification window."""
        passed = store.record_dod(p["task_id"], list(p.get("checks") or []))
        current = store.get_task(p["task_id"])
        return {"all_passed": passed, "state": current.state if current else ""}

    def verification_resolve(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        """Owner's verdict on a failed DoD round: recheck / rework / escalate."""
        state = store.resolve_verification(
            p["task_id"], str(p.get("decision") or ""), actor=agent_id,
            note=p.get("note", ""),
        )
        return {"state": state}

    def task_reopen(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        """Put a terminal card back in the queue with a fresh attempt budget."""
        store.reopen(p["task_id"], actor=agent_id, note=p.get("note", ""))
        return {"state": "queued"}

    def review_accept(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        store.accept(p["task_id"], reviewer=agent_id,
                     dwell_seconds=p.get("dwell_seconds"), note=p.get("note", ""))
        return {"accepted": True}

    def review_reject(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        store.reject(p["task_id"], reviewer=agent_id,
                     dwell_seconds=p.get("dwell_seconds"), note=p.get("note", ""))
        return {"rejected": True}

    def policy_bump(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        updated = store.bump_policy(p.get("changes") or {}, note=p.get("note", ""), actor=agent_id)
        return {"version": updated.version, "hash": updated.hash}

    def ceremony_start(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return store.ceremony_start(p["name"], p["window_key"], trigger_src=p.get("trigger_src", "cron"),
                                    actor=agent_id)

    def ceremony_finish(p: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return store.ceremony_finish(p["run_id"], status=p.get("status", "ok"),
                                     note=p.get("note", ""))

    return {
        "pbi_create": pbi_create,
        "pbi_estimate": pbi_estimate,
        "pbi_reorder": pbi_reorder,
        "pbi_add_to_sprint": pbi_add_to_sprint,
        "sprint_create": sprint_create,
        "sprint_commit": sprint_commit,
        "sprint_close": sprint_close,
        "task_create": task_create,
        "task_get": task_get,
        "task_claim": task_claim,
        "task_ack": task_ack,
        "task_heartbeat": task_heartbeat,
        "task_submit": task_submit,
        "task_block": task_block,
        "task_release": task_release,
        "dod_record": dod_record,
        "verification_resolve": verification_resolve,
        "task_reopen": task_reopen,
        "task_reverify": task_reverify,
        "review_accept": review_accept,
        "review_reject": review_reject,
        "policy_bump": policy_bump,
        "ceremony_start": ceremony_start,
        "ceremony_finish": ceremony_finish,
    }


# ── owner console (HTML) ────────────────────────────────────────────────────
# Two entry points: ask for work, accept/reject the delivered increment.
# PO / SM / team moves are hidden unless ?debug=1, and are never the intended
# workflow. The three artefacts above the flow are the progress report.
_COLUMNS = [
    ("queued", "Queued"),
    ("running", "Running"),
    ("submitted", "Submitted"),
    ("verifying", "DoD 复核"),
    ("in_review", "Review"),
    ("needs_revision", "待改需求"),
    ("blocked", "Blocked"),
    ("done", "Done"),
]

_CSS = """
*{box-sizing:border-box}
body{font:13px/1.55 -apple-system,system-ui,-apple-system-font,sans-serif;background:#0e1118;color:#e6e8ee;margin:0;padding:0 18px 40px}
a{color:#8ab4f8;text-decoration:none}
a:hover{text-decoration:underline}
header.top{position:sticky;top:0;z-index:5;background:#0e1118ee;backdrop-filter:blur(8px);border-bottom:1px solid #232838;padding:12px 0 10px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:600;white-space:nowrap}
nav{display:flex;gap:12px;flex-wrap:wrap;font-size:12px}
nav a{color:#9aa4b8}
nav a:hover{color:#cfe0ff}
.alive{color:#6fcf97;font-size:11px}
.dead{color:#e0846a;font-size:11px}
.head-actions{margin-left:auto;display:flex;gap:8px;align-items:center}
.guide-btn,.octop-btn{background:#243044;border:1px solid #3f6bb0;color:#dce9ff;border-radius:6px;padding:4px 10px;font-size:11px;white-space:nowrap}
.guide-btn:hover,.octop-btn:hover{background:#2d3c56;text-decoration:none}
.octop-btn{background:#22432f;border-color:#2f6b45;color:#c9efd6}
.octop-btn:hover{background:#2a543b}
.revision-card{background:#241f18;border:1px solid #7a6428;border-radius:9px;padding:12px;margin-bottom:10px}
.revision-card h3{font-size:13px;margin:0 0 4px;font-weight:600}
.revision-card form.revise{display:grid;grid-template-columns:88px 1fr;gap:6px 8px;align-items:center;margin-top:9px}
.revision-card form.revise label{font-size:11px;color:#9aa4b8}
.revision-card form.revise input,.revision-card form.revise textarea{width:100%}
.revision-card form.revise .full{grid-column:1 / -1}
.warn{color:#e0a25a}
h4.bad{color:#e8a2a2}
.prov{font-size:12px;line-height:1.7;border-radius:8px;padding:10px 12px;margin:9px 0}
.prov.bad{background:#2a1919;border:1px solid #8a3b3b;color:#f0bdbd}
.prov.ok{background:#14261c;border:1px solid #356b48;color:#a9e0bd}
.prov code{color:#ffd9a0;word-break:break-all}
.prov a{color:#8ab4f8}
.prov-path{font-size:11px;margin-top:7px;color:#b9c2d4}
.prov-path code{color:#ffd9a0;word-break:break-all}
.prov-path a{color:#8ab4f8}
.glossary dt{color:#cfe0ff;font-size:12px;font-weight:600;margin-top:12px}
.glossary dd{margin:2px 0 0;color:#9aa4b8;font-size:12px}
.guide-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.flow-diagram{background:#0b0e14;border:1px solid #232838;border-radius:8px;padding:12px;overflow:auto;font:12px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;color:#cfe0ff;white-space:pre}
.guide-section ul{margin:4px 0;padding-left:18px;color:#9aa4b8;font-size:12px}
.guide-section li{margin:3px 0}
code{color:#8ab4f8;font-size:11px}
.flash{background:#1d2a3a;border:1px solid #2f5a86;border-radius:8px;padding:7px 10px;margin:12px 0;font-size:12px;color:#bcd3ee}
section.panel{background:#141821;border:1px solid #232838;border-radius:10px;padding:14px;margin:14px 0}
section.panel>h2{font-size:12px;margin:0 0 10px;color:#9aa4b8;font-weight:600;text-transform:uppercase;letter-spacing:.05em}
section.panel>h2 span{color:#e0a25a}
.hint{font-size:11px;color:#7d879c;margin:8px 0 0}
form.ask{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
input,textarea,select{background:#1e2432;border:1px solid #2b3244;color:#e6e8ee;border-radius:6px;padding:5px 7px;font-size:12px;font-family:inherit}
textarea{resize:vertical;min-height:34px}
button{background:#243044;border:1px solid #35486a;color:#cfdcf0;border-radius:6px;padding:4px 9px;font-size:11px;cursor:pointer}
button:hover{background:#2d3c56}
button.ok{background:#22432f;border-color:#2f6b45;color:#c9efd6}
button.no{background:#43262a;border-color:#6b3535;color:#f0cdcd}
button.primary{background:#2a4a7a;border-color:#3f6bb0;color:#dce9ff}
.btn{display:inline-block;background:#243044;border:1px solid #35486a;color:#cfdcf0;border-radius:6px;padding:3px 9px;font-size:11px}
.btn:hover{background:#2d3c56;text-decoration:none}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #232838;vertical-align:top}
th{color:#7d879c;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:#cfe0ff}
tr:last-child td{border-bottom:none}
.chip{display:inline-block;border:1px solid #35486a;border-radius:10px;padding:0 6px;font-size:10px;color:#b9c9e2;margin-right:3px}
.chip.done{color:#a8e0bd;border-color:#2f6b45}
.chip.review{color:#ecd39a;border-color:#7a6428}
.chip.verifying{color:#e8c39a;border-color:#7a5228}
.chip.blocked{color:#e8a2a2;border-color:#7a3030}
.chip.run{color:#a8c8f0;border-color:#2f5a86}
.ac{color:#7d879c;font-size:11px}
.bar{height:6px;background:#1e2432;border-radius:4px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:#6fcf97}
.art-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
.chart-wrap{background:#141821;border-radius:8px;overflow:hidden}
.legend{font-size:11px;color:#7d879c;margin-top:8px}
.horizons{display:flex;gap:8px;font-size:11px;margin-top:8px;flex-wrap:wrap}
.horizons a.on{color:#cfe0ff;font-weight:600}
.review-card{background:#1e2432;border:1px solid #7a6428;border-radius:9px;padding:12px;margin-bottom:10px}
.review-card h3{font-size:13px;margin:0 0 4px;font-weight:600}
.review-card .meta{font-size:11px;color:#7d879c;margin-bottom:8px}
.review-card h4{font-size:11px;color:#9aa4b8;margin:9px 0 3px;font-weight:600}
.review-card ul{margin:2px 0;padding-left:18px;font-size:12px}
.review-card dl{display:grid;grid-template-columns:80px 1fr;gap:2px 8px;margin:0;font-size:12px}
.review-card dt{color:#7d879c}
.review-card dd{margin:0;word-break:break-all}
.acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px;align-items:center}
form.decision{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:10px;border-top:1px solid #2b3244;padding-top:9px}
.empty{font-size:12px;color:#7d879c;padding:8px 0}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
main section{background:#171b26;border:1px solid #232838;border-radius:9px;padding:9px;min-height:96px}
main section.running{border-color:#2f5a86}
main section.in_review{border-color:#7a6428}
main section.verifying{border-color:#7a5228}
main section.needs_revision{border-color:#7a6428}
main section.blocked{border-color:#7a3030}
main section.done{opacity:.75}
main h2{font-size:11px;margin:0 0 7px;color:#9aa4b8;font-weight:600;text-transform:uppercase}
main h2 span{color:#5d6679}
.card{background:#1e2432;border:1px solid #2b3244;border-radius:7px;padding:7px;margin-bottom:7px}
.card b{font-size:10px;color:#8ab4f8}
.card .row{display:flex;justify-content:space-between;align-items:baseline}
.card .t{font-size:11px;margin-top:3px}
.card .m{font-size:10px;color:#7d879c}
.card .links{margin-top:5px;font-size:10px}
.acts.own form{margin:0}
details{font-size:11px;color:#9aa4b8}
details.dbg{margin-top:6px;opacity:.65}
details.dbg summary{cursor:pointer;list-style:none}
details form{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}
aside{padding:10px 14px;background:#141821;border:1px solid #232838;border-radius:10px;margin-top:14px}
aside h2{font-size:11px;color:#9aa4b8;margin:0 0 6px}
aside ul{list-style:none;margin:0;padding:0;font-size:11px;color:#9aa4b8}
aside li{display:flex;gap:8px;padding:1px 0;font-variant-numeric:tabular-nums}
aside .ts{color:#5d6679}
aside .k{color:#8ab4f8;min-width:140px}
.doc h2{font-size:13px;color:#cfe0ff;margin:18px 0 6px}
.doc pre{background:#0b0e14;border:1px solid #232838;border-radius:8px;padding:12px;overflow:auto;max-height:520px;font-size:12px;line-height:1.6;color:#d5dbe8;white-space:pre-wrap}
.doc .kv{font-size:12px;color:#9aa4b8}
.doc .pass{color:#8ee0aa}
.doc .fail{color:#e8a2a2}
"""


_GUIDE_BTN = '<a class="guide-btn" href="/guide">流程说明 · 术语表 ↗</a>'


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


#: The board is a side-car (default 8099); Octop itself listens elsewhere.
#: Keep the jump target configurable instead of baking one port into every page.
_OCTOP_URL_DEFAULT = "http://127.0.0.1:8088"  # matches cli.py --octop-url


def _header_actions(back_btn: str = _GUIDE_BTN) -> str:
    """Right-hand side of every page header: jump to Octop, then the second button."""
    raw = os.environ.get("OCTOP_URL", _OCTOP_URL_DEFAULT).strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raw = _OCTOP_URL_DEFAULT
    return (
        '<div class="head-actions">'
        f'<a class="octop-btn" href="{_esc(raw)}" target="_blank" rel="noopener">'
        '打开 Octop ↗</a>'
        f'{back_btn}</div>'
    )


def _provenance(task: Any, artifact_text: str | None = None) -> tuple[str, str]:
    """Say who actually ran the card, so a template cannot pass as an increment.

    Provenance is decided by the artefact's *content*, not by a self-reported
    label: an executor can claim to be an Octop agent while writing the demo
    skeleton. An absent ``executor`` field (older cards) is unproven, not real.
    """
    evidence = task.evidence or {}
    declared = str(evidence.get("executor") or "").strip()
    path = str(evidence.get("artifact") or evidence.get("files_changed") or "")
    link = (
        f'<div class="prov-path">产物绝对路径：<code>{_esc(path)}</code> · '
        f'<a href="/artifact/raw?task_id={_esc(task.task_id)}" target="_blank">'
        '下载原始文档</a> · <a href="/guide#l2">L1 / L2 是什么意思？</a></div>'
    )
    content_is_placeholder = (
        artifact_text is not None and PLACEHOLDER_MARK in artifact_text
    )
    declared_real = bool(declared) and declared != PLACEHOLDER_EXECUTOR

    if declared_real and content_is_placeholder:
        return f"{declared}（与产物矛盾）", (
            '<div class="prov bad"><b>⚠ 执行主体声明与产物内容矛盾</b><br>'
            f'证据里声明 <code>{_esc(declared)}</code>，产物文件却写着占位标记 '
            f'<code>{_esc(PLACEHOLDER_MARK)}</code>。以产物内容为准：'
            '<b>没有 agent 真正执行过</b>，存在伪造执行主体的可能，本卡不可验收。</div>' + link
        )
    if declared_real:
        return declared, (
            f'<div class="prov ok">执行主体：<b>{_esc(declared)}</b> · '
            "产物由该执行器生成，请在下方核对其内容是否满足验收条件。</div>" + link
        )
    label = declared or "未声明（L1 本地演示执行器）"
    return label, (
        '<div class="prov bad"><b>⚠ 占位产物 · 未经 Octop agent 执行</b><br>'
        '这张卡属于 <b>L1 控制面演示</b>：本地 <code>DemoExecutor</code> 只写了一个模板文件，'
        '<b>没有任何 dev agent 跑过 turn</b>，所以产物里<b>没有该需求的业务结果</b>。'
        '当前 DoD 只复验「文件存在且非空」，因此它仍被送进了 Review —— 这正是'
        ' <b>L2（真实 agent 执行）尚未打通</b>的缺口，<b>不能作为验收依据</b>。</div>' + link
    )


def _artifact_path_row(task: Any, evidence: dict[str, Any]) -> str:
    """Where the increment actually lives on disk — an owner must not have to guess."""
    claimed = str(
        evidence.get("artifact") or evidence.get("files_changed") or ""
    ).split(",")[0].strip()
    if not claimed:
        return "<tr><th>产物绝对路径</th><td>（未登记）</td></tr>"
    return (
        f'<tr><th>产物绝对路径</th><td><code>{_esc(claimed)}</code> · '
        f'<a href="/artifact/raw?task_id={_esc(task.task_id)}" '
        'target="_blank">下载原始文档</a></td></tr>'
    )


def _action(method: str, task_id: str, label: str, **extra: str) -> str:
    """Team-side control. Only rendered under ?debug=1."""
    fields = "".join(
        f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">'
        for k, v in {"method": method, "task_id": task_id, "debug": "1", **extra}.items()
    )
    return f'<form method="post" action="/ui/action">{fields}<button>{label}</button></form>'


def _debug_buttons(task: Any, devs: list[str], sm: str) -> str:
    out: list[str] = []
    if task.state == "queued":
        out += [_action("task_claim", task.task_id, f"领给 {d}", actor=d) for d in devs]
    elif task.state in ("leased", "running"):
        owner = task.agent_id or (devs[0] if devs else "dev-1")
        out.append(
            '<details><summary>提交证据</summary><form method="post" action="/ui/action">'
            '<input type="hidden" name="method" value="task_submit">'
            '<input type="hidden" name="debug" value="1">'
            f'<input type="hidden" name="task_id" value="{_esc(task.task_id)}">'
            '<input name="summary" placeholder="做了什么" size="12">'
            '<input name="tests_output" placeholder="pytest: 12 passed" size="14">'
            '<input name="files_changed" placeholder="a.py" size="10">'
            "<button>提交</button></form></details>"
        )
        out.append(_action("task_block", task.task_id, "做不下去", actor=owner,
                           reason="spec_unclear"))
    elif task.state == "submitted":
        out.append(_action("dod_record", task.task_id, "人工判定 DoD 通过", actor=sm))
    elif task.state == "verifying":
        out.append(_action("verification_resolve", task.task_id, "请 SM 再复验",
                           decision="recheck"))
        out.append(_action("dod_record", task.task_id, "人工判定 DoD 通过", actor=sm))
    elif task.state == "blocked":
        # ``task_release`` is a no-op on terminal states, so the old button
        # looked like it worked while the card never moved. Reopen is the
        # owner's real escape hatch, and it resets the attempt budget.
        out.append(_action("task_reverify", task.task_id, "让 SM 重新复验"))
        out.append(_action("task_reopen", task.task_id, "重开为新的一轮"))
    elif task.state == "needs_revision":
        out.append(_action("task_release", task.task_id, "放回队列", actor=sm,
                           reason="human"))
    return "".join(out)


def _flow_card(store: BoardStore, task: Any, devs: list[str], sm: str, debug: bool) -> str:
    links = ""
    if task.state == "in_review":
        links = '<div class="links"><a href="#review">去验收 →</a></div>'
    elif task.state == "verifying":
        links = '<div class="links"><a href="#review">去裁决 DoD 复核 →</a></div>'
    elif task.state == "needs_revision":
        links = '<div class="links"><a href="#review">去修改需求 →</a></div>'
    elif task.state == "done" and task.evidence:
        links = (f'<div class="links"><a href="/deliverable/{_esc(task.task_id)}">'
                 "交付物 →</a></div>")
    # A card that never moves must say why. Silence looks like a broken worker;
    # an empty acceptance list is a deliberate DoR gate, not a hang.
    gate = ""
    if task.state == "needs_revision":
        gate = f'<div class="m warn">↩ 打回给 owner：{_esc(task.blocked_reason or "DoR 未放行")}</div>'
    elif task.state == "verifying":
        gate = (f'<div class="m warn">⚖ DoD 未全通过：'
                f'{_esc(task.blocked_reason or "待复核")} — 等 SM 复验或 owner 裁决</div>')
    elif task.state == "queued":
        pbi = store.get_pbi(task.pbi_id)
        if pbi is not None and not pbi.get("acceptance"):
            gate = '<div class="m">⏸ 验收条件为空，DoR 打回中…</div>'
        elif task.blocked_reason:
            gate = f'<div class="m">⏸ {_esc(task.blocked_reason)}</div>'
    drawer = ""
    if debug:
        moves = _debug_buttons(task, devs, sm)
        if moves:
            drawer = f'<details class="dbg"><summary>调试 / 人工兜底</summary>{moves}</details>'
    return (
        f'<div class="card"><div class="row"><b>{_esc(task.task_id)}</b>'
        f'<span class="m">{_esc(task.agent_id or "-")}</span></div>'
        f'<div class="t">{_esc(task.title)}</div>{gate}{links}{drawer}</div>'
    )


def _activity_strip(store: BoardStore) -> str:
    """The columns say where work is; this says how it got there."""
    rows = [e for e in store.recent_events(20) if e["kind"] != "dod_check"][:9]
    if not rows:
        return ""
    items = "".join(
        f'<li><span class="ts">{time.strftime("%H:%M:%S", time.localtime(e["ts"]))}</span>'
        f'<span class="k">{_esc(e["kind"])}</span>'
        f'<span class="e">{_esc(e["entity_id"])}</span>'
        f'<span class="a">{_esc(e["actor"])}</span></li>'
        for e in rows
    )
    return f'<aside><h2>刚刚发生（事件日志）</h2><ul>{items}</ul></aside>'


def _request_form() -> str:
    return """<section class="panel" id="ask"><h2>① 提需求 <span>owner</span></h2>
<form class="ask" method="post" action="/ui/new_card">
  <input name="title" placeholder="我要什么（一句话需求）" size="34" required>
  <textarea name="body" placeholder="补充说明 / 背景（可选）" cols="30" rows="1"></textarea>
  <input name="acceptance" placeholder="验收条件，用 ; 分隔" size="34">
  <select name="class_of_service" title="服务类别">
    <option value="standard">standard</option>
    <option value="expedite">expedite</option>
    <option value="intangible">intangible</option>
  </select>
  <button class="primary">提交需求</button>
</form>
<p class="hint">这里没有故事点：估点由接单的 dev agent 完成（见 Product Backlog 的「团队估点」列）。
提交后卡片留在 Product Backlog：PO 细化排序 → dev 估点 → PO 起草 Sprint 提案 + SM 校验容量
→ <b>你批准</b> → 才拆任务开工。验收条件为空会在 DoR 环节被打回。</p></section>"""


def _planning_section(store: BoardStore) -> str:
    """The owner's approval gate for the PO's Sprint proposal."""
    planned = store.planned_sprint()
    if planned is None:
        note = (
            "当前有进行中的 Sprint，新需求由 PO 作为范围注入（scope_injected）。"
            if store.active_sprint_id() else
            "还没有 Sprint 提案。提交需求后 PO 会细化排序、dev 估点、SM 校验容量。"
        )
        return ('<section class="panel" id="approve"><h2>② 批准 Sprint 提案 '
                f'<span>owner</span></h2><div class="empty">{note}</div></section>')

    sprint_id = str(planned["sprint_id"])
    pbi_ids = store.candidates_for(sprint_id)
    rows: list[str] = []
    total = 0.0
    for pbi_id in pbi_ids:
        pbi = store.get_pbi(pbi_id) or {}
        points = float(pbi.get("points") or 0.0)
        total += points
        criteria = "; ".join(str(a) for a in (pbi.get("acceptance") or [])) or "—"
        state = str(pbi.get("state") or "")
        rows.append(
            f"<tr><td><b>{_esc(pbi.get('title'))}</b><br><span class='ac'>"
            f"{_esc(pbi_id)} · {_esc(criteria)}</span></td>"
            f"<td class='num'>{points:g}</td>"
            f"<td>{_chip(state, reports.state_label(state))}</td></tr>"
        )
    body = "".join(rows) or "<tr><td colspan='3' class='empty'>提案里还没有需求</td></tr>"
    capacity = float(planned["capacity_pts"])
    verdict = "容量足够" if total <= capacity else "超出容量，批准前请考虑缩减范围"
    return f"""<section class="panel" id="approve"><h2>② 批准 Sprint 提案 <span>owner</span></h2>
<div class="review-card">
<h3>{_esc(planned['name'])} <span class="m">{_esc(sprint_id)} · state=planned</span></h3>
<div class="meta">目标 {_esc(planned['goal'])} · 提案 {total:g} pt / 容量 {capacity:g} pt
 · SM 校验：<b>{_esc(verdict)}</b> · PO 已细化排序，dev 已估点</div>
<table><thead><tr><th>候选需求</th><th>点</th><th>状态</th></tr></thead><tbody>{body}</tbody></table>
<p class="hint">批准是 planned → active 的唯一入口；批准后由 PO 拆任务，卡片才进入派单队列。
不批准，Sprint 不会自己开工。</p>
<form class="decision" method="post" action="/ui/approve_sprint">
  <input type="hidden" name="sprint_id" value="{_esc(sprint_id)}">
  <button class="ok">批准开工（承诺 {total:g} pt）</button>
</form></div></section>"""


def _acceptance_list(items: list[Any]) -> str:
    if not items:
        return '<span class="ac">（未填写验收条件，DoR 会拦住派单）</span>'
    return "<ul>" + "".join(f"<li>{_esc(a)}</li>" for a in items) + "</ul>"


def _review_card(store: BoardStore, task: Any) -> str:
    pbi = store.get_pbi(task.pbi_id) or {}
    evidence = task.evidence
    checks = store.dod_checks(task.task_id)
    passed = sum(1 for c in checks if c["ok"])
    dod = (f"{passed}/{len(checks)} 项通过" if checks else "无记录")
    summary = evidence.get("summary") or "（未填写）"
    tests = evidence.get("tests_output") or "（未填写）"
    files = evidence.get("files_changed") or "（未填写）"
    executor, prov = _provenance(task)
    return f"""<div class="review-card">
<h3>{_esc(task.title)} <span class="m">{_esc(task.task_id)}</span></h3>
<div class="meta">{_esc(task.pbi_id)} · {_esc(pbi.get('points') or 0)} pt ·
认领 {_esc(task.agent_id or '-')} · 执行主体 {_esc(executor)} ·
第 {task.attempt} 次 · DoD {_esc(dod)} · 分支 {_esc(task.branch or '-')}</div>
{prov}
<h4>当初提的验收条件</h4>{_acceptance_list(list(pbi.get('acceptance') or []))}
<h4>团队交付的证据</h4>
<dl><dt>做了什么</dt><dd>{_esc(summary)}</dd>
<dt>检查输出</dt><dd><code>{_esc(tests)}</code></dd>
<dt>变更产物</dt><dd><code>{_esc(files)}</code></dd></dl>
<div class="acts own">
  <a class="btn" href="/deliverable/{_esc(task.task_id)}">查看完整交付物 →</a>
  <a class="btn" href="/artifact/raw?task_id={_esc(task.task_id)}" target="_blank">下载原始文档</a>
</div>
<form class="decision" method="post" action="/ui/action">
  <input type="hidden" name="task_id" value="{_esc(task.task_id)}">
  <input name="note" placeholder="验收备注（通过或打回的原因）" size="34">
  <button class="ok" name="method" value="review_accept">验收通过</button>
  <button class="no" name="method" value="review_reject">打回</button>
</form></div>"""


def _verification_card(store: BoardStore, task: Any) -> str:
    """A failed DoD round. The owner sees the checks and decides what happens next.

    This card is the fix for the "one FAIL -> release -> three retries ->
    blocked" dead end: a verifier's misread acceptance criterion is now visible
    and correctable instead of becoming an invisible terminal state.
    """
    checks = store.dod_checks(task.task_id)
    latest = max((int(c.get("check_round") or 1) for c in checks), default=0)
    current = [c for c in checks if int(c.get("check_round") or 1) == latest]
    failed = [str(c["check_id"]) for c in current if not c["ok"]]
    rounds = store.dod_rounds(task.task_id)
    history = "".join(
        f'<li>第 {int(r["check_round"])} 轮：{int(r["passed"] or 0)}/{int(r["total"])} 通过'
        + (f'，未通过 {_esc(r["failed"])}' if r["failed"] else "") + "</li>"
        for r in rounds
    )
    lines = "".join(
        f'<li>{"✓" if c["ok"] else "✗"} {_esc(c["check_id"])}'
        + (f' — {_esc(c["output_tail"][:160])}' if not c["ok"] and c["output_tail"] else "")
        + "</li>"
        for c in current
    )
    pbi = store.get_pbi(task.pbi_id) or {}
    return f"""<div class="review-card">
<h3>{_esc(task.title)} <span class="m">{_esc(task.task_id)} · DoD 复核窗口</span></h3>
<div class="meta">第 {latest} 轮 · {len(current) - len(failed)}/{len(current)} 项通过 ·
未通过：{_esc(', '.join(failed) or task.blocked_reason or '—')} ·
执行主体 {_esc(_provenance(task)[0])}</div>
<div class="m warn">DoD 有失败项，卡片没有被自动打回，也不会无限重试。
SM 可以重新复验并更正结论；你也可以请 SM 再看一次、打回 Dev 重做（重试计数清零），或确认阻塞。</div>
<h4>当初提的验收条件</h4>{_acceptance_list(list(pbi.get('acceptance') or []))}
<h4>最新一轮 DoD</h4><ul>{lines}</ul>
<h4>历轮记录（只认最新一轮，历史不删）</h4><ul>{history}</ul>
<div class="acts own"><a class="btn" href="/deliverable/{_esc(task.task_id)}">查看完整交付物 →</a></div>
<form class="decision" method="post" action="/ui/action">
  <input type="hidden" name="task_id" value="{_esc(task.task_id)}">
  <input type="hidden" name="method" value="verification_resolve">
  <input name="note" placeholder="裁决说明（复验依据 / 重做要求 / 阻塞原因）" size="30">
  <button class="primary" name="decision" value="recheck">请 SM 再复验</button>
  <button class="no" name="decision" value="rework">打回 Dev 重做</button>
  <button class="no" name="decision" value="escalate">确认阻塞</button>
</form></div>"""


def _revision_card(store: BoardStore, pbi: dict[str, Any], task: Any | None = None) -> str:
    """A DoR-rejected request, with everything the owner needs to fix it.

    Two shapes share this card: a task that bounced at admission (``task`` set),
    and a request the PO held at refinement before any task existed (``task`` is
    ``None``). Both are the same conversation with the owner, so they render the
    same form -- only the destination of a successful save differs.
    """
    readiness = pbi.get("readiness") or {}
    by = readiness.get("estimated_by") if isinstance(readiness, dict) else None
    points = pbi.get("points")
    size = (
        f"{float(points):g} pt（{_esc(by or 'dev agent')} 估）"
        if points is not None else "尚未估点，团队会按新需求估"
    )
    criteria = "; ".join(str(a) for a in (pbi.get("acceptance") or []))
    reason = (task.blocked_reason if task is not None else None) or "dor_fail:missing_acceptance"
    pbi_id = str(pbi.get("pbi_id") or getattr(task, "pbi_id", ""))
    task_id = getattr(task, "task_id", None)
    uid = task_id or pbi_id
    ident = f"{pbi_id} / {task_id}" if task_id else pbi_id
    if task is not None:
        outcome = ("补齐验收条件后重新提交，卡片会自动回到派单队列；"
                   "旧估点作废，由团队按新需求重新估点。")
    else:
        outcome = ("补齐验收条件后保存，这张需求立刻进入团队估点；"
                   "有进行中的 Sprint 会按优先级排入，否则进入下一轮 Sprint 提案。")
    return f"""<div class="revision-card">
<h3>{_esc(pbi.get('title') or getattr(task, 'title', ''))} <span class="m">{_esc(ident)}</span></h3>
<div class="meta">DoR 未放行：<span class="warn">{_esc(reason)}</span> · 当前估点 {size}
 · <a href="/guide#dor">DoR 是什么？</a></div>
<p class="hint">{outcome}</p>
<form class="revise" method="post" action="/ui/revise">
  <input type="hidden" name="pbi_id" value="{_esc(pbi_id)}">
  <label for="rt-{_esc(uid)}">标题</label>
  <input id="rt-{_esc(uid)}" name="title" value="{_esc(pbi.get('title') or getattr(task, 'title', ''))}" required>
  <label for="rb-{_esc(uid)}">补充说明</label>
  <textarea id="rb-{_esc(uid)}" name="body" rows="2">{_esc(pbi.get('body'))}</textarea>
  <label for="ra-{_esc(uid)}">验收条件</label>
  <input id="ra-{_esc(uid)}" name="acceptance" value="{_esc(criteria)}"
         placeholder="用 ; 分隔，至少一条" size="40">
  <label for="rn-{_esc(uid)}">修改说明</label>
  <input id="rn-{_esc(uid)}" name="note" placeholder="可选：改了什么" size="40">
  <div class="full"><button class="primary" type="submit">保存并重新提交需求</button></div>
</form></div>"""


def _blocked_card(store: BoardStore, task: Any) -> str:
    """A card that stopped moving, with the only two honest ways out.

    ``blocked`` used to be a dead end in the owner's UI: the reopen button lived
    behind ``?debug=1``, so a card blocked by a *tooling* failure looked exactly
    like one blocked by a bad artefact and neither could be moved. The two exits
    are different on purpose -- redo the work, or just look at it again.
    """
    reason = task.blocked_reason or "blocked"
    try:
        with store._lock:  # noqa: SLF001 - read-only, same lock as every other query
            row = store.conn.execute(
                "SELECT payload_json FROM event_log WHERE entity_id=?"
                " AND kind='task_blocked' ORDER BY id DESC LIMIT 1",
                (task.task_id,),
            ).fetchone()
    except Exception:  # noqa: BLE001 - the card must render even if history is odd
        row = None
    detail = ""
    if row is not None:
        try:
            detail = str(json.loads(row["payload_json"] or "{}").get("detail") or "")
        except (json.JSONDecodeError, TypeError, IndexError):
            detail = ""
    detail_html = (
        f'<details><summary>现场说明</summary><p class="hint">{_esc(detail)}</p></details>'
        if detail else ""
    )
    inspection = any(k in reason for k in ("tooling", "wake", "lease", "dod_runner"))
    first = (
        ('task_reverify', "让 SM 重新复验", "primary")
        if inspection else
        ('task_reopen', "打回 Dev 重做", "primary")
    )
    second = (
        ('task_reopen', "打回 Dev 重做", "")
        if inspection else
        ('task_reverify', "让 SM 重新复验", "")
    )
    buttons = "".join(
        f'<button class="{cls}" name="method" value="{method}">{label}</button>'
        for method, label, cls in (first, second)
    )
    return f"""<div class="revision-card">
<h3>{_esc(task.title)} <span class="m">{_esc(task.pbi_id)} / {_esc(task.task_id)}</span></h3>
<div class="meta">阻塞原因：<span class="warn">{_esc(reason)}</span>
 · <a href="/deliverable/{_esc(task.task_id)}">看它的交付物 →</a></div>
<p class="hint">{"产物可能没问题，卡住的是取证用的工具/通道。先让 SM 再看一次；确认产物不行再打回重做，重做会消耗一轮新的执行预算。" if inspection else "产物或执行本身出了问题。打回重做会开一轮新预算（attempt 归零）；如果只是复验没跑通，选重新复验，别浪费一次执行。"}</p>
{detail_html}
<form class="decision" method="post" action="/ui/action">
  <input type="hidden" name="task_id" value="{_esc(task.task_id)}">
  <input name="note" placeholder="裁决说明（可选）" size="30">
  {buttons}
</form></div>"""


def _review_section(store: BoardStore) -> str:
    """The owner's single decision panel: fix rejected requests, accept work."""
    revisions = store.tasks_in_state("needs_revision")
    verifying = store.tasks_in_state("verifying")
    blocked = store.tasks_in_state("blocked")
    delivered = store.tasks_in_state("in_review")
    # A held PBI has no task yet; a bounced task is shown through its own card.
    # One card per PBI, whichever way it was rejected.
    held_cards = [
        (pbi, None) for pbi in store.pbis_in_state("needs_revision")
        if pbi["pbi_id"] not in {t.pbi_id for t in revisions}
    ]
    revision_cards = (
        [(store.get_pbi(t.pbi_id) or {"pbi_id": t.pbi_id, "title": t.title}, t) for t in revisions]
        + held_cards
    )
    blocks: list[str] = []
    if revision_cards:
        blocks.append(
            f'<h4 class="warn">需求被打回 · {len(revision_cards)} 条待你修改</h4>'
            + "".join(_revision_card(store, pbi, task) for pbi, task in revision_cards)
        )
    if verifying:
        blocks.append(
            f'<h4 class="warn">DoD 复核窗口 · {len(verifying)} 条待裁决</h4>'
            + "".join(_verification_card(store, t) for t in verifying)
        )
    if delivered:
        blocks.append(
            '<h4>交付物待验收</h4>'
            + "".join(_review_card(store, t) for t in delivered)
        )
    if blocked:
        blocks.append(
            f'<h4 class="bad">阻塞待裁决 · {len(blocked)} 条停住</h4>'
            + "".join(_blocked_card(store, t) for t in blocked)
        )
    if not blocks:
        blocks.append(
            '<div class="empty">当前没有待验收的交付物、待裁决的 DoD 复核，'
            '也没有被打回的需求。任务到达 Review 后会自动出现在这里，并附上可查看的产出。</div>'
        )
    return (
        f'<section class="panel" id="review"><h2>③ Review 验收 / DoD 复核 / 需求修改 '
        f'<span>{len(delivered)} 待验收 · {len(verifying)} 待裁决 · '
        f'{len(revision_cards)} 待修改 · {len(blocked)} 阻塞</span></h2>'
        + "".join(blocks) + "</section>"
    )


def _chip(state: str, label: str) -> str:
    css = {
        "done": "done", "in_review": "review", "verifying": "verifying",
        "blocked": "blocked",
        "running": "run", "leased": "run", "submitted": "run",
    }.get(state, "")
    return f'<span class="chip {css}">{_esc(label)}</span>'


def _product_table(product: dict[str, Any]) -> str:
    rows = []
    for item in product["items"]:
        ac = "; ".join(str(a) for a in item["acceptance"]) or "—"
        where = item["sprint_id"] or "未进 Sprint"
        if item["points_estimated"]:
            by = item["estimated_by"] or "dev agent"
            size = (
                f"<b>{item['points']:g}</b><br><span class='ac' title=\""
                f"{_esc(item['estimate_rationale'])}\">{_esc(by)} 估</span>"
            )
        else:
            size = '<span class="warn">待团队估点</span>'
        rows.append(
            f"<tr><td class='num'>{item['priority_rank']:g}</td>"
            f"<td><b>{_esc(item['title'])}</b><br><span class='ac'>{_esc(item['pbi_id'])}"
            f" · {_esc(item['class_of_service'])} · {_esc(where)}</span></td>"
            f"<td>{_chip(item['state'], item['state_label'])}</td>"
            f"<td class='num'>{size}</td>"
            f"<td class='ac'>{_esc(ac)}</td></tr>"
        )
    body = "".join(rows) or "<tr><td colspan='5' class='empty'>还没有需求</td></tr>"
    return f"""<section class="panel" id="pb"><h2>工件 1 · Product Backlog
<span>{product['count']} 项 / {product['points_total']:g} pt，剩余 {product['points_remaining']:g} pt</span></h2>
<table><thead><tr><th>优先</th><th>需求</th><th>状态</th><th>点 / 估点人</th><th>验收条件</th></tr></thead>
<tbody>{body}</tbody></table>
<p class="hint">故事点由干活的 dev agent 评估，不由提需求的人填；需求被改写后旧估点作废，团队重估。</p></section>"""


def _sprint_table(sprint: dict[str, Any]) -> str:
    meta = sprint["sprint"]
    if meta is None:
        return ('<section class="panel" id="sb"><h2>工件 2 · Sprint Backlog</h2>'
                '<div class="empty">还没有已批准的 Sprint。PO 起草提案后，'
                '需要 owner 在「② 批准 Sprint 提案」里点批准才会开工。</div></section>')
    rows = []
    for item in sprint["items"]:
        tasks = "".join(
            f"<div>{_chip(t['state'], t['state_label'])} "
            f"<b>{_esc(t['task_id'])}</b> <span class='ac'>{_esc(t['title'])}"
            f" · {_esc(t['agent_id'] or '-')} · 第 {t['attempt']} 次</span></div>"
            for t in item["tasks"]
        ) or '<span class="ac">尚未拆任务</span>'
        points = (
            f"{item['points']:g}<br><span class='ac'>{_esc(item['estimated_by'] or 'agent')} 估</span>"
            if item["points_estimated"] else '<span class="warn">待估点</span>'
        )
        rows.append(
            f"<tr><td><b>{_esc(item['title'])}</b><br><span class='ac'>"
            f"{_esc(item['pbi_id'])}</span></td>"
            f"<td class='num'>{points}</td>"
            f"<td>{_chip(item['state'], item['state_label'])}</td><td>{tasks}</td></tr>"
        )
    body = "".join(rows) or "<tr><td colspan='4' class='empty'>Sprint 里还没有条目</td></tr>"
    pct = sprint["progress_pct"]
    return f"""<section class="panel" id="sb"><h2>工件 2 · Sprint Backlog
<span>{_esc(meta['name'])} · 承诺 {sprint['points_committed']:g} pt ·
完成 {sprint['points_done']:g} pt · 剩余 {sprint['points_remaining']:g} pt</span></h2>
<div class="bar"><i style="width:{pct}%"></i></div>
<div class="ac">完成度 {pct}% · {sprint['tasks_total']} 个交付任务</div>
<table><thead><tr><th>已承诺需求</th><th>点</th><th>状态</th><th>拆出的任务 / 流转</th></tr></thead>
<tbody>{body}</tbody></table></section>"""


_HORIZONS = [
    (None, "自适应"),
    (3600.0, "1 小时"),
    (86400.0, "1 天"),
    (1209600.0, "2 周"),
]


def _burndown_section(chart: dict[str, Any], horizon: float | None) -> str:
    links = " ".join(
        f'<a class="{"on" if h == horizon else ""}" href="/?horizon={h:g}#bd">{label}</a>'
        if h is not None else
        f'<a class="{"on" if horizon is None else ""}" href="/#bd">{label}</a>'
        for h, label in _HORIZONS
    )
    name = chart["sprint"]["name"] if chart["sprint"] else "—"
    caption = "自适应时间盒" if chart["adaptive"] else f"{chart['horizon_s'] / 3600:g} 小时时间盒"
    note = (
        f"Sprint {_esc(name)} · 起始承诺 {chart['baseline_pts']:g} pt · "
        f"当前剩余 {chart['current_pts']:g} pt · {_esc(caption)} · "
        f"{chart['events_used']} 个记账事件"
    )
    return f"""<section class="panel" id="bd"><h2>工件 3 · Burndown chart
<span>剩余故事点 / 时间</span></h2>
<div class="chart-wrap">{reports.burndown_svg(chart)}</div>
<div class="horizons">时间盒：{links}</div>
<div class="legend">{note}。ideal 线按 Sprint 起始承诺绘制；橙色点是中途的范围注入或团队估点，
会让实际线抬高——这是 scope creep / 补估点的证据，不是绘图误差。
需求改写导致的估点作废会画成一次回落，随后团队重估再抬升。</div></section>"""


def _flow_section(store: BoardStore, debug: bool) -> str:
    tasks = store.tasks_in_state(
        "queued", "leased", "running", "submitted", "verifying", "in_review",
        "needs_revision", "blocked", "done",
    )
    devs = store.agents_by_role("dev")
    sm = (store.agents_by_role("sm") or ["sm"])[0]
    columns: dict[str, list[Any]] = {key: [] for key, _ in _COLUMNS}
    for task in tasks:
        key = "running" if task.state == "leased" else task.state
        columns.setdefault(key, []).append(task)
    cells = []
    for key, label in _COLUMNS:
        rows = columns.get(key, [])
        if key == "done":
            rows = sorted(rows, key=lambda t: t.updated_at or 0.0, reverse=True)[:12]
        cards = "".join(_flow_card(store, t, devs, sm, debug) for t in rows)
        cells.append(
            f'<section class="{key}"><h2>{label} <span>{len(rows)}</span></h2>{cards}</section>'
        )
    hint = ('<p class="hint">调试模式：团队动作已展开，仅用于解卡，不是正常流程。'
            '<a href="/">退出调试</a></p>' if debug else
            '<p class="hint">只读。中间状态由常驻 worker 自动推进；'
            '如需人工解卡，<a href="/?debug=1">打开调试模式</a>。</p>')
    return (f'<section class="panel" id="flow"><h2>任务流（过程观察）</h2>'
            f'<main>{"".join(cells)}</main>{hint}</section>')


def _worker_badge(store: BoardStore) -> str:
    beat = read_heartbeat(store)
    if beat is not None and beat < STALE_AFTER_S:
        return f'<span class="alive">● worker 在线（{beat:.0f}s 前）</span>'
    why = "从未启动" if beat is None else f"{beat:.0f}s 前"
    return (f'<span class="dead">○ 没有 worker（{why}）— 卡片不会自己动，'
            "运行 <code>octop.scrum worker --watch</code></span>")


def _render_board(
    store: BoardStore,
    msg: str = "",
    *,
    horizon: float | None = None,
    debug: bool = False,
) -> str:
    artifacts = reports.all_artifacts(store, horizon_s=horizon)
    flash = f'<div class="flash">{_esc(msg)}</div>' if msg else ""
    nav = (
        '<nav><a href="#ask">① 提需求</a><a href="#approve">② 批准 Sprint</a>'
        '<a href="#review">③ 验收</a>'
        '<a href="#pb">Product Backlog</a><a href="#sb">Sprint Backlog</a>'
        '<a href="#bd">Burndown</a><a href="#flow">任务流</a></nav>'
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scrum · owner 视图</title><style>{_CSS}</style></head><body>
<header class="top"><h1>Scrum · owner 视图</h1>{nav}{_worker_badge(store)}{_header_actions()}</header>
{flash}
{_request_form()}
{_planning_section(store)}
{_review_section(store)}
<div class="art-grid">{_product_table(artifacts['product_backlog'])}</div>
{_sprint_table(artifacts['sprint_backlog'])}
{_burndown_section(artifacts['burndown'], horizon)}
{_flow_section(store, debug)}
{_activity_strip(store)}
<script>
// Poll without eating input: a blind reload would wipe a half-typed request
// or an acceptance note, and losing that text is how a review gets rubber-stamped.
setInterval(function () {{
  var tag = document.activeElement ? document.activeElement.tagName : "";
  var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag);
  var editing = document.querySelector("details[open]");
  if (!typing && !editing) location.reload();
}}, 4000);
</script>
</body></html>"""


def _render_deliverable(
    store: BoardStore,
    task: Any,
    payload: tuple[str, str] | None,
) -> str:
    """The acceptance page: a reviewer must be able to *see* the increment."""
    info = _deliverable_json(store, task.task_id)
    pbi = info["pbi"]
    evidence = info["evidence"]
    dod = info["dod_checks"]
    ac = _acceptance_list(list(pbi["acceptance"]))
    executor, prov = _provenance(task, payload[0] if payload else None)
    evidence_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td><code>{_esc(v)}</code></td></tr>"
        for k, v in sorted(evidence.items())
    ) or "<tr><td colspan='2' class='empty'>尚未提交证据</td></tr>"
    dod_rows = "".join(
        f"<tr><td>{_esc(c['check_id'])} <span class='ac'>R{c.get('check_round', 1)}</span></td>"
        f"<td class='{'pass' if c['ok'] else 'fail'}'>{'PASS' if c['ok'] else 'FAIL'}</td>"
        f"<td class='ac'>{_esc(c['output_tail'])}</td></tr>"
        for c in dod
    ) or "<tr><td colspan='3' class='empty'>还没有 DoD 记录</td></tr>"

    if payload is None:
        doc = ('<div class="empty">该任务没有登记可读产物。'
               "产物必须落在 board 的 artifacts 目录内才会被读取（防目录穿越）。</div>")
    else:
        text, path = payload
        doc = (f'<p class="kv">文件：<code>{_esc(path)}</code> · '
               f'<a href="/artifact/raw?task_id={_esc(task.task_id)}" '
               'target="_blank">下载原始文档</a></p>'
               f"<pre>{_esc(text)}</pre>")

    decision = info["review_decision"]
    if task.state == "in_review":
        verdict = f"""<form class="decision" method="post" action="/ui/action">
  <input type="hidden" name="task_id" value="{_esc(task.task_id)}">
  <input name="note" placeholder="验收备注" size="40">
  <button class="ok" name="method" value="review_accept">验收通过</button>
  <button class="no" name="method" value="review_reject">打回</button>
</form>"""
    elif task.state == "verifying":
        verdict = ('<p class="kv">DoD 有失败项，正在复核窗口中，此时不能验收。'
                   '<a href="/#review">返回看板裁决（请 SM 复验 / 打回重做 / 确认阻塞）→</a>'
                   "</p>")
    elif decision is not None:
        verdict = (f'<p class="kv">结论：<b>{_esc(decision["decision"])}</b> · '
                   f'{_esc(decision["reviewer"])} · 备注 {_esc(decision["note"] or "—")}'
                   "</p>")
    else:
        verdict = f'<p class="kv">当前状态 {_esc(task.state)}，尚不可验收。</p>'

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>交付物 · {_esc(task.task_id)}</title><style>{_CSS}</style></head><body>
<header class="top"><h1>交付物 · {_esc(task.task_id)}</h1>
<nav><a href="/">← 返回看板</a><a href="#doc">产物全文</a><a href="#dod">DoD</a></nav>
{_header_actions()}
</header>
<div class="doc">
<h2>① 当初提的需求</h2>
<p><b>{_esc(pbi['title'])}</b> · {_esc(pbi['pbi_id'])} · {_esc(pbi['points'] or 0)} pt</p>
<p class="kv">{_esc(pbi['body']) or '（无补充说明）'}</p>{ac}
<h2>② 执行记录 / 执行主体</h2>
<p class="kv">任务 {_esc(task.task_id)} · 状态 <b>{_esc(task.state)}</b> ·
认领 {_esc(task.agent_id or '-')} · 执行主体 <b>{_esc(executor)}</b> · 分支 {_esc(task.branch or '-')} ·
第 {task.attempt} 次尝试 · 规格 {_esc(task.spec or '—')}</p>
{prov}
<h2>③ 交付证据（团队提交，不可自报完成）</h2>
<table><tbody>{evidence_rows}{_artifact_path_row(task, evidence)}</tbody></table>
<h2 id="dod">④ DoD 复验</h2>
<table><thead><tr><th>检查项</th><th>结果</th><th>输出</th></tr></thead>
<tbody>{dod_rows}</tbody></table>
<h2 id="doc">⑤ 产物全文</h2>{doc}
<h2>⑥ 你的验收</h2>{verdict}
</div></body></html>"""


def _render_guide() -> str:
    """A glossary for the person who only has to ask and review.

    The board uses Scrum vocabulary because the vocabulary encodes decisions
    ("who may declare done?"). Acronyms nobody explains are just gatekeeping, so
    every term here says what it changes on *this* board.
    """
    flow = """① 提需求（owner：标题 / 背景 / 验收条件，不填故事点）
      │
      ▼
   PO 细化排序（pbi_refining → pbi_refined，new → ready）
      │
      ▼
   团队估点（dev agent）──── pbi_estimated ────▶ Burndown +pt
      │  需求被改写 → 旧估点作废（−pt）→ 重新估点
      ▼
   PO 起草 Sprint 提案（state=planned）＋ SM 校验容量
      │
      ▼
   ② owner 批准（sprint_committed：planned → active，PO 才拆任务）
      │
      ▼
   DoR 准入 ──不合格──▶ needs_revision ──▶ 回到 Review：owner 修改并重新提交
      │ 合格                                        │
      ▼                                            │
   queued → leased/running → submitted ◀───────────┘
                │                │
                │                ▼
                │            DoD 复验 ──任一 FAIL──▶ verifying（复核窗口）
                │                │                      ├─ SM 重新复验 → 新的一轮
                │                │                      ├─ owner 请 SM 再验 → submitted
                │                │                      ├─ owner 打回重做 → queued（计数归零）
                │                │                      └─ owner 确认阻塞 → blocked
                │                │ 最新一轮全部通过
                │                ▼
                │            in_review ── 验收通过 ──▶ done（PBI 燃尽）
                │                     └─ 打回 ──▶ queued（计数归零，新一轮）
                └─ 执行失败 ─────────────────────▶ blocked
"""
    glossary: list[tuple[str, str, str]] = [
        ("PBI / Product Backlog Item", "pb",
         "一条需求。Product Backlog 是它按优先级排好的全量清单，包含未进 Sprint 的需求。"),
        ("Sprint / Sprint Backlog", "sprint",
         "当前时间盒里团队承诺交付的 PBI 集合，以及从每条 PBI 拆出的执行任务与实时状态。"),
        ("故事点（估点）", "estimate",
         "相对工作量的粗刻度（1/2/3/5/8/13），由接单的 dev agent 评估，不由提需求的人填。"
         "估点带依据写进事件日志；需求改写后作废，团队按新范围重估。"),
        ("DoR · Definition of Ready", "dor",
         "准入闸门，检查两次。第一次在 PO 细化时：没有验收条件的需求直接停在 needs_revision，"
         "不估点、不进 Sprint 提案，你在 Review 面板补齐后才继续；第二次在派单时，"
         "兜住中途被删掉条件的卡，打回同一个面板，不会在队列里空转。"),
        ("DoD · Definition of Done", "dod",
         "完成闸门：检查执行者提交的证据包（做了什么 / 原始测试输出 / 变更产物），"
         "并复验产物文件确实存在且非空。每轮结果按 round 追加留痕，只有最新一轮全部 PASS "
         "才进 in_review；任一 FAIL 打开 verifying 复核窗口，不会自动打回或判死。"),
        ("verifying · DoD 复核窗口", "verifying",
         "DoD 出现 FAIL 时的中间态，不是结论。SM 可以重新复验并更正自己的误判，owner 可以"
         "「请 SM 再复验 / 打回 Dev 重做 / 确认阻塞」。历轮检查不删除，验收只认最新一轮。"),
        ("Review / 验收", "review",
         "你唯一需要动手的地方之一：看完整交付物页（原始需求、证据、DoD 每项 PASS/FAIL、"
         "产物全文），然后「验收通过」或「打回」。通过后任务才变 done，PBI 才燃尽；"
         "DoD 未全通过的卡会出现在同一个面板的复核窗口里，不会被静默重试掉。"),
        ("needs_revision · 待改需求", "revision",
         "DoR 打回状态。卡片不会自己执行，也不会消失；Review 面板给出可编辑表单，"
         "保存并重新提交后回到 queued，旧估点作废、团队重估。"),
        ("blocked · 阻塞", "blocked",
         "执行失败、重试预算耗尽、agent 主动弃权，或 owner 在复核窗口里明确「确认阻塞」。"
         "DoD 失败本身不再直接判死。板上写明原因，不会无限重试烧 token。"
         "Review 面板给两个出口：只是取证通道坏了就「让 SM 重新复验」（保留产物、不耗执行预算），"
         "产物真的不行才「打回 Dev 重做」（清零 attempt 开新一轮）。历史轮次都保留。"),
        ("lease / claim / ack", "lease",
         "claim 用 CAS 给 agent 发一张带 uuid 的租约，ack 表示真的开始执行。"
         "租约是能力令牌：只有持有当前 lease 的一方才能 submit，防止重复唤醒覆盖增量。"),
        ("WIP / in_flight_pbi_cap", "wip",
         "同时在执行的 PBI 上限。它管的是并行度与尾延迟，不是 token 成本闸门；"
         "成本闸门是每张卡自己的预算。"),
        ("review backpressure", "backpressure",
         "待验收队列达到上限时停止派单。瓶颈在验收带宽而不是产能时，继续生产只会堆积库存。"),
        ("interrupt budget / expedite", "interrupt",
         "expedite（插单）按估点消耗一定比例的中断预算，用完就不再自动放行；"
         "打断是有价格的，不是免费的优先级按钮。"),
        ("scope injection / scope creep", "scope",
         "Sprint 进行中新增需求。事件日志记 scope_injected，燃尽图抬升一个橙色台阶——"
         "范围变大是事实，不能被画成平滑曲线藏起来。"),
        ("event_log / 重放", "events",
         "唯一事实来源。所有状态列、三工件、燃尽图都是它的投影；"
         "可事后重算，能回答「当时的 DoD 是不是被放宽了」。"),
        ("Burndown chart", "burndown",
         "按事件重放的剩余故事点曲线。只有 PBI 验收完成才燃尽，agent 自称完成不算；"
         "估点 / 范围注入抬升，需求改写作废估点回落。ideal 线只是参考，不是承诺。"),
        ("dwell_seconds / review_lead_time", "metrics",
         "从卡片进入 in_review 到你按下验收按钮的时长。极短 + 极高通过率 = 橡皮图章，"
         "是判断流程是否真的在验收的关键埋点。"),
        ("L1 / L2 e2e", "e2e",
         "L1 是控制面闭环（HTTP 表单 → worker → DoD → Review → 验收），已验证，"
         "但执行器是本地占位 DemoExecutor，产物没有业务结果。"
         "L2 是真实 Octop agent 经 WebSocket 唤醒、在自己的 turn 里执行、交回证据，"
         "再由另一个 SM agent 独立复验 —— 这条已经跑通（2026-11-12 实测：dev 27s 交产出、"
         "sm 68s 复验进 Review）。两条通道由 agent_role.channel 隔离，"
         "本地占位执行器不能再借用真实 agent 的身份。"),
    ]
    terms = "".join(
        f'<dt id="{anchor}">{_esc(term)}</dt><dd>{_esc(body)}</dd>'
        for term, anchor, body in glossary
    )
    head = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>流程说明 · 术语表</title><style>' + _CSS + '</style></head><body>'
        '<header class="top"><h1>流程说明 · 术语表</h1>'
        '<nav><a href="/">← 返回看板</a><a href="#l2">L1 / L2</a>'
        '<a href="#flow">流转图</a><a href="#terms">术语</a>'
        '<a href="#faq">常见疑问</a></nav>'
        + _header_actions('<a class="guide-btn" href="/#ask">去提需求 →</a>')
        + '</header>'
    )
    body = (
        '<section class="panel guide-section"><h2>你在这个流程里的位置</h2>'
        '<p>你不在 PO / SM / TEAM 里。你有三个入口：<b>① 提需求</b>、'
        '<b>② 批准 Sprint 提案</b>、<b>③ Review</b>（验收交付物，或修改被 DoR 打回的需求）。'
        'PO 负责细化、排序、起草 Sprint 提案；dev 负责估点；SM 校验容量与 DoD；'
        '批准后 PO 才拆任务。没有你的批准，卡片只会停在 Product Backlog。'
        '认领、执行、提交证据由团队侧完成，页面默认不渲染团队按钮。</p>'
        '<p class="hint">三个观察窗口：<a href="/#pb">Product Backlog</a> · '
        '<a href="/#sb">Sprint Backlog</a> · <a href="/#bd">Burndown chart</a>。'
        '它们是事件日志的只读投影，不是另一份需要人工维护的台账。</p></section>'
        '<section class="panel guide-section" id="l2"><h2>谁在真正执行？L1 vs L2</h2>'
        '<div class="prov ok"><b>✓ 现在两条通道并存，靠 agent_role.channel 隔离</b><br>'
        '<code>local</code> 通道由常驻 worker 驱动，执行器是 <code>DemoExecutor</code>：'
        '只写模板 Markdown，不联网、不调模型、不调用 Octop agent，产物一律标「占位产物」，'
        '<b>不应验收通过</b>。<code>octop</code> 通道由派发器唤起真实 agent，'
        '本地执行器<b>不能借用</b>这条通道上的身份（有测试兜底）。</div>'
        '<p><b>L1（已验证）</b>：控制面闭环 —— 提需求 → 团队估点 → DoR → 派单 / 租约 → '
        'submit → DoD → Review → 验收，全部事件可重放。它证明的是流程状态机，不是执行力。</p>'
        '<p><b>L2（已跑通，2026-11-12 实测）</b>：派发器只推一条 pointer 给真实 agent，'
        'agent 在自己的 turn 里调 <code>POST /rpc/&lt;method&gt;</code> 完成认领与提交；'
        'dev 在 27 秒内自己查到数据、写出产物并交回证据，随后 <b>另一个 sm agent</b> '
        '独立复验 7 条 DoD 后把卡推进 Review（68 秒）。执行主体由服务端盖章'
        '（<code>octop-agent/&lt;id&gt;</code>），agent 无法自报。</p>'
        '<p class="hint">注意：由助手或 owner 自己查到答案再手工填进产物，等于伪造执行结果，'
        '不是 agent 交付。执行主体必须可追溯 —— 交付页的「执行主体」字段就是为此存在。</p>'
        '<p class="hint">仍未自动化：DoD 的判定是 agent 的判断（记录为带时间戳的 checks），'
        '不是确定性的代码闸门；「验收条件覆盖」的硬校验待实现。</p></section>'
        '<section class="panel guide-section" id="flow"><h2>状态流转图</h2>'
        '<pre class="flow-diagram">' + _esc(flow) + '</pre>'
        '<p class="hint">实线是正常路径；do&nbsp;→&nbsp;done 只可能经过你的验收，'
        'agent 不能自己宣布完成。</p></section>'
        '<section class="panel guide-section" id="terms"><h2>术语表</h2>'
        '<dl class="glossary">' + terms + '</dl></section>'
        '<section class="panel guide-section" id="faq"><h2>常见疑问</h2><ul>'
        '<li><b>为什么表单里没有故事点？</b> 因为出钱的人估点只会量出自己的信心。'
        '点数由具体干活的 dev agent 在 refinement 阶段给出，并在 Product Backlog 标注估点人。</li>'
        '<li><b>为什么我的需求出现在 Review 而不是执行中？</b> DoR 没放行（通常是验收条件为空）。'
        '在 Review 里补齐条件、点「保存并重新提交需求」，卡片会自动回到队列。</li>'
        '<li><b>改动需求后点数会怎样？</b> 旧估点作废（燃尽图回落），团队按新范围重新估点（再抬升）。'
        '这两步都记在事件日志里，可追溯。</li>'
        '<li><b>卡片一直不动？</b> 看页首 worker 状态：红色表示常驻 worker 未启动；'
        '若卡片在 Blocked，读 blocked_reason；若 Review 堆满，是 review backpressure 主动停止派单。</li>'
        '<li><b>验收时能看到什么成果？</b> 「查看完整交付物」页含原始需求、执行主体、证据、'
        'DoD 每项 PASS/FAIL、<b>产物绝对路径</b>与产物全文，并可下载原始文档。'
        '若页首出现红色「占位产物」警示，说明本卡是 L1 演示、没有 agent 执行结果，'
        '应当打回而不是验收通过（见 <a href="#l2">L1 vs L2</a>）。</li>'
        '<li><b>时间盒怎么理解？</b> Burndown 页可切换 1 小时 / 1 天 / 2 周，'
        '那是展示参数；真实 Sprint 的时间盒应落在 sprint 数据行上。</li>'
        '</ul></section>'
        '<section class="panel guide-section"><h2>机器可读接口</h2><ul>'
        '<li><a href="/api/artifacts">/api/artifacts</a> — 三工件 JSON</li>'
        '<li><code>POST /rpc/&lt;method&gt;</code> — agent 侧 RPC，角色由 '
        '<code>X-Octop-Agent</code> 服务端解析，请求体里自称 PO 无效</li>'
        '<li><code>GET /deliverable/{task_id}</code> — 完整验收页；'
        '<code>GET /artifact/raw?task_id=…</code> — 原始产物文本</li>'
        '</ul></section>'
    )
    return head + body + "</body></html>"
