"""test_rank.py — Tests for the Phase 2 ranking engine (rank.py).

HONESTY GUARD tests are explicitly labeled.  They guard the
omniscient-auditor trap: a zero-violation count must NEVER become a
deletion/convert signal, even with large opportunity counts.

All tests are falsifiable: the ranking order, support floor, and confidence
labels are deterministic functions of the input counts — any change to the
ranking logic that breaks these assertions reveals an actual regression.

Run via:
    uv run --with pytest --with-editable . --python 3.12 pytest tests/test_rank.py -v
"""

from __future__ import annotations

import pytest

from misfire.classify import CONVERT_NEVER_COMMAND, CONVERT_TOOL_SUBSTITUTION
from misfire.evidence import ToolAction
from misfire.match import RuleViolation
from misfire.parse import Rule
from misfire.rank import (
    CONFIDENCE_HIGH,
    CONFIDENCE_INSUFFICIENT_DATA,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    RECOMMENDATION_ENFORCE_CANDIDATE,
    RECOMMENDATION_INSUFFICIENT_EVIDENCE,
    RECOMMENDATION_OBSERVED_NO_VIOLATIONS,
    RankReport,
    RankedRule,
    _sanitize_excerpt,
    rank_rules,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_rule(rule_id: str, normalized_text: str = "", source_rel: str = "rules/test.md") -> Rule:
    """Build a minimal Rule for use in rules_by_id."""
    return Rule(
        rule_id=rule_id,
        source_path=f"/home/user/.claude/{source_rel}",
        source_rel=source_rel,
        precedence_tier="rules_file",
        section="Test",
        line_start=1,
        line_end=1,
        raw_text=normalized_text,
        normalized_text=normalized_text,
        imperative=True,
    )


def _make_violation(
    rule_id: str,
    violation_count: int,
    opportunity_count: int,
    convert_kind: str = CONVERT_NEVER_COMMAND,
    predicate: dict | None = None,
    excluded_by_exception: int = 0,
) -> RuleViolation:
    """Build a RuleViolation with the given counts."""
    if predicate is None:
        predicate = {"tool": "Bash", "match": "git commit", "decision": "deny"}
    # Build a list of ToolAction stubs matching the violation count
    violations = [
        ToolAction(
            session_id="sess-1",
            timestamp="2026-06-22T10:00:00.000Z",
            tool_name="Bash",
            command="git commit -m 'test'",
            input_summary="",
            is_sidechain=False,
            agent_type=None,
            transcript_rel="~/projects/test/session.jsonl",
            cwd_rel="~/projects/test",
            git_branch="main",
        )
        for _ in range(violation_count)
    ]
    return RuleViolation(
        rule_id=rule_id,
        predicate=predicate,
        convert_kind=convert_kind,
        violations=violations,
        violation_count=violation_count,
        opportunity_count=opportunity_count,
        excluded_by_exception=excluded_by_exception,
    )


# ---------------------------------------------------------------------------
# _sanitize_excerpt
# ---------------------------------------------------------------------------


class TestSanitizeExcerpt:
    def test_collapses_user_path(self):
        text = "Never edit /Users/alice/.claude/settings.json directly"
        result = _sanitize_excerpt(text)
        assert "/Users/alice/" not in result
        assert "~/.claude/settings.json" in result

    def test_collapses_any_username(self):
        text = "Path is /Users/john_doe123/Documents/foo.py"
        result = _sanitize_excerpt(text)
        assert "/Users/john_doe123/" not in result
        assert "~/Documents/foo.py" in result

    def test_passes_through_already_collapsed(self):
        text = "Never edit ~/settings.json"
        assert _sanitize_excerpt(text) == text

    def test_truncates_long_text(self):
        text = "a" * 200
        result = _sanitize_excerpt(text)
        assert len(result) == 100
        assert result.endswith("…")

    def test_short_text_unchanged(self):
        text = "Never raw git commit"
        result = _sanitize_excerpt(text)
        assert result == text

    def test_exactly_100_chars_not_truncated(self):
        text = "x" * 100
        result = _sanitize_excerpt(text)
        assert result == text
        assert not result.endswith("…")


# ---------------------------------------------------------------------------
# RankReport structure
# ---------------------------------------------------------------------------


class TestRankReportStructure:
    """Thresholds and disclaimer are always present."""

    def test_thresholds_present(self):
        report = rank_rules([], {})
        assert "min_support" in report.thresholds
        assert "min_violations" in report.thresholds

    def test_custom_thresholds_reflected(self):
        report = rank_rules([], {}, min_support=50, min_violations=3)
        assert report.thresholds["min_support"] == 50
        assert report.thresholds["min_violations"] == 3

    def test_disclaimer_present_and_non_empty(self):
        report = rank_rules([], {})
        assert isinstance(report.disclaimer, str)
        assert len(report.disclaimer) > 20

    def test_disclaimer_mentions_support_floor(self):
        report = rank_rules([], {}, min_support=42)
        assert "42" in report.disclaimer

    def test_disclaimer_honesty_guard_text(self):
        report = rank_rules([], {})
        # Must mention that zero violations != deletion recommendation
        assert "NOT a deletion" in report.disclaimer or "not a deletion" in report.disclaimer.lower()

    def test_empty_violations_returns_empty_ranked(self):
        report = rank_rules([], {})
        assert report.ranked == []


# ---------------------------------------------------------------------------
# Ranking order — more violations ranks higher
# ---------------------------------------------------------------------------


class TestRankingOrder:
    """More violations → ranked higher within the enforce_candidate tier."""

    def setup_method(self):
        self.rules_by_id = {
            "rule-A": _make_rule("rule-A", "Never raw git commit"),
            "rule-B": _make_rule("rule-B", "Never git push"),
            "rule-C": _make_rule("rule-C", "Never run bats tests/"),
        }

    def test_more_violations_ranked_first(self):
        """rule-A (62 violations) must outrank rule-B (14 violations)."""
        violations = [
            _make_violation("rule-A", violation_count=62, opportunity_count=200),
            _make_violation("rule-B", violation_count=14, opportunity_count=200),
        ]
        report = rank_rules(violations, self.rules_by_id)
        assert report.ranked[0].rule_id == "rule-A"
        assert report.ranked[1].rule_id == "rule-B"

    def test_violation_rate_breaks_count_tie(self):
        """Equal violation_count: higher violation_rate ranks first."""
        violations = [
            _make_violation("rule-A", violation_count=5, opportunity_count=100),  # rate=0.05
            _make_violation("rule-B", violation_count=5, opportunity_count=50),   # rate=0.10
        ]
        report = rank_rules(violations, self.rules_by_id)
        assert report.ranked[0].rule_id == "rule-B"
        assert report.ranked[1].rule_id == "rule-A"

    def test_rule_id_breaks_full_tie(self):
        """All counts equal: rule_id ascending is the tiebreak."""
        self.rules_by_id["rule-Z"] = _make_rule("rule-Z", "Never something else")
        violations = [
            _make_violation("rule-Z", violation_count=5, opportunity_count=50),
            _make_violation("rule-A", violation_count=5, opportunity_count=50),
        ]
        report = rank_rules(violations, self.rules_by_id)
        ids = [r.rule_id for r in report.ranked]
        assert ids == sorted(ids)  # deterministic alphabetical tiebreak

    def test_enforce_candidate_before_insufficient_evidence(self):
        """enforce_candidate always precedes insufficient_evidence."""
        violations = [
            _make_violation("rule-A", violation_count=1, opportunity_count=5),   # insufficient
            _make_violation("rule-B", violation_count=50, opportunity_count=200), # enforce
        ]
        report = rank_rules(violations, self.rules_by_id)
        assert report.ranked[0].rule_id == "rule-B"
        assert report.ranked[0].recommendation == RECOMMENDATION_ENFORCE_CANDIDATE
        assert report.ranked[1].rule_id == "rule-A"
        assert report.ranked[1].recommendation == RECOMMENDATION_INSUFFICIENT_EVIDENCE

    def test_insufficient_evidence_before_observed_no_violations(self):
        """insufficient_evidence always precedes observed_no_violations."""
        violations = [
            _make_violation("rule-A", violation_count=0, opportunity_count=100),  # no violations
            _make_violation("rule-B", violation_count=1, opportunity_count=5),    # insufficient
        ]
        report = rank_rules(violations, self.rules_by_id)
        assert report.ranked[0].recommendation == RECOMMENDATION_INSUFFICIENT_EVIDENCE
        assert report.ranked[1].recommendation == RECOMMENDATION_OBSERVED_NO_VIOLATIONS


# ---------------------------------------------------------------------------
# Support floor
# ---------------------------------------------------------------------------


class TestSupportFloor:
    """Rules below the support floor get insufficient_evidence, not enforce_candidate."""

    def setup_method(self):
        self.rules_by_id = {
            "rule-A": _make_rule("rule-A", "Never raw git commit"),
        }

    def test_below_floor_with_violations_is_insufficient_evidence(self):
        """opportunity_count < min_support AND violation_count >= 1 → insufficient_evidence."""
        rv = _make_violation("rule-A", violation_count=5, opportunity_count=10)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        ranked = report.ranked[0]
        assert ranked.meets_support_floor is False
        assert ranked.recommendation == RECOMMENDATION_INSUFFICIENT_EVIDENCE

    def test_below_floor_confidence_is_low(self):
        """Regardless of violation count, below-floor confidence is low."""
        rv = _make_violation("rule-A", violation_count=100, opportunity_count=10)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_LOW

    def test_at_floor_is_enforce_candidate(self):
        """opportunity_count == min_support exactly → meets floor → enforce_candidate."""
        rv = _make_violation("rule-A", violation_count=1, opportunity_count=30)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        ranked = report.ranked[0]
        assert ranked.meets_support_floor is True
        assert ranked.recommendation == RECOMMENDATION_ENFORCE_CANDIDATE

    def test_above_floor_is_enforce_candidate(self):
        """opportunity_count > min_support AND violations ≥ min_violations → enforce_candidate."""
        rv = _make_violation("rule-A", violation_count=10, opportunity_count=200)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].recommendation == RECOMMENDATION_ENFORCE_CANDIDATE


