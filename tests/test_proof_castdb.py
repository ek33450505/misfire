"""test_proof_castdb.py — byte-reproducibility proof for the cast.db adapter.

Builds a synthetic cast.db from ``proof/castdb-sample/seed.sql`` and asserts
that running (from the repo root)::

    misfire rank proof/castdb-sample/config \\
        --cast-db proof/castdb-sample/generated.db \\
        --projects-dir <empty> --json

produces output BYTE-FOR-BYTE identical to the committed fixture
``proof/castdb-sample/expected_castdb_rank.json``.

Both ``config_root`` and ``--cast-db`` are passed as RELATIVE paths with
``monkeypatch.chdir(repo_root)`` so the echoed ``config_root`` and the
``cast_db.db_path_rel`` stay portable.  The committed fixture must contain ZERO
machine-specific paths.

The generated DB is gitignored (``proof/castdb-sample/generated.db``) and rebuilt
fresh from ``seed.sql`` on every run — only the SQL text is committed, never the
binary DB.

Stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from misfire.cli import main

_REPO_ROOT = Path(__file__).parent.parent
_SAMPLE_DIR = _REPO_ROOT / "proof" / "castdb-sample"
_SEED_SQL = _SAMPLE_DIR / "seed.sql"
_EXPECTED_JSON = _SAMPLE_DIR / "expected_castdb_rank.json"

# Relative args — match the documented reproduce command.
_CONFIG_REL = "proof/castdb-sample/config"
_CASTDB_REL = "proof/castdb-sample/generated.db"


def _build_db() -> Path:
    """(Re)build the synthetic cast.db from seed.sql. Returns its path."""
    db = _SAMPLE_DIR / "generated.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_SEED_SQL.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    return db


# ---------------------------------------------------------------------------
# Static fixture guards
# ---------------------------------------------------------------------------


def test_seed_sql_exists() -> None:
    assert _SEED_SQL.exists(), f"seed.sql not found: {_SEED_SQL}"


def test_expected_castdb_json_fixture_exists() -> None:
    assert _EXPECTED_JSON.exists(), (
        f"Committed fixture not found: {_EXPECTED_JSON}. Regenerate from repo root: "
        "misfire rank proof/castdb-sample/config "
        "--cast-db proof/castdb-sample/generated.db --projects-dir <empty> --json "
        "> proof/castdb-sample/expected_castdb_rank.json"
    )


def test_proof_castdb_no_machine_paths() -> None:
    """The committed fixture must contain ZERO machine-specific absolute paths."""
    text = _EXPECTED_JSON.read_text(encoding="utf-8")
    leaks = [m for m in ("/Users/", "/home/", "/private/", "Projects/") if m in text]
    assert not leaks, f"expected_castdb_rank.json contains machine path(s): {leaks}"


def test_proof_castdb_config_root_relative() -> None:
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    assert data["config_root"] == _CONFIG_REL


def test_proof_castdb_db_path_rel_is_relative() -> None:
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    db_path_rel = data["cast_db"]["db_path_rel"]
    assert db_path_rel == _CASTDB_REL
    assert not db_path_rel.startswith("/")
    assert not db_path_rel.startswith("~/")


def test_proof_castdb_handoff_and_status_enforce_candidates() -> None:
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    output_shape = [r for r in data["ranked"] if r["convert_kind"] == "output_shape"]
    assert {r["predicate"]["signal"] for r in output_shape} == {"handoff", "status"}
    for r in output_shape:
        assert r["recommendation"] == "enforce_candidate"


def test_proof_castdb_unmapped_prose_dispatch() -> None:
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    assert data["cast_db"]["unmapped_by_signal"] == {"prose_dispatch": 1}
    assert data["cast_db"]["total_violations_read"] == 6
    assert data["cast_db"]["mapped_violations"] == 5


# ---------------------------------------------------------------------------
# The byte-equality proof
# ---------------------------------------------------------------------------


def test_proof_castdb_matches_expected_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _build_db()
    empty_projects = tmp_path / "empty"
    empty_projects.mkdir()

    monkeypatch.chdir(_REPO_ROOT)
    ret = main([
        "rank", _CONFIG_REL,
        "--cast-db", _CASTDB_REL,
        "--projects-dir", str(empty_projects),
        "--json",
    ])
    assert ret == 0, "rank command must exit 0"

    actual = capsys.readouterr().out
    expected = _EXPECTED_JSON.read_text(encoding="utf-8")
    assert actual == expected, (
        "cast.db rank JSON differs from committed expected_castdb_rank.json.\n"
        "If fixtures or logic changed intentionally, regenerate from repo root:\n"
        "  python3 -c \"import sqlite3; c=sqlite3.connect('proof/castdb-sample/generated.db');"
        " c.executescript(open('proof/castdb-sample/seed.sql').read()); c.commit(); c.close()\"\n"
        "  misfire rank proof/castdb-sample/config --cast-db proof/castdb-sample/generated.db "
        "--projects-dir <empty> --json > proof/castdb-sample/expected_castdb_rank.json"
    )
