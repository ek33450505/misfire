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
- Bash command excerpts in evidence output are sanitized (``/Users/<n>/`` → ``~/``).
Sanitization is applied at the output boundary in this module; ``audit.py`` and
``parse.py`` internals are unchanged.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from misfire import __version__
from misfire.adapters.cast_db import (
    CastDbResult,
    CONVERT_OUTPUT_SHAPE,
    castdb_available,
    find_output_shape_violations,
)
from misfire.adapters.transcript import iter_tool_actions
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
    CONVERT_NEVER_COMMAND,
    CONVERT_TOOL_SUBSTITUTION,
    classify_rules,
)
from misfire.evidence import ToolAction
from misfire.match import find_violations, RuleViolation
from misfire.parse import ParseResult, Rule, SourceFile, _collapse_home, parse_config
from misfire.rank import (
    RECOMMENDATION_ENFORCE_CANDIDATE,
    RECOMMENDATION_INSUFFICIENT_EVIDENCE,
    RECOMMENDATION_OBSERVED_NO_VIOLATIONS,
    rank_rules,
    RankReport,
    RankedRule,
)
from misfire.scaffold import (
    HookScaffold,
    RUNG_ENFORCE,
    RUNG_KEEP,
    detect_claude_version,
    event_support_note,
    scaffold_hook,
)


# ---------------------------------------------------------------------------
# Optional cast.db enablement (portable-first: default OFF)
# ---------------------------------------------------------------------------


class _CastDbDefault:
    """Sentinel: ``--cast-db`` given with NO value → use the default cast.db."""


# argparse ``const`` value meaning "use the default ~/.claude/cast.db path".
_CASTDB_DEFAULT = _CastDbDefault()


def _resolve_cast_db(cast_db_arg: object) -> Optional[Path]:
    """Resolve the ``--cast-db`` argument into a usable Path, or ``None`` if OFF.

    - ``None`` (flag absent)              → ``None`` (cast.db is NOT touched).
    - ``_CASTDB_DEFAULT`` (flag, no value) → ``~/.claude/cast.db``.
    - a string (flag with a value)         → that path.

    The path is only ``expanduser``-ed, NOT ``resolve``-d: a relative input stays
    relative and a ``~`` input home-collapses, so the ``db_path_rel`` echoed in
    output stays portable (mirroring how ``config_root_raw`` is preserved).
    """
    if cast_db_arg is None:
        return None
    if isinstance(cast_db_arg, _CastDbDefault):
        return (Path("~") / ".claude" / "cast.db").expanduser()
    return Path(str(cast_db_arg)).expanduser()


def _cast_db_summary(result: CastDbResult) -> Dict:
    """Build the JSON/text provenance summary for a cast.db result."""
    return {
        "agent_runs_denominator": result.agent_runs_denominator,
        "db_path_rel": result.db_path_rel,
        "mapped_violations": result.mapped_violations,
        "total_violations_read": result.total_violations_read,
        "unmapped_by_signal": result.unmapped_by_signal,
    }


def _maybe_augment_with_cast_db(
    args: argparse.Namespace,
    classifications: List[Classification],
    rules_by_id: Dict[str, Rule],
    rule_violations: List[RuleViolation],
) -> Tuple[List[RuleViolation], Optional[CastDbResult]]:
    """Optionally append cast.db output_shape violations to ``rule_violations``.

    Portable-first / observer posture:
    - Flag absent → returns the input list unchanged and ``None`` (cast.db is
      never opened).
    - DB unavailable (missing / unreadable / missing tables) → prints a concise
      notice to STDERR and returns the input list unchanged and ``None``.  Exit
      status is unaffected (the caller still returns 0).
    - DB available → returns ``input + result.rule_violations`` and the result.
    """
    cast_db_path = _resolve_cast_db(args.cast_db)
    if cast_db_path is None:
        return rule_violations, None

    availability = castdb_available(cast_db_path)
    if not availability.available:
        print(
            f"cast.db: {availability.reason} at {availability.db_path_rel} "
            "— continuing with transcript evidence only",
            file=sys.stderr,
        )
        return rule_violations, None

    result = find_output_shape_violations(
        classifications, rules_by_id, db_path=cast_db_path
    )
    return rule_violations + result.rule_violations, result


def _print_cast_db_provenance(result: CastDbResult) -> None:
    """Print a concise cast.db provenance line to STDERR (text mode)."""
    print(
        f"cast.db: {result.db_path_rel} — "
        f"{result.total_violations_read} protocol violations read, "
        f"{result.mapped_violations} mapped, "
        f"agent_runs denominator {result.agent_runs_denominator}",
        file=sys.stderr,
    )
    if result.unmapped_by_signal:
        unmapped = ", ".join(
            f"{k}={v}" for k, v in sorted(result.unmapped_by_signal.items())
        )
        print(f"cast.db: unmapped (no matching prose rule): {unmapped}", file=sys.stderr)


