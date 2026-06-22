"""audit.py — Phase 1 deterministic static audit for misfire.

Four sub-audits + an aggregator:

    audit_stale_paths(parse_result, *, base_dir=None) -> list[Finding]
    audit_token_rent(parse_result)                    -> list[Finding]
    audit_conflicts(parse_result)                     -> list[Finding]
    audit_load_fidelity(parse_result, *, project_dir=None) -> list[Finding]
    audit_all(parse_result, *, base_dir=None, project_dir=None) -> list[Finding]

Design principles
~~~~~~~~~~~~~~~~~
- **Deterministic and conservative**: only flag CLEAR issues.  When in doubt,
  do NOT flag — a false positive (stale path that isn't, conflict that isn't)
  is worse than a miss.
- **Zero-LLM**: stdlib only.  No external calls.
- **Privacy**: ``source_rel`` is always home-collapsed (never leaks
  ``/Users/<name>/``).  Use ``parse.py``'s ``_collapse_home`` for any path
  written into ``Finding``.
- **YAGNI**: audit logic only — no CLI wiring.  That is Phase 1 Unit 4.

Conservative stale-path rules (all must hold to flag a token):
1. Token is ABSOLUTE (``/x/y``) or HOME-relative (``~/x``).
   Bare-relative tokens are NEVER flagged without an explicit ``base_dir``
   — resolving them against the source file dir caused cross-repo FPs.
2. Token has NO placeholder characters (``<``, ``>``, ``{``, ``}``, ``*``,
   ``?``) — those are template variables, glob patterns, or examples.
3. Token can be resolved to a filesystem path without ambiguity.
4. ``os.stat()`` raises ``FileNotFoundError`` or ``NotADirectoryError``
   (confirmed missing). ``PermissionError`` or any other ``OSError``
   → the path may well exist but is unreadable (sandbox, permissions) →
   NOT flagged.  Exists-but-unreadable ≠ missing.
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from misfire.parse import ParseResult, Rule, SourceFile, TIER_IMPORT, _collapse_home
from misfire.classify import classify_rule, CATEGORY_CONVERTIBLE

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"

KIND_STALE_PATH = "stale_path"
KIND_TOKEN_RENT = "token_rent"
KIND_CONFLICT = "conflict"
KIND_LOAD_FIDELITY = "load_fidelity"

# Line-count threshold from the /memory ">200 lines reduces adherence" guidance.
TOKEN_RENT_LINE_THRESHOLD = 200

# Token-count heuristic: chars / 4 (documented here per the task spec).
# This is a rough BPE approximation: 1 token ≈ 4 characters of English text.
_CHARS_PER_TOKEN = 4


@dataclasses.dataclass(frozen=True)
class Finding:
    """A single audit finding.

    ``source_rel``  — home-collapsed display path; never contains ``/Users/<name>/``.
    ``line``        — 1-based line number; ``None`` when the finding applies to
                      the whole file (e.g. token-rent aggregate).
    ``detail``      — structured data for machine consumers.
    """

    kind: str        # stale_path | token_rent | conflict | load_fidelity
    severity: str    # info | warn
    source_rel: str  # home-collapsed, never leak /Users/<name>/
    line: Optional[int]
    message: str
    detail: Dict


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _collapse(p: Path) -> str:
    """Home-collapse a Path for display."""
    return _collapse_home(p)


def _count_lines(path: Path) -> int:
    """Return the number of lines in *path* (0 if unreadable)."""
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:  # fake-success-ok — unreadable file returns 0, not a fake count
        return 0


def _count_chars(path: Path) -> int:
    """Return the character count of *path* (0 if unreadable)."""
    try:
        return len(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:  # fake-success-ok — unreadable file returns 0, not a fake count
        return 0


def _path_definitively_missing(path: Path) -> bool:
    """Return True ONLY when the path is confirmed absent.

    ``os.stat`` semantics:
    - Succeeds → exists.  Return False.
    - ``FileNotFoundError`` → confirmed missing.  Return True.
    - ``NotADirectoryError`` → a component is a file, not a dir.  Return True.
    - ``PermissionError`` → exists but unreadable (sandbox, chmod 000).
      Exists-but-unreadable ≠ missing → Return False.
    - Other ``OSError`` → unknown state → conservative → Return False.
    """
    # fake-success-ok — each branch is an explicit, documented exception route,
    # not a silent swallow.  PermissionError/OSError return False (conservative:
    # "may exist, can't confirm missing") rather than masking the error.
    try:
        os.stat(path)
        return False
    except FileNotFoundError:
        return True
    except NotADirectoryError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Path-token extraction helpers (stale_path audit)
# ---------------------------------------------------------------------------

# Absolute paths: /something (must have at least one component after /)
# Negative lookbehind excludes backtick, word chars, AND tilde (~) so that
# ~/foo does not also emit a tilde-less /.claude/... duplicate.
_ABS_PATH_RE = re.compile(r"(?<![`\w~])(/[\w][\w./-]*)")

# Home-relative paths: ~/something
_HOME_PATH_RE = re.compile(r"(~/[\w./-]+)")

# Backtick-wrapped path-like tokens: `scripts/something.py`
# Require at least one / or a file extension to avoid capturing plain words.
_BACKTICK_PATH_RE = re.compile(r"`([^`\s]+(?:/[^`\s]*|[^`\s]*\.[a-z]{1,6}))`")

# URL pattern — excluded from stale-path checks (not a filesystem path).
_URL_RE = re.compile(r"https?://\S+")

# Code fence: strip content inside ```...``` or ~~~...~~~ blocks.
# Using non-greedy so adjacent fences don't swallow each other.
_FENCE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")

# Placeholder characters that mark template/glob tokens — never real paths.
_PLACEHOLDER_RE = re.compile(r"[<>{}\*\?]")


def _strip_urls(text: str) -> str:
    return _URL_RE.sub("", text)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text)


def _has_placeholder(token: str) -> bool:
    """Return True if *token* contains template/glob metacharacters."""
    return bool(_PLACEHOLDER_RE.search(token))


def _extract_path_tokens(text: str) -> List[str]:
    """Extract candidate filesystem path tokens from ``text``.

    Returns tokens that are:
    - Backtick-wrapped AND path-like (has / or file extension)
    - Absolute (``/x/y``)
    - Home-relative (``~/x``)

    Excludes:
    - Tokens from inside fenced code blocks (examples, not real paths)
    - URLs
    - Tokens with placeholder/glob chars (``<``, ``>``, ``{``, ``}``, ``*``, ``?``)
    - Bare-relative tokens (no leading / or ~/): those are never returned here
    """
    # Strip fences FIRST so backtick paths inside ``` ``` blocks are excluded.
    cleaned = _strip_urls(_strip_fences(text))

    seen: set = set()
    candidates: List[str] = []

    def _add(tok: str) -> None:
        tok = tok.rstrip(".,;)'\"")
        if tok and tok not in seen and not _has_placeholder(tok):
            seen.add(tok)
            candidates.append(tok)

    # Backtick-wrapped paths — run on cleaned so fence contents are already gone.
    for m in _BACKTICK_PATH_RE.finditer(cleaned):
        tok = m.group(1)
        if not _URL_RE.match(tok):
            _add(tok)

    # Absolute paths — from cleaned text (URLs + fences already stripped).
    for m in _ABS_PATH_RE.finditer(cleaned):
        _add(m.group(1))

    # Home-relative paths — from cleaned text.
    for m in _HOME_PATH_RE.finditer(cleaned):
        _add(m.group(1))

    return candidates


def _resolve_path_token(
    token: str,
    *,
    base_dir: Optional[Path],
) -> Optional[Path]:
    """Resolve a path token to an absolute ``Path``, or ``None`` if unresolvable.

    Resolution rules:
    - Absolute paths (``/...``) → as-is.
    - Home paths (``~/...``) → ``Path.home() / rest``.
    - Bare-relative paths → resolved against ``base_dir`` only when the caller
      explicitly provides one (default ``None`` → NOT resolved, NOT flagged).
      The default of not-resolving-relative prevents cross-repo false positives.
    """
    if token.startswith("/"):
        return Path(token)
    if token.startswith("~/"):
        return Path.home() / token[2:]
    # bare-relative: only when an explicit base_dir was passed
    if base_dir is not None:
        return (base_dir / token).resolve()
    return None


# ---------------------------------------------------------------------------
# 1. audit_stale_paths
# ---------------------------------------------------------------------------


def audit_stale_paths(
    parse_result: ParseResult,
    *,
    base_dir: Optional[Path] = None,
) -> List[Finding]:
    """Flag path tokens extracted from rule text that do not exist on disk.

    Conservative rules (ALL must hold to produce a finding):
    - Token is absolute or home-relative (bare-relative → skipped unless
      ``base_dir`` is explicitly provided by the caller).
    - Token has no placeholder/glob metacharacters.
    - ``os.stat()`` raises ``FileNotFoundError`` or ``NotADirectoryError``
      (PermissionError and other OSError → unknown, NOT flagged).
    """
    findings: List[Finding] = []
    seen_pairs: set = set()  # (source_rel, token) — deduplicate across rules

    for rule in parse_result.rules:
        tokens = _extract_path_tokens(rule.raw_text)

        for token in tokens:
            key = (rule.source_rel, token)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)

            # Resolve: only absolute and home-relative are resolved by default.
            # base_dir=None means bare-relative tokens return None → skipped.
            resolved = _resolve_path_token(token, base_dir=base_dir)
            if resolved is None:
                continue  # cannot resolve → do NOT flag

            if _path_definitively_missing(resolved):
                findings.append(Finding(
                    kind=KIND_STALE_PATH,
                    severity=SEVERITY_WARN,
                    source_rel=rule.source_rel,
                    line=rule.line_start,
                    message=f"Path does not exist: {token!r}",
                    detail={
                        "token": token,
                        "resolved": str(resolved),
                        "rule_id": rule.rule_id,
                    },
                ))

    return findings


# ---------------------------------------------------------------------------
# 2. audit_token_rent
# ---------------------------------------------------------------------------


def audit_token_rent(parse_result: ParseResult) -> List[Finding]:
    """Flag files that exceed the 200-line adherence threshold.

    Per the /memory guidance: ">200 lines reduces adherence."

    Also emits one ``info`` finding with the aggregate totals across all
    source files.

    Token-count heuristic: chars / 4 (BPE approximation; documented above).
    """
    findings: List[Finding] = []

    total_lines = 0
    total_chars = 0
    seen_paths: set = set()  # avoid double-counting duplicate source entries

    # Build a path → source_rel map from the rules (first occurrence wins).
    path_to_rel: Dict[str, str] = {}
    for rule in parse_result.rules:
        if rule.source_path not in path_to_rel:
            path_to_rel[rule.source_path] = rule.source_rel

    for sf in parse_result.sources:
        p = Path(sf.path)
        if not p.exists():
            continue
        abs_str = str(p.resolve())
        if abs_str in seen_paths:
            continue
        seen_paths.add(abs_str)

        line_count = _count_lines(p)
        char_count = _count_chars(p)
        total_lines += line_count
        total_chars += char_count

        if line_count > TOKEN_RENT_LINE_THRESHOLD:
            source_rel = path_to_rel.get(sf.path, _collapse(p))
            findings.append(Finding(
                kind=KIND_TOKEN_RENT,
                severity=SEVERITY_WARN,
                source_rel=source_rel,
                line=None,
                message=(
                    f"{source_rel!r} is {line_count} lines "
                    f"(threshold: {TOKEN_RENT_LINE_THRESHOLD}). "
                    "Per /memory guidance, files >200 lines reduce adherence."
                ),
                detail={
                    "line_count": line_count,
                    "approx_tokens": char_count // _CHARS_PER_TOKEN,
                    "threshold": TOKEN_RENT_LINE_THRESHOLD,
                },
            ))

    # Aggregate info finding (always emitted, even if no individual warns)
    approx_total_tokens = total_chars // _CHARS_PER_TOKEN
    findings.append(Finding(
        kind=KIND_TOKEN_RENT,
        severity=SEVERITY_INFO,
        source_rel="(aggregate)",
        line=None,
        message=(
            f"Total config: {total_lines} lines / "
            f"~{approx_total_tokens} tokens "
            f"(heuristic: chars/{_CHARS_PER_TOKEN}) "
            f"across {len(seen_paths)} source file(s)."
        ),
        detail={
            "total_lines": total_lines,
            "total_chars": total_chars,
            "approx_total_tokens": approx_total_tokens,
            "source_file_count": len(seen_paths),
            "token_heuristic": f"chars/{_CHARS_PER_TOKEN}",
        },
    ))

    return findings


# ---------------------------------------------------------------------------
# 3. audit_conflicts
# ---------------------------------------------------------------------------


def _predicate_key(predicate: Optional[Dict]) -> Optional[Tuple]:
    """Return a canonical (tool, match_token) tuple from a classify predicate.

    Used to detect same-target rules.  Returns ``None`` if no usable key.
    """
    if predicate is None:
        return None

    tool = predicate.get("tool", "")
    # never_command uses 'match'; tool_substitution uses 'forbidden'
    match = predicate.get("match") or predicate.get("forbidden") or ""
    if tool and match:
        return (tool.lower(), match.lower())
    return None


def audit_conflicts(parse_result: ParseResult) -> List[Finding]:
    """Detect clear STRUCTURAL conflicts between convertible rules.

    Only the predicate-level case is flagged (conservative):

    Two CONVERTIBLE rules (from ``classify.py``) with the SAME
    (tool, match/forbidden) key but OPPOSING values:
    - Same (tool, match) with ``decision: deny`` in one and no deny in the
      other (permit / prefer) — a clear contradiction.
    - Same (tool, forbidden) but DIFFERENT ``prefer`` values — two rules
      disagree on what to use instead.

    DROPPED (too fuzzy, caused FPs on real config):
    - The "never X" vs "always X" normalised-text heuristic.  A single rule
      can legitimately contain both polarities in different clauses, e.g.
      "always gate (never run ad hoc)".  Only structural predicate conflicts
      (same tool + same target, opposing action) are reliable enough to flag.
    """
    findings: List[Finding] = []

    # Classify all rules; keep only CONVERTIBLE ones with a usable predicate
    classified_convertible: List[Tuple[Rule, Dict]] = []
    for rule in parse_result.rules:
        cl = classify_rule(rule)
        if cl.category == CATEGORY_CONVERTIBLE and cl.predicate:
            classified_convertible.append((rule, cl.predicate))

    # Group by (tool, match/forbidden) key
    by_key: Dict[Tuple, List[Tuple[Rule, Dict]]] = {}
    for rule, pred in classified_convertible:
        key = _predicate_key(pred)
        if key is None:
            continue
        by_key.setdefault(key, []).append((rule, pred))

    for key, group in by_key.items():
        if len(group) < 2:
            continue

        # Case A: same (tool, forbidden) but different 'prefer' values
        prefer_values = {p.get("prefer", "") for _, p in group if "prefer" in p}
        if len(prefer_values) > 1:
            rules_in_conflict = [(r, p) for r, p in group if "prefer" in p]
            if len(rules_in_conflict) >= 2:
                r1, p1 = rules_in_conflict[0]
                r2, p2 = rules_in_conflict[1]
                findings.append(Finding(
                    kind=KIND_CONFLICT,
                    severity=SEVERITY_WARN,
                    source_rel=r1.source_rel,
                    line=r1.line_start,
                    message=(
                        f"Conflicting tool-substitution rules for {key[1]!r}: "
                        f"{r1.source_rel}:{r1.line_start} prefers {p1.get('prefer')!r} "
                        f"but {r2.source_rel}:{r2.line_start} prefers {p2.get('prefer')!r}."
                    ),
                    detail={
                        "key": list(key),
                        "rule_a": {"source_rel": r1.source_rel, "line": r1.line_start,
                                   "prefer": p1.get("prefer")},
                        "rule_b": {"source_rel": r2.source_rel, "line": r2.line_start,
                                   "prefer": p2.get("prefer")},
                    },
                ))

        # Case B: same (tool, match) — one denies, another permits/prefers
        deny_rules = [(r, p) for r, p in group if p.get("decision") == "deny"]
        permit_rules = [
            (r, p) for r, p in group
            if "prefer" in p or p.get("decision") not in ("deny", None)
        ]
        if deny_rules and permit_rules:
            r1, p1 = deny_rules[0]
            r2, p2 = permit_rules[0]
            findings.append(Finding(
                kind=KIND_CONFLICT,
                severity=SEVERITY_WARN,
                source_rel=r1.source_rel,
                line=r1.line_start,
                message=(
                    f"Conflicting rules on {key}: "
                    f"{r1.source_rel}:{r1.line_start} denies "
                    f"but {r2.source_rel}:{r2.line_start} permits/prefers the same tool+target."
                ),
                detail={
                    "key": list(key),
                    "deny_rule": {"source_rel": r1.source_rel, "line": r1.line_start},
                    "permit_rule": {"source_rel": r2.source_rel, "line": r2.line_start},
                },
            ))

    return findings


# ---------------------------------------------------------------------------
# 4. audit_load_fidelity
# ---------------------------------------------------------------------------


def audit_load_fidelity(
    parse_result: ParseResult,
    *,
    project_dir: Optional[Path] = None,
) -> List[Finding]:
    """Flag rules files or imports that will never load or are broken.

    Three cases:

    (a) Broken ``@import`` targets recorded as missing by parse.py
        (a SourceFile with ``tier == TIER_IMPORT`` that does not exist on disk).
        These are always flagged — the import is broken regardless of project_dir.

    (b) A ``.claude/rules/*.md`` with ``paths:`` frontmatter globs that match
        NO file under ``project_dir`` — so the file never loads for this project.
        Only checked when ``project_dir`` is provided; skipped otherwise.

    (c) No guess: if we cannot determine whether a file loads, we do NOT flag it.
    """
    findings: List[Finding] = []

    # (a) Missing imports
    for sf in parse_result.sources:
        if sf.tier != TIER_IMPORT:
            continue
        p = Path(sf.path)
        if not p.exists():
            importer_rel = (
                _collapse(Path(sf.imported_from))
                if sf.imported_from
                else "(unknown)"
            )
            findings.append(Finding(
                kind=KIND_LOAD_FIDELITY,
                severity=SEVERITY_WARN,
                source_rel=_collapse(p),
                line=None,
                message=(
                    f"Broken @import: {_collapse(p)!r} does not exist "
                    f"(imported from {importer_rel!r})."
                ),
                detail={
                    "missing_path": str(p),
                    "imported_from": sf.imported_from or "",
                },
            ))

    # (b) Path-scoped globs that match nothing under project_dir
    if project_dir is not None:
        project_resolved = project_dir.resolve()
        for sf in parse_result.sources:
            if not sf.paths_globs:
                continue  # no paths: frontmatter → always loads
            p = Path(sf.path)
            source_rel = _collapse(p)

            matched = False
            for glob_pattern in sf.paths_globs:
                # project_resolved.glob() correctly handles ** semantics.
                try:
                    for candidate in project_resolved.glob(glob_pattern):
                        if candidate.is_file():
                            matched = True
                            break
                except OSError:  # fake-success-ok — I/O error on glob: skip pattern, keep matched=False
                    pass
                if matched:
                    break

            if not matched:
                findings.append(Finding(
                    kind=KIND_LOAD_FIDELITY,
                    severity=SEVERITY_WARN,
                    source_rel=source_rel,
                    line=None,
                    message=(
                        f"{source_rel!r} has paths: {sf.paths_globs!r} "
                        f"but no matching files found under "
                        f"{_collapse(project_resolved)!r} — "
                        "this rules file will never load for this project."
                    ),
                    detail={
                        "source": str(p),
                        "paths_globs": sf.paths_globs,
                        "project_dir": str(project_resolved),
                    },
                ))

    return findings


# ---------------------------------------------------------------------------
# 5. Aggregator
# ---------------------------------------------------------------------------


def audit_all(
    parse_result: ParseResult,
    *,
    base_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None,
) -> List[Finding]:
    """Run all four sub-audits and return the combined finding list.

    Order: stale_path → token_rent → conflict → load_fidelity.
    """
    findings: List[Finding] = []
    findings.extend(audit_stale_paths(parse_result, base_dir=base_dir))
    findings.extend(audit_token_rent(parse_result))
    findings.extend(audit_conflicts(parse_result))
    findings.extend(audit_load_fidelity(parse_result, project_dir=project_dir))
    return findings
