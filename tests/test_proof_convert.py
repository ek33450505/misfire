"""test_proof_convert.py — byte-reproducibility proof for ``misfire convert``.

Asserts that running (from the repo root)::

    misfire convert proof/evidence-sample/config \\
        --projects-dir proof/evidence-sample/projects --top --json

produces output BYTE-FOR-BYTE identical to ``proof/expected_convert.json`` — and
that the hook embedded in that golden output, when executed against the real
PreToolUse stdin contract, actually denies ``git commit`` while allowing
``git status`` and ignoring a quoted occurrence.

This reuses the portable evidence-sample fixture (no DB, no PII), so the convert
proof builds directly on the Phase-2 evidence proof.  Stdlib + pytest only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from misfire.cli import main

_REPO_ROOT = Path(__file__).parent.parent
_EXPECTED_JSON = _REPO_ROOT / "proof" / "expected_convert.json"

_CONFIG_REL = "proof/evidence-sample/config"
_PROJECTS_REL = "proof/evidence-sample/projects"
_ARGV = ["convert", _CONFIG_REL, "--projects-dir", _PROJECTS_REL, "--top", "--json"]


def test_expected_convert_json_fixture_exists() -> None:
    assert _EXPECTED_JSON.exists(), (
        f"Committed fixture not found: {_EXPECTED_JSON}. Regenerate from the repo "
        "root with: misfire convert proof/evidence-sample/config "
        "--projects-dir proof/evidence-sample/projects --top --json "
        "> proof/expected_convert.json"
    )


def test_proof_convert_no_machine_paths() -> None:
    text = _EXPECTED_JSON.read_text(encoding="utf-8")
    leaks = [m for m in ("/Users/", "/home/", "/private/", "Projects/") if m in text]
    assert not leaks, f"proof/expected_convert.json contains machine path(s): {leaks}"


def test_proof_convert_matches_expected_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    ret = main(_ARGV)
    assert ret == 0, "convert command must exit 0"
    actual = capsys.readouterr().out
    expected = _EXPECTED_JSON.read_text(encoding="utf-8")
    assert actual == expected, (
        "convert JSON differs from committed expected_convert.json.\n"
        "If fixtures or scaffold/convert logic changed intentionally, regenerate:\n"
        "  cd <repo-root> && misfire convert proof/evidence-sample/config "
        "--projects-dir proof/evidence-sample/projects --top --json "
        "> proof/expected_convert.json"
    )


def test_proof_convert_shape() -> None:
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    assert data["status"] == "enforce"
    assert data["recommended"] is True
    assert data["evidence"]["status"] == "enforce_candidate"
    assert data["hook"]["event"] == "PreToolUse"
    assert data["hook"]["matcher"] == "Bash"
    assert data["hook"]["is_skeleton"] is False
    cmd = data["hook"]["settings_snippet"]["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd.startswith("${CLAUDE_PROJECT_DIR}/")


def test_proof_convert_hook_denies_git_commit(tmp_path: Path) -> None:
    """The hook in the golden output enforces the rule against the real contract."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    hook = tmp_path / data["hook"]["filename"]
    hook.write_text(data["hook"]["script"], encoding="utf-8")

    def denies(cmd: str) -> bool:
        proc = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        return '"permissionDecision": "deny"' in proc.stdout

    assert denies("git commit -m wip") is True
    assert denies("git status") is False
    assert denies('echo "git commit"') is False  # quoted → not an invocation
    # honors the rule's own escape hatch
    assert denies("CAST_COMMIT_AGENT=1 git commit") is False
