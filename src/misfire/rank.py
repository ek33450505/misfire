"""rank.py — Phase 2 ranking engine for misfire.

Turns per-rule violation records (from ``match.py``'s ``find_violations``)
into a ranked, confidence-and-support-disclosed list.

HONESTY GUARDS (the design contract):
- A rule with ``violation_count == 0`` is **NEVER** a deletion or conversion
  recommendation — it may be obeyed or simply never triggered.
- A rule below the support floor (``opportunity_count < min_support``) is
  ``insufficient_evidence`` — low confidence, explicitly disclosed.
- Thresholds are made explicit in every ``RankReport`` for reproducibility.
- A cold corpus (``opportunity_count == 0``) yields ``insufficient_data``
  confidence regardless of violation count.

Architecture note (Phase 2, Unit 3a):
    The ranking engine is deterministic, stdlib-only, and produces ZERO
    cast.db queries or CLI wiring — those belong to later units.

Public API::

    rank_rules(
        rule_violations: list[RuleViolation],
        rules_by_id: dict[str, Rule],
        *,
        min_support: int = 30,
        min_violations: int = 1,
    ) -> RankReport
"""

from __future__ import annotations

import dataclasses
import re
from typing import Dict, List, Optional

from misfire.match import RuleViolation
from misfire.parse import Rule


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

RECOMMENDATION_ENFORCE_CANDIDATE = "enforce_candidate"
RECOMMENDATION_OBSERVED_NO_VIOLATIONS = "observed_no_violations"
RECOMMENDATION_INSUFFICIENT_EVIDENCE = "insufficient_evidence"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_INSUFFICIENT_DATA = "insufficient_data"

# Maximum characters emitted in a rule_excerpt
_EXCERPT_MAX_CHARS = 100

# Regex to collapse /Users/<any-username>/ → ~/ in inline text
_USER_PATH_RE = re.compile(r"/Users/[^/\s]+/")

# Sort-tier mapping — lower value ranks higher in the output
_RECOMMENDATION_TIER = {
    RECOMMENDATION_ENFORCE_CANDIDATE: 0,
    RECOMMENDATION_INSUFFICIENT_EVIDENCE: 1,
    RECOMMENDATION_OBSERVED_NO_VIOLATIONS: 2,
}

