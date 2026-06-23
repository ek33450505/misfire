"""test_cast_db.py — unit tests for the OPTIONAL cast.db adapter.

Builds a synthetic cast.db in ``tmp_path`` with the EXACT column layout of the
real ``~/.claude/cast.db`` (probed live 2026-06-22) and exercises every public
function of ``misfire.adapters.cast_db``.

Every assertion is falsifiable: counts, mappings, the unmapped bucket, the
safety/convertible/judgment exclusion, path sanitisation, the missing-DB empty
result, and the strict read-only invariant (no mtime change, no WAL/journal
side files).

Stdlib only. Builds its own DB — never reads the real cast.db.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from misfire.adapters.cast_db import (
    CONVERT_OUTPUT_SHAPE,
    _PROTOCOL_SIGNALS,
    _match_rule_for_signal,
    _sanitize_excerpt,
    castdb_available,
    count_agent_runs,
    find_output_shape_violations,
    read_protocol_violations,
)
from misfire.parse import Rule
from misfire.classify import (
    CATEGORY_CONVERTIBLE,
    CATEGORY_JUDGMENT_KEEP,
    CATEGORY_OUTPUT_SHAPE,
    CATEGORY_SAFETY_KEEP,
    classify_rules,
)
from misfire.parse import parse_config

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# A config that spreads the relevant categories.  The Handoff and Status rules
# classify as output_shape (the only adapter candidates); the others must NEVER
# be attributed cast.db violations.
_CONFIG_TEXT = """\
# Synthetic Test Config

MANDATORY: Every agent MUST include a Handoff block before the Work Log.

MANDATORY: All agents end with Status: DONE | BLOCKED.

MANDATORY: Never use raw git commit directly.

Avoid irreversible force-push to the main branch.

