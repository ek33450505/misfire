"""test_proof_rank.py — byte-reproducibility proof for the Phase 2 rank pipeline.

Asserts that running (from the repo root)::

    misfire rank proof/evidence-sample/config \\
        --projects-dir proof/evidence-sample/projects --json

produces output that is BYTE-FOR-BYTE identical to the committed fixture at
``proof/expected_rank.json``.

The test reproduces the EXACT invocation: ``config_root`` is the RELATIVE
path ``"proof/evidence-sample/config"`` (not an absolute path), and pytest
cwd is set to the repo root via ``monkeypatch.chdir``.  This mirrors the CI
invocation documented in ``proof/README.md`` and ensures the ``config_root``
field in the JSON output is the portable string ``"proof/evidence-sample/config"``.

The committed fixture must contain ZERO machine-specific paths — the portability
guard test asserts that ``/Users/``, ``/home/``, ``/private/``, and ``Projects/``
are absent from ``expected_rank.json``.

No database required. Stdlib-only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from misfire.cli import main

_REPO_ROOT = Path(__file__).parent.parent
_EXPECTED_JSON = _REPO_ROOT / "proof" / "expected_rank.json"

# The relative paths to pass as args — matches the documented reproduce command.
_RANK_CONFIG_REL = "proof/evidence-sample/config"
_RANK_PROJECTS_REL = "proof/evidence-sample/projects"


def test_expected_rank_json_fixture_exists() -> None:
    """Guard: the committed expected fixture must exist on disk."""
    assert _EXPECTED_JSON.exists(), (
        f"Committed fixture not found: {_EXPECTED_JSON}. "
        "Regenerate from the repo root with: "
        "  misfire rank proof/evidence-sample/config "
        "--projects-dir proof/evidence-sample/projects --json > proof/expected_rank.json"
    )


def test_proof_rank_no_machine_paths() -> None:
    """Guard: expected_rank.json must contain ZERO machine-specific absolute paths.

    Checks that none of the following substrings appear anywhere in the file:
    ``/Users/``, ``/home/``, ``/private/``, ``Projects/``.  A CI runner at
    ``/home/runner/work/misfire/misfire/`` would produce a different resolved
    path than a dev machine — if these appear, the fixture is NOT portable.

    Note: ``transcript_rel`` (from evidence output) is NOT included in the rank
    JSON, so the ``Projects/`` guard applies cleanly.
    """
    text = _EXPECTED_JSON.read_text(encoding="utf-8")
    machine_markers = ["/Users/", "/home/", "/private/", "Projects/"]
    leaks = [m for m in machine_markers if m in text]
    assert not leaks, (
        f"proof/expected_rank.json contains machine-specific path(s): {leaks}. "
        "Regenerate from the repo root with: "
        "  misfire rank proof/evidence-sample/config "
        "--projects-dir proof/evidence-sample/projects --json > proof/expected_rank.json"
    )


def test_proof_rank_matches_expected_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running misfire rank ... --json matches proof/expected_rank.json byte-for-byte.

    Uses ``monkeypatch.chdir`` so that the relative config/projects args resolve
    the same way they do in the documented reproduce command.
    """
    monkeypatch.chdir(_REPO_ROOT)

    ret = main([
        "rank",
        _RANK_CONFIG_REL,
        "--projects-dir",
        _RANK_PROJECTS_REL,
        "--json",
    ])
    assert ret == 0, "rank command must exit 0"

    actual_out = capsys.readouterr().out
    expected_out = _EXPECTED_JSON.read_text(encoding="utf-8")

    assert actual_out == expected_out, (
        "Rank JSON output differs from committed expected_rank.json.\n"
        "If fixture files or rank logic changed intentionally, regenerate with:\n"
        "  cd <repo-root> && misfire rank proof/evidence-sample/config "
        "--projects-dir proof/evidence-sample/projects --json > proof/expected_rank.json\n"
        "and commit the updated file."
    )


def test_proof_rank_json_is_valid_json() -> None:
    """The committed fixture is valid JSON with the expected top-level keys."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    for key in ["config_root", "ranked", "thresholds", "disclaimer"]:
        assert key in data, f"expected_rank.json is missing key: {key!r}"


def test_proof_rank_config_root_is_relative() -> None:
    """config_root in the JSON fixture is the relative path, not an absolutized one."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    cr = data["config_root"]
    assert not cr.startswith("/"), (
        f"config_root should be a relative path but got: {cr!r}"
    )
    assert cr == _RANK_CONFIG_REL, (
        f"config_root should be {_RANK_CONFIG_REL!r} but got: {cr!r}"
    )


def test_proof_rank_has_enforce_candidates() -> None:
    """The committed fixture must have at least one enforce_candidate rule."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    recommendations = [rr["recommendation"] for rr in data["ranked"]]
    assert "enforce_candidate" in recommendations, (
        "expected_rank.json must contain at least one enforce_candidate rule"
    )


def test_proof_rank_git_commit_rule_present() -> None:
    """The git commit rule is in the ranked list with correct violation data."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    commit_rules = [
        rr for rr in data["ranked"]
        if rr.get("predicate", {}).get("match") == "git commit"
    ]
    assert len(commit_rules) == 1, "expected exactly one git commit rule"
    rule = commit_rules[0]
    assert rule["violation_count"] == 5
    assert rule["excluded_by_exception"] == 2
    assert rule["recommendation"] == "enforce_candidate"
    assert rule["source_rel"] == "CLAUDE.md"


def test_proof_rank_git_push_rule_present() -> None:
    """The git push rule is in the ranked list with correct violation data."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    push_rules = [
        rr for rr in data["ranked"]
        if rr.get("predicate", {}).get("match") == "git push"
    ]
    assert len(push_rules) == 1, "expected exactly one git push rule"
    rule = push_rules[0]
    assert rule["violation_count"] == 3
    assert rule["excluded_by_exception"] == 0
    assert rule["recommendation"] == "enforce_candidate"


def test_proof_rank_commit_rule_ranks_first() -> None:
    """git commit rule (5 violations) must rank above git push (3 violations)."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    ranked = data["ranked"]
    assert len(ranked) >= 2
    assert ranked[0]["predicate"]["match"] == "git commit", (
        "git commit (5 violations) should rank first"
    )
    assert ranked[1]["predicate"]["match"] == "git push", (
        "git push (3 violations) should rank second"
    )


def test_proof_rank_source_rel_machine_independent() -> None:
    """All source_rel fields in ranked rules must be relative (no absolute paths)."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    for rr in data["ranked"]:
        sr = rr.get("source_rel", "")
        assert not sr.startswith("/"), f"source_rel is absolute: {sr!r}"
        assert not sr.startswith("~/"), f"source_rel has tilde-home prefix: {sr!r}"
