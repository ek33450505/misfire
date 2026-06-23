-- seed.sql — synthetic cast.db for the misfire cast.db-adapter proof.
--
-- Builds the three tables the adapter touches, with the EXACT columns of the
-- real ~/.claude/cast.db (probed live 2026-06-22), and inserts a SMALL,
-- deterministic set of synthetic rows. NO PII, NO /Users/ or /home/ paths.
--
-- Reproduce the DB (from the repo root):
--   python3 -c "import sqlite3; c=sqlite3.connect('proof/castdb-sample/generated.db'); \
--     c.executescript(open('proof/castdb-sample/seed.sql').read()); c.commit(); c.close()"
--
-- The adapter opens this DB STRICTLY read-only; this script is the only writer.

-- ---------------------------------------------------------------------------
-- Schema (column names/types mirror the real cast.db)
-- ---------------------------------------------------------------------------

CREATE TABLE agent_protocol_violations (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT,
    agent_type  TEXT NOT NULL,
    agent_id    TEXT,
    batch_id    INTEGER,
    violation   TEXT NOT NULL,
    pattern     TEXT,
    timestamp   TEXT NOT NULL,
    raw_excerpt TEXT
);

CREATE TABLE quality_gates (
    id              TEXT PRIMARY KEY,
    session_id      TEXT,
    agent_name      TEXT,
    timestamp       TEXT,
    status_line     TEXT,
    contract_passed INTEGER,
    retry_count     INTEGER,
    gate_type       TEXT,
    created_at      TEXT
);

CREATE TABLE agent_runs (
    id         INTEGER PRIMARY KEY,
    session_id TEXT,
    agent      TEXT NOT NULL,
    model      TEXT,
    started_at TEXT,
    ended_at   TEXT,
    status     TEXT
);

-- ---------------------------------------------------------------------------
-- agent_protocol_violations
--   3 handoff_schema_violation  -> maps to the "## Handoff block" rule
--   2 missing_formality         -> maps to the "end with Status:" rule
--   1 prose_dispatch            -> NO signal -> stays UNMAPPED (honest)
-- Explicit ids keep ORDER BY id deterministic.
-- ---------------------------------------------------------------------------

INSERT INTO agent_protocol_violations
    (id, session_id, agent_type, agent_id, batch_id, violation, pattern, timestamp, raw_excerpt)
VALUES
    (1, 'sess-h1', 'code-writer', 'a1', NULL, 'handoff_schema_violation', 'empty_field:files_changed',  '2026-06-20T10:00:00Z', 'files_changed: (empty)'),
    (2, 'sess-h2', 'debugger',    'a2', NULL, 'handoff_schema_violation', 'missing_field:files_changed','2026-06-20T11:00:00Z', 'Handoff block omitted entirely'),
    (3, 'sess-h3', 'commit',      'a3', NULL, 'handoff_schema_violation', 'empty_field:blockers',       '2026-06-20T12:00:00Z', 'blockers field absent'),
    (4, 'sess-s1', 'code-writer', 'a4', NULL, 'missing_formality',        'no_status_block',            '2026-06-20T13:00:00Z', 'agent ended mid-sentence with no Status line'),
    (5, 'sess-s2', 'test-writer', 'a5', NULL, 'missing_formality',        'no_status_block',            '2026-06-20T14:00:00Z', 'no Status: DONE block found'),
    (6, 'sess-p1', 'main',        'a6', NULL, 'prose_dispatch',           'free_text_dispatch',         '2026-06-20T15:00:00Z', 'dispatched code-writer via prose, not the Agent tool');

-- ---------------------------------------------------------------------------
-- quality_gates (present so the DB resembles a real cast.db; unused by adapter)
-- ---------------------------------------------------------------------------

INSERT INTO quality_gates
    (id, session_id, agent_name, timestamp, status_line, contract_passed, retry_count, gate_type, created_at)
VALUES
    ('qg-1', 'sess-h1', 'code-writer', '2026-06-20T10:00:01Z', 'Status: DONE', 1, 0, 'contract', '2026-06-20T10:00:01Z');

-- ---------------------------------------------------------------------------
-- agent_runs: 40 non-running (opportunity denominator) + 2 running (excluded).
-- 40 >= the default min_support (30), so mapped rules clear the support floor.
-- ---------------------------------------------------------------------------

WITH RECURSIVE seq(n) AS (
    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 40
)
INSERT INTO agent_runs (session_id, agent, model, started_at, ended_at, status)
SELECT
    'sess-run-' || n,
    'code-writer',
    'sonnet',
    '2026-06-20T00:00:00Z',
    '2026-06-20T00:01:00Z',
    CASE WHEN n % 10 = 0 THEN 'BLOCKED' ELSE 'DONE' END
FROM seq;

INSERT INTO agent_runs (session_id, agent, model, started_at, ended_at, status)
VALUES
    ('sess-run-r1', 'code-writer', 'sonnet', '2026-06-20T00:02:00Z', NULL, 'running'),
    ('sess-run-r2', 'debugger',    'sonnet', '2026-06-20T00:03:00Z', NULL, 'running');
