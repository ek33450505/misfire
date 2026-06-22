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

``main(argv=None)`` returns an ``int`` status and is the console entry point.
Stdlib-only, no global state.

Privacy guarantee
~~~~~~~~~~~~~~~~~
All output (both ``--json`` and text) is free of machine-specific paths:
- Paths under *config_root* are made relative to config_root.
- Paths under ``$HOME`` but not under config_root are home-collapsed (``~/``).
- ``config_root`` is echoed as the user supplied it (relative inputs stay relative).
Sanitization is applied at the output boundary in this module; ``audit.py`` and
``parse.py`` internals are unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from misfire import __version__
from misfire.audit import (
    Finding,
    KIND_CONFLICT,
    KIND_LOAD_FIDELITY,
    KIND_STALE_PATH,
    KIND_TOKEN_RENT,
    audit_all,
)
from misfire.classify import (
    CATEGORY_CONVERTIBLE,
    Classification,
    classify_rules,
)
from misfire.parse import ParseResult, Rule, SourceFile, _collapse_home, parse_config


# ---------------------------------------------------------------------------
# Stubs for Phase 2/3 commands
# ---------------------------------------------------------------------------


def _stub(cmd: str, phase: int) -> int:
    """Print a not-yet-implemented notice to stderr and return exit code 2."""
    print(f"{cmd}: not yet implemented (Phase {phase})", file=sys.stderr)
    return 2


def _cmd_rank(_args: argparse.Namespace) -> int:
    return _stub("rank", 2)


def _cmd_evidence(_args: argparse.Namespace) -> int:
    return _stub("evidence", 2)


def _cmd_convert(_args: argparse.Namespace) -> int:
    return _stub("convert", 3)


# ---------------------------------------------------------------------------
# Path sanitization helpers (output-boundary privacy layer)
# ---------------------------------------------------------------------------


def _display_config_root(raw: str) -> str:
    """Machine-safe display of the config root as supplied by the caller.

    Relative inputs (``proof/sample-config``) and tilde inputs (``~/.claude``)
    are returned as-is.  Absolute paths are home-collapsed so that
    ``/Users/alice/...`` becomes ``~/...``.
    """
    if raw.startswith("/"):
        return _collapse_home(Path(raw))
    return raw


def _sanitize_path_str(path_str: str, config_root: Path) -> str:
    """Return a machine-independent representation of *path_str*.

    Priority:
    1. Under *config_root* → relative to config_root (e.g. ``CLAUDE.md``,
       ``rules/tools.md``, ``nonexistent-import.md``).
    2. Under ``$HOME`` but not config_root → home-collapsed (``~/...``).
    3. Already relative (no leading ``/`` or ``~/``) → returned unchanged.
    4. Non-home absolute path → returned unchanged.

    Strings that are not path-like are returned unchanged.
    """
    if not path_str:
        return path_str
    try:
        if path_str.startswith("~/"):
            p: Path = Path.home() / path_str[2:]
        elif path_str.startswith("/"):
            p = Path(path_str)
        else:
            return path_str  # already relative or non-path string
        # p is now absolute
        config_root_resolved = config_root.resolve()
        try:
            return str(p.relative_to(config_root_resolved))
        except ValueError:
            pass
        # Not under config_root → home-collapse (may be a no-op if outside home)
        return _collapse_home(p)
    except Exception:
        # fail CLOSED: a sanitization error must never emit a raw absolute/home path
        base = os.path.basename(path_str.rstrip("/"))
        return base if base else "<path>"


def _sanitize_message(msg: str, config_root: Path) -> str:
    """Replace machine-specific path substrings embedded in a finding message.

    Handles both absolute forms (``/Users/alice/...``) and the home-collapsed
    tilde forms (``~/Projects/.../proof/sample-config/...``) that audit.py
    can produce via ``_collapse_home``.
    """
    config_root_resolved = config_root.resolve()
    config_root_abs = str(config_root_resolved)
    config_root_home = _collapse_home(config_root_resolved)
    home_str = str(Path.home())

    # Most specific first: home-collapsed config_root prefix
    msg = msg.replace(config_root_home + "/", "")
    msg = msg.replace(config_root_home, ".")
    # Absolute config_root prefix
    msg = msg.replace(config_root_abs + "/", "")
    msg = msg.replace(config_root_abs, ".")
    # Any remaining absolute home paths → ~/
    msg = msg.replace(home_str + "/", "~/")
    return msg