# ---------------------------------------------------------------------------
# HONESTY GUARD — zero violations is never a deletion/convert signal
# ---------------------------------------------------------------------------


class TestHonestyGuard:
    """
    HONESTY GUARD: a non-triggered rule produces no deletion or conversion
    recommendation, regardless of how large the opportunity_count is.
    """

    def setup_method(self):
        self.rules_by_id = {
            "rule-A": _make_rule("rule-A", "Never raw git commit"),
        }

    def test_zero_violations_large_opportunity_is_observed_no_violations(self):
        """Even with 10000 opportunities and 0 violations: observed_no_violations."""
        rv = _make_violation("rule-A", violation_count=0, opportunity_count=10_000)
        report = rank_rules([rv], self.rules_by_id)
        ranked = report.ranked[0]
        assert ranked.recommendation == RECOMMENDATION_OBSERVED_NO_VIOLATIONS

    def test_zero_violations_never_enforce_candidate(self):
        """Zero violations must NOT produce enforce_candidate under any thresholds."""
        rv = _make_violation("rule-A", violation_count=0, opportunity_count=10_000)
        report = rank_rules([rv], self.rules_by_id, min_violations=1)
        assert report.ranked[0].recommendation != RECOMMENDATION_ENFORCE_CANDIDATE

    def test_zero_violations_never_insufficient_evidence(self):
        """Zero violations + below floor: still observed_no_violations, not insufficient."""
        rv = _make_violation("rule-A", violation_count=0, opportunity_count=5)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].recommendation == RECOMMENDATION_OBSERVED_NO_VIOLATIONS

    def test_zero_violations_high_opportunity_confidence_is_high(self):
        """Obeyed consistently (0 violations, meets floor) → high confidence."""
        rv = _make_violation("rule-A", violation_count=0, opportunity_count=100)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_HIGH

    def test_zero_violations_below_floor_confidence_is_low(self):
        """Obeyed but few observations → low confidence (not enough history)."""
        rv = _make_violation("rule-A", violation_count=0, opportunity_count=5)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_LOW

    def test_zero_violations_zero_opportunities_is_insufficient_data(self):
        """Never even exercised → insufficient_data (cold corpus)."""
        rv = _make_violation("rule-A", violation_count=0, opportunity_count=0)
        report = rank_rules([rv], self.rules_by_id)
        assert report.ranked[0].confidence == CONFIDENCE_INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# Confidence label determinism