YAGNI: build only what was asked.
"""

_PII_EXCERPT = "leaked path /Users/alice/secret/notes.md in handoff"


def _write_config(tmp_path: Path) -> Path:
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "CLAUDE.md").write_text(_CONFIG_TEXT, encoding="utf-8")
    return config_root


def _build_db(db_path: Path, *, with_tables: bool = True) -> None:
    """Build a synthetic cast.db at ``db_path``.

    When ``with_tables`` is False, only an unrelated table is created so the
    required-tables check fails.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        if not with_tables:
            conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            conn.commit()
            return

        conn.executescript(
            """
            CREATE TABLE agent_protocol_violations (
                id INTEGER PRIMARY KEY, session_id TEXT, agent_type TEXT NOT NULL,
                agent_id TEXT, batch_id INTEGER, violation TEXT NOT NULL,
                pattern TEXT, timestamp TEXT NOT NULL, raw_excerpt TEXT);
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY, session_id TEXT, agent TEXT NOT NULL,
                model TEXT, started_at TEXT, ended_at TEXT, status TEXT);
            CREATE TABLE quality_gates (
                id TEXT PRIMARY KEY, session_id TEXT, agent_name TEXT,
                timestamp TEXT, status_line TEXT, contract_passed INTEGER,
                retry_count INTEGER, gate_type TEXT, created_at TEXT);
            """
        )
        conn.executemany(
            "INSERT INTO agent_protocol_violations "
            "(id, session_id, agent_type, violation, pattern, timestamp, raw_excerpt) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (1, "s-h1", "code-writer", "handoff_schema_violation",
                 "empty_field:files_changed", "2026-06-20T10:00:00Z", "files_changed: (empty)"),
                (2, "s-h2", "debugger", "handoff_schema_violation",
                 "missing_field:files_changed", "2026-06-20T11:00:00Z", _PII_EXCERPT),
                (3, "s-h3", "commit", "handoff_schema_violation",
                 "empty_field:blockers", "2026-06-20T12:00:00Z", "blockers absent"),
                (4, "s-s1", "code-writer", "missing_formality",
                 "no_status_block", "2026-06-20T13:00:00Z", "no Status line"),
                (5, "s-s2", "test-writer", "missing_formality",
                 "no_status_block", "2026-06-20T14:00:00Z", "no Status: DONE"),
                (6, "s-p1", "main", "prose_dispatch",
                 "free_text_dispatch", "2026-06-20T15:00:00Z", "prose dispatch"),
            ],
        )
        # 5 non-running + 1 running → denominator (default) = 5
        conn.executemany(
            "INSERT INTO agent_runs (session_id, agent, status) VALUES (?,?,?)",
            [
                ("r1", "code-writer", "DONE"),
                ("r2", "debugger", "DONE"),
                ("r3", "commit", "BLOCKED"),
                ("r4", "code-writer", "DONE"),
                ("r5", "test-writer", "abandoned"),
                ("r6", "code-writer", "running"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _classify(config_root: Path):
    pr = parse_config(config_root)
    classifications = classify_rules(pr.rules)
    rules_by_id = {r.rule_id: r for r in pr.rules}
    return classifications, rules_by_id


# ---------------------------------------------------------------------------
# castdb_available
# ---------------------------------------------------------------------------


def test_available_true(tmp_path: Path) -> None:
    db = tmp_path / "cast.db"
    _build_db(db)
    res = castdb_available(db)
    assert res.available is True
    assert res.reason == "ok"


def test_available_not_found(tmp_path: Path) -> None:
    res = castdb_available(tmp_path / "missing.db")
    assert res.available is False
    assert res.reason == "not_found"


def test_available_missing_tables(tmp_path: Path) -> None:
    db = tmp_path / "bare.db"
    _build_db(db, with_tables=False)
    res = castdb_available(db)
    assert res.available is False
    assert res.reason == "missing_tables"


def test_available_db_path_rel_never_raw_user_path(tmp_path: Path) -> None:
    """db_path_rel must never be a raw /Users/<name>/ path (home-collapsed)."""
    db = tmp_path / "cast.db"
    _build_db(db)
    res = castdb_available(db)
    # tmp_path is outside $HOME in CI, but assert the invariant regardless:
    home = str(Path.home())
    if str(db).startswith(home + "/"):
        assert res.db_path_rel.startswith("~/")
        assert home not in res.db_path_rel


# ---------------------------------------------------------------------------
# count_agent_runs
# ---------------------------------------------------------------------------


def test_count_excludes_running_by_default(tmp_path: Path) -> None:
    db = tmp_path / "cast.db"
    _build_db(db)
    assert count_agent_runs(db) == 5  # 6 total, 1 running excluded


def test_count_includes_running_when_requested(tmp_path: Path) -> None:
    db = tmp_path / "cast.db"
    _build_db(db)
    assert count_agent_runs(db, include_running=True) == 6


def test_count_returns_zero_on_missing_db(tmp_path: Path) -> None:
    assert count_agent_runs(tmp_path / "nope.db") == 0


# ---------------------------------------------------------------------------
# read_protocol_violations
# ---------------------------------------------------------------------------


def test_read_protocol_violations_count_and_order(tmp_path: Path) -> None:
    db = tmp_path / "cast.db"
    _build_db(db)
    rows = read_protocol_violations(db)
    assert len(rows) == 6
    # ORDER BY id → first row is id=1 handoff
    assert rows[0].violation == "handoff_schema_violation"
    assert rows[0].pattern == "empty_field:files_changed"


def test_read_protocol_violations_empty_on_missing(tmp_path: Path) -> None:
    assert read_protocol_violations(tmp_path / "nope.db") == []


# ---------------------------------------------------------------------------
# find_output_shape_violations — mapping
# ---------------------------------------------------------------------------


def test_maps_handoff_and_status(tmp_path: Path) -> None:
    db = tmp_path / "cast.db"
    _build_db(db)
    classifications, rules_by_id = _classify(_write_config(tmp_path))

    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)

    by_signal = {rv.predicate["signal"]: rv for rv in result.rule_violations}
    assert set(by_signal) == {"handoff", "status"}
    assert by_signal["handoff"].violation_count == 3
    assert by_signal["status"].violation_count == 2
    for rv in result.rule_violations:
        assert rv.convert_kind == CONVERT_OUTPUT_SHAPE
        assert rv.predicate["hook"] == "SubagentStop"
        assert rv.opportunity_count == 5  # agent_runs denominator (excl running)
        assert rv.excluded_by_exception == 0


def test_mapped_rule_ids_are_the_output_shape_rules(tmp_path: Path) -> None:
    """The handoff/status RuleViolations point at the correct output_shape rules."""
    db = tmp_path / "cast.db"
    _build_db(db)
    config_root = _write_config(tmp_path)
    classifications, rules_by_id = _classify(config_root)

    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)
    by_signal = {rv.predicate["signal"]: rv for rv in result.rule_violations}

    handoff_rule = rules_by_id[by_signal["handoff"].rule_id]
    status_rule = rules_by_id[by_signal["status"].rule_id]
    assert "handoff block" in handoff_rule.normalized_text.lower()
    assert "end with status" in status_rule.normalized_text.lower()


def test_prose_dispatch_is_unmapped_not_attributed(tmp_path: Path) -> None:
    db = tmp_path / "cast.db"
    _build_db(db)
    classifications, rules_by_id = _classify(_write_config(tmp_path))

    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)

    assert result.unmapped_by_signal == {"prose_dispatch": 1}
    assert result.total_violations_read == 6
    assert result.mapped_violations == 5
    # prose_dispatch must not have produced a RuleViolation
    signals = {rv.predicate["signal"] for rv in result.rule_violations}
    assert "prose_dispatch" not in signals


