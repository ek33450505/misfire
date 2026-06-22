"""test_classify.py — Tests for src/misfire/classify.py (Phase 1).

Coverage (all required by spec):
- Each category: non_directive, safety_keep, output_shape, convertible, judgment_keep
- All four convert_kinds: never_command, tool_substitution, before_action, after_action
- Conservative default: judgment_keep + confidence=low when no clean match
- Blockquote provenance note → non_directive (real "Recreated ... wipe" example)
- Safety with predicate: "never force-push to main" → safety_keep, is_safety=True,
  predicate populated, convert_kind=None
- "never raw git commit" → convertible / never_command
- "use rg not grep" → tool_substitution
- Handoff block rule → output_shape
- YAGNI → judgment_keep
- classify_rules: maps list correctly
"""

from __future__ import annotations

import pytest

from misfire.classify import (
    CATEGORY_CONVERTIBLE,
    CATEGORY_JUDGMENT_KEEP,
    CATEGORY_NON_DIRECTIVE,
    CATEGORY_OUTPUT_SHAPE,
    CATEGORY_SAFETY_KEEP,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONVERT_AFTER_ACTION,
    CONVERT_BEFORE_ACTION,
    CONVERT_NEVER_COMMAND,
    CONVERT_TOOL_SUBSTITUTION,
    Classification,
    classify_rule,
    classify_rules,
)
from misfire.parse import Rule


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_rule(
    raw_text: str,
    normalized_text: str,
    imperative: bool,
    rule_id: str = "aabbcc112233",
    section: str = "",
) -> Rule:
    """Minimal Rule factory for classifier tests.

    We bypass parse_config here — the classifier only needs raw_text,
    normalized_text, imperative, and rule_id.
    """
    return Rule(
        rule_id=rule_id,
        source_path="/tmp/test.md",
        source_rel="~/.claude/CLAUDE.md",
        precedence_tier="user",
        section=section,
        line_start=1,
        line_end=1,
        raw_text=raw_text,
        normalized_text=normalized_text,
        imperative=imperative,
    )


# ---------------------------------------------------------------------------
# Step 1: non_directive
# ---------------------------------------------------------------------------


class TestNonDirective:
    def test_blockquote_provenance_note(self):
        """The real over-trigger example: blockquote with 'after' matching _IMPERATIVE_RE.

        This is the exact text from ~/.claude/rules/working-conventions.md.
        Despite ``imperative=True`` (because 'after' matches the imperative regex),
        the leading '>' marks it as a provenance note — not an actionable directive.
        """
        raw = (
            "> Recreated 2026-06-02 from session context after the ~/.claude wipe"
            " (see memory: project_cast_recovery_state)."
        )
        norm = (
            "Recreated 2026-06-02 from session context after the ~/.claude wipe"
            " (see memory: project_cast_recovery_state)."
        )
        rule = _make_rule(raw, norm, imperative=True)
        c = classify_rule(rule)

        assert c.category == CATEGORY_NON_DIRECTIVE
        assert c.is_safety is False
        assert c.convert_kind is None
        assert c.predicate is None

    def test_blockquote_generic(self):
        """Any line starting with '>' is non_directive."""
        rule = _make_rule(
            raw_text="> This is a quoted annotation.",
            normalized_text="This is a quoted annotation.",
            imperative=False,
        )
        assert classify_rule(rule).category == CATEGORY_NON_DIRECTIVE

    def test_provenance_keyword_recreated(self):
        """'Recreated' at the start of normalised text → non_directive (no blockquote needed)."""
        rule = _make_rule(
            raw_text="Recreated 2026-06-02 after the wipe.",
            normalized_text="Recreated 2026-06-02 after the wipe.",
            imperative=True,  # "after" triggers imperative
        )
        assert classify_rule(rule).category == CATEGORY_NON_DIRECTIVE

    def test_provenance_keyword_moved(self):
        """'Moved to ...' is a provenance note → non_directive."""
        rule = _make_rule(
            raw_text="Moved to docs/architecture.md (v7.5 Phase 1).",
            normalized_text="Moved to docs/architecture.md (v7.5 Phase 1).",
            imperative=False,
        )
        assert classify_rule(rule).category == CATEGORY_NON_DIRECTIVE

    def test_before_only_descriptive_non_directive(self):
        """If the only imperative signal is a descriptive 'before'/'after' with no
        strong-directive verb, classify as non_directive rather than mis-converting."""
        rule = _make_rule(
            raw_text="The session context was lost after the system restart.",
            normalized_text="The session context was lost after the system restart.",
            imperative=True,  # "after" triggers imperative marker in parse.py
        )
        c = classify_rule(rule)
        assert c.category == CATEGORY_NON_DIRECTIVE