# ---------------------------------------------------------------------------


class TestConfidenceLabels:
    """Confidence labels are deterministic functions of the input counts."""

    def setup_method(self):
        self.rules_by_id = {
            "rule-A": _make_rule("rule-A", "Never raw git commit"),
        }

    def test_cold_corpus_is_insufficient_data(self):
        rv = _make_violation("rule-A", violation_count=0, opportunity_count=0)
        report = rank_rules([rv], self.rules_by_id)
        assert report.ranked[0].confidence == CONFIDENCE_INSUFFICIENT_DATA

    def test_below_floor_any_violation_count_is_low(self):
        rv = _make_violation("rule-A", violation_count=99, opportunity_count=10)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_LOW

    def test_enforce_candidate_high_count_and_rate(self):
        """≥10 violations AND ≥10% rate AND meets floor → high."""
        rv = _make_violation("rule-A", violation_count=50, opportunity_count=200)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_HIGH

    def test_enforce_candidate_medium_count(self):
        """≥3 violations, meets floor, but rate < 10% → medium."""
        rv = _make_violation("rule-A", violation_count=3, opportunity_count=200)
        # rate = 0.015 < 0.05, count = 3 → medium (count >= 3 OR rate >= 0.05)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_MEDIUM

    def test_enforce_candidate_medium_rate(self):
        """≥5% rate, meets floor, but count < 3 → medium."""
        rv = _make_violation("rule-A", violation_count=2, opportunity_count=30)
        # rate = 0.067 >= 0.05 → medium
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_MEDIUM

    def test_enforce_candidate_low_confidence_small_signal(self):
        """Meets floor, count < 3, rate < 5% → low confidence."""
        rv = _make_violation("rule-A", violation_count=1, opportunity_count=50)
        # rate = 0.02 < 0.05, count = 1 < 3 → low
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_LOW

    def test_high_count_and_rate_must_both_be_met_for_high(self):
        """Count ≥ 10 but rate < 10%: not high (falls to medium if count≥3 or rate≥5%)."""
        rv = _make_violation("rule-A", violation_count=10, opportunity_count=1000)
        # rate = 0.01 < 0.10 → NOT high; count=10>=3 → medium
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].confidence == CONFIDENCE_MEDIUM


