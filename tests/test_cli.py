"""test_cli.py — smoke tests for the misfire CLI stubs.

Tests run via pytest. They call ``main()`` directly with an explicit argv list
to avoid subprocess overhead and to exercise the argparse layer cleanly.

Covered:
- ``misfire --version`` prints ``misfire <version>`` and exits 0
- Each subcommand stub (audit, rank, evidence, convert) exits 2
- An unknown subcommand causes argparse to exit 2
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from misfire import __version__
from misfire.cli import main


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
# subcommand stubs exit 2 and print to stderr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["audit", "rank", "evidence", "convert"])
def test_stub_exit_code(cmd: str) -> None:
    assert main([cmd]) == 2


@pytest.mark.parametrize("cmd", ["audit", "rank", "evidence", "convert"])
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
