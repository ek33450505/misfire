"""test_cli.py — tests for the misfire CLI.

Tests run via pytest. They call ``main()`` directly with an explicit argv list
to avoid subprocess overhead and to exercise the argparse layer cleanly.

Covered:
- ``misfire --version`` prints ``misfire <version>`` and exits 0
- ``misfire audit`` exits 0 and emits expected output sections (text + JSON)
- ``misfire rank`` exits 0, emits expected output sections (text + JSON), no PII
- ``misfire evidence`` exits 0, emits violation listing, no PII
- ``misfire convert`` is covered in ``tests/test_cli_convert.py``
- An unknown subcommand causes argparse to exit 2
- No subcommand prints help and exits 0
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from misfire import __version__
from misfire.cli import main

_REPO_ROOT = Path(__file__).parent.parent
# Absolute path to the audit proof fixture
_PROOF_CONFIG = str(_REPO_ROOT / "proof" / "sample-config")
# Absolute paths for the evidence-sample fixture (Phase 2 rank/evidence proof)
_EVIDENCE_CONFIG = str(_REPO_ROOT / "proof" / "evidence-sample" / "config")
_EVIDENCE_PROJECTS = str(_REPO_ROOT / "proof" / "evidence-sample" / "projects")


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


def test_version_exit_code() -> None:
    assert main(["--version"]) == 0


def test_version_output(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--version"])
    out = capsys.readouterr().out.strip()
    assert out == f"misfire {__version__}"


# ---------------------------------------------------------------------------
# audit — real implementation (Phase 1)
# ---------------------------------------------------------------------------


def test_audit_exits_zero(tmp_path: Path) -> None:
    """audit always exits 0 (observer posture), even with an empty config root."""
    assert main(["audit", str(tmp_path)]) == 0


def test_audit_empty_root_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """audit on an empty directory is graceful: 0 sources, 0 rules, no crash."""
    main(["audit", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Sources: 0" in out
    assert "Rules: 0" in out


def test_audit_text_output_sections(capsys: pytest.CaptureFixture[str]) -> None:
    """Text output contains all expected section headers and blocks."""
    main(["audit", _PROOF_CONFIG])
    out = capsys.readouterr().out
    assert "=== stale_path" in out
    assert "=== token_rent" in out
    assert "=== conflict" in out
    assert "=== load_fidelity" in out
    assert "Classification summary:" in out
    assert "Convertible candidates" in out


def test_audit_text_no_raw_absolute_paths(capsys: pytest.CaptureFixture[str]) -> None:
    """Text output must not leak un-collapsed absolute /Users/<name>/ paths.

    Home-collapsed forms (``~/...``) are acceptable in text output.
    The stronger no-machine-path guarantee (no ``Projects/`` at all) is enforced
    on the committed JSON fixture in ``test_proof.py::test_proof_audit_no_machine_paths``.
    """
    import getpass
    username = getpass.getuser()
    main(["audit", _PROOF_CONFIG])
    out = capsys.readouterr().out
    assert f"/Users/{username}/" not in out
    assert f"/home/{username}/" not in out


def test_audit_json_exits_zero() -> None:
    assert main(["audit", _PROOF_CONFIG, "--json"]) == 0


def test_audit_json_top_level_keys(capsys: pytest.CaptureFixture[str]) -> None:
    """--json output parses and has all required top-level keys."""
    main(["audit", _PROOF_CONFIG, "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    for key in [
        "config_root",
        "sources",
        "rules",
        "findings",
        "classification_counts",
        "convertible",
    ]:
        assert key in data, f"missing key: {key!r}"


def test_audit_json_classification_counts(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON classification_counts has all five expected categories."""
    main(["audit", _PROOF_CONFIG, "--json"])
    data = json.loads(capsys.readouterr().out)
    counts = data["classification_counts"]
    for cat in ["convertible", "judgment_keep", "non_directive", "output_shape", "safety_keep"]:
        assert cat in counts, f"missing category: {cat!r}"
        assert isinstance(counts[cat], int)