# ---------------------------------------------------------------------------
# RankedRule field correctness
# ---------------------------------------------------------------------------


class TestRankedRuleFields:
    """Individual field values on RankedRule are correct."""

    def setup_method(self):
        self.rule = _make_rule(
            "rule-A",
            normalized_text="Never raw git commit",
            source_rel="rules/working-conventions.md",
        )
        self.rules_by_id = {"rule-A": self.rule}

    def test_source_rel_from_rule(self):
        rv = _make_violation("rule-A", violation_count=5, opportunity_count=100)
        report = rank_rules([rv], self.rules_by_id)
        assert report.ranked[0].source_rel == "rules/working-conventions.md"

    def test_rule_excerpt_from_normalized_text(self):
        rv = _make_violation("rule-A", violation_count=5, opportunity_count=100)
        report = rank_rules([rv], self.rules_by_id)
        assert report.ranked[0].rule_excerpt == "Never raw git commit"

    def test_violation_rate_computed_correctly(self):
        rv = _make_violation("rule-A", violation_count=10, opportunity_count=100)
        report = rank_rules([rv], self.rules_by_id)
        assert abs(report.ranked[0].violation_rate - 0.10) < 1e-9

    def test_violation_rate_zero_when_no_opportunities(self):
        rv = _make_violation("rule-A", violation_count=0, opportunity_count=0)
        report = rank_rules([rv], self.rules_by_id)
        assert report.ranked[0].violation_rate == 0.0

    def test_excluded_by_exception_forwarded(self):
        rv = _make_violation(
            "rule-A", violation_count=5, opportunity_count=100, excluded_by_exception=14
        )
        report = rank_rules([rv], self.rules_by_id)
        assert report.ranked[0].excluded_by_exception == 14

    def test_convert_kind_forwarded(self):
        rv = _make_violation(
            "rule-A",
            violation_count=5,
            opportunity_count=100,
            convert_kind=CONVERT_TOOL_SUBSTITUTION,
            predicate={"tool": "Bash", "forbidden": "grep", "prefer": "rg"},
        )
        self.rules_by_id["rule-A"] = _make_rule("rule-A", "Use rg not grep")
        report = rank_rules([rv], self.rules_by_id)
        assert report.ranked[0].convert_kind == CONVERT_TOOL_SUBSTITUTION

    def test_predicate_forwarded(self):
        pred = {"tool": "Bash", "match": "git commit", "decision": "deny"}
        rv = _make_violation("rule-A", violation_count=5, opportunity_count=100, predicate=pred)
        report = rank_rules([rv], self.rules_by_id)
        assert report.ranked[0].predicate == pred

    def test_source_rel_no_user_path(self):
        """source_rel must never contain /Users/<name>/."""
        rv = _make_violation("rule-A", violation_count=1, opportunity_count=50)
        report = rank_rules([rv], self.rules_by_id)
        assert "/Users/" not in report.ranked[0].source_rel

    def test_rule_excerpt_no_user_path(self):
        """rule_excerpt must never expose /Users/<name>/."""
        self.rule = _make_rule(
            "rule-A",
            normalized_text="Never edit /Users/alice/.claude/settings.json",
            source_rel="CLAUDE.md",
        )
        self.rules_by_id["rule-A"] = self.rule
        rv = _make_violation("rule-A", violation_count=1, opportunity_count=50)
        report = rank_rules([rv], self.rules_by_id)
        assert "/Users/" not in report.ranked[0].rule_excerpt
        assert "~/" in report.ranked[0].rule_excerpt

    def test_missing_rule_graceful_degradation(self):
        """If rule_id not in rules_by_id, source_rel is empty, no crash."""
        rv = _make_violation("missing-id", violation_count=1, opportunity_count=50)
        report = rank_rules([rv], {})  # empty rules_by_id
        assert report.ranked[0].source_rel == ""

    def test_meets_support_floor_true_at_boundary(self):
        rv = _make_violation("rule-A", violation_count=1, opportunity_count=30)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].meets_support_floor is True

    def test_meets_support_floor_false_below_boundary(self):
        rv = _make_violation("rule-A", violation_count=1, opportunity_count=29)
        report = rank_rules([rv], self.rules_by_id, min_support=30)
        assert report.ranked[0].meets_support_floor is False