def test_safety_convertible_judgment_rules_never_returned(tmp_path: Path) -> None:
    """Only output_shape rules may be attributed; others must never appear."""
    db = tmp_path / "cast.db"
    _build_db(db)
    config_root = _write_config(tmp_path)
    classifications, rules_by_id = _classify(config_root)

    # Sanity: the config really does contain the other categories.
    cats = {c.category for c in classifications}
    assert CATEGORY_SAFETY_KEEP in cats
    assert CATEGORY_CONVERTIBLE in cats
    assert CATEGORY_JUDGMENT_KEEP in cats
    assert CATEGORY_OUTPUT_SHAPE in cats

    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)
    returned_ids = {rv.rule_id for rv in result.rule_violations}

    non_output_shape_ids = {
        c.rule_id for c in classifications if c.category != CATEGORY_OUTPUT_SHAPE
    }
    assert returned_ids.isdisjoint(non_output_shape_ids)


def test_pii_excerpt_is_collapsed_to_tilde(tmp_path: Path) -> None:
    """A /Users/alice/ path in raw_excerpt is collapsed to ~/ in input_summary."""
    db = tmp_path / "cast.db"
    _build_db(db)
    classifications, rules_by_id = _classify(_write_config(tmp_path))

    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)
    handoff = next(rv for rv in result.rule_violations if rv.predicate["signal"] == "handoff")

    summaries = [a.input_summary for a in handoff.violations]
    joined = "\n".join(summaries)
    assert "/Users/alice/" not in joined
    assert "/Users/" not in joined
    assert "~/" in joined  # the collapsed form is present


def test_synthesized_action_shape(tmp_path: Path) -> None:
    """Synthesised ToolActions carry the documented marker fields."""
    db = tmp_path / "cast.db"
    _build_db(db)
    classifications, rules_by_id = _classify(_write_config(tmp_path))

    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)
    action = result.rule_violations[0].violations[0]
    assert action.transcript_rel == "cast.db"
    assert action.tool_name == ""
    assert action.command == ""
    assert action.is_sidechain is True
    assert action.cwd_rel == ""
    assert action.git_branch is None
    assert len(action.input_summary) <= 120


