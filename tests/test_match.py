"""test_match.py — Tests for the Phase 2 predicate-matching / violation engine.

The structural command matcher (``command_invokes``) is the load-bearing piece:
the spike confirmed an ~80% false-positive rate with naive substring matching.
These tests assert the structural matcher eliminates the known false-positive
classes (grep patterns, PR-body strings, JSON payloads, heredoc bodies inside
quoted spans) while still detecting real invocations.

The HONESTY GUARD tests are explicitly labeled — they guard against the
"omniscient auditor trap" where a zero-violation count is misread as a signal
for deletion.

Run via:
    uv run --with pytest --with-editable . --python 3.12 pytest tests/test_match.py -v
"""

from __future__ import annotations

import pytest

from misfire.classify import (
    CATEGORY_CONVERTIBLE,
    CATEGORY_JUDGMENT_KEEP,
    CATEGORY_SAFETY_KEEP,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONVERT_BEFORE_ACTION,
    CONVERT_NEVER_COMMAND,
    CONVERT_TOOL_SUBSTITUTION,
    Classification,
)
from misfire.evidence import ToolAction
from misfire.match import (
    RuleViolation,
    _strip_quoted_spans,
    command_invokes,
    find_violations,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _bash(command: str, session_id: str = "sess-1") -> ToolAction:
    """Create a minimal Bash ToolAction."""
    return ToolAction(
        session_id=session_id,
        timestamp="2026-06-22T10:00:00.000Z",
        tool_name="Bash",
        command=command,
        input_summary=command[:80],
        is_sidechain=False,
        agent_type=None,
        transcript_rel="~/test/session.jsonl",
        cwd_rel="~/project",
        git_branch=None,
    )


def _edit(file_path: str) -> ToolAction:
    """Create a minimal Edit ToolAction."""
    return ToolAction(
        session_id="sess-1",
        timestamp="2026-06-22T10:00:00.000Z",
        tool_name="Edit",
        command="",
        input_summary=file_path,
        is_sidechain=False,
        agent_type=None,
        transcript_rel="~/test/session.jsonl",
        cwd_rel="~/project",
        git_branch=None,
    )


def _read(file_path: str) -> ToolAction:
    """Create a minimal Read ToolAction."""
    return ToolAction(
        session_id="sess-1",
        timestamp="2026-06-22T10:00:00.000Z",
        tool_name="Read",
        command="",
        input_summary=file_path,
        is_sidechain=False,
        agent_type=None,
        transcript_rel="~/test/session.jsonl",
        cwd_rel="~/project",
        git_branch=None,
    )


def _classification(
    rule_id: str = "test-r-001",
    category: str = CATEGORY_CONVERTIBLE,
    convert_kind: str = CONVERT_NEVER_COMMAND,
    predicate: dict | None = None,
    is_safety: bool = False,
    confidence: str = CONFIDENCE_HIGH,
) -> Classification:
    """Create a minimal Classification."""
    if predicate is None:
        predicate = {"tool": "Bash", "match": "git commit", "decision": "deny"}
    return Classification(
        rule_id=rule_id,
        category=category,
        convert_kind=convert_kind,
        predicate=predicate,
        is_safety=is_safety,
        confidence=confidence,
        rationale="test rule",
    )


# ---------------------------------------------------------------------------
# Tests: _strip_quoted_spans
# ---------------------------------------------------------------------------


class TestStripQuotedSpans:
    def test_single_quoted_content_removed(self):
        """Single-quoted grep pattern is stripped."""
        result = _strip_quoted_spans("grep 'git commit' file")
        assert "git commit" not in result
        # Outer tokens preserved
        assert "grep" in result
        assert "file" in result

    def test_double_quoted_content_removed(self):
        """Double-quoted grep pattern is stripped."""
        result = _strip_quoted_spans('grep "git commit" file')
        assert "git commit" not in result
        assert "grep" in result
        assert "file" in result

    def test_no_quotes_unchanged(self):
        """A command with no quotes is returned unchanged."""
        cmd = "git commit -m message"
        assert _strip_quoted_spans(cmd) == cmd

    def test_double_quoted_backslash_escape_consumed(self):
        """Backslash-escaped double-quote inside a DQ span does not end the span."""
        result = _strip_quoted_spans(r'git commit -m "line1 \"quote\" line2"')
        assert "git commit -m" in result
        assert "quote" not in result

    def test_empty_string(self):
        assert _strip_quoted_spans("") == ""

    def test_single_quote_no_escape_sequences(self):
        """Inside single quotes, backslash is literal (not an escape)."""
        # The entire 'no \\n escape' span is stripped
        result = _strip_quoted_spans(r"echo 'no \n escape'")
        assert "escape" not in result
        assert "echo" in result

    def test_adjacent_single_quoted_spans(self):
        result = _strip_quoted_spans("echo 'hello''world'")
        assert "hello" not in result
        assert "world" not in result
        assert "echo" in result

    def test_env_var_before_unquoted_command(self):
        """Content before a quoted arg remains intact."""
        result = _strip_quoted_spans("CAST_COMMIT_AGENT=1 git commit -m 'msg'")
        assert "CAST_COMMIT_AGENT=1" in result
        assert "git commit" in result
        assert "msg" not in result

    def test_double_quoted_span_with_nested_single_char(self):
        """A single-quote inside a double-quoted span is not treated as SQ start."""
        result = _strip_quoted_spans("echo \"it's fine\"")
        # "it's fine" is inside the double-quoted span → stripped
        assert "it's fine" not in result
        assert "echo" in result


# ---------------------------------------------------------------------------
# Tests: command_invokes — FALSE cases (target is data, not an invocation)
# ---------------------------------------------------------------------------


class TestCommandInvokesFalseCases:
    """Assert that false positives from the spike are eliminated."""

    def test_not_in_single_quoted_grep_pattern(self):
        """grep 'git commit' file — the pattern is inside SQ span (data)."""
        assert not command_invokes("grep 'git commit' file", "git commit")

    def test_not_in_double_quoted_grep_pattern(self):
        """grep "git commit" file — the pattern is inside DQ span (data)."""
        assert not command_invokes('grep "git commit" file', "git commit")

    def test_not_in_pr_body_string(self):
        """PR body text inside a DQ span — git commit is data, not an invocation."""
        assert not command_invokes(
            'echo "PR body: please run git commit before merging"',
            "git commit",
        )

    def test_not_in_single_quoted_json_payload(self):
        """JSON payload in a SQ span — git commit is data."""
        assert not command_invokes(
            "curl -d '{\"command\": \"git commit\"}'",
            "git commit",
        )

    def test_not_as_prefix_of_longer_subcommand_token(self):
        """git commitizen — 'commit' is part of a longer token."""
        assert not command_invokes("git commitizen", "git commit")

    def test_not_in_script_path_containing_commit(self):
        """Path containing 'commit' should not trigger on 'git commit' target."""
        assert not command_invokes("ls .git/hooks/commit-msg", "git commit")

    def test_not_grep_in_single_quoted_arg(self):
        """rg 'grep pattern' — grep appears only as data inside SQ span."""
        assert not command_invokes("rg 'grep pattern' src/", "grep")

    def test_not_grep_in_double_quoted_arg(self):
        """rg "grep pattern" — grep appears only as data inside DQ span."""
        assert not command_invokes('rg "grep pattern" src/', "grep")

    def test_not_git_push_force_without_flag(self):
        """git push without --force does not match 'git push --force'."""
        assert not command_invokes("git push origin main", "git push --force")

    def test_empty_command_never_matches(self):
        assert not command_invokes("", "git commit")

    def test_empty_target_never_matches(self):
        # Empty target would match everywhere; guard against it
        # (in practice predicates always have non-empty match strings)
        assert not command_invokes("git commit -m msg", "")

    def test_not_in_heredoc_body_inside_dq_span(self):
        """The heredoc body is inside the DQ span and gets stripped.

        The REAL 'git commit' invocation at the start of the command IS
        detected separately (see TestCommandInvokesTrueCases); this test
        checks that the heredoc body string 'commit message' is not a
        false trigger on a different (absent) target.
        """
        cmd = "git commit -m \"$(cat <<'EOF'\ncommit message body\nEOF\n)\""
        # The word 'commit' inside the heredoc should not match 'git commit'
        # as the heredoc content is inside the DQ span and gets stripped.
        # 'git commit' DOES appear in the outer command, so this is still
        # TRUE for 'git commit' — but we verify the body alone is not
        # triggering a match for a different unrelated target.
        assert not command_invokes(cmd, "commit message body")


# ---------------------------------------------------------------------------
# Tests: command_invokes — TRUE cases (target IS the invocation)
# ---------------------------------------------------------------------------


class TestCommandInvokesTrueCases:
    """Assert that real invocations are detected."""

    def test_at_start_of_command(self):
        """git commit at position 0 — canonical case."""
        assert command_invokes("git commit -m message", "git commit")

    def test_with_amend_flag(self):
        """git commit --amend — flag follows the command."""
        assert command_invokes("git commit --amend", "git commit")

    def test_with_no_args(self):
        """git commit alone (end of string)."""
        assert command_invokes("git commit", "git commit")

    def test_with_env_var_prefix(self):
        """CAST_COMMIT_AGENT=1 git commit — env var before the command."""
        assert command_invokes("CAST_COMMIT_AGENT=1 git commit", "git commit")

    def test_with_quoted_message_arg(self):
        """git commit -m 'message' — message is quoted but git commit is not."""
        assert command_invokes("git commit -m 'message'", "git commit")

    def test_git_push_force(self):
        assert command_invokes("git push --force", "git push --force")

    def test_git_push_force_with_remote(self):
        assert command_invokes("git push --force origin main", "git push --force")

    def test_real_grep_invocation(self):
        """grep as a real invocation (for tool_substitution: use rg not grep)."""
        assert command_invokes("grep -r pattern src/", "grep")

    def test_grep_with_flags_only(self):
        assert command_invokes("grep -rn TODO .", "grep")

    def test_before_pipe(self):
        """Command followed by a pipe — still an invocation."""
        assert command_invokes("git commit -m msg | cat", "git commit")

    def test_after_semicolon(self):
        """Second command in a chain (after semicolon)."""
        assert command_invokes("git add . ; git commit -m msg", "git commit")

    def test_after_double_ampersand(self):
        """Second command in an AND-chain."""
        assert command_invokes("git add . && git commit -m msg", "git commit")

    def test_outer_invocation_with_heredoc(self):
        """git commit at the outer level; heredoc body is inside DQ (stripped)."""
        cmd = "git commit -m \"$(cat <<'EOF'\nCo-Authored-By: Claude\nEOF\n)\""
        # The real 'git commit' is outside any quoted span → detected
        assert command_invokes(cmd, "git commit")


# ---------------------------------------------------------------------------
# Tests: find_violations — core behavior
# ---------------------------------------------------------------------------


class TestFindViolationsCore:
    def test_never_command_violation_detected(self):
        """A Bash command structurally invoking the forbidden target is a violation."""
        c = _classification(
            rule_id="r-git-commit",
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        actions = [_bash("git commit -m 'message'")]
        results = find_violations([c], iter(actions))
        assert len(results) == 1
        assert results[0].rule_id == "r-git-commit"
        assert results[0].violation_count == 1
        assert results[0].opportunity_count == 1
        assert results[0].excluded_by_exception == 0

    def test_tool_substitution_fires_on_grep_invocation(self):
        """grep invoked as a real command → tool_substitution violation."""
        c = _classification(
            rule_id="r-use-rg",
            convert_kind=CONVERT_TOOL_SUBSTITUTION,
            predicate={"tool": "Bash", "forbidden": "grep", "prefer": "rg"},
        )
        actions = [_bash("grep -r pattern src/")]
        results = find_violations([c], iter(actions))
        assert results[0].violation_count == 1

    def test_tool_substitution_not_fired_for_quoted_grep(self):
        """grep appears only inside a quoted span → NOT a tool_substitution violation."""
        c = _classification(
            rule_id="r-use-rg",
            convert_kind=CONVERT_TOOL_SUBSTITUTION,
            predicate={"tool": "Bash", "forbidden": "grep", "prefer": "rg"},
        )
        # rg is used correctly; 'grep' only appears in a SQ span
        actions = [_bash("rg 'grep pattern' src/")]
        results = find_violations([c], iter(actions))
        assert results[0].violation_count == 0

    def test_opportunity_count_counts_only_relevant_tool_type(self):
        """opportunity_count counts Bash actions; Read/Edit are not Bash opportunities."""
        c = _classification(
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        actions = [
            _bash("git status"),   # Bash — opportunity
            _bash("git push"),     # Bash — opportunity
            _read("~/file.txt"),   # Read — NOT a Bash opportunity
            _edit("~/file.txt"),   # Edit — NOT a Bash opportunity
        ]
        results = find_violations([c], iter(actions))
        assert results[0].opportunity_count == 2

    def test_empty_action_stream(self):
        """No actions → both violation_count and opportunity_count are 0."""
        c = _classification()
        results = find_violations([c], iter([]))
        assert results[0].violation_count == 0
        assert results[0].opportunity_count == 0

    def test_multiple_rules_single_stream_pass(self):
        """Multiple active rules are evaluated in one stream pass."""
        c1 = _classification(
            rule_id="r-git-commit",
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        c2 = _classification(
            rule_id="r-use-rg",
            convert_kind=CONVERT_TOOL_SUBSTITUTION,
            predicate={"tool": "Bash", "forbidden": "grep", "prefer": "rg"},
        )
        actions = [
            _bash("git commit -m msg"),     # violates c1, not c2
            _bash("grep -r TODO ."),        # violates c2, not c1
            _bash("git status"),            # neither
        ]
        results = find_violations([c1, c2], iter(actions))
        by_id = {r.rule_id: r for r in results}
        assert by_id["r-git-commit"].violation_count == 1
        assert by_id["r-git-commit"].opportunity_count == 3  # all 3 Bash actions
        assert by_id["r-use-rg"].violation_count == 1
        assert by_id["r-use-rg"].opportunity_count == 3

    def test_result_order_matches_classification_order(self):
        """Results are in the same order as the input classifications."""
        c1 = _classification(rule_id="r-first")
        c2 = _classification(rule_id="r-second")
        results = find_violations([c1, c2], iter([]))
        assert results[0].rule_id == "r-first"
        assert results[1].rule_id == "r-second"

    def test_violations_list_contains_matching_actions(self):
        """violations list contains the actual ToolAction objects that matched."""
        c = _classification(
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        action = _bash("git commit -m msg")
        results = find_violations([c], iter([action]))
        assert results[0].violations == [action]


# ---------------------------------------------------------------------------
# Tests: find_violations — HONESTY GUARD
# ---------------------------------------------------------------------------


class TestHonestyGuard:
    """The HONESTY GUARD: non-triggered or non-convertible rules are never signals."""

    def test_never_command_zero_violations_returns_record_not_signal(self):
        """HONESTY GUARD: a never_command rule with zero matching actions returns a
        RuleViolation with violation_count==0.  This is 'observed, never violated'
        — NOT a deletion/convert signal.  The record IS returned so callers can
        distinguish it from an unseen rule.
        """
        c = _classification(
            rule_id="r-git-commit",
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        # Only git status — never git commit
        actions = [_bash("git status"), _bash("git push origin")]
        results = find_violations([c], iter(actions))
        assert len(results) == 1
        rv = results[0]
        assert rv.violation_count == 0
        assert rv.opportunity_count == 2  # two Bash actions seen
        # The violations list is empty — no signal
        assert rv.violations == []

    def test_judgment_keep_never_returned(self):
        """HONESTY GUARD: judgment_keep rules are NEVER returned by find_violations."""
        c = Classification(
            rule_id="r-yagni",
            category=CATEGORY_JUDGMENT_KEEP,
            convert_kind=None,
            predicate=None,
            is_safety=False,
            confidence=CONFIDENCE_HIGH,
            rationale="YAGNI — judgment rule",
        )
        actions = [_bash("git commit -m msg")]
        results = find_violations([c], iter(actions))
        assert results == []

    def test_safety_keep_never_returned(self):
        """HONESTY GUARD: safety_keep rules are NEVER returned by find_violations."""
        c = Classification(
            rule_id="r-rm-rf",
            category=CATEGORY_SAFETY_KEEP,
            convert_kind=None,
            predicate={"tool": "Bash", "match": "rm -rf", "decision": "deny"},
            is_safety=True,
            confidence=CONFIDENCE_HIGH,
            rationale="destructive — keep as prose",
        )
        # Even with a matching action, safety_keep is excluded
        actions = [_bash("rm -rf /tmp/test")]
        results = find_violations([c], iter(actions))
        assert results == []

    def test_before_action_not_returned(self):
        """HONESTY GUARD: before_action rules are omitted (ordering context unavailable)."""
        c = Classification(
            rule_id="r-run-before-commit",
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_BEFORE_ACTION,
            predicate={"hook": "PreToolUse", "action": "run", "before": "commit"},
            is_safety=False,
            confidence=CONFIDENCE_MEDIUM,
            rationale="run X before commit",
        )
        actions = [_bash("git commit -m msg")]
        results = find_violations([c], iter(actions))
        assert results == []

    def test_all_non_convertible_classifications_return_empty(self):
        """When NO classification is active, return []."""
        classifications = [
            Classification(
                rule_id="r-j",
                category=CATEGORY_JUDGMENT_KEEP,
                convert_kind=None,
                predicate=None,
                is_safety=False,
                confidence=CONFIDENCE_HIGH,
                rationale="judgment",
            ),
            Classification(
                rule_id="r-s",
                category=CATEGORY_SAFETY_KEEP,
                convert_kind=None,
                predicate=None,
                is_safety=True,
                confidence=CONFIDENCE_HIGH,
                rationale="safety",
            ),
        ]
        results = find_violations(classifications, iter([_bash("git commit -m msg")]))
        assert results == []

    def test_convertible_no_predicate_not_returned(self):
        """A convertible classification with predicate=None is silently skipped."""
        c = Classification(
            rule_id="r-no-pred",
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_NEVER_COMMAND,
            predicate=None,  # no predicate → not reconstructible
            is_safety=False,
            confidence=CONFIDENCE_MEDIUM,
            rationale="convertible but no predicate extracted",
        )
        results = find_violations([c], iter([_bash("git commit -m msg")]))
        assert results == []


# ---------------------------------------------------------------------------
# Tests: find_violations — exception handling
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    def test_exception_marker_excludes_sanctioned_action(self):
        """An action containing the exception marker is EXCLUDED (not a violation)."""
        c = _classification(
            rule_id="r-git-commit",
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        # The escape hatch: CAST_COMMIT_AGENT=1 is present → sanctioned
        actions = [_bash("CAST_COMMIT_AGENT=1 git commit -m 'release'")]
        results = find_violations(
            [c],
            iter(actions),
            exceptions={"r-git-commit": "CAST_COMMIT_AGENT=1"},
        )
        assert results[0].violation_count == 0
        assert results[0].excluded_by_exception == 1

    def test_exception_does_not_exclude_unsanctioned_action(self):
        """A plain git commit (no escape hatch) is still a violation."""
        c = _classification(
            rule_id="r-git-commit",
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        actions = [_bash("git commit -m 'raw commit'")]
        results = find_violations(
            [c],
            iter(actions),
            exceptions={"r-git-commit": "CAST_COMMIT_AGENT=1"},
        )
        assert results[0].violation_count == 1
        assert results[0].excluded_by_exception == 0

    def test_exception_mixed_sanctioned_and_violation(self):
        """Some actions use the escape hatch; others do not."""
        c = _classification(
            rule_id="r-git-commit",
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        actions = [
            _bash("CAST_COMMIT_AGENT=1 git commit -m 'via agent'"),  # sanctioned
            _bash("git commit -m 'raw'"),                              # violation
            _bash("CAST_COMMIT_AGENT=1 git commit --amend"),          # sanctioned
        ]
        results = find_violations(
            [c],
            iter(actions),
            exceptions={"r-git-commit": "CAST_COMMIT_AGENT=1"},
        )
        assert results[0].violation_count == 1
        assert results[0].excluded_by_exception == 2

    def test_exception_for_different_rule_id_does_not_apply(self):
        """An exception keyed to a different rule_id has no effect here."""
        c = _classification(
            rule_id="r-git-commit",
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        actions = [_bash("CAST_COMMIT_AGENT=1 git commit -m 'msg'")]
        # Exception is registered for a DIFFERENT rule_id
        results = find_violations(
            [c],
            iter(actions),
            exceptions={"r-other-rule": "CAST_COMMIT_AGENT=1"},
        )
        # Not excluded — the exception does not apply to r-git-commit
        assert results[0].violation_count == 1
        assert results[0].excluded_by_exception == 0

    def test_no_exceptions_kwarg(self):
        """find_violations works correctly when exceptions=None (default)."""
        c = _classification(
            rule_id="r-git-commit",
            predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        )
        actions = [_bash("git commit -m 'msg'")]
        results = find_violations([c], iter(actions))  # no exceptions kwarg
        assert results[0].violation_count == 1
        assert results[0].excluded_by_exception == 0
