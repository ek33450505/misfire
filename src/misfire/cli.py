"""cli.py — the ``misfire`` command-line entry point.

Stdlib ``argparse`` only. One top-level flag plus four subcommands:

* ``misfire --version``   — print ``misfire <__version__>`` and exit 0.
* ``misfire audit``       — Phase 1: static parse + deterministic audit
                            (stale paths, token rent, conflicts, load-fidelity).
* ``misfire rank``        — Phase 2: evidence-ranked violation list from run history;
                            which prose rules your agents demonstrably ignore.
* ``misfire evidence``    — Phase 2: show raw per-rule evidence (violation + support
                            counts, raw excerpts) for a given rule or all rules.
* ``misfire convert``     — Phase 3: scaffold a deterministic hook for a violated
                            convertible rule; print the diff for review.

All subcommands are Phase-0 stubs. Each exits 2 and prints a clear
"not yet implemented (Phase N)" message to stderr.

``main(argv=None)`` returns an ``int`` status and is the console entry point.
Stdlib-only, no global state.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from misfire import __version__


def _stub(cmd: str, phase: int) -> int:
    """Print a not-yet-implemented notice to stderr and return exit code 2."""
    print(f"{cmd}: not yet implemented (Phase {phase})", file=sys.stderr)
    return 2


def _cmd_audit(_args: argparse.Namespace) -> int:
    return _stub("audit", 1)


def _cmd_rank(_args: argparse.Namespace) -> int:
    return _stub("rank", 2)


def _cmd_evidence(_args: argparse.Namespace) -> int:
    return _stub("evidence", 2)


def _cmd_convert(_args: argparse.Namespace) -> int:
    return _stub("convert", 3)


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the ``misfire`` CLI."""
    parser = argparse.ArgumentParser(
        prog="misfire",
        description=(
            "Trace-grounded CLAUDE.md adherence auditor. "
            "Tells you which prose rules your agents actually ignore."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the misfire version and exit",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "audit",
        help=(
            "[Phase 1] Static parse + deterministic audit: stale paths, "
            "token rent, conflicts, load-fidelity"
        ),
    )
    subparsers.add_parser(
        "rank",
        help=(
            "[Phase 2] Evidence-ranked list of prose rules your agents "
            "demonstrably ignore, from your run history"
        ),
    )
    subparsers.add_parser(
        "evidence",
        help=(
            "[Phase 2] Show raw per-rule evidence: violation + support counts "
            "and raw excerpts"
        ),
    )
    subparsers.add_parser(
        "convert",
        help=(
            "[Phase 3] Scaffold a deterministic hook for a violated convertible "
            "rule; print the diff for review"
        ),
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Parse ``argv`` and dispatch to a subcommand. Returns an int status.

    ``--version`` short-circuits before any subcommand. With no subcommand the
    help text is printed and 0 is returned. Unknown subcommands cause argparse
    to print usage and call ``sys.exit(2)``.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"misfire {__version__}")
        return 0

    dispatch = {
        "audit": _cmd_audit,
        "rank": _cmd_rank,
        "evidence": _cmd_evidence,
        "convert": _cmd_convert,
    }

    if args.command in dispatch:
        return dispatch[args.command](args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