def test_input_summary_truncated_to_120(tmp_path: Path) -> None:
    """A very long raw_excerpt is truncated to <=120 chars after sanitisation."""
    db = tmp_path / "cast.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE agent_protocol_violations (
            id INTEGER PRIMARY KEY, session_id TEXT, agent_type TEXT NOT NULL,
            agent_id TEXT, batch_id INTEGER, violation TEXT NOT NULL,
            pattern TEXT, timestamp TEXT NOT NULL, raw_excerpt TEXT);
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY, session_id TEXT, agent TEXT NOT NULL,
            model TEXT, started_at TEXT, ended_at TEXT, status TEXT);
        """
    )
    conn.execute(
        "INSERT INTO agent_protocol_violations "
        "(id, session_id, agent_type, violation, pattern, timestamp, raw_excerpt) "
        "VALUES (?,?,?,?,?,?,?)",
        (1, "s", "code-writer", "handoff_schema_violation", "p", "t", "x" * 500),
    )
    conn.execute("INSERT INTO agent_runs (session_id, agent, status) VALUES ('r','a','DONE')")
    conn.commit()
    conn.close()

    classifications, rules_by_id = _classify(_write_config(tmp_path))
    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)
    action = result.rule_violations[0].violations[0]
    assert len(action.input_summary) == 120


# ---------------------------------------------------------------------------
# Observer posture — missing / unreadable DB
# ---------------------------------------------------------------------------


def test_missing_db_returns_empty_no_raise(tmp_path: Path) -> None:
    classifications, rules_by_id = _classify(_write_config(tmp_path))
    result = find_output_shape_violations(
        classifications, rules_by_id, db_path=tmp_path / "does-not-exist.db"
    )
    assert result.rule_violations == []
    assert result.total_violations_read == 0
    assert result.mapped_violations == 0
    assert result.unmapped_by_signal == {}
    assert result.agent_runs_denominator == 0


def test_missing_tables_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "bare.db"
    _build_db(db, with_tables=False)
    classifications, rules_by_id = _classify(_write_config(tmp_path))
    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)
    assert result.rule_violations == []


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


def test_db_is_not_modified(tmp_path: Path) -> None:
    """The adapter must not change the DB mtime nor create WAL/journal side files."""
    db = tmp_path / "cast.db"
    _build_db(db)
    before_mtime = db.stat().st_mtime_ns

    classifications, rules_by_id = _classify(_write_config(tmp_path))
    find_output_shape_violations(classifications, rules_by_id, db_path=db)
    count_agent_runs(db)
    read_protocol_violations(db)
    castdb_available(db)

    after_mtime = db.stat().st_mtime_ns
    assert after_mtime == before_mtime

    side_files = [
        name
        for name in os.listdir(tmp_path)
        if name.startswith("cast.db") and name != "cast.db"
    ]
    assert side_files == [], f"read-only opens must not create side files: {side_files}"


def test_wal_mode_db_read_only_data_safety(tmp_path: Path) -> None:
    """A WAL-mode cast.db is read without altering its committed data or mtime.

    SQLite materialises ``-shm``/``-wal`` side files to read a WAL database even
    under ``mode=ro`` (so the old 'no side files' claim does NOT hold for WAL),
    but the committed data and the main file's mtime are untouched — the
    read-only DATA-safety property that actually matters.  This exercises the
    journal mode the production cast.db (better-sqlite3) commonly uses, which the
    rollback-journal ``test_db_is_not_modified`` fixture never covers.
    """
    db = tmp_path / "cast.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE agent_protocol_violations (
                id INTEGER PRIMARY KEY, session_id TEXT, agent_type TEXT NOT NULL,
                agent_id TEXT, batch_id INTEGER, violation TEXT NOT NULL,
                pattern TEXT, timestamp TEXT NOT NULL, raw_excerpt TEXT);
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY, session_id TEXT, agent TEXT NOT NULL,
                model TEXT, started_at TEXT, ended_at TEXT, status TEXT);
            """
        )
        conn.execute(
            "INSERT INTO agent_protocol_violations "
            "(id, session_id, agent_type, violation, pattern, timestamp, raw_excerpt) "
            "VALUES (1,'s','code-writer','handoff_schema_violation','p','t','x')"
        )
        conn.execute(
            "INSERT INTO agent_runs (session_id, agent, status) VALUES ('r','a','DONE')"
        )
        conn.commit()
        # Force a checkpoint so the committed data lives in the main file.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()

    before_mtime = db.stat().st_mtime_ns

    classifications, rules_by_id = _classify(_write_config(tmp_path))
    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)
    count_agent_runs(db)
    read_protocol_violations(db)

    # Data-safety: committed rows read correctly and the main file is unchanged.
    assert result.total_violations_read == 1
    assert db.stat().st_mtime_ns == before_mtime

    # The DB is still in WAL mode (the read did not silently rewrite the header).
    chk = sqlite3.connect(str(db))
    try:
        mode = chk.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        chk.close()
    assert mode.lower() == "wal"