def _disclaimer_excluding_castdb_actions(
    report: RankReport,
    transcript_violations: List[RuleViolation],
    cast_db_result: CastDbResult,
) -> str:
    """Rewrite the rank disclaimer so cast.db ``agent_runs`` are not mislabeled.

    ``rank_rules`` sums ``opportunity_count`` across ALL ranked rules and labels
    the total "observed tool actions".  cast.db output_shape ``RuleViolation``s
    carry the ``agent_runs`` denominator (one shared value, emitted once PER
    signal — so it is double-counted across the handoff + status rules), and
    ``agent_runs`` are NOT tool actions.  We replace the headline figure with the
    transcript-only action total and disclose the single ``agent_runs``
    denominator separately, exactly once.
    """
    transcript_actions = sum(rv.opportunity_count for rv in transcript_violations)
    castdb_actions = sum(rv.opportunity_count for rv in cast_db_result.rule_violations)
    n_castdb = len(cast_db_result.rule_violations)
    n_rules_total = len(transcript_violations) + n_castdb
    old_total = transcript_actions + castdb_actions
    old_first = (
        f"Rankings reflect {old_total} observed tool actions "
        f"across {n_rules_total} active rules."
    )
    new_first = (
        f"Rankings reflect {transcript_actions} observed tool actions "
        f"across {n_rules_total} active rules "
        f"({n_castdb} cast.db output_shape rule(s) are scored against "
        f"{cast_db_result.agent_runs_denominator} agent_runs — agent_runs are "
        f"NOT tool actions and are not summed into this total)."
    )
    return report.disclaimer.replace(old_first, new_first, 1)


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
# Command sanitization (evidence output — strip /Users/<n>/ from commands)
# ---------------------------------------------------------------------------

# Matches /Users/<name> or /home/<name> (for portability to Linux CI runners).
# NO trailing slash is required: a bare "/Users/ed" (e.g. "cwd: /Users/ed") must
# also collapse, or the username leaks.  The replacement is "~" (not "~/") so
# "/Users/alice/secret" → "~/secret" and bare "/Users/bob" → "~" both hold.
_ABS_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s]+")


def _sanitize_command_str(cmd: str) -> str:
    """Collapse ``/Users/<name>`` or ``/home/<name>`` → ``~`` in a command string.

    Applied to all Bash command excerpts in ``evidence`` output so that real
    transcripts containing absolute paths never leak a username — including a
    bare directory/cwd reference with no trailing slash.
    """
    return _ABS_HOME_PATH_RE.sub("~", cmd)


# ---------------------------------------------------------------------------
# Escape-hatch extraction (auto-detect sanctioned exception markers)
# ---------------------------------------------------------------------------

# Matches an escape-hatch/exception keyword followed (within 150 chars) by a
# backtick-wrapped literal span.  The literal span may contain spaces
# (e.g. "`CAST_COMMIT_AGENT=1 git commit`") — [^`]+ allows any char except
# the closing backtick.
#
# Trigger keywords: "escape hatch", "escape-hatch", "exception", "unless"
# (case-insensitive).  Only extracts when BOTH a trigger keyword AND a
# following backtick-wrapped literal are clearly present; otherwise → nothing.
_ESCAPE_HATCH_RE = re.compile(
    r"(?:escape[\s\-]+hatch|exception|unless).{0,150}?`([^`]+)`",
    re.IGNORECASE,
)

# Matches an env-var assignment token of the form VAR=VALUE where VALUE is
# non-whitespace.  Used by _refine_exception_marker to extract the distinctive
# part of a backtick span (e.g. "CAST_COMMIT_AGENT=1 git commit" →
# "CAST_COMMIT_AGENT=1") so that substring checks match export/env/&&/multi-var
# real-world command variants.
_ENV_VAR_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*=\S+)")


def _refine_exception_marker(span: str, predicate_match: str) -> str:
    """Reduce a full backtick span to its most distinctive substring marker.

    Priority:
    1. If span contains an env-var assignment token (``VAR=VALUE``), return
       that token alone.  A substring check on ``CAST_COMMIT_AGENT=1`` then
       matches ``export CAST_COMMIT_AGENT=1 && git commit``,
       ``env CAST_COMMIT_AGENT=1 OTHER=1 git commit``, etc.
    2. If span ends with the predicate's forbidden command (e.g. ``git commit``),
       strip it.  The remaining prefix is the distinctive part.
    3. Return the full span unchanged.

    Args:
        span:             The content of the backtick-quoted literal from the
                          escape-hatch clause.
        predicate_match:  The ``predicate["match"]`` string for the rule (the
                          forbidden command, e.g. ``"git commit"``).

    Returns:
        The most distinctive substring marker for use in ``find_violations``.
    """
    # 1. Env-var token
    env_m = _ENV_VAR_TOKEN_RE.search(span)
    if env_m:
        return env_m.group(1)

    # 2. Strip trailing forbidden command
    stripped = span.strip()
    if predicate_match:
        suffix = " " + predicate_match
        if stripped.endswith(suffix):
            refined = stripped[: -len(suffix)].strip()
            if refined:
                return refined

    # 3. Full span
    return span