# Disclaimer template — N is filled in at report-generation time.
_DISCLAIMER_TEMPLATE = (
    "Rankings reflect {n_actions} observed tool actions across {n_rules} active "
    "rules. Rules below the support floor (min_support={min_support}) are "
    "low-confidence; they appear as 'insufficient_evidence'. "
    "A rule with zero observed violations is NOT a deletion recommendation "
    "— it may be obeyed, enforced by other means, or simply never triggered "
    "(the omniscient-auditor trap). Only 'enforce_candidate' rules, which have "
    "both sufficient support AND observed violations, are genuine convert-to-hook "
    "candidates. Always review recommendations before acting."
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RankedRule:
    """A single rule ranked by observed violation evidence.

    Fields
    ------
    rule_id
        Stable 12-char SHA-1 prefix from ``Rule.rule_id``.
    convert_kind
        ``Classification.convert_kind`` — ``"never_command"`` or
        ``"tool_substitution"``.
    predicate
        Structured predicate dict from the ``Classification``.
    source_rel
        Home-collapsed path to the source file — never ``/Users/<name>/``.
    rule_excerpt
        First ≤100 chars of the normalized rule text, with any
        ``/Users/<name>/`` references collapsed to ``~/``.
    violation_count
        Number of tool actions that matched the predicate and were NOT
        excluded by a caller-supplied exception marker.
    opportunity_count
        Total tool actions of the relevant kind — the support denominator.
    excluded_by_exception
        Actions sanctioned by an explicit escape-hatch marker.
    violation_rate
        ``violation_count / opportunity_count``; 0.0 when
        ``opportunity_count == 0`` (guards against division by zero).
    meets_support_floor
        ``True`` iff ``opportunity_count >= min_support``.
    confidence
        One of ``"high" | "medium" | "low" | "insufficient_data"``.

        Assignment rules (applied in this order):
        1. ``opportunity_count == 0`` → ``insufficient_data`` (cold corpus —
           the rule was never even exercised in the observed history).
        2. ``not meets_support_floor`` → ``low`` (below minimum-support floor;
           confidence is suppressed regardless of violation count or rate).
        3. ``violation_count == 0`` AND ``meets_support_floor`` → ``high``
           (many observations, consistently followed — strong obeyed signal).
        4. ``enforce_candidate`` tier (``meets_support_floor`` and
           ``violation_count >= min_violations``):
           - ``high`` if ``violation_count >= 10`` AND ``violation_rate >= 0.1``
           - ``medium`` if ``violation_count >= 3`` OR ``violation_rate >= 0.05``
           - ``low`` otherwise (floor met but violation signal is small)
    recommendation
        One of:

        - ``"enforce_candidate"`` — violated ≥ ``min_violations`` times
          with ``meets_support_floor`` → genuine convert-to-hook candidate.
        - ``"observed_no_violations"`` — zero violations observed.
          **HONESTY GUARD: this is NEVER a deletion/convert recommendation.**
        - ``"insufficient_evidence"`` — ≥1 violation but below support
          floor; low confidence, disclosed in the report.
    """

    rule_id: str
    convert_kind: str
    predicate: dict
    source_rel: str
    rule_excerpt: str
    violation_count: int
    opportunity_count: int
    excluded_by_exception: int
    violation_rate: float
    meets_support_floor: bool
    confidence: str
    recommendation: str


@dataclasses.dataclass(frozen=True)
class RankReport:
    """Output of ``rank_rules``.

    Fields
    ------
    ranked
        Ranked rules in canonical order:
        ``enforce_candidate`` first (violation_count desc, violation_rate
        desc, rule_id asc), then ``insufficient_evidence``, then
        ``observed_no_violations``. Deterministic tiebreak by rule_id asc.
    thresholds
        The actual ``min_support`` and ``min_violations`` used — emitted for
        disclosure and reproducibility.
    disclaimer
        Plain-language honest caveat for display in any consumer (CLI,
        JSON output, etc.).
    """

    ranked: List[RankedRule]
    thresholds: Dict
    disclaimer: str


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _sanitize_excerpt(text: str) -> str:
    """Collapse ``/Users/<name>/`` → ``~/`` and truncate to ≤100 chars.

    ``Rule.normalized_text`` is already markdown-stripped, but may contain
    inline path examples that expose a real username (e.g. a rule that says
    "never edit /Users/alice/.claude/settings.json").  This helper removes
    those before emitting the excerpt for display.
    """
    sanitized = _USER_PATH_RE.sub("~/", text)
    if len(sanitized) > _EXCERPT_MAX_CHARS:
        return sanitized[:_EXCERPT_MAX_CHARS - 1] + "…"
    return sanitized


def _compute_confidence(
    opportunity_count: int,
    violation_count: int,
    violation_rate: float,
    meets_support_floor: bool,
) -> str:
    """Determine the confidence label for a ranked rule.

    See ``RankedRule.confidence`` field documentation for the full assignment
    logic.  This function is a pure function of its four arguments.
    """
    # Rule 1: cold corpus — never even exercised
    if opportunity_count == 0:
        return CONFIDENCE_INSUFFICIENT_DATA

    # Rule 2: below support floor
    if not meets_support_floor:
        return CONFIDENCE_LOW

    # Rule 3: obeyed (zero violations, enough observations)
    if violation_count == 0:
        return CONFIDENCE_HIGH

    # Rule 4: enforce_candidate — scale by count + rate
    if violation_count >= 10 and violation_rate >= 0.1:
        return CONFIDENCE_HIGH
    if violation_count >= 3 or violation_rate >= 0.05:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _compute_recommendation(
    violation_count: int,
    meets_support_floor: bool,
    min_violations: int,
) -> str:
    """Determine the recommendation label for a ranked rule.

    HONESTY GUARD: ``violation_count == 0`` yields ``observed_no_violations``
    regardless of opportunity_count — a non-triggered rule is NOT a deletion
    or conversion signal.
    """
    if violation_count == 0:
        return RECOMMENDATION_OBSERVED_NO_VIOLATIONS
    if violation_count >= min_violations and meets_support_floor:
        return RECOMMENDATION_ENFORCE_CANDIDATE
    # violation_count >= 1 but below support floor (or below min_violations)
    return RECOMMENDATION_INSUFFICIENT_EVIDENCE


def _sort_key(rr: RankedRule) -> tuple:
    """Sort key for canonical ranking order.

    Primary: recommendation tier (enforce_candidate < insufficient_evidence
    < observed_no_violations).
    Secondary (within tier): violation_count descending.
    Tertiary: violation_rate descending.
    Quaternary: rule_id ascending (deterministic tiebreak).
    """
    tier = _RECOMMENDATION_TIER.get(rr.recommendation, 99)
    return (tier, -rr.violation_count, -rr.violation_rate, rr.rule_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rank_rules(
    rule_violations: List[RuleViolation],
    rules_by_id: Dict[str, Rule],
    *,
    min_support: int = 30,
    min_violations: int = 1,
) -> RankReport:
    """Rank convertible rules by observed violation evidence.

    Turns the per-rule ``RuleViolation`` records from ``find_violations`` into
    a ``RankReport``: ranked rules, disclosed thresholds, and a plain-language
    disclaimer.

    The ranking is **deterministic** and **conservative**:
    - Only rules with machine-checkable predicates are included (the
      ``rule_violations`` input only contains such rules, by construction).
    - Confidence is suppressed below the support floor to avoid over-ranking
      rules with sparse observation history.
    - A rule with zero violations is NEVER recommended for conversion or
      deletion (the HONESTY GUARD).

    Args:
        rule_violations:
            Output of ``find_violations`` — one ``RuleViolation`` per active
            convertible rule.
        rules_by_id:
            Mapping from ``rule_id`` to ``Rule`` — used to retrieve
            ``source_rel`` and ``normalized_text`` for the excerpt.  Rules
            absent from this mapping produce ``source_rel=""`` and a
            truncated ``predicate``-derived excerpt (graceful degradation).
        min_support:
            Minimum ``opportunity_count`` required to consider a rule's
            violation record trustworthy.  Rules below this floor are
            classified ``insufficient_evidence`` with ``low`` confidence.
            Default: 30 (roughly one month of daily agent runs).
        min_violations:
            Minimum ``violation_count`` required to recommend enforcement.
            Default: 1 (any observed violation clears the bar, subject to
            the support floor).

    Returns:
        ``RankReport`` with ``.ranked`` sorted in canonical order,
        ``.thresholds`` for disclosure, and a ``.disclaimer`` string.
    """
    ranked: List[RankedRule] = []

    total_actions = sum(rv.opportunity_count for rv in rule_violations)

    for rv in rule_violations:
        # Retrieve the Rule for display metadata
        rule: Optional[Rule] = rules_by_id.get(rv.rule_id)
        source_rel = rule.source_rel if rule is not None else ""
        excerpt_base = rule.normalized_text if rule is not None else str(rv.predicate)
        rule_excerpt = _sanitize_excerpt(excerpt_base)

        # Derived metrics
        opportunity_count = rv.opportunity_count
        violation_count = rv.violation_count
        violation_rate = (
            violation_count / opportunity_count if opportunity_count > 0 else 0.0
        )
        meets_support_floor = opportunity_count >= min_support

        confidence = _compute_confidence(
            opportunity_count, violation_count, violation_rate, meets_support_floor
        )
        recommendation = _compute_recommendation(
            violation_count, meets_support_floor, min_violations
        )

        ranked.append(
            RankedRule(
                rule_id=rv.rule_id,
                convert_kind=rv.convert_kind,
                predicate=rv.predicate if rv.predicate is not None else {},
                source_rel=source_rel,
                rule_excerpt=rule_excerpt,
                violation_count=violation_count,
                opportunity_count=opportunity_count,
                excluded_by_exception=rv.excluded_by_exception,
                violation_rate=violation_rate,
                meets_support_floor=meets_support_floor,
                confidence=confidence,
                recommendation=recommendation,
            )
        )

    # Sort into canonical order
    ranked.sort(key=_sort_key)

    thresholds = {
        "min_support": min_support,
        "min_violations": min_violations,
    }

    disclaimer = _DISCLAIMER_TEMPLATE.format(
        n_actions=total_actions,
        n_rules=len(rule_violations),
        min_support=min_support,
    )

    return RankReport(ranked=ranked, thresholds=thresholds, disclaimer=disclaimer)
