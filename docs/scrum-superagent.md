# Scrum control plane (`octop.scrum`)

Experimental. Side-car only: nothing in Octop's request path imports this package,
and the dashboard does not know it exists.

## Why it is not a plugin

`~/.octop/plugins/*` was the obvious place and it does not work, for three source-level
reasons:

| Constraint | Consequence |
|---|---|
| Plugin hot reload re-executes the module (`/api/plugins/reload` → `load_installed` → `reload_all`) | a queue living inside a plugin dies silently on reload |
| `PluginContext` exposes only `tool / middleware / channel / skills` | no HTTP routes, no cron registration |
| The dashboard under `src/octop/dashboard/` is a compiled Vite bundle | no page or menu can be injected; a board must be its own server |

So the board runs as a separate process and Octop is used purely as an executor.

## Division of labour

```
Scrum          decides what may be worked on   (Product Backlog → Sprint → Ready task)
Super-agent    decides who picks it up         (admit → claim → wake → observe)
octop.scrum    owns the durable record both of them need
```

## Layout

| File | Role |
|---|---|
| `constants.py` | task states, classes of service, admission reasons — one source of truth |
| `schema.sql` | entire storage contract, reviewable in one screen |
| `store.py` | `BoardStore`, the only writer; CAS claim, lease, evidence, `done` only via `accept` |
| `policy.py` | versioned + hash-checked knobs; refuses values that disable a gate |
| `transport.py` | `Transport` protocol, `pointer_text`, `FakeTransport` |
| `ws_transport.py` | real Octop transport: login + dashboard WebSocket, ack-only wait |
| `observe.py` | read-only probe over `octop.db` (progress, not liveness) |
| `dispatcher.py` | expire → drop → stall → wake, with per-task backoff |
| `worker.py` | the local team: estimate → pull → execute → submit → DoD, until review |
| `estimate.py` | team-owned sizing (Estimator protocol + deterministic heuristic stand-in) |
| `planning.py` | PO refinement, dev sizing, PO/SM Sprint proposal, owner approval gate |
| `reconcile.py` | six-step restart reconciliation |
| `reports.py` | three artefacts: Product/Sprint Backlog + event-log burndown and its SVG |
| `service.py` | RPC + owner console: 3 entries (ask / approve / accept), 3 artefacts, deliverable/raw pages, `/guide` glossary; team moves only under `?debug=1` |
| `cli.py` | `python -m octop.scrum …` |

Plugins in `plugins/`:

| Plugin | Kind | Role |
|---|---|---|
| `scrum-board` | `tool` | the agent's verbs: `board_claim / heartbeat / submit / block / view / estimate / backlog_add` |
| `scrum-guard` | `hook` | scope gate via `awrap_tool_call`: no commit, no push, read-only roles stay read-only |

### The scope gate

The board can refuse a call, but only after the agent already decided to make it.
`scrum-guard` runs *before* the tool executes, so the refusal costs one failed
tool result instead of a side effect someone has to clean up.

Three rules, in this order:

| Rule | Applies to | Why here |
|---|---|---|
| Role deny-list | `po` and `reviewer` cannot call `write_file` / `edit_file` / `delete` / `execute`; `po` also loses `task`. `sm` keeps `execute` and loses only the write tools | `task` spawns a sub-agent whose tool calls this middleware never sees — leaving it open would make the deny-list decorative. The SM is *not* read-only: DoD verification is commands and RPC calls, and denying `execute` turned every review into `blocked / tooling_blocked` (T-4c39edf4) |
| Destructive patterns | `git commit\|push\|rebase`, `reset --hard`, `--no-verify`, `--force`, `rm -rf /` — only on `execute` / `write_file` / `edit_file` / `delete` | A global substring match rejected honest DoD text that merely *mentioned* `git commit`; the tool that can run the command is the honest scope |
| Protected paths | `/.git/`, `/.ssh/`, `/.octop/secrets`, `/.octop/config.json` on every filesystem tool | Reading a private key is as damaging as writing one |