def _extract_exceptions(
    classifications: List[Classification],
    rules_by_id: Dict[str, Rule],
) -> Dict[str, str]:
    """Extract escape-hatch exception markers from convertible rule raw texts.

    Scans the ``raw_text`` (backtick-aware) of each ``never_command`` or
    ``tool_substitution`` convertible rule for a clearly-stated escape hatch
    that names a literal
    marker in backticks.  The extracted marker is refined to its most
    distinctive token (FIX A: env-var token preferred over the full span so
    that ``export VAR=1 && git commit`` variants are also caught).

    After extraction, propagates each exception to ALL ``never_command`` rules
    that share the same ``predicate["match"]`` — so a hatch stated once for
    ``"git commit"`` covers every rule that forbids ``git commit``, regardless
    of whether each individual rule's text restates it (FIX B).  Only
    exceptions actually extracted from some rule's text are propagated; none
    are invented.

    Returns a dict of ``{rule_id: exception_marker}`` for use with
    ``find_violations``.
    """
    # Step 1 — collect per-rule exceptions from rules that explicitly state a hatch,
    # and map rule_id → predicate_match for all active never_command rules.
    raw_exceptions: Dict[str, str] = {}   # rule_id → refined marker (hatch-stating rules only)
    predicate_match_for: Dict[str, str] = {}  # rule_id → predicate["match"]

    for cl in classifications:
        if cl.category != CATEGORY_CONVERTIBLE or cl.convert_kind not in (
            CONVERT_NEVER_COMMAND,
            CONVERT_TOOL_SUBSTITUTION,
        ):
            continue
        if cl.predicate is None:
            continue
        # never_command predicates carry "match"; tool_substitution carry "forbidden".
        # Honoring the hatch for both keeps violation accounting AND the generated
        # hook from penalizing usage the rule itself sanctions.
        pred_match = cl.predicate.get("match") or cl.predicate.get("forbidden", "")
        predicate_match_for[cl.rule_id] = pred_match

        rule = rules_by_id.get(cl.rule_id)
        if rule is None:
            continue
        m = _ESCAPE_HATCH_RE.search(rule.raw_text)
        if m:
            span = m.group(1).strip()
            if span:
                raw_exceptions[cl.rule_id] = _refine_exception_marker(span, pred_match)

    if not raw_exceptions:
        return {}

    # Step 2 — build predicate_match → exception_marker from rules that stated a hatch.
    match_to_marker: Dict[str, str] = {}
    for rule_id, marker in raw_exceptions.items():
        pred_match = predicate_match_for.get(rule_id, "")
        if pred_match:
            match_to_marker[pred_match] = marker

    # Step 3 — propagate: every active never_command rule whose predicate["match"]
    # has a known exception gets that exception (FIX B).
    exceptions: Dict[str, str] = {}
    for rule_id, pred_match in predicate_match_for.items():
        if pred_match in match_to_marker:
            exceptions[rule_id] = match_to_marker[pred_match]

    return exceptions


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


def _ranked_rule_to_dict(rr: RankedRule) -> Dict:
    """Serialize a RankedRule to a JSON-safe dict (all paths already sanitized)."""
    return {
        "confidence": rr.confidence,
        "convert_kind": rr.convert_kind,
        "excluded_by_exception": rr.excluded_by_exception,
        "meets_support_floor": rr.meets_support_floor,
        "opportunity_count": rr.opportunity_count,
        "predicate": rr.predicate,
        "recommendation": rr.recommendation,
        "rule_excerpt": rr.rule_excerpt,
        "rule_id": rr.rule_id,
        "source_rel": rr.source_rel,
        "violation_count": rr.violation_count,
        "violation_rate": rr.violation_rate,
    }