def test_audit_json_findings_sorted(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON findings list is sorted by (source_rel, line, kind)."""
    main(["audit", _PROOF_CONFIG, "--json"])
    data = json.loads(capsys.readouterr().out)
    findings = data["findings"]
    keys = [(f["source_rel"], f["line"] if f["line"] is not None else -1, f["kind"]) for f in findings]
    assert keys == sorted(keys)


def test_audit_json_convertible_has_convert_kind(capsys: pytest.CaptureFixture[str]) -> None:
    """Every entry in the convertible list has a convert_kind field."""
    main(["audit", _PROOF_CONFIG, "--json"])
    data = json.loads(capsys.readouterr().out)
    for entry in data["convertible"]:
        assert "convert_kind" in entry
        assert entry["convert_kind"] is not None


def test_audit_default_config_root_is_dot_claude() -> None:
    """When no CONFIG_ROOT is given, the command does not crash (may find no files)."""
    # We simply confirm exit code is 0 whether ~/.claude exists or not.
    result = main(["audit"])
    assert result == 0


# ---------------------------------------------------------------------------
# rank — Phase 2 implementation
# ---------------------------------------------------------------------------


def test_rank_exits_zero(tmp_path: Path) -> None:
    """rank exits 0 with empty config root and empty projects dir."""
    assert main(["rank", str(tmp_path), "--projects-dir", str(tmp_path)]) == 0


def test_rank_empty_root_text_sections(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """rank on empty dirs prints header and threshold sections."""
    main(["rank", str(tmp_path), "--projects-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "misfire rank" in out
    assert "Thresholds:" in out


def test_rank_json_exits_zero(tmp_path: Path) -> None:
    """rank --json exits 0 with empty dirs."""
    assert main(["rank", str(tmp_path), "--projects-dir", str(tmp_path), "--json"]) == 0


def test_rank_json_top_level_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """rank --json output parses and has all required top-level keys."""
    main(["rank", str(tmp_path), "--projects-dir", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    for key in ["config_root", "ranked", "thresholds", "disclaimer"]:
        assert key in data, f"missing key: {key!r}"


def test_rank_json_thresholds_present(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Thresholds dict has min_support and min_violations."""
    main(["rank", str(tmp_path), "--projects-dir", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert "min_support" in data["thresholds"]
    assert "min_violations" in data["thresholds"]


def test_rank_json_ranked_rule_keys(capsys: pytest.CaptureFixture[str]) -> None:
    """Each ranked rule has all required fields."""
    main(["rank", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data["ranked"]) > 0, "evidence-sample fixture must produce ranked rules"
    for rr in data["ranked"]:
        for key in [
            "confidence",
            "convert_kind",
            "excluded_by_exception",
            "meets_support_floor",
            "opportunity_count",
            "predicate",
            "recommendation",
            "rule_excerpt",
            "rule_id",
            "source_rel",
            "violation_count",
            "violation_rate",
        ]:
            assert key in rr, f"ranked rule missing key: {key!r}"


def test_rank_fixture_violations_and_exceptions(capsys: pytest.CaptureFixture[str]) -> None:
    """evidence-sample fixture produces expected violation/exception counts."""
    main(["rank", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS, "--json"])
    data = json.loads(capsys.readouterr().out)
    ranked = {rr["predicate"]["match"]: rr for rr in data["ranked"]}
    # git commit: 5 violations, 2 excluded by CAST_COMMIT_AGENT=1 escape hatch
    commit_rule = ranked["git commit"]
    assert commit_rule["violation_count"] == 5
    assert commit_rule["excluded_by_exception"] == 2
    assert commit_rule["recommendation"] == "enforce_candidate"
    # git push: 3 violations, 0 excluded
    push_rule = ranked["git push"]
    assert push_rule["violation_count"] == 3
    assert push_rule["excluded_by_exception"] == 0
    assert push_rule["recommendation"] == "enforce_candidate"


def test_rank_no_users_path_in_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """rank --json must not leak /Users/<name>/ in output.

    Uses monkeypatch.chdir + relative paths so that config_root is echoed as a
    relative string (not an absolute /Users/... path) in the JSON output.
    This mirrors the documented usage pattern for portable output.
    """
    import getpass
    username = getpass.getuser()
    monkeypatch.chdir(_REPO_ROOT)
    main(["rank", "proof/evidence-sample/config",
          "--projects-dir", "proof/evidence-sample/projects", "--json"])
    out = capsys.readouterr().out
    assert f"/Users/{username}/" not in out, "rank JSON must not contain raw /Users/<name>/"
    assert f"/home/{username}/" not in out, "rank JSON must not contain raw /home/<name>/"


def test_rank_text_no_users_path_in_output(capsys: pytest.CaptureFixture[str]) -> None:
    """rank text output must not leak /Users/<name>/."""
    import getpass
    username = getpass.getuser()
    main(["rank", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS])
    out = capsys.readouterr().out
    assert f"/Users/{username}/" not in out


def test_rank_text_sections_present(capsys: pytest.CaptureFixture[str]) -> None:
    """rank text output contains all recommendation group sections."""
    main(["rank", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS])
    out = capsys.readouterr().out
    assert "=== enforce_candidate" in out
    assert "=== insufficient_evidence" in out
    assert "=== observed_no_violations" in out


def test_rank_custom_thresholds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--min-support and --min-violations are reflected in JSON thresholds."""
    main(["rank", str(tmp_path), "--projects-dir", str(tmp_path),
          "--min-support", "99", "--min-violations", "5", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["thresholds"]["min_support"] == 99
    assert data["thresholds"]["min_violations"] == 5


# ---------------------------------------------------------------------------
# evidence — Phase 2 implementation
# ---------------------------------------------------------------------------


def test_evidence_exits_zero(tmp_path: Path) -> None:
    """evidence exits 0 with empty dirs (no active rules → prints message to stderr)."""
    assert main(["evidence", str(tmp_path), "--projects-dir", str(tmp_path)]) == 0


def test_evidence_fixture_exits_zero() -> None:
    """evidence exits 0 on the evidence-sample fixture."""
    assert main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS]) == 0


def test_evidence_text_header(capsys: pytest.CaptureFixture[str]) -> None:
    """evidence text output contains header and rule info."""
    main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS])
    out = capsys.readouterr().out
    assert "misfire evidence" in out
    assert "Rule:" in out
    assert "Violations:" in out


def test_evidence_text_shows_violating_actions(capsys: pytest.CaptureFixture[str]) -> None:
    """evidence text output lists violating commands."""
    main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS])
    out = capsys.readouterr().out
    # Top rule is git commit — should show "git commit" in command listings
    assert "git commit" in out
    # Sanctioned commits must NOT appear as violations
    assert "CAST_COMMIT_AGENT=1" not in out


def test_evidence_text_no_raw_absolute_paths(capsys: pytest.CaptureFixture[str]) -> None:
    """evidence text must not leak /Users/<name>/ (commands are sanitized)."""
    import getpass
    username = getpass.getuser()
    main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS])
    out = capsys.readouterr().out
    assert f"/Users/{username}/" not in out
    assert f"/home/{username}/" not in out


def test_evidence_json_exits_zero() -> None:
    """evidence --json exits 0 on the evidence-sample fixture."""
    assert main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS, "--json"]) == 0


def test_evidence_json_top_level_keys(capsys: pytest.CaptureFixture[str]) -> None:
    """evidence --json output has required top-level keys."""
    main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS, "--json"])
    data = json.loads(capsys.readouterr().out)
    for key in ["config_root", "limit", "rule", "violations"]:
        assert key in data, f"missing key: {key!r}"


def test_evidence_json_violations_list(capsys: pytest.CaptureFixture[str]) -> None:
    """evidence --json violations list has 5 entries for git commit rule."""
    main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["rule"]["predicate"]["match"] == "git commit"
    assert len(data["violations"]) == 5


def test_evidence_json_violation_keys(capsys: pytest.CaptureFixture[str]) -> None:
    """Each violation entry has required fields."""
    main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS, "--json"])
    data = json.loads(capsys.readouterr().out)
    for v in data["violations"]:
        for key in ["agent_type", "command", "is_sidechain", "session_id", "timestamp", "transcript_rel"]:
            assert key in v, f"violation missing key: {key!r}"


def test_evidence_json_no_users_path_in_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """evidence --json must not leak /Users/<name>/ in command fields.

    Uses monkeypatch.chdir + relative paths so that config_root is echoed as a
    relative string (not an absolute /Users/... path) in the JSON output.
    The fixture commands themselves contain no absolute paths, so command
    excerpts are already clean.
    """
    import getpass
    username = getpass.getuser()
    monkeypatch.chdir(_REPO_ROOT)
    main(["evidence", "proof/evidence-sample/config",
          "--projects-dir", "proof/evidence-sample/projects", "--json"])
    out = capsys.readouterr().out
    assert f"/Users/{username}/" not in out
    assert f"/home/{username}/" not in out


def test_evidence_rule_flag_prefix_match(capsys: pytest.CaptureFixture[str]) -> None:
    """--rule accepts a prefix of rule_id and drills into the matching rule."""
    # First get the git push rule_id
    main(["rank", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS, "--json"])
    rank_data = json.loads(capsys.readouterr().out)
    push_rule = next(
        rr for rr in rank_data["ranked"] if rr["predicate"]["match"] == "git push"
    )
    push_id_prefix = push_rule["rule_id"][:6]

    main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS,
          "--rule", push_id_prefix, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["rule"]["predicate"]["match"] == "git push"
    assert len(data["violations"]) == 3


def test_evidence_limit_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """--limit caps the number of violations shown."""
    main(["evidence", _EVIDENCE_CONFIG, "--projects-dir", _EVIDENCE_PROJECTS,
          "--limit", "2", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data["violations"]) == 2
    assert data["limit"] == 2


# ---------------------------------------------------------------------------
# convert — Phase 3; full coverage in tests/test_cli_convert.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# unknown subcommand
# ---------------------------------------------------------------------------


def test_unknown_subcommand_exits_nonzero() -> None:
    """argparse calls sys.exit(2) for unknown subcommands."""
    with pytest.raises(SystemExit) as exc_info:
        main(["thiscommanddoesnotexist"])
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# no subcommand — prints help and exits 0
# ---------------------------------------------------------------------------


def test_no_subcommand_exit_code() -> None:
    assert main([]) == 0


# ---------------------------------------------------------------------------
# subprocess sanity check — entry point is wired
# ---------------------------------------------------------------------------


def test_entry_point_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "misfire.cli", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert f"misfire {__version__}" in result.stdout


# ---------------------------------------------------------------------------
# _sanitize_path_str fail-closed security guard
# ---------------------------------------------------------------------------


def test_sanitize_path_str_fails_closed_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_sanitize_path_str must never leak a raw absolute/home path on error.

    Force the except branch by monkeypatching _collapse_home (called when a
    path is under $HOME but not config_root) to raise RuntimeError.  The input
    is an absolute /Users/<name>/... path; the expected safe fallback is the
    basename only — no username, no directory components.
    """
    import misfire.cli as cli_mod
    from misfire.cli import _sanitize_path_str

    def _raise(_p: object) -> str:
        raise RuntimeError("forced sanitization failure")

    monkeypatch.setattr(cli_mod, "_collapse_home", _raise)

    result = _sanitize_path_str("/Users/alice/secret/notes.md", Path("/some/config"))

    assert "/Users" not in result, (
        f"fail-closed must not leak /Users in result: {result!r}"
    )
    assert "alice" not in result, (
        f"fail-closed must not leak username in result: {result!r}"
    )
    assert result == "notes.md", (
        f"Expected basename 'notes.md' as safe fallback, got {result!r}"
    )


# ---------------------------------------------------------------------------
# _extract_exceptions unit tests — regression for multi-word escape-hatch literals
# ---------------------------------------------------------------------------


def _make_rule_for_exceptions(rule_id: str, raw_text: str, normalized_text: str):
    """Build a minimal Rule object for _extract_exceptions tests."""
    from misfire.parse import Rule
    return Rule(
        rule_id=rule_id,
        source_path="/fake/CLAUDE.md",
        source_rel="CLAUDE.md",
        precedence_tier="user",
        section="",
        line_start=1,
        line_end=1,
        raw_text=raw_text,
        normalized_text=normalized_text,
        imperative=True,
    )


def _make_cl_for_exceptions(rule_id: str):
    """Build a minimal convertible/never_command Classification for tests."""
    from misfire.classify import Classification, CATEGORY_CONVERTIBLE, CONVERT_NEVER_COMMAND
    return Classification(
        rule_id=rule_id,
        category=CATEGORY_CONVERTIBLE,
        convert_kind=CONVERT_NEVER_COMMAND,
        predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        is_safety=False,
        confidence="high",
        rationale="never_command",
    )


def test_extract_exceptions_real_world_phrasing_with_spaces() -> None:
    """_extract_exceptions handles multi-word escape-hatch literals (spaces in backticks).

    Regression test for the real CAST working-conventions.md phrasing where the
    escape-hatch literal is ``CAST_COMMIT_AGENT=1 git commit`` (contains a space).
    The old regex ``[^`\\s]+`` stopped at the space, producing no match.
    """
    from misfire.cli import _extract_exceptions

    rule_id = "test-rule-001"
    # Real-world phrasing with a multi-word (space-containing) backtick literal
    real_raw_text = (
        "- MANDATORY: Use `commit` agent — never raw `git commit` "
        "(escape hatch when agent unavailable: `CAST_COMMIT_AGENT=1 git commit`)"
    )
    norm = (
        "MANDATORY: Use commit agent — never raw git commit "
        "(escape hatch when agent unavailable: CAST_COMMIT_AGENT=1 git commit)"
    )
    rule = _make_rule_for_exceptions(rule_id, real_raw_text, norm)
    cl = _make_cl_for_exceptions(rule_id)

    result = _extract_exceptions([cl], {rule_id: rule})

    assert rule_id in result, (
        f"expected exception entry for rule_id {rule_id!r}, got keys: {list(result)}"
    )
    # FIX A: marker must be refined to the distinctive env-var token, not the full span.
    # A substring check on "CAST_COMMIT_AGENT=1" matches export/env/&&/multi-var variants.
    assert result[rule_id] == "CAST_COMMIT_AGENT=1", (
        f"expected env-var token 'CAST_COMMIT_AGENT=1', got {result[rule_id]!r}"
    )


def test_extract_exceptions_no_escape_hatch_clause() -> None:
    """_extract_exceptions returns nothing for a rule without any escape-hatch clause.

    The rule ``never git commit directly — use the commit agent`` has no
    escape-hatch keyword, so no exception should be extracted.  This ensures that
    rules without an explicit carve-out remain fully counted.
    """
    from misfire.cli import _extract_exceptions

    rule_id = "test-rule-002"
    plain_raw_text = "MANDATORY: Never `git commit` directly — use the commit agent."
    norm = "MANDATORY: Never git commit directly — use the commit agent."
    rule = _make_rule_for_exceptions(rule_id, plain_raw_text, norm)
    cl = _make_cl_for_exceptions(rule_id)

    result = _extract_exceptions([cl], {rule_id: rule})

    assert result == {}, (
        f"expected no exceptions for a rule without escape-hatch clause, got {result!r}"
    )


def test_extract_exceptions_single_word_literal() -> None:
    """_extract_exceptions also handles single-word escape-hatch literals (no spaces).

    Ensures the existing single-word case (e.g. `CAST_COMMIT_AGENT=1` without
    ' git commit') still works after the [^`]+ fix.
    """
    from misfire.cli import _extract_exceptions

    rule_id = "test-rule-003"
    raw_text = (
        "Never use raw git commit directly. "
        "Escape hatch when agent unavailable: `CAST_COMMIT_AGENT=1`"
    )
    norm = (
        "Never use raw git commit directly. "
        "Escape hatch when agent unavailable: CAST_COMMIT_AGENT=1"
    )
    rule = _make_rule_for_exceptions(rule_id, raw_text, norm)
    cl = _make_cl_for_exceptions(rule_id)

    result = _extract_exceptions([cl], {rule_id: rule})

    assert rule_id in result, "single-word escape-hatch literal must still be extracted"
    assert result[rule_id] == "CAST_COMMIT_AGENT=1"


def test_extract_exceptions_propagates_to_same_predicate_rules() -> None:
    """FIX B: exception propagates to all rules sharing the same predicate match.

    Two never_command rules both forbid 'git commit'.  Only the first states the
    escape hatch.  After _extract_exceptions, both rules must carry the exception
    so that sanctioned commits are excluded regardless of which rule is evaluated.
    """
    from misfire.cli import _extract_exceptions

    rule_with_hatch_id = "rule-hatch-004"
    rule_without_hatch_id = "rule-plain-005"

    raw_with_hatch = (
        "MANDATORY: Never raw `git commit` "
        "(escape hatch when agent unavailable: `CAST_COMMIT_AGENT=1 git commit`)"
    )
    raw_without_hatch = "Never `git commit` directly — use the commit agent."

    rule_with = _make_rule_for_exceptions(rule_with_hatch_id, raw_with_hatch, raw_with_hatch)
    rule_without = _make_rule_for_exceptions(rule_without_hatch_id, raw_without_hatch, raw_without_hatch)

    cl_with = _make_cl_for_exceptions(rule_with_hatch_id)
    cl_without = _make_cl_for_exceptions(rule_without_hatch_id)

    result = _extract_exceptions(
        [cl_with, cl_without],
        {rule_with_hatch_id: rule_with, rule_without_hatch_id: rule_without},
    )

    assert rule_with_hatch_id in result, "rule that stated hatch must have exception"
    assert rule_without_hatch_id in result, (
        "rule without hatch clause must also get exception via propagation (FIX B)"
    )
    assert result[rule_with_hatch_id] == "CAST_COMMIT_AGENT=1"
    assert result[rule_without_hatch_id] == "CAST_COMMIT_AGENT=1"


def test_find_violations_export_variant_excluded() -> None:
    """FIX A: 'export CAST_COMMIT_AGENT=1 && git commit' is excluded, not a violation.

    The refined env-var token marker 'CAST_COMMIT_AGENT=1' is a substring of the
    export/env/multi-var real-world sanctioned forms that the old full-span marker
    'CAST_COMMIT_AGENT=1 git commit' missed.
    """
    from misfire.cli import _extract_exceptions
    from misfire.match import find_violations
    from misfire.evidence import ToolAction

    rule_id = "test-export-variant-006"
    raw_text = (
        "MANDATORY: Never raw git commit "
        "(escape hatch when agent unavailable: `CAST_COMMIT_AGENT=1 git commit`)"
    )
    rule = _make_rule_for_exceptions(rule_id, raw_text, raw_text)
    cl = _make_cl_for_exceptions(rule_id)

    exceptions = _extract_exceptions([cl], {rule_id: rule})
    assert exceptions.get(rule_id) == "CAST_COMMIT_AGENT=1", (
        f"expected marker 'CAST_COMMIT_AGENT=1', got {exceptions.get(rule_id)!r}"
    )

    def _action(cmd: str, ts: str) -> ToolAction:
        return ToolAction(
            tool_name="Bash",
            command=cmd,
            input_summary="",
            session_id="sess-001",
            timestamp=ts,
            transcript_rel="test.jsonl",
            is_sidechain=False,
            agent_type=None,
            cwd_rel="",
            git_branch=None,
        )

    sanctioned = _action("export CAST_COMMIT_AGENT=1 && git commit -m 'sanctioned'", "2026-01-01T00:00:00Z")
    plain_violation = _action("git commit -m 'raw violation'", "2026-01-01T00:01:00Z")

    rule_violations = find_violations([cl], [sanctioned, plain_violation], exceptions=exceptions)

    assert len(rule_violations) == 1
    rv = rule_violations[0]
    assert rv.violation_count == 1, "plain 'git commit' must be a violation"
    assert rv.excluded_by_exception == 1, (
        "'export CAST_COMMIT_AGENT=1 && git commit' must be excluded (FIX A)"
    )


# ---------------------------------------------------------------------------
# Privacy regression: ABSOLUTE config_root must be home-collapsed in JSON
# ---------------------------------------------------------------------------
#
# The pre-existing no-PII tests pass a RELATIVE config_root via monkeypatch.chdir,
# so they never exercised the natural invocation `misfire <cmd> ~/.claude --json`,
# where the shell expands ~ to an absolute /Users/<name>/.claude that was echoed
# verbatim into the JSON `config_root` field — a username leak on the primary
# command. These tests pin the fix (route config_root through _display_config_root).
# A NON-EXISTENT directory under $HOME is used so the real ~/.claude is never read.


def test_display_config_root_collapses_home_absolute() -> None:
    """_display_config_root home-collapses an absolute path under $HOME."""
    from misfire.cli import _display_config_root

    out = _display_config_root(str(Path.home() / "some_config_root"))
    assert out == "~/some_config_root"
    assert "/Users/" not in out and "/home/" not in out


def test_rank_json_absolute_home_config_root_collapsed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rank --json with an absolute ~-expanded config_root must not leak /Users/<name>/."""
    import getpass

    abs_home_config = str(Path.home() / "misfire_nonexistent_test_root")
    ret = main(["rank", abs_home_config, "--projects-dir", str(tmp_path), "--json"])
    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["config_root"] == "~/misfire_nonexistent_test_root"
    assert f"/Users/{getpass.getuser()}/" not in out
    assert "/Users/" not in out


def test_audit_json_absolute_home_config_root_collapsed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """audit --json with an absolute ~-expanded config_root must not leak /Users/<name>/."""
    import getpass

    abs_home_config = str(Path.home() / "misfire_nonexistent_test_root")
    ret = main(["audit", abs_home_config, "--json"])
    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["config_root"] == "~/misfire_nonexistent_test_root"
    assert f"/Users/{getpass.getuser()}/" not in out
    assert "/Users/" not in out
