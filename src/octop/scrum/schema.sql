-- Scrum control plane — storage contract.
--
-- Design rules encoded here:
--   1. event_log is the truth. Every state column is a projection of it; the
--      test suite replays the log and asserts the projection matches.
--   2. policy is versioned and append-only. "Editing the DoD" must be an
--      auditable event, otherwise you can never answer "did it get easier?".
--   3. lease_id is the capability token. It is a uuid4 minted per claim, so a
--      task that starts twice cannot accidentally write to the same branch.
--   4. All timestamps are REAL epoch seconds (arithmetic happens in SQL).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── policy ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS policy (
    version    INTEGER PRIMARY KEY AUTOINCREMENT,
    body_json  TEXT NOT NULL,
    body_hash  TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

-- ── agents ───────────────────────────────────────────────────────────────────
-- Roles are resolved from agent_id by the server. The model never states its
-- own role: it only proves which identity it is by which credential it holds.
-- ``channel`` says which executor may drive this identity, and it exists because
-- of a real incident: the local demo worker happily claimed a card as a *real*
-- Octop agent id and then produced a template file. The board then showed an
-- agent name next to work that agent never did. Splitting the registry makes
-- that impersonation impossible rather than merely discouraged:
--   local  → only ``octop.scrum worker`` (DemoExecutor/ShellExecutor) may use it
--   octop  → only the dispatcher may wake it, via a real Octop turn
--   any    → test harness only; a real install must pick one side
CREATE TABLE IF NOT EXISTS agent_role (
    agent_id   TEXT PRIMARY KEY,
    role       TEXT NOT NULL CHECK (role IN ('po', 'sm', 'dev', 'reviewer', 'host')),
    channel    TEXT NOT NULL DEFAULT 'local' CHECK (channel IN ('local', 'octop', 'any')),
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

-- ── backlog ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pbi (
    pbi_id          TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL CHECK (state IN (
                        'new', 'refining', 'needs_revision', 'ready',
                        'committed', 'done', 'parked')),
    points          REAL,
    priority_rank   REAL NOT NULL,
    class_of_service TEXT NOT NULL DEFAULT 'standard',
    sprint_id       TEXT,
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    readiness_json  TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_pbi_rank ON pbi (state, priority_rank);

CREATE TABLE IF NOT EXISTS sprint (
    sprint_id            TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    goal                 TEXT NOT NULL DEFAULT '',
    state                TEXT NOT NULL CHECK (state IN ('planned', 'active', 'closed')),
    capacity_pts         REAL NOT NULL,
    committed_pts        REAL NOT NULL DEFAULT 0,
    interrupt_budget_pts REAL NOT NULL,
    interrupts_used_pts  REAL NOT NULL DEFAULT 0,
    policy_version       INTEGER NOT NULL,
    started_at           REAL,
    closed_at            REAL,
    created_at           REAL NOT NULL
);

-- ── tasks ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task (
    task_id             TEXT PRIMARY KEY,
    pbi_id              TEXT NOT NULL,
    sprint_id           TEXT NOT NULL,
    title               TEXT NOT NULL,
    spec                TEXT NOT NULL DEFAULT '',
    state               TEXT NOT NULL CHECK (state IN (
                            'queued', 'leased', 'running', 'submitted',
                            'verifying', 'in_review', 'needs_revision', 'done',
                            'blocked', 'parked')),
    role                TEXT NOT NULL DEFAULT 'dev',
    agent_id            TEXT,
    lease_id            TEXT,
    attempt             INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    wake_failures       INTEGER NOT NULL DEFAULT 0,
    lease_expires_at    REAL,
    lease_hard_deadline REAL,
    last_wake_at        REAL,
    acked_at            REAL,
    thread_id           TEXT,
    branch              TEXT,
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    idem_key            TEXT,               -- UNIQUE enforced by index below
    budget_tokens       INTEGER NOT NULL DEFAULT 0,
    spent_tokens        INTEGER NOT NULL DEFAULT 0,
    blocked_reason      TEXT,
    retry_after         REAL,   -- backoff gate: do not re-wake before this instant
    verify_wake_at      REAL,   -- when the SM was last asked to verify the increment
    verify_agent_id     TEXT,   -- which SM was asked (so a second wake is not sent)
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_task_state ON task (state, created_at);
CREATE INDEX IF NOT EXISTS ix_task_agent ON task (agent_id, state);
CREATE INDEX IF NOT EXISTS ix_task_pbi ON task (pbi_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS ux_task_idem ON task (idem_key) WHERE idem_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS dod_check (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    check_id    TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    exit_code   INTEGER,
    duration_ms INTEGER,
    output_tail TEXT NOT NULL DEFAULT '',
    ran_at      REAL NOT NULL,
    -- A verifier may correct its own verdict inside the re-verification window.
    -- Rounds are append-only: deleting the failed rows would erase the evidence
    -- that a check once failed, which is exactly what an audit needs to see.
    -- Only the latest round may gate ``accept``.
    check_round INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_dod_task ON dod_check (task_id, id);

CREATE TABLE IF NOT EXISTS review_decision (
    task_id       TEXT PRIMARY KEY,
    decision      TEXT NOT NULL CHECK (decision IN ('accept', 'reject')),
    reviewer      TEXT NOT NULL,
    dwell_seconds REAL,   -- < 60s with a > 90% accept rate is a rubber stamp
    note          TEXT NOT NULL DEFAULT '',
    decided_at    REAL NOT NULL
);

-- ── ceremonies ───────────────────────────────────────────────────────────────
-- cron_jobs.last_status is NOT evidence that a ceremony happened: APScheduler
-- drops overlapping ticks and missed runs silently. This table is the evidence.
CREATE TABLE IF NOT EXISTS ceremony_run (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    window_key  TEXT NOT NULL,
    trigger_src TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'skipped')),
    started_at  REAL NOT NULL,
    finished_at REAL,
    note        TEXT NOT NULL DEFAULT '',
    UNIQUE (name, window_key)
);

-- ── event log ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS event_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    entity       TEXT NOT NULL,
    entity_id    TEXT,
    actor        TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_event_entity ON event_log (entity, entity_id, id);
CREATE INDEX IF NOT EXISTS ix_event_kind ON event_log (kind, id);