The deny-list is applied per role directly. It used to sit behind
`read_only_roles`, which conflated "must not edit product files" with "must not
run anything" and made the SM unable to read its own evidence.

The role is never configured in `guard.json`: `POST /rpc/whoami` on the board is
the only source, so revoking a role on the board takes effect within the cache
TTL (60 s). Two deliberate failure modes:

- **Board unreachable** → replay the last known role from
  `~/.octop/scrum-guard-roles.json` for up to 15 minutes, then fail open with a
  warning. A board outage must not brick the fleet, and it must not silently
  promote a read-only PO either.
- **Loopback bypasses `HTTP_PROXY`.** The gateway process inherits an outbound
  proxy; routing the role lookup through it turns "unknown role" into permanent
  fail-open.

It is a scope gate, not a security boundary — a shell one-liner or a sub-agent
can still reach the filesystem. That is why `task_submit` independently requires
a valid lease on the board side.

## Run it

**Prerequisite:** the `octop` package needs `octop-harness>=1.0.9`. A stale
`.venv` (e.g. 1.0.0) fails at import time with
`ModuleNotFoundError: octop_harness.backends.postgres`, which looks like a code
bug but is a dependency skew. Fix with `uv sync --frozen` before starting.

```bash
# 0. make the environment match the lock file
uv sync --frozen

# 1. board + agents
python -m octop.scrum --db ~/.octop/scrum/board.db init --dev-agents 2
python -m octop.scrum --db ~/.octop/scrum/board.db seed-demo --pbis 3

# 2. one dry pass — read-only: reports would_wake, writes nothing
python -m octop.scrum --db ~/.octop/scrum/board.db tick
python -m octop.scrum --db ~/.octop/scrum/board.db board

# 3. the RPC server the plugin talks to
SCRUM_TOKEN=$(openssl rand -hex 16) python -m octop.scrum --db ~/.octop/scrum/board.db serve --port 8099

# 3b. the local team — cards you create now reach review on their own
python -m octop.scrum --db ~/.octop/scrum/board.db worker --watch --delay 2

# 4. reconcile, then loop with the real transport (L2: real agents instead of the worker)
OCTOP_PASSWORD=… python -m octop.scrum --db ~/.octop/scrum/board.db run --live --interval 5
```

Install the plugins by copying the directories into `~/.octop/plugins/` and calling
`POST /api/plugins/reload`; set `board_url` / `board_token` in the agent's tool
configuration (`config_fields` on `board_claim`).

## Who does what (the owner has exactly three jobs)

The owner is **not** a PO, SM, or team member. They fund the work, approve the
commitment, and accept the increment; they never route cards. Anything else
turns the owner into a router with extra steps.

| Role | Owner does | Agents do |
|---|---|---|
| Ask for work | state the request + acceptance criteria | — |
| Refine / order Backlog | — | PO: `pbi_refining` → `pbi_refined`, `priority_rank` |
| Size the request | **nothing — no points field exists** | a `dev` agent estimates, with a rationale in the event log |
| Draft a Sprint | — | PO proposes a `planned` Sprint; SM checks capacity |
| **Approve the commitment** | **the only path `planned → active`** | PO decomposes the approved PBIs into tasks |
| Refine a rejected request | edit + resubmit from Review | invalidate the old estimate, re-estimate on the next tick |
| Pull, execute, submit | — | `octop.scrum worker` (or a dispatched agent) |
| DoD | — | the SM verifier; a FAIL opens the `verifying` re-verification window (multi-round, owner decides) |
| Accept or reject | **the primary buttons on the page** | — |
| Observe progress | read the three artefacts + `/guide` | — |

`POST /ui/new_card` creates a PBI in state `new` and stops. `planning.py` runs
one idempotent tick on every board render (and via `octop.scrum plan`): the PO
refines `new → refining → ready`, a dev sizes it, the PO drafts a `planned`
Sprint and the SM records a capacity check. **No task exists until the owner
posts `/ui/approve_sprint`.** A second request while a Sprint is active is a PO
scope injection (`scope_injected`), not a second approval round.

