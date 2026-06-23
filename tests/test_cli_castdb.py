"""test_cli_castdb.py — CLI tests for the OPTIONAL --cast-db flag.

Covers the three contract points of Phase 2 Unit 4 wiring:

1. ``rank ... --cast-db <db>`` injects the Handoff + Status output_shape rules
   into ``ranked`` as ``enforce_candidate`` and emits a top-level ``cast_db``
   provenance object.
2. ``rank ... --cast-db <missing>`` prints a notice to STDERR and still exits 0
   with transcript-only results (no ``cast_db`` object).
3. WITHOUT ``--cast-db`` the output has no ``cast_db`` object and no
   output_shape rules, and the DB file is never opened (mtime unchanged).

Plus an ``evidence`` drill-down that surfaces the synthesised cast.db evidence.

Stdlib only. Builds its own synthetic DB — never reads the real cast.db.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from misfire.cli import main

_CONFIG_TEXT = """\
# Synthetic Test Config

MANDATORY: Every agent MUST include a Handoff block before the Work Log.

MANDATORY: All agents end with Status: DONE | BLOCKED.

MANDATORY: Never use raw git commit directly.
"""


def _write_config(tmp_path: Path) -> Path:
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "CLAUDE.md").write_text(_CONFIG_TEXT, encoding="utf-8")
    return config_root


def _build_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
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
        conn.executemany(
            "INSERT INTO agent_protocol_violations "
            "(id, session_id, agent_type, violation, pattern, timestamp, raw_excerpt) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (1, "s-h1", "code-writer", "handoff_schema_violation", "p", "t1", "x"),
                (2, "s-h2", "debugger", "handoff_schema_violation", "p", "t2", "y"),
                (3, "s-h3", "commit", "handoff_schema_violation", "p", "t3", "z"),
                (4, "s-s1", "code-writer", "missing_formality", "no_status_block", "t4", "a"),
                (5, "s-s2", "test-writer", "missing_formality", "no_status_block", "t5", "b"),
                (6, "s-p1", "main", "prose_dispatch", "free_text_dispatch", "t6", "c"),
            ],
        )
        # 40 non-running so the support floor (default 30) is met → enforce_candidate
        conn.executemany(
            "INSERT INTO agent_runs (session_id, agent, status) VALUES (?,?,?)",
            [(f"r{i}", "code-writer", "DONE") for i in range(40)]
            + [("rr", "debugger", "running")],
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. cast.db engaged → output_shape enforce_candidates + cast_db provenance
# ---------------------------------------------------------------------------


def test_rank_cast_db_adds_output_shape_enforce_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    db = tmp_path / "cast.db"
    _build_db(db)
    empty = tmp_path / "empty"
    empty.mkdir()

    ret = main([
        "rank", str(config),
        "--cast-db", str(db),
        "--projects-dir", str(empty),
        "--json",
    ])
    assert ret == 0

    data = json.loads(capsys.readouterr().out)
    assert "cast_db" in data
    assert data["cast_db"]["mapped_violations"] == 5
    assert data["cast_db"]["agent_runs_denominator"] == 40
    assert data["cast_db"]["unmapped_by_signal"] == {"prose_dispatch": 1}

    output_shape = [r for r in data["ranked"] if r["convert_kind"] == "output_shape"]
    assert len(output_shape) == 2
    assert {r["predicate"]["signal"] for r in output_shape} == {"handoff", "status"}
    for r in output_shape:
        assert r["recommendation"] == "enforce_candidate"
        assert r["predicate"]["hook"] == "SubagentStop"
        assert r["opportunity_count"] == 40


# ---------------------------------------------------------------------------
# 2. missing cast.db → notice on STDERR, exit 0, transcript-only
# ---------------------------------------------------------------------------


def test_rank_missing_cast_db_notice_and_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()

    ret = main([
        "rank", str(config),
        "--cast-db", str(tmp_path / "does-not-exist.db"),
        "--projects-dir", str(empty),
        "--json",
    ])
    assert ret == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "cast_db" not in data
    assert all(r["convert_kind"] != "output_shape" for r in data["ranked"])
    assert "cast.db:" in captured.err
    assert "not_found" in captured.err
    assert "transcript evidence only" in captured.err


# ---------------------------------------------------------------------------
# 3. no --cast-db → cast.db untouched, output unchanged-shape
# ---------------------------------------------------------------------------


def test_rank_without_cast_db_does_not_touch_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    db = tmp_path / "cast.db"
    _build_db(db)
    empty = tmp_path / "empty"
    empty.mkdir()
    before_mtime = db.stat().st_mtime_ns

    ret = main(["rank", str(config), "--projects-dir", str(empty), "--json"])
    assert ret == 0

    data = json.loads(capsys.readouterr().out)
    assert "cast_db" not in data
    assert all(r["convert_kind"] != "output_shape" for r in data["ranked"])

    # The DB must not have been opened at all when the flag is absent.
    assert db.stat().st_mtime_ns == before_mtime
    side = [n for n in tmp_path.iterdir() if n.name.startswith("cast.db") and n.name != "cast.db"]
    assert side == []


def test_rank_text_mode_provenance_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    db = tmp_path / "cast.db"
    _build_db(db)
    empty = tmp_path / "empty"
    empty.mkdir()

    ret = main(["rank", str(config), "--cast-db", str(db), "--projects-dir", str(empty)])
    assert ret == 0
    captured = capsys.readouterr()
    # Text mode: provenance goes to STDERR, not the stdout report.
    assert "cast.db:" in captured.err
    assert "denominator 40" in captured.err
    assert "prose_dispatch=1" in captured.err


# ---------------------------------------------------------------------------
# evidence drill-down into a cast.db-sourced rule
# ---------------------------------------------------------------------------


def test_evidence_cast_db_rule_shows_synthesized_violations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    db = tmp_path / "cast.db"
    _build_db(db)
    empty = tmp_path / "empty"
    empty.mkdir()

    # No --rule → top-ranked rule (handoff, 3 violations) is shown.
    ret = main([
        "evidence", str(config),
        "--cast-db", str(db),
        "--projects-dir", str(empty),
        "--json",
    ])
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rule"]["convert_kind"] == "output_shape"
    assert len(data["violations"]) == 3
    for v in data["violations"]:
        assert v["transcript_rel"] == "cast.db"
        # command falls back to the (non-empty) input_summary
        assert v["command"] != ""