def _violation_to_dict(action: ToolAction) -> Dict:
    """Serialize a ToolAction violation to a JSON-safe dict with command sanitized.

    When ``command`` is empty (cast.db rows, Edit/Write, tool_substitution), the
    ``command`` field falls back to the (already-sanitised) ``input_summary`` so
    the evidence is never blank.
    """
    return {
        "agent_type": action.agent_type,
        "command": _sanitize_command_str(action.command or action.input_summary),
        "is_sidechain": action.is_sidechain,
        "session_id": action.session_id,
        "timestamp": action.timestamp,
        "transcript_rel": action.transcript_rel,
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

# Recommendation buckets in display order (same as _RECOMMENDATION_TIER in rank.py)
_RANK_GROUPS = [
    "enforce_candidate",
    "insufficient_evidence",
    "observed_no_violations",
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
        "config_root": _display_config_root(config_root_raw),
        "convertible": convertible_list,
        "findings": findings_dicts,
        "rules": rules_dicts,
        "sources": sources_dicts,
    }
    return json.dumps(output, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# rank — text output
# ---------------------------------------------------------------------------


def _print_rank_text(
    report: RankReport,
    config_root_display: str,
    projects_dir_display: str,
) -> None:
    """Print the human-readable rank report to stdout."""
    n_active = len(report.ranked)
    print(f"misfire rank — {config_root_display}")
    print(f"Projects dir: {projects_dir_display}")
    print(f"Active rules: {n_active}")
    print()
    print(
        f"Thresholds: min_support={report.thresholds['min_support']}  "
        f"min_violations={report.thresholds['min_violations']}"
    )
    print()

    groups: Dict[str, List[RankedRule]] = {g: [] for g in _RANK_GROUPS}
    for rr in report.ranked:
        groups.setdefault(rr.recommendation, []).append(rr)

    for group_key in _RANK_GROUPS:
        grp = groups.get(group_key, [])
        print(f"=== {group_key} ({len(grp)}) ===")
        if not grp:
            print("  (none)")
        else:
            for i, rr in enumerate(grp, 1):
                rate_pct = f"{rr.violation_rate * 100:.1f}%"
                excl_note = (
                    f"  excluded (sanctioned): {rr.excluded_by_exception}"
                    if rr.excluded_by_exception
                    else ""
                )
                print(
                    f"\n  {i}. {rr.source_rel}  [{rr.convert_kind}]  "
                    f"confidence={rr.confidence}"
                )
                print(f"     rule_id: {rr.rule_id}")
                print(
                    f"     violations: {rr.violation_count}  "
                    f"opportunities: {rr.opportunity_count}  "
                    f"rate: {rate_pct}{excl_note}"
                )
                print(f"     \"{rr.rule_excerpt}\"")
        print()

    print("---")
    print(report.disclaimer)


# ---------------------------------------------------------------------------
# rank — JSON output
# ---------------------------------------------------------------------------


def _build_rank_json(
    report: RankReport,
    config_root_raw: str,
    cast_db_result: Optional[CastDbResult] = None,
) -> str:
    """Build deterministic JSON for the rank command.

    Uses ``sort_keys=True, indent=2`` for byte-stable output across machines.
    The ``ranked`` list is already in canonical order from ``rank_rules``.

    When ``cast_db_result`` is provided (the optional cast.db substrate was
    engaged and the DB was available), a top-level ``cast_db`` provenance object
    is included.  Absent the flag, the output is byte-identical to before.
    """
    output = {
        "config_root": _display_config_root(config_root_raw),
        "disclaimer": report.disclaimer,
        "ranked": [_ranked_rule_to_dict(rr) for rr in report.ranked],
        "thresholds": report.thresholds,
    }
    if cast_db_result is not None:
        output["cast_db"] = _cast_db_summary(cast_db_result)
    return json.dumps(output, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# rank command dispatcher
# ---------------------------------------------------------------------------


def _cmd_rank(args: argparse.Namespace) -> int:
    """Implement ``misfire rank``.

    Pipeline: parse_config → classify_rules → extract_exceptions →
    iter_tool_actions → find_violations → rank_rules → print.

    Observer posture: always exits 0.  All output is PII-free (sanitized at
    the output boundary — no ``/Users/<name>/`` leakage).
    """
    config_root_raw: str = args.config_root if args.config_root is not None else "~/.claude"
    config_root: Path = (
        Path(args.config_root).expanduser().resolve()
        if args.config_root is not None
        else Path.home() / ".claude"
    )

    projects_dir: Path = (
        Path(args.projects_dir).expanduser().resolve()
        if args.projects_dir
        else Path.home() / ".claude" / "projects"
    )

    min_support: int = args.min_support
    min_violations: int = args.min_violations

    config_root_display = _display_config_root(config_root_raw)
    projects_dir_display = _collapse_home(projects_dir)

    parse_result = parse_config(config_root)
    rules = parse_result.rules
    classifications = classify_rules(rules)
    rules_by_id: Dict[str, Rule] = {r.rule_id: r for r in rules}

    exceptions = _extract_exceptions(classifications, rules_by_id)

    actions = iter_tool_actions(projects_dir)
    transcript_violations = find_violations(classifications, actions, exceptions=exceptions)

    # OPTIONAL cast.db substrate (default OFF; portable-first).  Augments the
    # transcript evidence with output_shape (Handoff / Status) violations.
    rule_violations, cast_db_result = _maybe_augment_with_cast_db(
        args, classifications, rules_by_id, transcript_violations
    )

    report = rank_rules(
        rule_violations,
        rules_by_id,
        min_support=min_support,
        min_violations=min_violations,
    )

    # HONESTY FIX: when cast.db is engaged, rank_rules folded the agent_runs
    # denominators (one per signal, double-counted) into the disclaimer's
    # "observed tool actions" headline.  Correct that figure here so agent_runs
    # are disclosed separately and never mislabeled as tool actions.
    if cast_db_result is not None:
        report = dataclasses.replace(
            report,
            disclaimer=_disclaimer_excluding_castdb_actions(
                report, transcript_violations, cast_db_result
            ),
        )

    if args.json:
        print(_build_rank_json(report, config_root_raw, cast_db_result))
    else:
        _print_rank_text(report, config_root_display, projects_dir_display)
        if cast_db_result is not None:
            _print_cast_db_provenance(cast_db_result)

    return 0


# ---------------------------------------------------------------------------
# evidence — text output
# ---------------------------------------------------------------------------


def _print_evidence_text(
    rr: RankedRule,
    violations: List[ToolAction],
    rv: RuleViolation,
    config_root_display: str,
    limit: int,
) -> None:
    """Print the human-readable evidence drill-down for a single rule."""
    print(f"misfire evidence — {config_root_display}")
    print(f"Rule: {rr.rule_id}  [{rr.convert_kind}]  confidence={rr.confidence}")
    print(f"\"{rr.rule_excerpt}\"")
    rate_pct = f"{rr.violation_rate * 100:.1f}%"
    if rr.convert_kind == CONVERT_OUTPUT_SHAPE:
        # cast.db-sourced: the denominator is agent_runs (an UPPER BOUND on real
        # Handoff/Status opportunities), so the rate is a conservative LOWER
        # BOUND — never label it a generic "Opportunities"/"Rate".
        print(
            f"Violations: {rr.violation_count}  "
            f"agent_runs (denominator): {rr.opportunity_count}  "
            f"Rate (lower bound): {rate_pct}  "
            f"Excluded (sanctioned): {rr.excluded_by_exception}"
        )
    else:
        print(
            f"Violations: {rr.violation_count}  Opportunities: {rr.opportunity_count}  "
            f"Rate: {rate_pct}  Excluded (sanctioned): {rr.excluded_by_exception}"
        )
    print()

    total = rr.violation_count
    showing = len(violations)
    print(
        f"--- {total} violating action{'s' if total != 1 else ''} "
        f"(showing {showing} of {total}) ---"
    )
    print()

    for action in violations:
        # Fall back to input_summary when there is no command string, so that
        # cast.db evidence (and Edit/Write/tool_substitution evidence, which
        # carry a file path in input_summary rather than a command) is not blank.
        cmd = _sanitize_command_str(action.command or action.input_summary)
        sidechain_str = "sidechain" if action.is_sidechain else "main"
        agent_str = action.agent_type or "main-session"
        print(f"  {action.timestamp}  {cmd[:120]}")
        print(f"  transcript: {action.transcript_rel}  [{sidechain_str}]  agent: {agent_str}")
        print()


# ---------------------------------------------------------------------------
# evidence — JSON output
# ---------------------------------------------------------------------------


def _build_evidence_json(
    rr: RankedRule,
    violations: List[ToolAction],
    config_root_raw: str,
    limit: int,
    cast_db_result: Optional[CastDbResult] = None,
) -> str:
    """Build deterministic JSON for the evidence command.

    When ``cast_db_result`` is provided (the optional cast.db substrate was
    engaged and the DB was available), a top-level ``cast_db`` provenance object
    is included so the agent_runs denominator and the honest under-coverage
    (``unmapped_by_signal``) are never silently dropped on the evidence surface.
    """
    output = {
        "config_root": _display_config_root(config_root_raw),
        "limit": limit,
        "rule": _ranked_rule_to_dict(rr),
        "violations": [_violation_to_dict(a) for a in violations],
    }
    if cast_db_result is not None:
        output["cast_db"] = _cast_db_summary(cast_db_result)
    return json.dumps(output, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# evidence command dispatcher
# ---------------------------------------------------------------------------


def _cmd_evidence(args: argparse.Namespace) -> int:
    """Implement ``misfire evidence``.

    Drills down into the evidence for one rule (``--rule RULE_ID``) or the
    top-ranked rule when no rule is specified.

    Observer posture: exits 0 on success.  All command excerpts in output are
    sanitized (``/Users/<name>/`` → ``~/``).
    """
    config_root_raw: str = args.config_root if args.config_root is not None else "~/.claude"
    config_root: Path = (
        Path(args.config_root).expanduser().resolve()
        if args.config_root is not None
        else Path.home() / ".claude"
    )

    projects_dir: Path = (
        Path(args.projects_dir).expanduser().resolve()
        if args.projects_dir
        else Path.home() / ".claude" / "projects"
    )

    limit: int = args.limit
    rule_id_filter: Optional[str] = args.rule

    config_root_display = _display_config_root(config_root_raw)

    parse_result = parse_config(config_root)
    rules = parse_result.rules
    classifications = classify_rules(rules)
    rules_by_id: Dict[str, Rule] = {r.rule_id: r for r in rules}

    exceptions = _extract_exceptions(classifications, rules_by_id)

    actions = iter_tool_actions(projects_dir)
    rule_violations = find_violations(classifications, actions, exceptions=exceptions)

    # OPTIONAL cast.db substrate (default OFF) — lets a --rule prefix drill into
    # a cast.db-sourced output_shape rule's synthesized violations.
    rule_violations, cast_db_result = _maybe_augment_with_cast_db(
        args, classifications, rules_by_id, rule_violations
    )

    if not rule_violations:
        print("No active convertible rules found.", file=sys.stderr)
        return 0

    report = rank_rules(rule_violations, rules_by_id)
    violations_by_rule_id: Dict[str, RuleViolation] = {
        rv.rule_id: rv for rv in rule_violations
    }

    # Resolve the target rule
    target_rr: Optional[RankedRule] = None
    target_rv: Optional[RuleViolation] = None

    if rule_id_filter:
        # Prefix match on rule_id
        for rv in rule_violations:
            if rv.rule_id.startswith(rule_id_filter):
                target_rv = rv
                target_rr = next(
                    (rr for rr in report.ranked if rr.rule_id == rv.rule_id), None
                )
                break
        if target_rv is None:
            print(
                f"evidence: no rule found with id prefix {rule_id_filter!r}",
                file=sys.stderr,
            )
            return 1
    else:
        # Top-ranked rule
        if not report.ranked:
            print("No ranked rules found.", file=sys.stderr)
            return 0
        target_rr = report.ranked[0]
        target_rv = violations_by_rule_id.get(target_rr.rule_id)

    if target_rv is None or target_rr is None:
        print("evidence: could not resolve target rule", file=sys.stderr)
        return 1

    # Cap at limit
    capped_violations = target_rv.violations[:limit]

    if args.json:
        print(
            _build_evidence_json(
                target_rr, capped_violations, config_root_raw, limit, cast_db_result
            )
        )
    else:
        _print_evidence_text(
            target_rr, capped_violations, target_rv, config_root_display, limit
        )
        # Surface cast.db provenance (denominator + honest under-coverage) so it
        # is never silently dropped on the evidence surface, mirroring rank.
        if cast_db_result is not None:
            _print_cast_db_provenance(cast_db_result)

    return 0


# ---------------------------------------------------------------------------
# convert (Phase 3) — the evidence-ranked convert-to-hook wedge
# ---------------------------------------------------------------------------

_CONVERT_DISCLAIMER = (
    "misfire is an observer: this scaffold is printed for you to review — "
    "misfire never writes settings.json. A converted rule should usually stay "
    "in prose too (defense in depth). Only rules with BOTH sufficient support "
    "AND observed violations are evidence-grounded conversion candidates."
)

_EVIDENCE_STATUS_NOT_COMPUTED = "not_computed"

_STATUS_KEEP = "keep"
_STATUS_ENFORCE = "enforce"
_STATUS_NOTHING = "nothing_to_convert"


# Leading markdown marker (bullet / numbered / heading / blockquote) to strip
# from a faithful excerpt.
_LEADING_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+\.|#{1,6}|>)\s*")


def _faithful_excerpt(raw_text: str, limit: int = 100) -> str:
    """Faithful, sanitized, truncated rule text for display + the deny reason.

    Sourced from ``Rule.raw_text`` (NOT ``normalized_text``) so identifiers like
    ``CAST_COMMIT_AGENT=1`` survive intact — the deny reason a blocked user reads
    must quote the rule and its escape hatch correctly.  (``normalized_text``'s
    markdown italic-strip eats the underscores in snake_case, turning
    ``CAST_COMMIT_AGENT`` into ``CASTCOMMITAGENT`` — wrong remediation in the one
    place it matters.)  Strips a leading markdown marker and backticks, collapses
    whitespace, and removes any home path (no ``/Users/<name>/`` leak).
    """
    text = _LEADING_MARKER_RE.sub("", raw_text)
    text = text.replace("`", "")
    text = _sanitize_command_str(text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _is_recommended(rr: Optional[RankedRule]) -> bool:
    """A conversion is *recommended* only when it is an evidence-grounded
    enforce_candidate (sufficient support AND observed violations)."""
    return rr is not None and rr.recommendation == RECOMMENDATION_ENFORCE_CANDIDATE


def _evidence_summary(rr: Optional[RankedRule]) -> Dict:
    """Serialize the evidence status for a target rule (``not_computed`` if none)."""
    if rr is None:
        return {
            "status": _EVIDENCE_STATUS_NOT_COMPUTED,
            "violation_count": None,
            "opportunity_count": None,
            "violation_rate": None,
        }
    return {
        "status": rr.recommendation,
        "violation_count": rr.violation_count,
        "opportunity_count": rr.opportunity_count,
        "violation_rate": rr.violation_rate,
    }


def _convert_honesty_note(sc: HookScaffold, rr: Optional[RankedRule]) -> str:
    """The honesty line tying the verdict to (the absence of) evidence."""
    if sc.rung == RUNG_KEEP:
        return sc.reason
    if sc.is_skeleton:
        return (
            "before_action / after_action rules carry NO violation evidence "
            "(ordering is not reconstructible) — UNRANKED. Skeleton shown for "
            "you to complete; not an evidence-grounded recommendation."
        )
    if rr is None:
        return (
            "Evidence not computed for this rule (no matching opportunities in "
            "the scanned run history). Scaffold shown for reference only."
        )
    if rr.recommendation == RECOMMENDATION_ENFORCE_CANDIDATE:
        return (
            f"Evidence-grounded: {rr.violation_count} observed violation(s) across "
            f"{rr.opportunity_count} opportunities "
            f"({rr.violation_rate * 100:.1f}%)."
        )
    if rr.recommendation == RECOMMENDATION_OBSERVED_NO_VIOLATIONS:
        return (
            "0 observed violations — NOT an evidence-grounded conversion (the "
            "honesty guard: a non-triggered rule is not a conversion signal). "
            "Consider KEEP; scaffold shown for reference only."
        )
    # insufficient_evidence
    return (
        f"Below the support floor ({rr.violation_count} violation(s), "
        f"{rr.opportunity_count} opportunities) — weak evidence; scaffold shown "
        "for reference only."
    )


def _scaffold_to_hook_dict(sc: HookScaffold) -> Optional[Dict]:
    """Serialize an ENFORCE scaffold's hook payload (``None`` for KEEP/ELEVATE)."""
    if sc.rung != RUNG_ENFORCE:
        return None
    return {
        "event": sc.event,
        "filename": sc.hook_filename,
        "is_skeleton": sc.is_skeleton,
        "matcher": sc.matcher,
        "script": sc.hook_script,
        "settings_snippet": sc.settings_snippet,
    }


def _convert_result_dict(
    config_root_raw: str,
    sc: HookScaffold,
    rule: Rule,
    rr: Optional[RankedRule],
    recommended: bool,
    note: str,
) -> Dict:
    """Build the deterministic result dict for both text and JSON rendering."""
    status = _STATUS_KEEP if sc.rung == RUNG_KEEP else _STATUS_ENFORCE
    hook = _scaffold_to_hook_dict(sc)
    rule_dict = {
        "convert_kind": sc.convert_kind,
        "excerpt": _faithful_excerpt(rule.raw_text),
        "rule_id": rule.rule_id,
        "rung": sc.rung,
        "source_rel": rule.source_rel,
    }
    return {
        "caveats": list(sc.caveats),
        "config_root": _display_config_root(config_root_raw),
        "disclaimer": _CONVERT_DISCLAIMER,
        "evidence": _evidence_summary(rr),
        "hook": hook,
        "recommended": recommended,
        "reason": note,
        "rule": rule_dict,
        "status": status,
    }


def _nothing_to_convert_result(config_root_raw: str) -> Dict:
    """Result dict for the honest 'no enforce_candidate' outcome."""
    return {
        "caveats": [],
        "config_root": _display_config_root(config_root_raw),
        "disclaimer": _CONVERT_DISCLAIMER,
        "evidence": None,
        "hook": None,
        "recommended": False,
        "reason": (
            "No rule qualifies for conversion: none have both sufficient support "
            "AND observed violations (the honesty guard). Nothing to convert."
        ),
        "rule": None,
        "status": _STATUS_NOTHING,
    }


def _print_convert_nothing(config_root_display: str) -> None:
    """Text rendering of the nothing-to-convert outcome."""
    print(f"misfire convert — {config_root_display}")
    print()
    print("Nothing to convert.")
    print(
        "No rule qualifies: none have both sufficient support AND observed "
        "violations\n(the honesty guard). Run `misfire rank` to see the evidence."
    )
    print()
    print(_CONVERT_DISCLAIMER)


def _print_convert_text(result: Dict, config_root_display: str) -> None:
    """Human-readable rendering of a convert result (KEEP or ENFORCE)."""
    rule = result["rule"]
    print(f"misfire convert — {config_root_display}")
    kind = rule["convert_kind"] or "n/a"
    print(f"Rule: {rule['rule_id']}  [{kind}]  ({rule['source_rel']})")
    print(f"\"{rule['excerpt']}\"")
    print()

    ev = result["evidence"]
    if ev and ev["status"] != _EVIDENCE_STATUS_NOT_COMPUTED:
        rate = ev["violation_rate"]
        rate_pct = f"{rate * 100:.1f}%" if rate is not None else "n/a"
        print(
            f"Evidence: {ev['status']}  "
            f"violations={ev['violation_count']}  "
            f"opportunities={ev['opportunity_count']}  rate={rate_pct}"
        )

    print(
        f"Verdict: {result['status'].upper()}  "
        f"recommended={str(result['recommended']).lower()}"
    )
    print(result["reason"])
    print()

    hook = result["hook"]
    if hook is not None:
        skeleton = "  [SKELETON — complete the TODO before installing]" if hook["is_skeleton"] else ""
        print(f"=== ENFORCE: {hook['event']} hook (matcher: {hook['matcher']}){skeleton} ===")
        print()
        print(f"--- save as: .claude/hooks/{hook['filename']} (chmod +x) ---")
        print(hook["script"])
        print("--- settings.json (merge this; misfire does NOT write it) ---")
        print(json.dumps(hook["settings_snippet"], indent=2, sort_keys=True))
        print()

    if result["caveats"]:
        print("Caveats:")
        for c in result["caveats"]:
            print(f"  - {c}")
        print()

    print(result["disclaimer"])


def _cmd_convert(args: argparse.Namespace) -> int:
    """Implement ``misfire convert``.

    Two modes:
    - ``--rule RULE_ID`` — target a specific rule (by id prefix).  Non-convertible
      rules return a KEEP verdict with NO hook (the honesty guard for safety /
      judgment / output-shape / non-directive rules).  A convertible rule with
      zero observed violations is shown for reference only (``recommended=false``).
    - default / ``--top`` — convert the top evidence-grounded enforce_candidate.
      If none qualifies, prints an honest "nothing to convert" and exits 0.

    Observer posture: always exits 0 on a resolved target (1 only when an
    explicit ``--rule`` prefix matches nothing).  Never writes settings.json.
    All output is PII-free (sanitized at the output boundary).
    """
    config_root_raw: str = args.config_root if args.config_root is not None else "~/.claude"
    config_root: Path = (
        Path(args.config_root).expanduser().resolve()
        if args.config_root is not None
        else Path.home() / ".claude"
    )
    projects_dir: Path = (
        Path(args.projects_dir).expanduser().resolve()
        if args.projects_dir
        else Path.home() / ".claude" / "projects"
    )
    config_root_display = _display_config_root(config_root_raw)

    parse_result = parse_config(config_root)
    rules = parse_result.rules
    classifications = classify_rules(rules)
    rules_by_id: Dict[str, Rule] = {r.rule_id: r for r in rules}
    classifications_by_id: Dict[str, Classification] = {
        c.rule_id: c for c in classifications
    }

    exceptions = _extract_exceptions(classifications, rules_by_id)

    # Evidence grounding (best-effort) — scan run history → rank.
    actions = iter_tool_actions(projects_dir)
    rule_violations = find_violations(classifications, actions, exceptions=exceptions)
    report = rank_rules(
        rule_violations,
        rules_by_id,
        min_support=args.min_support,
        min_violations=args.min_violations,
    )
    ranked_by_id: Dict[str, RankedRule] = {rr.rule_id: rr for rr in report.ranked}

    # ------------------------------------------------------------------
    # Resolve the target rule
    # ------------------------------------------------------------------
    if args.rule:
        target_rule: Optional[Rule] = next(
            (r for r in rules if r.rule_id.startswith(args.rule)), None
        )
        if target_rule is None:
            print(
                f"convert: no rule found with id prefix {args.rule!r}",
                file=sys.stderr,
            )
            return 1
        target_cl = classifications_by_id[target_rule.rule_id]
    else:
        # default / --top: the top evidence-grounded enforce_candidate
        top = next(
            (
                rr
                for rr in report.ranked
                if rr.recommendation == RECOMMENDATION_ENFORCE_CANDIDATE
            ),
            None,
        )
        if top is None:
            result = _nothing_to_convert_result(config_root_raw)
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                _print_convert_nothing(config_root_display)
            return 0
        target_cl = classifications_by_id[top.rule_id]
        target_rule = rules_by_id[top.rule_id]

    rr = ranked_by_id.get(target_cl.rule_id)
    marker = exceptions.get(target_cl.rule_id, "")
    sc = scaffold_hook(target_cl, _faithful_excerpt(target_rule.raw_text), marker)
    recommended = _is_recommended(rr)
    note = _convert_honesty_note(sc, rr)
    result = _convert_result_dict(
        config_root_raw, sc, target_rule, rr, recommended, note
    )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_convert_text(result, config_root_display)
        # Version advisory: STDERR only, text mode only → keeps --json deterministic.
        if sc.rung == RUNG_ENFORCE:
            version = detect_claude_version()
            if version:
                print(f"\nClaude Code detected: {version}", file=sys.stderr)
            vnote = event_support_note(sc.event, version)
            if vnote:
                print(vnote, file=sys.stderr)

    return 0


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

    # --- rank (Phase 2) ---
    rank_parser = subparsers.add_parser(
        "rank",
        help=(
            "[Phase 2] Evidence-ranked list of prose rules your agents "
            "demonstrably ignore, from your run history"
        ),
    )
    rank_parser.add_argument(
        "config_root",
        nargs="?",
        default=None,
        metavar="CONFIG_ROOT",
        help="config root directory (default: ~/.claude)",
    )
    rank_parser.add_argument(
        "--projects-dir",
        dest="projects_dir",
        metavar="DIR",
        default=None,
        help="Claude Code projects directory (default: ~/.claude/projects)",
    )
    rank_parser.add_argument(
        "--min-support",
        dest="min_support",
        type=int,
        default=30,
        metavar="N",
        help="minimum opportunity count for trusted ranking (default: 30)",
    )
    rank_parser.add_argument(
        "--min-violations",
        dest="min_violations",
        type=int,
        default=1,
        metavar="N",
        help="minimum violation count to recommend enforcement (default: 1)",
    )
    rank_parser.add_argument(
        "--cast-db",
        dest="cast_db",
        nargs="?",
        const=_CASTDB_DEFAULT,
        default=None,
        metavar="PATH",
        help=(
            "[OPTIONAL, CAST power users] also read output_shape (Handoff / "
            "Status) violations from a cast.db. Flag absent: cast.db is not "
            "touched (portable default). Flag with no value: use "
            "~/.claude/cast.db. Flag with PATH: use that DB. Opened STRICTLY "
            "read-only."
        ),
    )
    rank_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="output deterministic JSON (byte-stable; sort_keys=True)",
    )

    # --- evidence (Phase 2) ---
    evidence_parser = subparsers.add_parser(
        "evidence",
        help=(
            "[Phase 2] Show raw per-rule evidence: violation + support counts "
            "and raw excerpts"
        ),
    )
    evidence_parser.add_argument(
        "config_root",
        nargs="?",
        default=None,
        metavar="CONFIG_ROOT",
        help="config root directory (default: ~/.claude)",
    )
    evidence_parser.add_argument(
        "--rule",
        dest="rule",
        metavar="RULE_ID",
        default=None,
        help="rule_id prefix to drill into (default: top-ranked rule)",
    )
    evidence_parser.add_argument(
        "--projects-dir",
        dest="projects_dir",
        metavar="DIR",
        default=None,
        help="Claude Code projects directory (default: ~/.claude/projects)",
    )
    evidence_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=20,
        metavar="N",
        help="maximum violating actions to show (default: 20)",
    )
    evidence_parser.add_argument(
        "--cast-db",
        dest="cast_db",
        nargs="?",
        const=_CASTDB_DEFAULT,
        default=None,
        metavar="PATH",
        help=(
            "[OPTIONAL, CAST power users] include cast.db output_shape "
            "(Handoff / Status) rules so --rule can drill into their "
            "violations. Flag absent: cast.db not touched. Flag with no value: "
            "~/.claude/cast.db. Opened STRICTLY read-only."
        ),
    )
    evidence_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="output deterministic JSON (byte-stable; sort_keys=True)",
    )

    # --- convert (Phase 3) ---
    convert_parser = subparsers.add_parser(
        "convert",
        help=(
            "[Phase 3] Scaffold a deterministic hook for a violated convertible "
            "rule; print the hook + settings snippet for review (never written)"
        ),
    )
    convert_parser.add_argument(
        "config_root",
        nargs="?",
        default=None,
        metavar="CONFIG_ROOT",
        help="config root directory (default: ~/.claude)",
    )
    convert_parser.add_argument(
        "--rule",
        dest="rule",
        metavar="RULE_ID",
        default=None,
        help="rule_id prefix to convert (default: the top evidence-grounded candidate)",
    )
    convert_parser.add_argument(
        "--top",
        dest="top",
        action="store_true",
        default=False,
        help="convert the top enforce_candidate from rank (the default when no --rule)",
    )
    convert_parser.add_argument(
        "--projects-dir",
        dest="projects_dir",
        metavar="DIR",
        default=None,
        help="Claude Code projects directory for evidence (default: ~/.claude/projects)",
    )
    convert_parser.add_argument(
        "--min-support",
        dest="min_support",
        type=int,
        default=30,
        metavar="N",
        help="minimum opportunity count for trusted ranking (default: 30)",
    )
    convert_parser.add_argument(
        "--min-violations",
        dest="min_violations",
        type=int,
        default=1,
        metavar="N",
        help="minimum violation count to recommend enforcement (default: 1)",
    )
    convert_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="output deterministic JSON (byte-stable; sort_keys=True)",
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