The HTTP console fixes the actor to `owner` server-side. There is no identity
picker, and a forged `actor=po` in a form is ignored (tested). A forged
`points=99` is ignored too: `POST /ui/new_card` calls
`create_pbi(points=None, state="new")`, and the Planning tick calls
`estimate_pbi(..., actor=dev-N)` **before** the Sprint commitment, so
`sprint_committed` / `scope_injected` carry the real size and the burndown starts
from a truthful baseline. Team-side moves are **not rendered at all** by default
— they only appear with `?debug=1`, purely to unstick a wedged card.

So the intended loop is: **say what you want → approve the Sprint proposal →
watch the three artefacts → accept it.** DoR is checked **twice**, and the first
check is now the one that matters: PO refinement sends a request with no
acceptance criteria to `needs_revision` instead of `ready`, so it is never sized
and never proposed for a Sprint. The owner sees it in the Review panel with an
editable form before any commitment exists. `POST /ui/revise` saves
title/body/acceptance and puts it back in `ready`; from there the normal tick
sizes it and either injects it into the active Sprint or drafts the next
proposal.

The second check is admission, and it still exists: a card whose criteria were
removed mid-flight (or a task created before the hold existed) goes
`queued → needs_revision` with `dor_fail`, also in the Review panel. Either shape
lands in the same place, so the owner has one panel to read rather than two.

`/guide` (top-right button on every page) explains DoR, DoD, WIP, lease,
scope injection, story points, `needs_revision` and the whole state machine in
Chinese. It is static HTML, no JS or CDN.

## The three artefacts

`reports.py` computes all three as **read-only projections** of `pbi` /
`sprint` / `task` / `event_log`. There is no second backlog table to drift.

| Artefact | Source | What it answers |
|---|---|---|
| **Product Backlog** | `pbi` ordered by `priority_rank` | what was asked, in what order, the point size **and which dev agent estimated it**, which have acceptance criteria |
| **Sprint Backlog** | active `sprint` + its `pbi` + decomposed `task` rows | what the team committed to, per-task state / owner / attempt, progress % |
| **Burndown chart** | `event_log` replay | remaining points over time, ideal line, scope creep and re-estimates |

The burndown is replayed, not sampled into a metrics table. Five point-accounting
event kinds matter: `sprint_committed` fixes the baseline, `scope_injected`
raises it, `pbi_done` lowers it, `pbi_estimated` adjusts it when the team sizes
(or re-sizes) a request, and `estimate_invalidated` subtracts the old size when
the owner rewrites a request. Zero-point injections are skipped — the requester
no longer supplies points, so injecting an unsized PBI is not a fact about
scope. Task-state events are deliberately ignored — in Scrum a developer's
self-report does not burn down a point, an accepted PBI does. Upward steps show
as orange markers; that is the evidence of scope creep / new estimates, not a
drawing error.

The ideal line uses a display timebox (`?horizon=3600|86400|1209600`, adaptive
by default). A real sprint should store its timebox on the sprint row; keeping
it as a view parameter is an explicit, documented shortcut, not a claim that a
sprint has no timebox. The SVG is generated server-side with no CDN or JS.

JSON equivalent: `GET /api/artifacts?horizon=3600`, or the RPC read methods
`product_backlog` / `sprint_backlog` / `burndown` / `deliverable`.

## Acceptance means seeing the increment

A status column is not a deliverable. Every card in `in_review` renders its
acceptance criteria, submitted evidence and DoD result inline, plus:

- `GET /deliverable/{task_id}` — the full acceptance page: original request,
  spec, every evidence key, every DoD check, the **full text of the produced
  artefact**, the review decision, and the accept/reject form.
- `GET /artifact/raw?task_id=…` — the raw document for download.

Artifact reads are confined to `SCRUM_ARTIFACTS` (default `<db dir>/artifacts`)
by `commonpath`, restricted to text suffixes and 512 KiB. A task claiming a
path outside that root gets a 404, not a file (tested).

