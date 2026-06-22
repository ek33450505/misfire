"""test_proof.py — byte-reproducibility proof for the Phase 1 static audit.

Asserts that running (from the repo root)::

    misfire audit proof/sample-config --json

produces output that is BYTE-FOR-BYTE identical to the committed fixture at
``proof/expected_audit.json``.

The test reproduces the EXACT invocation: ``config_root`` is the RELATIVE
path ``"proof/sample-config"`` (not an absolute path), and pytest cwd is set
to the repo root via ``monkeypatch.chdir``.  This mirrors the CI invocation
documented in ``proof/README.md`` and ensures the ``config_root`` field in
the JSON output is the portable string ``"proof/sample-config"``.

The committed fixture must contain ZERO machine-specific paths — the portability
guard test asserts that ``/Users/``, ``/home/``, ``/private/``, and ``Projects/``
are absent from ``expected_audit.json``.

No database required. Stdlib-only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from misfire.cli import main

_REPO_ROOT = Path(__file__).parent.parent
_EXPECTED_JSON = _REPO_ROOT / "proof" / "expected_audit.json"

# The relative path to pass as CONFIG_ROOT — matches the documented reproduce command.
_PROOF_CONFIG_REL = "proof/sample-config"


def test_expected_json_fixture_exists() -> None:
    """Guard: the committed expected fixture must exist on disk."""
    assert _EXPECTED_JSON.exists(), (
        f"Committed fixture not found: {_EXPECTED_JSON}. "
        "Run `misfire audit proof/sample-config --json > proof/expected_audit.json` "
        "from the repo root to regenerate."
    )


def test_proof_audit_no_machine_paths() -> None:
    """Guard: expected_audit.json must contain ZERO machine-specific absolute paths.

    Checks that none of the following substrings appear anywhere in the file:
    ``/Users/``, ``/home/``, ``/private/``, ``Projects/``.  A CI runner at
    ``/home/runner/work/misfire/misfire/`` would produce a different resolved
    path than a dev machine — if these appear, the fixture is NOT portable.
    """
    text = _EXPECTED_JSON.read_text(encoding="utf-8")
    machine_markers = ["/Users/", "/home/", "/private/", "Projects/"]
    leaks = [m for m in machine_markers if m in text]
    assert not leaks, (
        f"proof/expected_audit.json contains machine-specific path(s): {leaks}. "
        "Regenerate from the repo root with: "
        "`misfire audit proof/sample-config --json > proof/expected_audit.json`"
    )


def test_proof_audit_matches_expected_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running misfire audit proof/sample-config --json matches proof/expected_audit.json byte-for-byte.

    Uses ``monkeypatch.chdir`` so that the relative ``proof/sample-config`` arg
    resolves the same way it does in the documented reproduce command.
    """
    monkeypatch.chdir(_REPO_ROOT)

    ret = main(["audit", _PROOF_CONFIG_REL, "--json"])
    assert ret == 0, "audit command must exit 0"

    actual_out = capsys.readouterr().out
    expected_out = _EXPECTED_JSON.read_text(encoding="utf-8")

    assert actual_out == expected_out, (
        "Audit JSON output differs from committed expected_audit.json.\n"
        "If fixture files or audit logic changed intentionally, regenerate with:\n"
        "  cd <repo-root> && misfire audit proof/sample-config --json > proof/expected_audit.json\n"
        "and commit the updated file."
    )


def test_proof_audit_json_is_valid_json() -> None:
    """The committed fixture is valid JSON with the expected top-level keys."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    for key in ["config_root", "sources", "rules", "findings", "classification_counts", "convertible"]:
        assert key in data, f"expected_audit.json is missing key: {key!r}"


def test_proof_audit_config_root_is_relative() -> None:
    """config_root in the JSON fixture is the relative path, not an absolutized one."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    cr = data["config_root"]
    assert not cr.startswith("/"), (
        f"config_root should be a relative path but got: {cr!r}"
    )
    assert cr == _PROOF_CONFIG_REL, (
        f"config_root should be {_PROOF_CONFIG_REL!r} but got: {cr!r}"
    )


def test_proof_audit_finding_kinds_present() -> None:
    """The committed fixture covers all four finding kinds."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    kinds = {f["kind"] for f in data["findings"]}
    for expected_kind in ["stale_path", "token_rent", "conflict", "load_fidelity"]:
        assert expected_kind in kinds, f"No {expected_kind!r} finding in expected_audit.json"


def test_proof_audit_all_classification_categories_present() -> None:
    """The committed fixture exercises all five classification categories."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    counts = data["classification_counts"]
    for cat in ["convertible", "judgment_keep", "non_directive", "output_shape", "safety_keep"]:
        assert cat in counts and counts[cat] > 0, (
            f"Classification category {cat!r} has zero rules in expected_audit.json"
        )


def test_proof_audit_sources_are_relative_paths() -> None:
    """Every source path in the fixture is relative to config_root (no absolute paths)."""
    data = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8"))
    for src in data["sources"]:
        path = src["path"]
        assert not path.startswith("/"), f"source path is absolute: {path!r}"
        assert not path.startswith("~/Projects/"), (
            f"source path contains machine-specific tilde path: {path!r}"
        )
