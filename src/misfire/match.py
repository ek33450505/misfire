"""match.py — Phase 2 predicate-matching / violation engine for misfire.

Joins classified convertible rules to the ToolAction stream to reconstruct
per-rule violation records.

Public API::

    command_invokes(command: str, target: str) -> bool
    find_violations(
        classifications: list[Classification],
        actions: Iterable[ToolAction],
        *,
        exceptions: dict[str, str] | None = None,
    ) -> list[RuleViolation]

HARD GUARD (the honesty guard):
    A convertible rule with ``violation_count == 0`` is representable as
    "observed, never violated" but is NEVER a deletion/convert signal.
    ``safety_keep`` and ``judgment_keep`` rules are NEVER returned by
    ``find_violations`` — they are not matched for conversion under any
    circumstances.

Ordering context:
    ``before_action`` / ``after_action`` convert kinds require ordering
    information (which action preceded which other action) that the flat,
    per-action stream cannot cleanly provide.  These convert kinds are
    intentionally omitted from ``find_violations`` results — honest
    under-reporting beats false positives.

Spike provenance (2026-06-22):
    Naive ``git commit`` substring match = 10 hits in a real transcript
    corpus; structural matching (strip quoted spans) → 2 material candidates
    (80% false-positive rate).  This module productionises that approach.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Dict, Iterable, List, Optional

from misfire.classify import (
    CATEGORY_CONVERTIBLE,
    CONVERT_NEVER_COMMAND,
    CONVERT_TOOL_SUBSTITUTION,
    Classification,
)
from misfire.evidence import ToolAction


# ---------------------------------------------------------------------------
# Structural command matcher
# ---------------------------------------------------------------------------


def _strip_quoted_spans(command: str) -> str:
    """Remove the content inside single- and double-quoted spans.

    Implements the minimal shell quoting rules needed to detect whether a
    target string is "data" (inside a quote span) or "code" (outside):

    - Single-quoted spans ``'...'`` have no escape sequences in POSIX shell;
      their interior is removed wholesale.
    - Double-quoted spans ``"..."`` respect ``\\``-escaping; a backslash
      followed by any character is consumed without being emitted.

    Quote *delimiters* are themselves not emitted, so after stripping,
    adjacent parts of the command are joined without quote marks.
    Example: ``grep 'git commit' file`` → ``grep  file`` (two spaces).
    This preserves the relative positions of non-quoted tokens.

    Limitations (by design — this is not a full Bash parser):
    - ``$'...'`` ANSI-C quoting is treated as a plain single-quoted span
      (the ``$`` is emitted, the content is dropped).
    - Heredoc bodies are not parsed; only their surrounding shell syntax is.
    - Nested ``$(...)`` subshells are not recursively parsed.

    Args:
        command: A raw shell command string.

    Returns:
        The command with all quoted-span contents removed.
    """
    result: List[str] = []
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "'":
            # Single-quoted: no escape sequences; skip to the next literal '
            i += 1
            while i < n and command[i] != "'":
                i += 1
            i += 1  # skip the closing '
        elif ch == '"':
            # Double-quoted: skip to the next unescaped "
            i += 1
            while i < n:
                c = command[i]
                if c == "\\":
                    i += 2  # consume escape char + the following char
                elif c == '"':
                    i += 1  # consume closing "
                    break
                else:
                    i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def command_invokes(command: str, target: str) -> bool:
    """Return ``True`` iff the shell ``command`` structurally EXECUTES ``target``.

    This is the spike-proven approach for eliminating the ~80% false-positive
    rate of naive substring matching.  Two-step algorithm:

    1. Strip single- and double-quoted spans from ``command`` via
       ``_strip_quoted_spans``.  This removes grep patterns, PR body text,
       JSON payloads, and other data strings that contain the target as a
       literal value rather than as an invocation.

    2. Test that ``target`` appears in the stripped command as a **command
       token**: preceded by start-of-string or whitespace (``(?<!\\S)``),
       and followed by end-of-string, whitespace, or a shell metacharacter
       that can legitimately follow a command name (``-`` for a flag, ``;``,
       ``&``, ``|``, ``#``, ``(``, ``)``).

    The word-boundary check prevents ``"git commit"`` from matching
    ``"git commitizen"`` (since ``i`` does not satisfy the follow-set).

    Known limitation:
        Unquoted ``echo git commit`` would register as a false positive since
        the args are not quoted.  In practice, such constructs quote the
        arguments, so this does not arise in the real corpus.

    Args:
        command: Raw ``input.command`` string from a Bash ToolAction.
        target:  Command string to structurally match (e.g. ``"git commit"``).

    Returns:
        ``True`` if the structural match succeeds; ``False`` otherwise.
    """
    if not target:
        return False
    stripped = _strip_quoted_spans(command)
    escaped = re.escape(target)
    # (?<!\S)  — preceded by whitespace or start-of-string (zero-width assertion)
    # (?=...)  — followed by end, whitespace, or a shell metachar
    pattern = r"(?<!\S)" + escaped + r"(?=\s|$|[-;&#|\(\)\\])"
    return bool(re.search(pattern, stripped))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RuleViolation:
    """Violation record for a single convertible rule.

    One ``RuleViolation`` is emitted per active (convertible + supported kind)
    rule, regardless of whether any violations were found.  A
    ``violation_count`` of ``0`` means "observed, never violated" — the
    recommendation layer MUST NOT treat this as a deletion/convert signal
    (the HONESTY GUARD).

    Fields
    ------
    rule_id
        The ``Classification.rule_id`` (= ``Rule.rule_id``) of the matched
        rule — stable across runs, derived from the rule's content hash.
    predicate
        The structured predicate dict from the ``Classification``.
    convert_kind
        One of the CONVERT_* constants (``"never_command"`` or
        ``"tool_substitution"``).
    violations
        The ``ToolAction`` objects that matched the predicate AND were NOT
        excluded by a caller-supplied exception marker.
    violation_count
        ``len(violations)`` — precomputed for convenient sorting / ranking.
        Zero means "observed, never violated" (HONESTY GUARD: NOT a signal).
    opportunity_count
        Total tool actions of the relevant kind seen in the corpus — the
        denominator for computing a violation rate.  A rule with
        ``opportunity_count == 0`` was never even exercised (cold corpus).
    excluded_by_exception
        Count of predicate-matching actions that were sanctioned by the
        caller-supplied exception marker.  Non-zero only when the rule
        explicitly names an escape hatch and the caller passes it via
        ``exceptions``.
    """

    rule_id: str
    predicate: Dict
    convert_kind: str
    violations: List[ToolAction]
    violation_count: int
    opportunity_count: int
    excluded_by_exception: int


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _opportunity_tool_names(c: Classification) -> frozenset:
    """Return the set of tool names that constitute 'opportunities' for ``c``.

    An opportunity is any tool action of the relevant kind — the support base
    (denominator) for the violation rate.

    - ``never_command`` with ``tool == "Bash"`` → ``{"Bash"}``
    - ``never_command`` with ``tool == "Edit|Write"`` → ``{"Edit", "Write"}``
    - ``tool_substitution`` → always ``{"Bash"}``
    """
    if c.convert_kind == CONVERT_TOOL_SUBSTITUTION:
        return frozenset({"Bash"})
    tool = (c.predicate or {}).get("tool", "Bash")
    if "|" in tool:
        return frozenset(tool.split("|"))
    return frozenset({tool})


def _matches_predicate(action: ToolAction, c: Classification) -> bool:
    """Return ``True`` if ``action`` matches the predicate in ``c``.

    Only handles ``CONVERT_NEVER_COMMAND`` and ``CONVERT_TOOL_SUBSTITUTION``;
    callers must pre-filter ``c`` to these kinds.

    For ``never_command``:
    - ``predicate["tool"] == "Bash"``: structurally checks that the Bash
      command invokes ``predicate["match"]`` (via ``command_invokes``).
    - ``predicate["tool"]`` containing ``"|"`` (e.g. ``"Edit|Write"``): checks
      that ``predicate["match"]`` appears as a substring of
      ``action.input_summary`` (the home-collapsed file path).

    For ``tool_substitution``:
    - Checks that the Bash command structurally invokes
      ``predicate["forbidden"]``.
    """
    predicate = c.predicate
    if predicate is None:
        return False

    if c.convert_kind == CONVERT_NEVER_COMMAND:
        tool = predicate.get("tool", "")
        match_target = predicate.get("match", "")
        if not match_target:
            return False
        if tool == "Bash":
            return command_invokes(action.command, match_target)
        # Edit|Write (or any non-Bash tool): substring match on the file path
        return bool(match_target in action.input_summary)

    if c.convert_kind == CONVERT_TOOL_SUBSTITUTION:
        forbidden = predicate.get("forbidden", "")
        return bool(forbidden and command_invokes(action.command, forbidden))

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_violations(
    classifications: List[Classification],
    actions: Iterable[ToolAction],
    *,
    exceptions: Optional[Dict[str, str]] = None,
) -> List[RuleViolation]:
    """Match convertible rule predicates against the tool-action stream.

    Scans the action stream exactly once, testing each action against every
    active rule simultaneously (O(actions × active_rules)).

    Rules included (active):
    - ``CATEGORY_CONVERTIBLE`` with ``convert_kind`` in
      ``{CONVERT_NEVER_COMMAND, CONVERT_TOOL_SUBSTITUTION}`` and a
      non-``None`` predicate.

    Rules excluded (HONESTY GUARD):
    - ``safety_keep``, ``judgment_keep``, ``non_directive``, ``output_shape``
      categories — NEVER matched for conversion.
    - ``before_action``, ``after_action`` convert kinds — require action
      ordering context unavailable in a flat stream; omitted to avoid false
      positives.

    One ``RuleViolation`` is returned per active rule.  A rule with
    ``violation_count == 0`` means "observed, never violated" — callers MUST
    NOT treat this as a deletion or conversion signal (the HONESTY GUARD).

    Args:
        classifications: Output of ``classify_rules``.  Non-active
            classifications are silently ignored.
        actions: Iterable of ``ToolAction`` objects (e.g. from
            ``iter_tool_actions``).  Consumed exactly once.
        exceptions: Optional per-rule exception markers.  Key = ``rule_id``;
            value = a substring marker whose presence in ``action.command``
            means the action is SANCTIONED by an escape hatch the rule itself
            names.  Sanctioned actions increment ``excluded_by_exception`` and
            are NOT added to ``violations``.
            Do NOT hardcode any project-specific escape hatch here — only
            honour caller-supplied markers.

    Returns:
        List of ``RuleViolation`` objects, one per active classification,
        in the same order as ``classifications``.
    """
    if exceptions is None:
        exceptions = {}

    # Filter to reconstructible convertible rules only
    active: List[Classification] = [
        c
        for c in classifications
        if c.category == CATEGORY_CONVERTIBLE
        and c.convert_kind in {CONVERT_NEVER_COMMAND, CONVERT_TOOL_SUBSTITUTION}
        and c.predicate is not None
    ]

    if not active:
        return []

    # Per-rule accumulators (keyed by rule_id)
    violations: Dict[str, List[ToolAction]] = {c.rule_id: [] for c in active}
    excluded: Dict[str, int] = {c.rule_id: 0 for c in active}
    opportunities: Dict[str, int] = {c.rule_id: 0 for c in active}

    # Pre-compute opportunity tool sets to avoid recomputing per action
    opp_tools: Dict[str, frozenset] = {
        c.rule_id: _opportunity_tool_names(c) for c in active
    }

    # Single pass through the action stream
    for action in actions:
        for c in active:
            if action.tool_name not in opp_tools[c.rule_id]:
                continue

            # Count toward the denominator (opportunity)
            opportunities[c.rule_id] += 1

            # Check predicate match
            if not _matches_predicate(action, c):
                continue

            # Check for a caller-supplied exception (escape hatch)
            exception_marker = exceptions.get(c.rule_id)
            if exception_marker and exception_marker in action.command:
                excluded[c.rule_id] += 1
            else:
                violations[c.rule_id].append(action)

    # Build result — one RuleViolation per active classification, same order
    return [
        RuleViolation(
            rule_id=c.rule_id,
            predicate=c.predicate,
            convert_kind=c.convert_kind,
            violations=violations[c.rule_id],
            violation_count=len(violations[c.rule_id]),
            opportunity_count=opportunities[c.rule_id],
            excluded_by_exception=excluded[c.rule_id],
        )
        for c in active
    ]