# ---------------------------------------------------------------------------
# Step 2: safety_keep
# ---------------------------------------------------------------------------


class TestSafetyKeep:
    def test_never_force_push_to_main(self):
        """'never force-push to main' → safety_keep, is_safety=True, predicate populated.

        Safety wins over convertible for the category, but the predicate IS
        populated so a later tier can offer 'keep prose + optionally enforce'.
        """
        rule = _make_rule(
            raw_text="NEVER force-push to main/master.",
            normalized_text="NEVER force-push to main/master.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_SAFETY_KEEP
        assert c.is_safety is True
        assert c.convert_kind is None          # safety wins; not 'convertible'
        assert c.predicate is not None         # machine-checkable predicate present
        assert c.predicate.get("tool") == "Bash"
        assert "git push" in c.predicate.get("match", "")

    def test_rm_rf_safety(self):
        """'rm -rf' anywhere → safety_keep."""
        rule = _make_rule(
            raw_text="Never run `rm -rf $HOME` without a backup.",
            normalized_text="Never run rm -rf $HOME without a backup.",
            imperative=True,
        )
        c = classify_rule(rule)
        assert c.category == CATEGORY_SAFETY_KEEP
        assert c.is_safety is True

    def test_destructive_keyword(self):
        """'destructive' keyword → safety_keep."""
        rule = _make_rule(
            raw_text="Only run destructive operations after taking a backup.",
            normalized_text="Only run destructive operations after taking a backup.",
            imperative=True,
        )
        c = classify_rule(rule)
        assert c.category == CATEGORY_SAFETY_KEEP
        assert c.is_safety is True

    def test_schema_migration_safety(self):
        """'schema migration' → safety_keep."""
        rule = _make_rule(
            raw_text="All schema migration scripts must be idempotent.",
            normalized_text="All schema migration scripts must be idempotent.",
            imperative=True,
        )
        c = classify_rule(rule)
        assert c.category == CATEGORY_SAFETY_KEEP
        assert c.is_safety is True

    def test_safety_predicate_for_force_push_without_never(self):
        """A safety rule without 'never' still gets a predicate when machine-checkable."""
        rule = _make_rule(
            raw_text="Avoid force-push to any protected branch.",
            normalized_text="Avoid force-push to any protected branch.",
            imperative=True,
        )
        c = classify_rule(rule)
        assert c.category == CATEGORY_SAFETY_KEEP
        assert c.is_safety is True
        assert c.predicate is not None


# ---------------------------------------------------------------------------
# Step 3: output_shape
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_handoff_block_rule(self):
        """Multi-agent Handoff block requirement → output_shape (SubagentStop ledger)."""
        rule = _make_rule(
            raw_text=(
                "Every agent in a multi-agent chain MUST include a Handoff block"
                " before Work Log."
            ),
            normalized_text=(
                "Every agent in a multi-agent chain MUST include a Handoff block"
                " before Work Log."
            ),
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_OUTPUT_SHAPE
        assert c.convert_kind is None
        assert c.is_safety is False

    def test_status_block_rule(self):
        """'end with Status block' → output_shape."""
        rule = _make_rule(
            raw_text="Every agent response MUST end with a Status block.",
            normalized_text="Every agent response MUST end with a Status block.",
            imperative=True,
        )
        assert classify_rule(rule).category == CATEGORY_OUTPUT_SHAPE

    def test_work_log_rule(self):
        """'Work Log' mention → output_shape.

        Note: 'include' is not in parse.py's _IMPERATIVE_RE, so imperative=False.
        _is_non_directive doesn't fire (case 3 requires imperative=True); the rule
        proceeds to _OUTPUT_SHAPE_RE which matches 'Work Log'.
        """
        rule = _make_rule(
            raw_text="Include a Work Log section at the end of every agent response.",
            normalized_text="Include a Work Log section at the end of every agent response.",
            imperative=False,  # 'include', 'end', 'every' are not imperative markers
        )
        assert classify_rule(rule).category == CATEGORY_OUTPUT_SHAPE

    def test_status_done_line(self):
        """Explicit Status: DONE format reference → output_shape.

        Note: 'end', 'with', 'done' are not in parse.py's _IMPERATIVE_RE, so
        imperative=False.  _OUTPUT_SHAPE_RE matches 'Status: DONE'.
        """
        rule = _make_rule(
            raw_text="End with Status: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT",
            normalized_text="End with Status: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT",
            imperative=False,  # no imperative markers present
        )
        assert classify_rule(rule).category == CATEGORY_OUTPUT_SHAPE


# ---------------------------------------------------------------------------
# Step 4: convertible
# ---------------------------------------------------------------------------


class TestConvertibleNeverCommand:
    def test_never_raw_git_commit(self):
        """Headline test: 'never raw git commit' → convertible / never_command."""
        rule = _make_rule(
            raw_text="MANDATORY: Never `git commit` directly — always use the `commit` agent",
            normalized_text=(
                "MANDATORY: Never git commit directly — always use the commit agent"
            ),
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_NEVER_COMMAND
        assert c.predicate == {"tool": "Bash", "match": "git commit", "decision": "deny"}
        assert c.is_safety is False
        assert c.confidence == CONFIDENCE_HIGH

    def test_never_push_to_main(self):
        """'Never push --force to main' → convertible / never_command (if not caught by safety).

        Note: 'force-push' would hit safety_keep first; plain push-to-main is convertible.
        """
        rule = _make_rule(
            raw_text="Never push directly to the main branch.",
            normalized_text="Never push directly to the main branch.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_NEVER_COMMAND
        assert c.predicate is not None
        assert c.predicate.get("tool") == "Bash"

    def test_never_touch_path(self):
        """'Never touch <path>' → convertible / never_command with Edit|Write tool."""
        rule = _make_rule(
            raw_text="Never touch settings.json directly.",
            normalized_text="Never touch settings.json directly.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_NEVER_COMMAND
        assert c.predicate is not None
        assert "Edit" in c.predicate.get("tool", "")


class TestConvertibleToolSubstitution:
    def test_use_rg_not_grep(self):
        """'use `rg` not `grep`' → convertible / tool_substitution with predicate.

        Both tokens must be backtick-wrapped in raw_text; normalized_text is the
        markup-stripped form the regex matches against.
        """
        rule = _make_rule(
            raw_text="use `rg` not `grep`",
            normalized_text="use rg not grep",
            imperative=True,  # "use...not" matches _IMPERATIVE_RE
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_TOOL_SUBSTITUTION
        assert c.predicate is not None
        assert c.predicate["forbidden"] == "grep"
        assert c.predicate["prefer"] == "rg"
        assert c.predicate["tool"] == "Bash"
        assert c.is_safety is False

    def test_use_pnpm_instead_of_npm(self):
        """'use `pnpm` instead of `npm`' → convertible / tool_substitution.

        Backtick wrapping in raw_text is required; normalized_text has no backticks.
        """
        rule = _make_rule(
            raw_text="Always use `pnpm` instead of `npm` for package management.",
            normalized_text="Always use pnpm instead of npm for package management.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_TOOL_SUBSTITUTION
        assert c.predicate["forbidden"] == "npm"
        assert c.predicate["prefer"] == "pnpm"

    # ------------------------------------------------------------------
    # False-positive regression tests (Phase 2 junk-rule bug)
    # ------------------------------------------------------------------

    def test_sql_create_not_exists_is_judgment_keep(self):
        """'use `CREATE TABLE IF NOT EXISTS`' is a SQL multi-word phrase, NOT a CLI swap.

        The backtick-wrapped phrase spans multiple words so no individual token
        matches; falls through to judgment_keep.
        """
        rule = _make_rule(
            raw_text=(
                "Schema changes: use `CREATE TABLE IF NOT EXISTS` and "
                "`ALTER TABLE ... ADD COLUMN` with try/except for idempotency"
            ),
            normalized_text=(
                "Schema changes: use CREATE TABLE IF NOT EXISTS and "
                "ALTER TABLE ... ADD COLUMN with try/except for idempotency"
            ),
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_JUDGMENT_KEEP, (
            f"SQL keywords should be judgment_keep, not {c.category}/{c.convert_kind}"
        )
        assert c.convert_kind != CONVERT_TOOL_SUBSTITUTION

    def test_jest_vitest_framework_choice_is_judgment_keep(self):
        """'not Jest' is a test-framework judgment, NOT a Bash CLI swap.

        Neither 'Vitest' nor 'Jest' is backtick-wrapped; TitleCase also fails
        the command-name shape check.  Must classify as judgment_keep.
        """
        rule = _make_rule(
            raw_text=(
                "Use Vitest + React Testing Library for tests (not Jest in Vite projects)"
            ),
            normalized_text=(
                "Use Vitest + React Testing Library for tests (not Jest in Vite projects)"
            ),
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_JUDGMENT_KEEP, (
            f"Framework choice should be judgment_keep, not {c.category}/{c.convert_kind}"
        )
        assert c.convert_kind != CONVERT_TOOL_SUBSTITUTION

    def test_bitbucket_github_hosting_service_is_judgment_keep(self):
        """'use bitbucket.org, not github.com' is a hosting-service preference, NOT a CLI swap.

        No backtick wrapping; github/bitbucket are also in the stoplist.
        Must classify as judgment_keep.
        """
        rule = _make_rule(
            raw_text=(
                "Remote: Bitbucket (not GitHub). When scaffolding remote URLs, "
                "CI config, or PR commands, use bitbucket.org, not github.com."
            ),
            normalized_text=(
                "Remote: Bitbucket (not GitHub). When scaffolding remote URLs, "
                "CI config, or PR commands, use bitbucket.org, not github.com."
            ),
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_JUDGMENT_KEEP, (
            f"Hosting service should be judgment_keep, not {c.category}/{c.convert_kind}"
        )
        assert c.convert_kind != CONVERT_TOOL_SUBSTITUTION


class TestConvertibleBeforeAction:
    def test_run_tests_before_commit(self):
        """'run tests before commit' → convertible / before_action."""
        rule = _make_rule(
            raw_text="Run the test suite before commit.",
            normalized_text="Run the test suite before commit.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_BEFORE_ACTION
        assert c.predicate is not None
        assert c.predicate.get("hook") == "PreToolUse"
        assert "commit" in c.predicate.get("before", "")

    def test_check_before_push(self):
        """'check X before push' → convertible / before_action."""
        rule = _make_rule(
            raw_text="Check for uncommitted changes before pushing.",
            normalized_text="Check for uncommitted changes before pushing.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_BEFORE_ACTION


class TestConvertibleAfterAction:
    def test_do_after_edit(self):
        """'do X after edit' → convertible / after_action."""
        rule = _make_rule(
            raw_text="Do a lint check after editing any Python file.",
            normalized_text="Do a lint check after editing any Python file.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_AFTER_ACTION
        assert c.predicate is not None
        assert c.predicate.get("hook") == "PostToolUse"

    def test_run_after_commit(self):
        """'run X after commit' → convertible / after_action."""
        rule = _make_rule(
            raw_text="Run the status check after committing.",
            normalized_text="Run the status check after committing.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_CONVERTIBLE
        assert c.convert_kind == CONVERT_AFTER_ACTION


# ---------------------------------------------------------------------------
# Step 5 + 6: judgment_keep
# ---------------------------------------------------------------------------


class TestJudgmentKeep:
    def test_yagni(self):
        """YAGNI rule → judgment_keep."""
        rule = _make_rule(
            raw_text="YAGNI: build only what was asked.",
            normalized_text="YAGNI: build only what was asked.",
            imperative=False,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_JUDGMENT_KEEP
        assert c.is_safety is False
        assert c.convert_kind is None

    def test_concise(self):
        """'be concise' → judgment_keep."""
        rule = _make_rule(
            raw_text="Keep responses concise and focused.",
            normalized_text="Keep responses concise and focused.",
            imperative=True,
        )
        assert classify_rule(rule).category == CATEGORY_JUDGMENT_KEEP

    def test_match_ceremony_to_task_size(self):
        """'match ceremony to task size' → judgment_keep."""
        rule = _make_rule(
            raw_text="Match planning ceremony to task size.",
            normalized_text="Match planning ceremony to task size.",
            imperative=False,
        )
        assert classify_rule(rule).category == CATEGORY_JUDGMENT_KEEP

    def test_prefer_existing_patterns(self):
        """'prefer existing patterns' → judgment_keep."""
        rule = _make_rule(
            raw_text="Prefer existing patterns before inventing new ones.",
            normalized_text="Prefer existing patterns before inventing new ones.",
            imperative=True,
        )
        assert classify_rule(rule).category == CATEGORY_JUDGMENT_KEEP

    def test_dry_principle(self):
        """DRY keyword → judgment_keep."""
        rule = _make_rule(
            raw_text="DRY: find existing patterns before inventing new ones.",
            normalized_text="DRY: find existing patterns before inventing new ones.",
            imperative=False,
        )
        assert classify_rule(rule).category == CATEGORY_JUDGMENT_KEEP


class TestConservativeDefault:
    def test_directive_without_clean_match_defaults_to_judgment_keep_low(self):
        """A directive with imperative markers but no specific convertible pattern
        defaults to judgment_keep with confidence=low — never to convertible.

        This is the core conservatism invariant: when unsure, KEEP.
        """
        rule = _make_rule(
            raw_text="Always strive to write idiomatic code that follows the project's conventions.",
            normalized_text=(
                "Always strive to write idiomatic code that follows the project's conventions."
            ),
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_JUDGMENT_KEEP
        assert c.confidence == CONFIDENCE_LOW  # low = conservative default
        assert c.convert_kind is None
        assert c.predicate is None

    def test_no_directive_signal_defaults_to_judgment_keep_low(self):
        """A rule with no imperative markers that also lacks judgment keywords
        defaults to judgment_keep (low confidence), not convertible."""
        rule = _make_rule(
            raw_text="The agent chooses the best approach for the task.",
            normalized_text="The agent chooses the best approach for the task.",
            imperative=False,
        )
        c = classify_rule(rule)

        # Not non_directive (no blockquote, no provenance keyword, imperative=False
        # doesn't trigger case 3 which requires imperative=True).
        # Falls through all patterns → conservative default.
        assert c.category == CATEGORY_JUDGMENT_KEEP
        assert c.confidence == CONFIDENCE_LOW


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_safety_wins_over_convertible_for_category(self):
        """A rule that is both safety AND machine-checkable must have category=safety_keep,
        convert_kind=None, but predicate populated — safety always wins the category."""
        rule = _make_rule(
            raw_text="NEVER force-push to main/master.",
            normalized_text="NEVER force-push to main/master.",
            imperative=True,
        )
        c = classify_rule(rule)

        assert c.category == CATEGORY_SAFETY_KEEP     # safety wins
        assert c.convert_kind is None                  # NOT "never_command"
        assert c.is_safety is True
        assert c.predicate is not None                 # but predicate IS populated

    def test_non_directive_before_safety(self):
        """A blockquote line that mentions 'destructive' is still non_directive
        because the blockquote check fires before the safety check."""
        rule = _make_rule(
            raw_text="> Note: destructive operations require user confirmation.",
            normalized_text="Note: destructive operations require user confirmation.",
            imperative=False,
        )
        c = classify_rule(rule)
        assert c.category == CATEGORY_NON_DIRECTIVE

    def test_is_safety_false_for_non_safety_categories(self):
        """is_safety is always False for non-safety categories."""
        cases = [
            _make_rule("YAGNI", "YAGNI", False),
            _make_rule("use rg not grep", "use rg not grep", True),
            _make_rule(
                "Never git commit directly",
                "Never git commit directly",
                True,
            ),
        ]
        for rule in cases:
            c = classify_rule(rule)
            if c.category != CATEGORY_SAFETY_KEEP:
                assert c.is_safety is False, f"Expected is_safety=False for {c.category}"

    def test_classify_rules_maps_list(self):
        """classify_rules maps classify_rule over a list correctly."""
        rules = [
            _make_rule("YAGNI", "YAGNI", False, rule_id="rule1"),
            _make_rule(
                "Never git commit directly",
                "Never git commit directly",
                True,
                rule_id="rule2",
            ),
            _make_rule(
                "> Recreated 2026-06-02 after the wipe.",
                "Recreated 2026-06-02 after the wipe.",
                True,
                rule_id="rule3",
            ),
        ]
        results = classify_rules(rules)

        assert len(results) == 3
        assert results[0].rule_id == "rule1"
        assert results[1].rule_id == "rule2"
        assert results[2].rule_id == "rule3"
        assert results[0].category == CATEGORY_JUDGMENT_KEEP
        assert results[1].category == CATEGORY_CONVERTIBLE
        assert results[2].category == CATEGORY_NON_DIRECTIVE

    def test_convert_kind_none_for_non_convertible(self):
        """convert_kind is None for every non-convertible category."""
        cases = [
            _make_rule("> meta", "meta", False),                       # non_directive
            _make_rule("YAGNI", "YAGNI", False),                       # judgment_keep
            _make_rule(
                "Never force-push to main",
                "Never force-push to main",
                True,
            ),  # safety_keep
            _make_rule(
                "Include a Work Log in every response",
                "Include a Work Log in every response",
                True,
            ),  # output_shape
        ]
        for rule in cases:
            c = classify_rule(rule)
            if c.category != CATEGORY_CONVERTIBLE:
                assert c.convert_kind is None, (
                    f"Expected convert_kind=None for {c.category}, got {c.convert_kind}"
                )
