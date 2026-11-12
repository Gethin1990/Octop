"""Command line entry points: ``python -m octop.scrum <command>``.

The commands are ordered by how you actually use them on day one:

    init      create the board and register agents
    seed-demo put one sprint, one PBI and one task in it
    tick      one dispatcher pass (dry-run by default, ``--live`` to wake agents)
    run       reconcile, then loop forever
    serve     the RPC + kanban server the plugin talks to
    board     print the current projection to stdout

``tick`` defaults to the fake transport on purpose: a single dry tick is the
cheapest way to see admission decisions without spending a token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .dispatcher import Dispatcher
from .observe import OctopObserver
from .reconcile import reconcile
from .store import BoardStore
from .transport import FakeTransport
from .worker import DemoExecutor, ShellExecutor, Worker

DEFAULT_DB = Path.home() / ".octop" / "scrum" / "board.db"


def _open(args: argparse.Namespace) -> BoardStore:
    store = BoardStore(args.db)
    store.init_schema()
    return store


def cmd_init(args: argparse.Namespace) -> int:
    store = _open(args)
    for index in range(args.dev_agents):
        store.register_agent(f"dev-{index + 1}", "dev")
    store.register_agent("po", "po")
    store.register_agent("sm", "sm")
    store.register_agent("reviewer", "reviewer")
    print(f"board ready: {args.db}")
    print(f"policy: v{store.policy().version} hash={store.policy().hash}")
    print(f"agents: {store.agents_by_role('dev')}")
    return 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    store = _open(args)
    sprint_id = store.create_sprint("Sprint 0", capacity_pts=args.capacity, goal="prove the loop")
    pbi_ids = []
    for index in range(args.pbis):
        pbi_ids.append(
            store.create_pbi(
                f"Demo task {index + 1}",
                points=3,
                acceptance=["file exists", "tests pass"],
                class_of_service="standard",
            )
        )
    store.commit_sprint(sprint_id, pbi_ids)
    task_ids = [
        store.create_task(pbi_id, f"Implement {pbi_id}", spec=f"Task for {pbi_id}")
        for pbi_id in pbi_ids
    ]
    print(json.dumps({"sprint_id": sprint_id, "pbi_ids": pbi_ids, "task_ids": task_ids}, indent=2))
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Drive admissible cards all the way to ``in_review``.

    This is the "team" the owner should not have to be. Each step is separated by
    ``--delay`` seconds on purpose: a board that jumps from empty to done in one
    frame tells you nothing about whether the loop is healthy.
    """
    store = _open(args)
    artifacts = Path(args.artifacts) if args.artifacts else Path(args.db).parent / "artifacts"
    executor: Any = (
        ShellExecutor(command=args.command, cwd=Path(args.cwd), timeout_s=args.timeout)
        if args.command
        else DemoExecutor(artifacts=artifacts)
    )
    worker = Worker(store, executor, step_delay=args.delay)
    print(f"executor={executor.name} delay={args.delay}s db={args.db}", flush=True)
    if args.watch:
        print(f"watching (interval {args.interval}s) — ctrl-c to stop", flush=True)
        try:
            worker.run_forever(interval=args.interval)
        except KeyboardInterrupt:
            print("stopped", flush=True)
        return 0
    results = worker.run(max_steps=args.max_steps)
    for result in results:
        print(
            f"{result.task_id or '-':<12} {result.action:<10} {result.state:<10} {result.detail}"
        )
    if worker.denied:
        print(json.dumps({"denied": worker.denied}, ensure_ascii=False))
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    store = _open(args)
    rows = store.tasks_in_state(
        "queued", "leased", "running", "submitted", "verifying", "in_review",
        "needs_revision", "blocked",
    )
    if not rows:
        print("(empty)")
    for task in rows:
        print(
            f"{task.task_id:<12} {task.state:<10} {task.agent_id or '-':<8} "
            f"a{task.attempt} {task.title[:40]}"
        )
    print(json.dumps({"counts": store.counts_by_state(),
                      "denied": store.admit_denied_histogram()}, ensure_ascii=False))
    return 0