Acceptance records `reviewer=owner`, the note, and `dwell_seconds` measured
from when the card entered `in_review` — `review_lead_time` is the metric that
separates a review from a rubber stamp.

```bash
python -m octop.scrum --db … init --dev-agents 2
python -m octop.scrum --db … serve --port 8099        # the board
python -m octop.scrum --db … worker --watch --delay 2 # the team
```

`--watch` matters: without it the worker exits as soon as the queue drains, so a
card you create five minutes later never moves. `--delay` is deliberate too — a
board that jumps from empty to done in one frame tells you nothing about whether
the loop is healthy.

The worker is a real executor, not a status flipper: `--exec "pnpm test"` runs the
card's command and submits its actual output, and `DodRunner` re-checks the
artefact on disk instead of trusting the evidence dict. Executor failure → the
card is **blocked with a reason**, never retried forever. A DoD FAIL no longer
blocks it: it opens the `verifying` window so the verifier can correct its own
verdict and the owner can re-verify, rework or escalate.

## Two e2e levels (and which one proves what)

The word "end to end" hides two very different tests. Keeping them apart is the
only way to know what a green run actually proves.

| Level | Who drives | What is real | What it proves | Needs Octop account |
|---|---|---|---|---|
| **L1 — local loop** | you + `worker` | the board DB, the worker, the state machine, the DoD gate, the event log | a request becomes a reviewed increment without human routing | no |
| **L2 — agent round trip** | a dispatched Octop agent | L1 + the dispatcher + WebSocket + the board RPC called *inside* a real turn | a real agent can be woken, do the work itself, and hand back verifiable evidence | yes |

**L1 is the one you run first, and it is a genuine e2e of the control plane.**
It is not a mock: the worker posts the same `task_claim` / `task_submit` /
`dod_record` transitions the agents use, through the same store, into the same
`event_log`. What it does not exercise is Octop itself.

**L1 does not produce an increment.** The default executor is `DemoExecutor`: it
writes a skeleton file, and the DoD only re-checks that the file exists and is
non-empty. `dev-1` in the evidence is a board lease identity, not an Octop agent
that ran a turn. A card created this way therefore has **no business result** —
ask it for three cities' weather and the artefact contains none. That is not a
defect in the harness; it is the boundary of the control plane, and conflating
the two is how a demo starts lying to its owner.

Because a template must never be mistaken for delivered work, every artefact now
carries `PLACEHOLDER: not executed by an Octop agent`, the evidence declares
`executor=demo/placeholder`, and the board renders a red "占位产物" banner plus
the artefact's absolute path on both the Review card and the deliverable page.
Provenance is decided by the artefact's **content**, not by a self-reported
label: an executor claiming `octop-agent/dev-2` while writing the demo skeleton
is reported as "执行主体声明与产物内容矛盾" instead of being believed.

Corollary: the owner (or an assistant acting for them) must not look the answer
up and paste it into the artefact. That is forging an execution result, not an
agent delivery. If a card shows the placeholder banner, the honest actions are
"打回" and fixing the L2 wiring — never "验收通过".

### L1 — the loop, timed

Measured on the demo board (`--delay 2`), one card from creation to review:

```
t+0.0s  queued      -
t+1.5s  running     dev-1
t+3.5s  submitted   dev-1
t+4.5s  in_review   dev-1
```

The activity strip under the columns shows the same thing as events
(`task_created → task_claimed → task_acked → task_submitted → task_review_queued`),
which is the difference between watching a system and watching a screenshot.

Behind the drawer, every transition is also clickable by hand. That path exists
for unblocking a wedged card, not for normal use; `tests/unit/scrum/test_ui.py`
covers it so a regression breaks a test rather than your demo.

### L2 — the same thing with real agents (verified 2026-11-12)

L2 needs three real Octop agents and a dispatcher that can log in:

```bash
# one tick, or `run --live` to loop
python -m octop.scrum --db … tick --live \
    --username SuperAgent --password "$OCTOP_PASSWORD"
```

The two identities are separated by `agent_role.channel`:

| channel | who may drive it | why |
|---|---|---|
| `local` | `octop.scrum worker` (DemoExecutor / ShellExecutor) | a demo run must not wear a real agent's name |
| `octop` | the dispatcher, via a WebSocket wake into a real turn | real work, real tokens, real transcript |
| `any` | test harness only | one double standing in for both sides |

Measured run — one owner request, three agents, no human in the middle:

| t | state | who | what really happened |
|---|---|---|---|
| 0s | `queued` | — | owner posted "查北京上海广州今天的实况天气" with 3 acceptance criteria |
| +0s | `running` | `NE09TY` (dev) | dispatcher admitted, claimed, and woke the agent over WS with a pointer only |
| +27s | `submitted` | `NE09TY` | agent called Open-Meteo itself (HTTP 200), wrote a 1.9 KB report + 2 KB raw JSON into its own workspace, and submitted the command, the URL and the exit code as evidence |
| +68s | `in_review` | `327P52` (sm) | dispatcher woke the *verifier*, a different agent, which read the artefact and recorded 7 DoD checks: `artifact_exists / answers_request / acceptance_met / evidence_traceable / data_fidelity / no_placeholder / honest_scope` |

The report contains 北京 6.0 °C / 体感 4.4 °C / 湿度 90 %, 上海 16.9 / 17.8 / 86 %,
广州 23.2 / 23.5 / 53 %, the request URL, the model grid points actually used, and
a caveat that these are numerical-model estimates rather than station observations.
Nothing in it was typed by the assistant: the only thing the control plane sends is
a pointer, and the only thing it stores afterwards is what the agent submitted.

Two failure modes this run pinned down:

* **The verifier must be a different mind.** Before the verify hop existed, the
  same process submitted *and* ran DoD, so "done" was self-report with a checklist.
  `Dispatcher._verify_ready` now wakes an `sm`-channel agent per `submitted` card,
  with a cooldown so a 2 s tick cannot fire an SM turn every 2 s, and with the ask
  written *before* the wake so a crash cannot re-ask.
* **The executor is stamped, not declared.** `task_submit` overwrites
  `evidence.executor` from the credential and the identity's channel. A model can
  no longer label a template `octop-agent/xyz`, and a local executor can no longer
  attach a skeleton to a real agent id (that is why `channel` exists at all — it
  happened once on this board).

## Verified end-to-end on a real process

Run against an isolated `OCTOP_HOME` and port so a running instance is untouched:

| Step | Result |
|---|---|
| `OCTOP_HOME=/tmp/… OCTOP_PORT=18088 octop run` | `/api/health` → `200`, `users_loaded=1` (~26 s cold start) |
| `octop plugin install plugins/scrum-board` + `scrum-guard` | `PluginManager.load_installed()` → 2 loaded, 6 tools on `scrum-board` |
| `python -m octop.scrum serve --port 8099` | owner page (2 entries + 3 artefacts) + `POST /rpc/<method>` with role checks |
| console form → board | `POST /ui/new_card` → `303` → queued (no points from owner) → `pbi_estimated` by dev agent → worker auto `running / submitted / in_review` in ~3s |
| DoR hold at refinement | `POST /ui/new_card` with empty acceptance → one Planning tick → `pbi.state=needs_revision`, **no** `pbi_estimated`, **no** `sprint_proposed`, no task → visible in Review → `POST /ui/revise` with criteria → `ready` → sized → committed/injected |
| DoR rejection at admission | a task whose PBI lost its criteria → `deny_admission` writes `queued → needs_revision` (same Review panel), and the denied event stops repeating |
| acceptance page | `GET /deliverable/{task_id}` → real artefact text + DoD checks; `GET /artifact/raw` → document |
| glossary | `GET /guide` → DoR / DoD / WIP / lease / 估点 / 状态流转图；每页右上角按钮可进入 |
| three artefacts | `GET /api/artifacts?horizon=3600` → Product/Sprint Backlog + replayed burndown |
| plugin tool → HTTP board | `board_claim` (re-claim returns the **same** lease) → `board_heartbeat` → `board_submit` → `dod_record` (sm) → `review_accept` (reviewer) → `done` |
| `tick` (dry) | fills `would_wake` only; board state and `event_log` unchanged |
| `tick --live` without credentials | `401 AUTH_FAILED` → both cards back to `queued`, `wake_failures=1` |
| **L2** `POST /api/agents` (real account) | `PO`=`TYD8PH`, `SM`=`327P52`, `Dev`=`NE09TY`, all `state=running`, model `omniroute/cbcn/deepseek-v4.1-flash` |
| **L2** agent ↔ board | the Dev agent ran `curl` against `POST /rpc/board_view` inside its own turn and returned the live board JSON — no plugin install needed, the RPC surface is enough |
| **L2** full loop | owner card → dispatcher WS wake → dev executes (real HTTP call, real files) → `submitted` → verify wake → SM records 7 DoD checks → `in_review`, 68 s wall clock |
| **L2** provenance | `evidence.executor=octop-agent/NE09TY` is stamped by `task_submit` from the credential; the deliverable page shows the real report text and no placeholder banner |
| **L2** scope gate, live agent | a real turn on `TYD8PH` (role `po`) called `write_file` → tool result `status=error` with the guard's refusal, `/tmp/scrum-guard-live.txt` never created; the same turn on `NE09TY` (role `dev`) wrote the file normally |
| **L2** scope gate, SM | after the deny-list was fixed, a real turn on `327P52` (role `sm`) ran `echo sm-execute-ok` and got the output back; the same role is still refused `write_file` |
| **L2** blocked recovery | `T-4c39edf4` was `blocked / tooling_blocked`; `POST /ui/action task_reverify` → `submitted` with the original evidence intact → dispatcher `verify_woken` on `327P52`. No dev turn was spent re-doing correct work |

Two findings from that run, both fixed here:

1. **`tick` without `--live` used to claim leases.** A diagnostic pass left cards
   in `leased`, indistinguishable from a working agent, until `ack_timeout_s`
   expired. It is now a read-only preview (`Dispatcher(dry_run=True)`) that writes
   no lease and no event rows.
2. **`submit` does not reach `in_review` by itself.** `dod_record` must run
   first; the agent must not certify its own work. The earlier manual seam is
   closed by `Worker` / `DodRunner`: `worker --watch` drives
   `claim → execute → submit → DoD → in_review` with no human in the middle.

## Invariants the code enforces

1. **`done` is reachable only from `in_review` via `accept`.** An agent's "I finished"
   costs no tool call, so no guard can catch it — completion has to be a positive
   action (`board_submit`) plus a review.
2. **One live task per agent identity.** Octop serialises turns per session and a
   session binds one thread, so an agent *is* a concurrency slot. Want WIP=3 →
   three agents, not three threads.
3. **`lease_id` is a capability.** Submit without the current lease is refused, so a
   duplicate or zombie worker cannot overwrite a newer increment.
4. **Interruptions are priced, not forbidden.** `expedite` cards spend
   `sprint.interrupt_budget_pts`; when the budget is gone they degrade to normal
   queuing instead of breaking the rule quietly.
5. **Admission denial is recorded with a reason.** `review_backpressure` vs
   `pbi_wip` vs `dor_fail` is the difference between "add reviewers", "add agents"
   and "write better cards". The two self-clearing reasons stay in `queued`; a
   `dor_fail` is **acted on**, not just counted: `deny_admission` writes
   `queued → needs_revision` with the reason attached, so the missing input
   becomes the owner's next action in the Review panel. Both executors call that
   one method — the L2 dispatcher used to record the denial and move on, which
   left the card re-checked every tick in a column nobody reads.
6. **Every mutation appends to `event_log`**, and the test suite replays the log to
   check the projection — otherwise "the board says X" is folklore.
7. **Story points come only from a `dev` agent.** The owner form has no points
   field; `POST /rpc/pbi_estimate` is role-gated to `dev`; `pbi_created` carries
   `points=null` and `pbi_estimated` records actor + rationale + delta. Rewriting
   a request invalidates the size instead of silently keeping it.