# ---------------------------------------------------------------------------
# Multi-rule integration scenarios
# ---------------------------------------------------------------------------


class TestMultiRuleScenarios:
    """Integration-style tests with multiple rules in realistic configurations."""

    def test_three_tier_sort_order(self):
        """enforce_candidate → insufficient_evidence → observed_no_violations."""
        rules_by_id = {
            "rule-enforced": _make_rule("rule-enforced", "Never raw git commit"),
            "rule-insuff": _make_rule("rule-insuff", "Never git push"),
            "rule-clean": _make_rule("rule-clean", "Never run npm"),
        }
        violations = [
            _make_violation("rule-clean", violation_count=0, opportunity_count=100),
            _make_violation("rule-insuff", violation_count=2, opportunity_count=10),
            _make_violation("rule-enforced", violation_count=50, opportunity_count=200),
        ]
        report = rank_rules(violations, rules_by_id, min_support=30)
        recommendations = [r.recommendation for r in report.ranked]
        assert recommendations == [
            RECOMMENDATION_ENFORCE_CANDIDATE,
            RECOMMENDATION_INSUFFICIENT_EVIDENCE,
            RECOMMENDATION_OBSERVED_NO_VIOLATIONS,
        ]

    def test_git_push_ranks_above_git_commit_when_more_violations(self):
        """Mirrors the expected real-data result: git push (62) > git commit (14)."""
        rules_by_id = {
            "rule-push": _make_rule("rule-push", "Never git push"),
            "rule-commit": _make_rule("rule-commit", "Never raw git commit"),
        }
        violations = [
            _make_violation("rule-commit", violation_count=14, opportunity_count=200),
            _make_violation("rule-push", violation_count=62, opportunity_count=200),
        ]
        report = rank_rules(violations, rules_by_id, min_support=30)
        assert report.ranked[0].rule_id == "rule-push"
        assert report.ranked[1].rule_id == "rule-commit"
        # Both should be enforce_candidate
        assert report.ranked[0].recommendation == RECOMMENDATION_ENFORCE_CANDIDATE
        assert report.ranked[1].recommendation == RECOMMENDATION_ENFORCE_CANDIDATE

    def test_custom_min_violations_threshold(self):
        """min_violations=5 means 3 violations is insufficient_evidence."""
        rules_by_id = {
            "rule-A": _make_rule("rule-A", "Never raw git commit"),
        }
        rv = _make_violation("rule-A", violation_count=3, opportunity_count=100)
        report = rank_rules([rv], rules_by_id, min_support=30, min_violations=5)
        assert report.ranked[0].recommendation == RECOMMENDATION_INSUFFICIENT_EVIDENCE

    def test_disclaimer_reflects_total_action_count(self):
        """Disclaimer mentions the total opportunity count across all rules."""
        rules_by_id = {
            "rule-A": _make_rule("rule-A", "Never raw git commit"),
            "rule-B": _make_rule("rule-B", "Never git push"),
        }
        violations = [
            _make_violation("rule-A", violation_count=5, opportunity_count=100),
            _make_violation("rule-B", violation_count=10, opportunity_count=200),
        ]
        report = rank_rules(violations, rules_by_id)
        # Total = 100 + 200 = 300
        assert "300" in report.disclaimer