def _build_transport(args: argparse.Namespace) -> Any:
    if not getattr(args, "live", False):
        return FakeTransport()
    from .ws_transport import OctopWS

    return OctopWS(
        base_url=args.octop_url,
        username=args.username,
        password=args.password,
        observer=OctopObserver(args.octop_db),
    )


def cmd_plan(args: argparse.Namespace) -> int:
    """One Planning tick: PO refinement, dev sizing, Sprint proposal."""
    from .estimate import HeuristicEstimator
    from .planning import advance_planning

    store = _open(args)
    report = advance_planning(store, HeuristicEstimator())
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    store = _open(args)
    transport = _build_transport(args)
    dispatcher = Dispatcher(store, transport, dry_run=not getattr(args, "live", False))
    report = asyncio.run(dispatcher.tick())
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    store = _open(args)
    transport = _build_transport(args)
    report = asyncio.run(reconcile(store, transport))
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    store = _open(args)
    transport = _build_transport(args)
    report = asyncio.run(reconcile(store, transport))
    print(f"[reconcile] {report.summary()}", flush=True)
    if not args.live:
        print("dry-run: pass --live to actually wake agents", flush=True)
    asyncio.run(Dispatcher(store, transport).run_forever(interval_s=args.interval))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .service import create_app

    store = _open(args)
    os.environ.setdefault("SCRUM_TOKEN", args.token or "")
    uvicorn.run(create_app(store), host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m octop.scrum",
        description="Scrum control plane for Octop agents (experimental)",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="board sqlite path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create schema and register agents")
    p_init.add_argument("--dev-agents", type=int, default=2)
    p_init.set_defaults(func=cmd_init)

    p_seed = sub.add_parser("seed-demo", help="one sprint + N PBIs + tasks")
    p_seed.add_argument("--pbis", type=int, default=2)
    p_seed.add_argument("--capacity", type=float, default=20.0)
    p_seed.set_defaults(func=cmd_seed_demo)

    p_board = sub.add_parser("board", help="print the projection")
    p_board.set_defaults(func=cmd_board)

    p_worker = sub.add_parser("worker", help="pull → execute → submit → DoD, until review")
    p_worker.add_argument("--exec", dest="command", default=None,
                          help="shell command to run per card (default: demo executor)")
    p_worker.add_argument("--cwd", default=".", help="working directory for --exec")
    p_worker.add_argument("--timeout", type=float, default=300.0)
    p_worker.add_argument("--delay", type=float, default=2.0,
                          help="pause between steps so the board visibly moves")
    p_worker.add_argument("--artifacts", default=None, help="where the demo executor writes")
    p_worker.add_argument("--max-steps", type=int, default=0, help="0 = until nothing is left")
    p_worker.add_argument("--watch", action="store_true",
                          help="stay resident so cards created later still get picked up")
    p_worker.add_argument("--interval", type=float, default=3.0, help="idle poll interval")
    p_worker.set_defaults(func=cmd_worker)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--live", action="store_true", help="use the real Octop transport")
    common.add_argument("--octop-url", default="http://127.0.0.1:8088")
    common.add_argument("--octop-db", default=str(Path.home() / ".octop" / "octop.db"))
    common.add_argument("--username", default="admin")
    common.add_argument("--password", default=os.environ.get("OCTOP_PASSWORD", ""))

    p_plan = sub.add_parser(
        "plan", help="one PO/dev/SM planning tick (stops at the approval gate)"
    )
    p_plan.set_defaults(func=cmd_plan)

    p_tick = sub.add_parser("tick", parents=[common], help="one dispatcher pass")
    p_tick.set_defaults(func=cmd_tick)

    p_rec = sub.add_parser("reconcile", parents=[common], help="six-step restart reconcile")
    p_rec.set_defaults(func=cmd_reconcile)

    p_run = sub.add_parser("run", parents=[common], help="reconcile then loop")
    p_run.add_argument("--interval", type=float, default=5.0)
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="RPC + kanban server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8099)
    p_serve.add_argument("--token", default=os.environ.get("SCRUM_TOKEN", ""))
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