# ---------------------------------------------------------------------------
# PII sanitisation — bare /Users/<name> with no trailing slash (regression)
# ---------------------------------------------------------------------------


def test_sanitize_excerpt_collapses_bare_user_path_no_trailing_slash() -> None:
    """A bare /Users/<name> (no trailing slash) must NOT leak the username.

    Regression: the embedded-home regex previously required a trailing slash, so
    'cwd: /Users/ed'-shaped excerpts leaked the username verbatim.
    """
    assert "/Users/" not in _sanitize_excerpt("cd /Users/bob then run")
    assert _sanitize_excerpt("home is /Users/charlie") == "home is ~"
    # The original (trailing-content) case still collapses correctly.
    out = _sanitize_excerpt("path /Users/alice/secret/notes.md here")
    assert "/Users/" not in out
    assert "~/secret/notes.md" in out


# ---------------------------------------------------------------------------
# Signal matching — uniqueness / ambiguity guard (handoff false-positive fix)
# ---------------------------------------------------------------------------


def _mk_rule(rule_id: str, source_rel: str, text: str) -> Rule:
    return Rule(
        rule_id=rule_id,
        source_path="/x",
        source_rel=source_rel,
        precedence_tier="t",
        section="",
        line_start=1,
        line_end=1,
        raw_text=text,
        normalized_text=text,
        imperative=True,
    )


def test_handoff_phrase_does_not_match_maxturns_rule() -> None:
    """The maxTurns rule ('…no Status/Handoff block…') must NOT win the handoff signal.

    The bare substring 'handoff block' appears in the maxTurns-symptom rule too;
    the tightened phrase 'handoff block before' is present ONLY in the real
    Handoff rule, so attribution no longer depends on a sort-order accident.
    """
    handoff_signal = next(s for s in _PROTOCOL_SIGNALS if s["key"] == "handoff")
    maxturns = _mk_rule(
        "maxt",
        "rules/working-conventions.md",
        "hitting it stops the agent silently no status/handoff block no "
        "subagentstop hook fire",
    )
    handoff = _mk_rule(
        "hand",
        "CLAUDE.md",
        "every agent must include a handoff block before the work log",
    )
    # Both candidates present → only the genuine Handoff rule matches.
    assert _match_rule_for_signal(handoff_signal, [maxturns, handoff]) is handoff


def test_match_rule_for_signal_refuses_ambiguous_attribution() -> None:
    """>1 matching rule → None (no silent sort-order pick); exactly one → that rule."""
    handoff_signal = next(s for s in _PROTOCOL_SIGNALS if s["key"] == "handoff")
    intended = _mk_rule(
        "aaaa", "CLAUDE.md", "must include a handoff block before the work log"
    )
    near_miss = _mk_rule(
        "bbbb", "AGENTS.md", "must include a handoff block before anything else"
    )
    # Ambiguity guard: two matches → refuse rather than pick by sort order.
    assert _match_rule_for_signal(handoff_signal, [intended, near_miss]) is None
    # Exactly one match → that rule.
    assert _match_rule_for_signal(handoff_signal, [intended]) is intended
    # Zero matches → None.
    other = _mk_rule("cccc", "x.md", "no relevant phrase here")
    assert _match_rule_for_signal(handoff_signal, [other]) is None


def test_no_output_shape_rules_yields_no_mapped(tmp_path: Path) -> None:
    """A config with no Handoff/Status prose → both signals unmapped."""
    db = tmp_path / "cast.db"
    _build_db(db)
    config_root = tmp_path / "cfg2"
    config_root.mkdir()
    (config_root / "CLAUDE.md").write_text(
        "MANDATORY: Never use raw git commit directly.\n", encoding="utf-8"
    )
    classifications, rules_by_id = _classify(config_root)

    result = find_output_shape_violations(classifications, rules_by_id, db_path=db)
    assert result.rule_violations == []
    # All read violations land in unmapped because no signal matched a rule.
    assert result.mapped_violations == 0
    assert result.unmapped_by_signal.get("handoff_schema_violation") == 3
    assert result.unmapped_by_signal.get("missing_formality") == 2
    assert result.unmapped_by_signal.get("prose_dispatch") == 1