def _sanitize_detail_dict(detail: Dict, config_root: Path) -> Dict:
    """Recursively sanitize path strings in a finding detail dict.

    Only string values are sanitized; ints, bools, None, and nested lists/dicts
    are handled recursively (lists of strings are sanitized element by element).
    """
    result: Dict = {}
    for k, v in detail.items():
        if isinstance(v, str):
            result[k] = _sanitize_path_str(v, config_root)
        elif isinstance(v, dict):
            result[k] = _sanitize_detail_dict(v, config_root)
        elif isinstance(v, list):
            result[k] = [
                _sanitize_path_str(item, config_root) if isinstance(item, str) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def _sanitize_finding(f: Finding, config_root: Path) -> Finding:
    """Return a new Finding with all machine-specific paths sanitized."""
    return Finding(
        kind=f.kind,
        severity=f.severity,
        source_rel=_sanitize_path_str(f.source_rel, config_root),
        line=f.line,
        message=_sanitize_message(f.message, config_root),
        detail=_sanitize_detail_dict(f.detail, config_root),
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _finding_to_dict(f: Finding) -> Dict:
    """Serialize a (pre-sanitized) Finding to a JSON-safe dict."""
    return {
        "detail": f.detail,
        "kind": f.kind,
        "line": f.line,
        "message": f.message,
        "severity": f.severity,
        "source_rel": f.source_rel,
    }


def _finding_sort_key(fd: Dict) -> Tuple:
    """Sort key for findings: (source_rel, line_or_minus1, kind)."""
    return (fd["source_rel"], fd["line"] if fd["line"] is not None else -1, fd["kind"])


def _rule_to_dict(r: Rule) -> Dict:
    """Serialize a Rule to a JSON-safe dict (source_rels use ~/.claude/ convention)."""
    return {
        "imperative": r.imperative,
        "line_end": r.line_end,
        "line_start": r.line_start,
        "normalized_text": r.normalized_text,
        "precedence_tier": r.precedence_tier,
        "rule_id": r.rule_id,
        "section": r.section,
        "source_rel": r.source_rel,
    }


def _source_to_dict(sf: SourceFile, config_root: Path) -> Dict:
    """Serialize a SourceFile with all paths sanitized for portability."""
    raw_path = _collapse_home(Path(sf.path))
    sanitized_path = _sanitize_path_str(raw_path, config_root)
    raw_from = _collapse_home(Path(sf.imported_from)) if sf.imported_from else None
    sanitized_from = _sanitize_path_str(raw_from, config_root) if raw_from else None
    return {
        "imported_from": sanitized_from,
        "path": sanitized_path,
        "paths_globs": sf.paths_globs,
        "tier": sf.tier,
    }


def _convertible_entry(rule: Rule, cl: Classification) -> Dict:
    """Build a convertible candidate entry for JSON output."""
    return {
        "confidence": cl.confidence,
        "convert_kind": cl.convert_kind,
        "line_start": rule.line_start,
        "normalized_text": rule.normalized_text,
        "predicate": cl.predicate,
        "rule_id": rule.rule_id,
        "source_rel": rule.source_rel,
    }


# ---------------------------------------------------------------------------
# audit — text output
# ---------------------------------------------------------------------------

_FINDING_KINDS = [KIND_STALE_PATH, KIND_TOKEN_RENT, KIND_CONFLICT, KIND_LOAD_FIDELITY]
_ALL_CATEGORIES = [
    "convertible",
    "judgment_keep",
    "non_directive",
    "output_shape",
    "safety_keep",
]


def _print_audit_text(
    config_root_display: str,
    parse_result: ParseResult,
    sanitized_findings: List[Finding],
    classifications: List[Classification],
    rules: List[Rule],
) -> None:
    """Print the human-readable audit report to stdout.

    ``sanitized_findings`` must already have machine-specific paths removed
    (call ``_sanitize_finding`` on each finding before passing here).
    """
    n_sources = len(parse_result.sources)
    n_rules = len(rules)

    # Header
    print(f"misfire audit — {config_root_display}")
    print(f"Sources: {n_sources} | Rules: {n_rules}")
    print()

    # Findings grouped by kind
    findings_by_kind: Dict[str, List[Finding]] = {k: [] for k in _FINDING_KINDS}
    for f in sanitized_findings:
        findings_by_kind.setdefault(f.kind, []).append(f)

    for kind in _FINDING_KINDS:
        group = findings_by_kind.get(kind, [])
        n = len(group)
        label = f"{kind} ({n} finding{'s' if n != 1 else ''})"
        print(f"=== {label} ===")
        if not group:
            print("  (none)")
        else:
            for f in group:
                loc = f"{f.source_rel}:{f.line}" if f.line is not None else f.source_rel
                print(f"  {loc} — {f.message}")
        print()

    # Classification summary
    counts: Dict[str, int] = {cat: 0 for cat in _ALL_CATEGORIES}
    for cl in classifications:
        counts[cl.category] = counts.get(cl.category, 0) + 1

    print("Classification summary:")
    max_cat_len = max(len(c) for c in _ALL_CATEGORIES)
    for cat in _ALL_CATEGORIES:
        print(f"  {cat:<{max_cat_len}}  {counts[cat]}")
    print()

    # Convertible candidates
    convertible_pairs = [
        (rule, cl)
        for rule, cl in zip(rules, classifications)
        if cl.category == CATEGORY_CONVERTIBLE
    ]
    convertible_pairs.sort(key=lambda pair: (pair[0].source_rel, pair[0].line_start))
    n_conv = len(convertible_pairs)
    print(f"Convertible candidates ({n_conv}) — Phase 2 will rank these:")
    if not convertible_pairs:
        print("  (none)")
    else:
        for rule, cl in convertible_pairs:
            snippet = rule.normalized_text[:70]
            if len(rule.normalized_text) > 70:
                snippet += "…"
            loc = f"{rule.source_rel}:{rule.line_start}"
            print(f"  {rule.rule_id[:8]}  {loc}  \"{snippet}\"  [{cl.convert_kind}]")


# ---------------------------------------------------------------------------
# audit — JSON output
# ---------------------------------------------------------------------------


def _build_audit_json(
    config_root_raw: str,
    parse_result: ParseResult,
    sanitized_findings: List[Finding],
    classifications: List[Classification],
    rules: List[Rule],
    config_root: Path,
) -> str:
    """Build deterministic JSON output for the audit command.

    ``config_root_raw`` is the string the user passed (echoed verbatim for
    relative inputs; machine-independent).  All lists are sorted deterministically;
    ``json.dumps`` uses ``indent=2, sort_keys=True`` for byte-stable output.
    """
    # findings: sorted by (source_rel, line or -1, kind)
    findings_dicts = [_finding_to_dict(f) for f in sanitized_findings]
    findings_dicts.sort(key=_finding_sort_key)

    # rules: sorted by (source_rel, line_start)
    rules_dicts = [_rule_to_dict(r) for r in rules]
    rules_dicts.sort(key=lambda d: (d["source_rel"], d["line_start"]))

    # sources: sorted by sanitized path
    sources_dicts = [_source_to_dict(sf, config_root) for sf in parse_result.sources]
    sources_dicts.sort(key=lambda d: d["path"])

    # classification_counts
    counts: Dict[str, int] = {cat: 0 for cat in _ALL_CATEGORIES}
    for cl in classifications:
        counts[cl.category] = counts.get(cl.category, 0) + 1

    # convertible candidates: sorted by (source_rel, line_start)
    convertible_pairs = [
        (rule, cl)
        for rule, cl in zip(rules, classifications)
        if cl.category == CATEGORY_CONVERTIBLE
    ]
    convertible_pairs.sort(key=lambda pair: (pair[0].source_rel, pair[0].line_start))
    convertible_list = [_convertible_entry(r, cl) for r, cl in convertible_pairs]

    output = {
        "classification_counts": counts,
        "config_root": config_root_raw,
        "convertible": convertible_list,
        "findings": findings_dicts,
        "rules": rules_dicts,
        "sources": sources_dicts,
    }
    return json.dumps(output, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# audit command dispatcher
# ---------------------------------------------------------------------------


def _cmd_audit(args: argparse.Namespace) -> int:
    """Implement ``misfire audit``.

    Observer posture: always exits 0 regardless of findings.
    Prints to stdout only.  All paths in output are machine-independent
    (sanitized at the output boundary — no ``/Users/<name>/`` leakage).
    """
    # Preserve the raw input for display / JSON config_root field
    config_root_raw: str = args.config_root if args.config_root is not None else "~/.claude"

    if args.config_root is None:
        config_root = Path.home() / ".claude"
    else:
        config_root = Path(args.config_root).expanduser().resolve()

    project_dir: Optional[Path] = None
    if args.project_dir:
        project_dir = Path(args.project_dir).expanduser().resolve()

    base_dir: Optional[Path] = None
    if args.base_dir:
        base_dir = Path(args.base_dir).expanduser().resolve()

    config_root_display = _display_config_root(config_root_raw)

    # Run the three phases
    parse_result = parse_config(config_root, project_dir=project_dir)
    findings = audit_all(parse_result, base_dir=base_dir, project_dir=project_dir)
    classifications = classify_rules(parse_result.rules)
    rules = parse_result.rules

    # Sanitize all findings at the output boundary (privacy + portability)
    sanitized_findings = [_sanitize_finding(f, config_root) for f in findings]

    if args.json:
        print(_build_audit_json(
            config_root_raw,
            parse_result,
            sanitized_findings,
            classifications,
            rules,
            config_root,
        ))
    else:
        _print_audit_text(
            config_root_display,
            parse_result,
            sanitized_findings,
            classifications,
            rules,
        )

    return 0  # Observer posture: always 0


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


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

    # --- audit ---
    audit_parser = subparsers.add_parser(
        "audit",
        help=(
            "[Phase 1] Static parse + deterministic audit: stale paths, "
            "token rent, conflicts, load-fidelity"
        ),
    )
    audit_parser.add_argument(
        "config_root",
        nargs="?",
        default=None,
        metavar="CONFIG_ROOT",
        help="config root directory to audit (default: ~/.claude)",
    )
    audit_parser.add_argument(
        "--project-dir",
        dest="project_dir",
        metavar="DIR",
        default=None,
        help="project directory for project-scoped sources and load-fidelity checks",
    )
    audit_parser.add_argument(
        "--base-dir",
        dest="base_dir",
        metavar="DIR",
        default=None,
        help="base directory for resolving bare-relative path tokens (stale_path audit)",
    )
    audit_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="output deterministic JSON (byte-stable; sort_keys=True)",
    )

    # --- rank (Phase 2 stub) ---
    subparsers.add_parser(
        "rank",
        help=(
            "[Phase 2] Evidence-ranked list of prose rules your agents "
            "demonstrably ignore, from your run history"
        ),
    )

    # --- evidence (Phase 2 stub) ---
    subparsers.add_parser(
        "evidence",
        help=(
            "[Phase 2] Show raw per-rule evidence: violation + support counts "
            "and raw excerpts"
        ),
    )

    # --- convert (Phase 3 stub) ---
    subparsers.add_parser(
        "convert",
        help=(
            "[Phase 3] Scaffold a deterministic hook for a violated convertible "
            "rule; print the diff for review"
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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