8. **DoR fires before the commitment.** PO refinement sends a request with no
   acceptance criteria to `pbi.state=needs_revision`, so it is never sized and
   never proposed for a Sprint; `planning.advance_planning` only sizes and
   proposes `ready` PBIs. The owner is asked in the Review panel while nothing
   has been committed yet. `revise_request` is the only way back (to `ready`),
   and only with at least one criterion. The admission-time check still exists
   for tasks created before the hold: `queued → needs_revision` is logged,
   replayable, and rendered in the same panel, so there is one place to look.
9. **A request is not a commitment.** `/ui/new_card` only creates a `new` PBI.
   `planning.advance_planning` stops at a `planned` Sprint; `create_task` is
   reachable only through `approve_sprint`, which commits with `actor=owner`.
   PO refinement, dev sizing and the SM capacity check are separate, attributed
   events (`po` / `dev-N` / `sm`) — the owner never performs them.
10. **A failed DoD is evidence, not a verdict.** `record_dod` appends a numbered
    round to `dod_check` instead of deleting or overwriting history; a FAIL moves
    `submitted → verifying` rather than releasing the card into an unattended
    retry loop. The SM may record a new round from `verifying`, and the owner may
    `recheck` (back to `submitted`, verifier wake cleared), `rework` (back to
    `queued`, attempt budget reset) or `escalate` (explicit `blocked`).
    `accept` counts **only the latest round**, so a corrected verdict is
    acceptable while the original FAIL remains in the audit trail. A human
    `review_reject` or `reopen` also resets the attempt budget: the cap exists to
    stop an unattended loop, not to override an active owner.
11. **A read-only role has no write tool to forget to avoid.** `scrum-guard` reads
    the role from the board and refuses the tools in `deny_tools_by_role` before
    the call runs; `po` also loses `task`, because a sub-agent's tool calls are
    invisible to this middleware. The SM keeps `execute` on purpose: it must run
    verification commands, and refusing them turns the review itself into the
    blocker. The scope gate reduces noise and accidents — the server-side lease
    check is what makes a forged submit impossible.
12. **`blocked` has two exits, and they are not interchangeable.**
    `task_reverify` puts the card back in `submitted` with the same evidence
    (the inspection failed); `task_reopen` returns it to `queued` with a fresh
    attempt budget (the work failed). Both are owner actions in the Review panel,
    not debug-only controls. Collapsing them into one button either wastes an
    execution on a tooling hiccup or hides a genuinely bad artefact.

## What is *not* built

Honest list, so nobody assumes otherwise:

- **PO / SM are orchestrated roles, not independent expert agents.** The design
  note (F5) asks for standalone expert agents with their own tool sets. This
  implementation models the boundary in `planning.py`: each transition is
  attributed to `po` / `dev-N` / `sm` and goes through the same store methods an
  agent would call, but the decisions are deterministic (heuristic sizing,
  priority-order candidates, capacity arithmetic). Swapping in real Octop
  expert agents changes the decision, not the state machine or the approval gate.
- **The local `DodRunner` is shallow.** It re-checks the artefact on disk and the
  required evidence keys; it does **not** do an independent clone, `git clean`, or
  run the project's test suite in a sandbox. That path only runs for `local`
  identities now, and every artefact it writes is labelled unproven — the shallow
  check can no longer certify anything that claims to be real work.
- **Acceptance coverage is judged by an agent, not by code.** The SM agent did
  check the acceptance criteria (`acceptance_met`, `data_fidelity`,
  `no_placeholder`), but that judgment is a model's, recorded as DoD checks with
  timestamps and outputs. A deterministic checker — parse the acceptance list,
  require each entity to appear in the artefact — is still not written.
- **No ceremony scheduler.** `ceremony_start/finish` exist and are idempotent on
  `(name, window_key)`, but the cron jobs that call them are not wired here.
