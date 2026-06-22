"""test_cli.py — tests for the misfire CLI.

Tests run via pytest. They call ``main()`` directly with an explicit argv list
to avoid subprocess overhead and to exercise the argparse layer cleanly.

Covered:
- ``misfire --version`` prints ``misfire <version>`` and exits 0
- ``misfire audit`` exits 0 and emits expected output sections (text + JSON)
- ``misfire rank``, ``evidence``, ``convert`` remain stubs that exit 2
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
# Absolute path to the proof fixture — used for integration tests that do NOT
# need the relative-path portability guarantee (those live in test_proof.py).
_PROOF_CONFIG = str(_REPO_ROOT / "proof" / "sample-config")


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
# Phase 2/3 stubs still exit 2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["rank", "evidence", "convert"])
def test_stub_exit_code(cmd: str) -> None:
    assert main([cmd]) == 2


@pytest.mark.parametrize("cmd", ["rank", "evidence", "convert"])
def test_stub_stderr_message(cmd: str, capsys: pytest.CaptureFixture[str]) -> None:
    main([cmd])
    err = capsys.readouterr().err
    assert cmd in err
    assert "not yet implemented" in err


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