- **The WebSocket happy path is verified, its edge cases are not.** A real login
  plus a real agent turn now works (see the L2 table): `steer.ack` is accepted,
  the turn runs, and the pointer text is all the agent receives. Still unproven:
  `turn_active` rejection (`steer.ack {status:"rejected"}`) against a genuinely
  busy agent, and token renewal over a dispatcher that runs longer than the 24 h
  TTL.
- **No metrics materialisation.** The SQL lives in `store.py`
  (`admit_denied_histogram`, `counts_by_state`, `review_depth`); the ETL that would
  join `octop.db` `usage_log` / `trajectory_events` is not written.
- **No multi-user scoping.** `agent_role` has no team or tenant column yet.

## Tests

```bash
python -m pytest tests/unit/scrum -q
```

185 tests: board invariants, dispatcher accounting (backoff, dropped wakes, stalls,
denials, dry-run read-only preview), the worker (queued → review in one step, DoR
refusal → `needs_revision`, executor failure → blocked, a lying executor caught by
the DoD artifact check, agent reuse, a `dor_fail` handed back to the owner instead
of being re-checked every tick), the PO-side hold (`test_planning.py`: an empty
acceptance request is never sized or proposed, the hold is idempotent across
renders, releasing it sizes and proposes exactly once; `test_store.py`: a board
created before the state existed is rebuilt and keeps its rows), the DoD
re-verification window
(`test_verification.py`: append-only rounds, a corrected verdict passes `accept`
while the original FAIL stays in the audit trail, recheck/rework/escalate, reopen
with a fresh attempt budget, a human reject not consuming it, and
`task_reverify` returning a tooling-blocked card to `submitted` with its evidence
intact), reconciliation
(adopt / release / hold), RPC
role enforcement, the plugin contract, the console (owner sees only accept/reject;
team controls hidden unless `?debug=1`), one closed loop
(dispatcher → pointer → claim → submit → DoD → accept → `done`), plus
`test_reports.py` (three artefacts, priority ordering, scope-creep replay, ideal
line, SVG), `test_deliverables.py` (deliverable page with real artefact text,
path traversal refusal, `owner` attribution, note + `dwell_seconds`,
`/api/artifacts`, RPC deliverable view), and `test_estimate.py` (team-owned
sizing, forged points ignored, RPC `pbi_estimate` is dev-only, DoR → Review →
edit → resubmit → re-estimate → Review, burndown estimate/invalidate replay,
legacy-DB CHECK migration, `/guide` glossary and header buttons), and
`test_provenance.py` (the placeholder artefact declares itself, a card with no
declared executor is treated as unproven, a *faked* `octop-agent/dev-2` claim is
exposed by the file's own placeholder marker, the Review/deliverable pages show
the warning plus the artefact's absolute path, and `/guide` states the L1/L2 gap
and forbids pasting a hand-looked-up answer into an artefact), `test_channels.py`
(a local identity is never woken by the dispatcher, an Octop identity is, the
local worker refuses to claim an Octop id and writes nothing, re-registration
moves an id between channels), and `test_verify.py` (submitted work goes to an
`sm`, never back to the executor; one ask per cooldown; no SM means a visible
wait rather than a self-check; dry-run spends nobody's turn), and `test_guard.py`
(read-only roles lose every mutating tool including `task`, `dev` keeps them, the
destructive patterns fire on `execute`, honest evidence mentioning `git commit`
is *not* rejected, protected paths cover `read_file` as well as writes, a board
outage replays the cached role until the grace window expires, loopback ignores
`HTTP_PROXY`, and `setup()` registers exactly one middleware at priority 50).

Also green on this branch: `ruff check` and `mypy --strict` over
`src/octop/scrum` (15 files).

Latest real-process L1 run (2026-11-12): `POST /ui/new_card` →
`running (dev-1)` → `submitted` → `in_review` in ~3s, artefact written and
readable via `/deliverable/*` and `/artifact/raw`.

Note: `--basetemp=$(mktemp -d)` is needed in this sandbox because the file broker
rejects `mkdir` on an existing directory, which breaks pytest's tmp factory.
