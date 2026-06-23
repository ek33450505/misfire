"""test_ablate.py — Tests for the Phase 4 ablation probe (ablate.py).

All tests are 100% offline — no real Ollama server is contacted.
The ChatClient protocol is injectable; FakeChatClient is the test double used
throughout.  OllamaClient shape tests monkeypatch urllib.request.urlopen.

Run via:
    uv run --with pytest --with-editable . --python 3.12 pytest tests/test_ablate.py -v
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import List, Optional

import pytest

from misfire.ablate import (
    AblationReport,
    OllamaClient,
    TrialResult,
    _build_disclaimers,
    _build_interpretation,
    _collapse_only,
    _sanitize_str,
    build_context,
    detect_violation,
    report_to_dict,
    run_ablation,
    synthesize_task,
)
from misfire.classify import (
    CATEGORY_CONVERTIBLE,
    CATEGORY_JUDGMENT_KEEP,
    CATEGORY_SAFETY_KEEP,
    CONFIDENCE_HIGH,
    CONVERT_NEVER_COMMAND,
    CONVERT_TOOL_SUBSTITUTION,
    Classification,
)
from misfire.parse import Rule


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_rule(
    rule_id: str = "r-ts-001",
    normalized_text: str = "Use rg instead of grep.",
    source_rel: str = "rules/test.md",
    section: str = "Search",
) -> Rule:
    return Rule(
        rule_id=rule_id,
        source_path=f"/Users/testuser/.claude/{source_rel}",
        source_rel=source_rel,
        precedence_tier="rules_file",
        section=section,
        line_start=1,
        line_end=1,
        raw_text=normalized_text,
        normalized_text=normalized_text,
        imperative=True,
    )


def _make_tool_sub_cls(rule_id: str = "r-ts-001") -> Classification:
    return Classification(
        rule_id=rule_id,
        category=CATEGORY_CONVERTIBLE,
        convert_kind=CONVERT_TOOL_SUBSTITUTION,
        predicate={"tool": "Bash", "forbidden": "grep", "prefer": "rg"},
        is_safety=False,
        confidence=CONFIDENCE_HIGH,
        rationale="Use rg not grep",
    )


def _make_never_cmd_rule(
    rule_id: str = "r-nc-001",
    normalized_text: str = "Never run git commit directly.",
    section: str = "Commits",
) -> Rule:
    return Rule(
        rule_id=rule_id,
        source_path="/Users/testuser/.claude/rules/test.md",
        source_rel="rules/test.md",
        precedence_tier="rules_file",
        section=section,
        line_start=10,
        line_end=10,
        raw_text=normalized_text,
        normalized_text=normalized_text,
        imperative=True,
    )


def _make_never_cmd_cls(rule_id: str = "r-nc-001") -> Classification:
    return Classification(
        rule_id=rule_id,
        category=CATEGORY_CONVERTIBLE,
        convert_kind=CONVERT_NEVER_COMMAND,
        predicate={"tool": "Bash", "match": "git commit", "decision": "deny"},
        is_safety=False,
        confidence=CONFIDENCE_HIGH,
        rationale="Never raw git commit",
    )


# ---------------------------------------------------------------------------
# FakeChatClient — test double implementing the ChatClient protocol
# ---------------------------------------------------------------------------


class FakeChatClient:
    """Offline test double for the ChatClient protocol.

    Never touches the network.  Responses can be:
    - a list of strings, cycled in call order, or
    - a callable(*, model, system, user, temperature) -> str for dynamic dispatch.

    side_effect can be:
    - an exception instance to raise on every call, or
    - a tuple (exception_instance, call_number: int) to raise on the Nth call
      (1-based).
    """

    def __init__(
        self,
        *,
        available: bool = True,
        responses=None,
        side_effect=None,
    ) -> None:
        self._available = available
        self._responses = responses
        self._side_effect = side_effect
        self.calls: List[dict] = []

    def available(self) -> bool:
        return self._available

    def chat(self, *, model: str, system: str, user: str, temperature: float) -> str:
        call_num = len(self.calls) + 1
        self.calls.append(
            {"model": model, "system": system, "user": user, "temperature": temperature}
        )
        if self._side_effect is not None:
            if isinstance(self._side_effect, tuple):
                exc, on_call = self._side_effect
                if call_num >= on_call:
                    raise exc
            else:
                raise self._side_effect
        if self._responses is None:
            return ""
        if callable(self._responses):
            return self._responses(
                model=model, system=system, user=user, temperature=temperature
            )
        idx = len(self.calls) - 1
        return self._responses[idx % len(self._responses)]


# ---------------------------------------------------------------------------
# run_ablation — Branch 1: causal effect (shift > 0, delta >= 2)
# ---------------------------------------------------------------------------


def _rule_presence_response(*, system: str, **_kwargs) -> str:
    """Return an obeying response when the rule text is present; violating otherwise.

    Present context includes "rg" and "grep" (from "Use rg instead of grep.").
    Ablated context is the bare header — "rg" is absent, so we return a violation.
    """
    if "rg" in system and "grep" in system:
        return "```sh\nrg -r TODO .\n```"
    return "```sh\ngrep -r TODO .\n```"


class TestRunAblationCausalEffect:
    """Branch 1: shift > 0 with delta >= 2 → marginal causal effect."""

    def test_interpretation_contains_marginal_causal_effect(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(responses=_rule_presence_response)
        report = run_ablation("r-ts-001", [rule], [cls], client=fake, trials=5, temperature=0.0)
        assert "marginal causal effect" in report.interpretation

    def test_shift_is_positive(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(responses=_rule_presence_response)
        report = run_ablation("r-ts-001", [rule], [cls], client=fake, trials=5, temperature=0.0)
        assert report.shift > 0

    def test_delta_at_least_two(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(responses=_rule_presence_response)
        report = run_ablation("r-ts-001", [rule], [cls], client=fake, trials=5, temperature=0.0)
        assert report.n_ablated_violations - report.n_present_violations >= 2

    def test_present_obeys_ablated_violates(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(responses=_rule_presence_response)
        report = run_ablation("r-ts-001", [rule], [cls], client=fake, trials=5, temperature=0.0)
        present_v = sum(1 for t in report.trial_results if t.condition == "present" and t.violated)
        ablated_v = sum(1 for t in report.trial_results if t.condition == "ablated" and t.violated)
        assert present_v == 0
        assert ablated_v == 5


# ---------------------------------------------------------------------------
# run_ablation — Branch 2: no shift (both conditions obey)
# ---------------------------------------------------------------------------


class TestRunAblationNoShift:
    """Branch 2: shift == 0.0 → no material behavior shift, never a delete word."""

    def _run(self) -> AblationReport:
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(responses=["```sh\nrg -r TODO .\n```"])
        return run_ablation("r-ts-001", [rule], [cls], client=fake, trials=5, temperature=0.0)

    def test_shift_is_zero(self):
        assert self._run().shift == 0.0

    def test_interpretation_no_material_shift(self):
        assert "No material behavior shift" in self._run().interpretation

    def test_interpretation_not_evidence(self):
        assert "NOT evidence" in self._run().interpretation

    def test_interpretation_never_recommends_deletion(self):
        interp = self._run().interpretation.lower()
        # "deletable" appears in a negation — but no standalone "delete" recommendation
        assert not re.search(r"\b(safe to delete|can delete|should delete|go ahead and delete)\b", interp)


# ---------------------------------------------------------------------------
# run_ablation — Branch 3: preliminary signal (delta == 1)
# ---------------------------------------------------------------------------


class TestRunAblationPreliminarySignal:
    """Branch 3: delta == 1 → preliminary signal, NOT marginal causal effect."""

    def _run(self) -> AblationReport:
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        # trials=3: call order is [present0, ablated0, present1, ablated1, present2, ablated2]
        # Only the 2nd call (first ablated) violates.
        scripted = [
            "```sh\nrg -r TODO .\n```",   # present trial 0 — obey
            "```sh\ngrep -r TODO .\n```",  # ablated trial 0 — violate
            "```sh\nrg -r TODO .\n```",   # present trial 1 — obey
            "```sh\nrg -r TODO .\n```",   # ablated trial 1 — obey
            "```sh\nrg -r TODO .\n```",   # present trial 2 — obey
            "```sh\nrg -r TODO .\n```",   # ablated trial 2 — obey
        ]
        fake = FakeChatClient(responses=scripted)
        return run_ablation("r-ts-001", [rule], [cls], client=fake, trials=3, temperature=0.0)

    def test_delta_is_one(self):
        report = self._run()
        assert report.n_ablated_violations - report.n_present_violations == 1

    def test_interpretation_preliminary_signal(self):
        assert "preliminary signal" in self._run().interpretation

    def test_interpretation_not_marginal_causal_effect(self):
        assert "marginal causal effect" not in self._run().interpretation


# ---------------------------------------------------------------------------
# run_ablation — Branch 4: unknown rule prefix
# ---------------------------------------------------------------------------


class TestRunAblationUnknownPrefix:
    """Branch 4: no matching classification → error set, trial_results empty."""

    def test_error_contains_no_classification_found(self):
        fake = FakeChatClient()
        report = run_ablation("nonexistent-prefix", [], [], client=fake, trials=3)
        assert report.error is not None
        assert "No classification found" in report.error

    def test_trial_results_empty(self):
        fake = FakeChatClient()
        report = run_ablation("nonexistent-prefix", [], [], client=fake, trials=3)
        assert report.trial_results == []

    def test_no_crash(self):
        # Must not raise any exception
        run_ablation("no-such-rule", [], [], client=FakeChatClient())


# ---------------------------------------------------------------------------
# run_ablation — Branch 5: non-convertible rule
# ---------------------------------------------------------------------------


class TestRunAblationNonConvertible:
    """Branch 5: convert_kind not in {never_command, tool_substitution} → error, no crash."""

    def test_safety_rule_error_mentions_convertible(self):
        rule = _make_rule(rule_id="r-safety")
        cls = Classification(
            rule_id="r-safety",
            category=CATEGORY_SAFETY_KEEP,
            convert_kind=None,
            predicate={"tool": "Bash", "match": "rm -rf", "decision": "deny"},
            is_safety=True,
            confidence=CONFIDENCE_HIGH,
            rationale="destructive",
        )
        fake = FakeChatClient()
        report = run_ablation("r-safety", [rule], [cls], client=fake)
        assert report.error is not None
        # Error must explain what kind of rule is required
        assert "convert_kind" in report.error or "convertible" in report.error

    def test_safety_rule_trial_results_empty(self):
        rule = _make_rule(rule_id="r-safety")
        cls = Classification(
            rule_id="r-safety",
            category=CATEGORY_SAFETY_KEEP,
            convert_kind=None,
            predicate=None,
            is_safety=True,
            confidence=CONFIDENCE_HIGH,
            rationale="destructive",
        )
        fake = FakeChatClient()
        report = run_ablation("r-safety", [rule], [cls], client=fake)
        assert report.trial_results == []

    def test_judgment_rule_error_set_no_crash(self):
        rule = _make_rule(rule_id="r-judgment")
        cls = Classification(
            rule_id="r-judgment",
            category=CATEGORY_JUDGMENT_KEEP,
            convert_kind=None,
            predicate=None,
            is_safety=False,
            confidence=CONFIDENCE_HIGH,
            rationale="judgment",
        )
        fake = FakeChatClient()
        report = run_ablation("r-judgment", [rule], [cls], client=fake)
        assert report.error is not None
        assert report.trial_results == []


# ---------------------------------------------------------------------------
# run_ablation — Branch 6: Ollama unavailable
# ---------------------------------------------------------------------------


class TestRunAblationUnavailable:
    """Branch 6: available() returns False → model_available False, no chat calls."""

    def test_model_available_is_false(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(available=False)
        report = run_ablation("r-ts-001", [rule], [cls], client=fake)
        assert report.model_available is False

    def test_error_is_set(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(available=False)
        report = run_ablation("r-ts-001", [rule], [cls], client=fake)
        assert report.error is not None

    def test_trial_results_empty(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(available=False)
        report = run_ablation("r-ts-001", [rule], [cls], client=fake)
        assert report.trial_results == []

    def test_no_chat_calls_made(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(available=False)
        run_ablation("r-ts-001", [rule], [cls], client=fake)
        assert len(fake.calls) == 0


# ---------------------------------------------------------------------------
# run_ablation — Branch 7: mid-run network error (guards FIX F)
# ---------------------------------------------------------------------------


class TestRunAblationMidRunNetworkError:
    """Branch 7: URLError on 3rd call → incompleteness interpretation, not fabricated verdict."""

    def _run(self) -> AblationReport:
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(
            responses=["```sh\nrg TODO\n```"],
            side_effect=(urllib.error.URLError("connection refused"), 3),
        )
        return run_ablation("r-ts-001", [rule], [cls], client=fake, trials=5)

    def test_error_set(self):
        assert self._run().error is not None

    def test_interpretation_is_incomplete_message(self):
        """Guards FIX F: must be the incompleteness message, not a fabricated verdict."""
        report = self._run()
        assert "Probe did not complete" in report.interpretation
        assert "no measurement was taken" in report.interpretation

    def test_interpretation_never_fabricates_no_shift(self):
        """The network-error interpretation must not masquerade as a no-shift verdict."""
        assert "No material behavior shift" not in self._run().interpretation

    def test_no_crash(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(side_effect=(urllib.error.URLError("x"), 3))
        run_ablation("r-ts-001", [rule], [cls], client=fake, trials=5)


# ---------------------------------------------------------------------------
# run_ablation — Branch 8: TypeError/ValueError from chat (guards FIX H)
# ---------------------------------------------------------------------------


class TestRunAblationMalformedResponse:
    """Branch 8: TypeError/ValueError from chat → caught, error set, no traceback."""

    def test_typeerror_sets_error(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(side_effect=TypeError("bad return type"))
        report = run_ablation("r-ts-001", [rule], [cls], client=fake, trials=3)
        assert report.error is not None

    def test_typeerror_does_not_propagate(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(side_effect=TypeError("bad return type"))
        run_ablation("r-ts-001", [rule], [cls], client=fake, trials=3)

    def test_valueerror_sets_error(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(side_effect=ValueError("unexpected shape"))
        report = run_ablation("r-ts-001", [rule], [cls], client=fake, trials=3)
        assert report.error is not None

    def test_valueerror_does_not_propagate(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(side_effect=ValueError("unexpected shape"))
        run_ablation("r-ts-001", [rule], [cls], client=fake, trials=3)


# ---------------------------------------------------------------------------
# FIX A — truncation guard (critical regression test)
# ---------------------------------------------------------------------------


class TestFixATruncationGuard:
    """task= prompt must pass through untruncated to the model (FIX A regression guard)."""

    def test_long_task_sent_untruncated_to_model(self):
        """user= kwarg in every recorded chat call must be the full (≥260 char) string."""
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        long_task = "You need to search for TODO comments in the source files. " * 5
        assert len(long_task) >= 260, "precondition: task is genuinely long"
        fake = FakeChatClient(responses=["```sh\nrg TODO\n```"])
        run_ablation("r-ts-001", [rule], [cls], client=fake, trials=1, task=long_task)
        assert len(fake.calls) > 0
        for call in fake.calls:
            assert len(call["user"]) >= 260, "task was truncated before sending to model"
            assert "…" not in call["user"], "truncation ellipsis must not appear in prompt"

    def test_long_task_preserved_in_report_task_prompt(self):
        """report.task_prompt must equal the full collapsed task, not a truncated excerpt."""
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        long_task = "You need to search for TODO comments in the source files. " * 5
        fake = FakeChatClient(responses=["```sh\nrg TODO\n```"])
        report = run_ablation("r-ts-001", [rule], [cls], client=fake, trials=1, task=long_task)
        assert len(report.task_prompt) >= 260

    def test_synthesized_task_output_format_instruction_survives(self):
        """Synthesized (default) task: recorded user= prompt must end with 'no explanation.'"""
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(responses=["```sh\nrg TODO\n```"])
        run_ablation("r-ts-001", [rule], [cls], client=fake, trials=1)
        assert len(fake.calls) > 0
        for call in fake.calls:
            assert call["user"].endswith("no explanation."), (
                f"output-format instruction missing from user prompt tail: {call['user'][-50:]!r}"
            )


# ---------------------------------------------------------------------------
# detect_violation — unit tests
# ---------------------------------------------------------------------------


class TestDetectViolationNeverCommand:
    def test_fenced_git_commit_violates(self):
        cls = _make_never_cmd_cls()
        violated, _ = detect_violation('```sh\ngit commit -m "x"\n```', cls)
        assert violated is True

    def test_commit_agent_prose_does_not_violate(self):
        cls = _make_never_cmd_cls()
        violated, _ = detect_violation("I would use the commit agent instead.", cls)
        assert violated is False

    def test_fenced_block_extraction_prefers_block_content(self):
        """Fenced block is extracted; unfenced prose outside is ignored."""
        cls = _make_never_cmd_cls()
        response = "I suggest:\n```sh\ngit status\n```\nOr use the commit agent."
        violated, _ = detect_violation(response, cls)
        assert violated is False

    def test_no_fence_line_fallback_detects_violation(self):
        """No fenced block: all non-empty lines are scanned."""
        cls = _make_never_cmd_cls()
        violated, _ = detect_violation("git commit -m 'changes'", cls)
        assert violated is True

    def test_no_fence_line_fallback_obey(self):
        cls = _make_never_cmd_cls()
        violated, _ = detect_violation("git status", cls)
        assert violated is False

    def test_returned_proposed_action_is_sanitized(self):
        """proposed_action must not contain raw /Users/ paths."""
        cls = _make_never_cmd_cls()
        _, proposed = detect_violation("git commit -m 'msg'", cls)
        assert "/Users/" not in proposed


class TestDetectViolationToolSubstitution:
    def test_grep_fenced_violates(self):
        cls = _make_tool_sub_cls()
        violated, _ = detect_violation("```sh\ngrep -r TODO .\n```", cls)
        assert violated is True

    def test_rg_fenced_does_not_violate(self):
        cls = _make_tool_sub_cls()
        violated, _ = detect_violation("```sh\nrg TODO\n```", cls)
        assert violated is False

    def test_grep_in_multi_line_fenced_block(self):
        cls = _make_tool_sub_cls()
        response = "Here's the command:\n```sh\ngrep -rn TODO src/\n```"
        violated, _ = detect_violation(response, cls)
        assert violated is True


class TestDetectViolationEditWrite:
    """never_command with tool='Edit|Write' → substring match anywhere in full response."""

    def _cls(self) -> Classification:
        return Classification(
            rule_id="r-env",
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_NEVER_COMMAND,
            predicate={"tool": "Edit|Write", "match": ".env", "decision": "deny"},
            is_safety=False,
            confidence=CONFIDENCE_HIGH,
            rationale="Never edit .env",
        )

    def test_path_substring_in_response_violates(self):
        violated, _ = detect_violation("I will edit the .env file.", self._cls())
        assert violated is True

    def test_absent_path_does_not_violate(self):
        violated, _ = detect_violation("I will use rg to search.", self._cls())
        assert violated is False

    def test_substring_match_works_outside_fenced_block(self):
        """Edit|Write uses full-response substring scan, not fenced-block extraction."""
        response = "I would update ```sh\nsome-command\n``` and the .env"
        violated, _ = detect_violation(response, self._cls())
        assert violated is True


# ---------------------------------------------------------------------------
# synthesize_task
# ---------------------------------------------------------------------------


class TestSynthesizeTask:
    def test_tool_sub_task_mentions_forbidden_tool_via_fallback(self):
        """Unknown forbidden tool falls back to a template that names the tool."""
        rule = _make_rule()
        cls = Classification(
            rule_id="r-ts-001",
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_TOOL_SUBSTITUTION,
            predicate={"tool": "Bash", "forbidden": "find", "prefer": "fd"},
            is_safety=False,
            confidence=CONFIDENCE_HIGH,
            rationale="Use fd not find",
        )
        task = synthesize_task(cls, rule)
        # Fallback template names the forbidden tool explicitly
        assert "find" in task

    def test_task_always_ends_with_output_format_instruction(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        assert synthesize_task(cls, rule).endswith("no explanation.")

    def test_never_command_task_ends_with_output_format_instruction(self):
        rule = _make_never_cmd_rule()
        cls = _make_never_cmd_cls()
        assert synthesize_task(cls, rule).endswith("no explanation.")

    def test_deterministic_for_same_inputs(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        assert synthesize_task(cls, rule) == synthesize_task(cls, rule)

    def test_known_grep_predicate_body_references_todo_search(self):
        """The grep fixture maps to a known template body about TODO searches."""
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        task = synthesize_task(cls, rule)
        assert "TODO" in task


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------


class TestBuildContext:
    def _two_rule_fixture(self):
        """Return (candidate, section_mate, classifications)."""
        candidate = _make_rule(
            rule_id="r-candidate",
            normalized_text="Use rg instead of grep.",
            section="Search",
            source_rel="rules/search.md",
        )
        mate = Rule(
            rule_id="r-mate",
            source_path="/Users/testuser/.claude/rules/search.md",
            source_rel="rules/search.md",
            precedence_tier="rules_file",
            section="Search",
            line_start=5,
            line_end=5,
            raw_text="Never use npm, use pnpm.",
            normalized_text="Never use npm, use pnpm.",
            imperative=True,
        )
        cls_list: list = []
        return candidate, mate, cls_list

    def test_present_includes_candidate_normalized_text(self):
        candidate, mate, cls_list = self._two_rule_fixture()
        ctx = build_context([candidate, mate], cls_list, "r-candidate", present=True)
        assert "Use rg instead of grep." in ctx

    def test_ablated_omits_candidate_text(self):
        candidate, mate, cls_list = self._two_rule_fixture()
        ctx = build_context([candidate, mate], cls_list, "r-candidate", present=False)
        assert "Use rg instead of grep." not in ctx

    def test_ablated_retains_section_mate(self):
        """Ablating the candidate must NOT remove co-located section-mates."""
        candidate, mate, cls_list = self._two_rule_fixture()
        ctx = build_context([candidate, mate], cls_list, "r-candidate", present=False)
        assert "pnpm" in ctx or "npm" in ctx

    def test_no_raw_user_path_emitted(self):
        """Context must never contain a raw /Users/<name>/ substring."""
        rule = Rule(
            rule_id="r-path",
            source_path="/Users/alice/.claude/rules/test.md",
            source_rel="rules/test.md",
            precedence_tier="rules_file",
            section="Privacy",
            line_start=1,
            line_end=1,
            raw_text="Never edit /Users/alice/.claude/settings.json.",
            normalized_text="Never edit /Users/alice/.claude/settings.json.",
            imperative=True,
        )
        ctx = build_context([rule], [], "r-path", present=True)
        assert "/Users/" not in ctx
        assert "~/" in ctx


# ---------------------------------------------------------------------------
# Privacy helpers: _collapse_only and _sanitize_str
# ---------------------------------------------------------------------------


class TestPrivacyHelpers:
    def test_collapse_only_with_trailing_slash(self):
        assert _collapse_only("/Users/alice/.claude/x") == "~/.claude/x"

    def test_collapse_only_bare_no_trailing_slash(self):
        """Bare /Users/<name> at end of token: username must be collapsed."""
        result = _collapse_only("see /Users/alice")
        assert "/Users/alice" not in result
        assert "~" in result

    def test_collapse_only_does_not_truncate_long_string(self):
        long = "/Users/alice/.claude/" + "a" * 300
        result = _collapse_only(long)
        assert len(result) > 200
        assert "…" not in result

    def test_sanitize_str_truncates_to_200_with_ellipsis(self):
        long = "a" * 300
        result = _sanitize_str(long)
        assert len(result) <= 200
        assert result.endswith("…")

    def test_sanitize_str_collapses_user_path(self):
        result = _sanitize_str("/Users/bob/project/foo.py")
        assert "/Users/bob" not in result
        assert "~/" in result

    def test_collapse_only_does_not_add_ellipsis(self):
        result = _collapse_only("x" * 300)
        assert "…" not in result

    def test_sanitize_and_collapse_both_remove_user_path(self):
        path = "/Users/charlie/.claude/rules/test.md"
        assert "/Users/charlie" not in _sanitize_str(path)
        assert "/Users/charlie" not in _collapse_only(path)


class TestPrivacyInReport:
    """No /Users/ path leaks in any string field of report_to_dict(report)."""

    def test_no_user_path_in_serialized_report(self):
        rule = Rule(
            rule_id="r-priv",
            source_path="/Users/alice/.claude/rules/test.md",
            source_rel="rules/test.md",
            precedence_tier="rules_file",
            section="Privacy",
            line_start=1,
            line_end=1,
            raw_text="Never edit /Users/alice/secret directly.",
            normalized_text="Never edit /Users/alice/secret directly.",
            imperative=True,
        )
        cls = Classification(
            rule_id="r-priv",
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_TOOL_SUBSTITUTION,
            predicate={"tool": "Bash", "forbidden": "grep", "prefer": "rg"},
            is_safety=False,
            confidence=CONFIDENCE_HIGH,
            rationale="test",
        )
        fake = FakeChatClient(responses=["```sh\nrg TODO\n```"])
        report = run_ablation("r-priv", [rule], [cls], client=fake, trials=1)
        serialized = json.dumps(report_to_dict(report), sort_keys=True)
        assert "/Users/" not in serialized


# ---------------------------------------------------------------------------
# OllamaClient shape validation (FIX H) — no real network via monkeypatch
# ---------------------------------------------------------------------------


class _FakeHTTPResp:
    """Minimal context-manager fake for urllib.request.urlopen responses."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.status = 200

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestOllamaClientShapeValidation:
    def test_null_message_raises_value_error(self, monkeypatch):
        """{'message': null} must raise ValueError (FIX H guard)."""
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=None: _FakeHTTPResp(b'{"message": null}'),
        )
        with pytest.raises(ValueError):
            OllamaClient().chat(model="llama3", system="s", user="u", temperature=0.0)

    def test_well_formed_response_returns_content(self, monkeypatch):
        """{'message': {'content': 'ok'}} must return 'ok'."""
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=None: _FakeHTTPResp(b'{"message": {"content": "ok"}}'),
        )
        result = OllamaClient().chat(model="llama3", system="s", user="u", temperature=0.0)
        assert result == "ok"


# ---------------------------------------------------------------------------
# report_to_dict
# ---------------------------------------------------------------------------


_EXPECTED_KEYS = {
    "convert_kind",
    "disclaimers",
    "error",
    "interpretation",
    "model",
    "model_available",
    "n_ablated_violations",
    "n_present_violations",
    "predicate",
    "rule_excerpt",
    "rule_id",
    "shift",
    "source_rel",
    "task_prompt",
    "temperature",
    "trial_results",
    "trials",
    "violation_rate_ablated",
    "violation_rate_present",
}


class TestReportToDict:
    def _success_report(self) -> AblationReport:
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(responses=["```sh\nrg TODO\n```"])
        return run_ablation("r-ts-001", [rule], [cls], client=fake, trials=1)

    def test_round_trips_json_without_error(self):
        json.dumps(report_to_dict(self._success_report()), sort_keys=True)

    def test_contains_all_documented_keys(self):
        d = report_to_dict(self._success_report())
        assert _EXPECTED_KEYS.issubset(d.keys())

    def test_model_available_true_on_success(self):
        d = report_to_dict(self._success_report())
        assert d["model_available"] is True

    def test_error_none_on_success(self):
        d = report_to_dict(self._success_report())
        assert d["error"] is None

    def test_error_report_model_available_false(self):
        rule = _make_rule()
        cls = _make_tool_sub_cls()
        fake = FakeChatClient(available=False)
        report = run_ablation("r-ts-001", [rule], [cls], client=fake)
        d = report_to_dict(report)
        assert d["model_available"] is False
        assert d["error"] is not None

    def test_trial_results_list_structure(self):
        d = report_to_dict(self._success_report())
        assert isinstance(d["trial_results"], list)
        for tr in d["trial_results"]:
            assert "condition" in tr
            assert "violated" in tr
            assert "trial_index" in tr
            assert "proposed_action" in tr
            assert "raw_excerpt" in tr


# ---------------------------------------------------------------------------
# _build_disclaimers — honesty caveats (FIX G: Constructed task)
# ---------------------------------------------------------------------------


class TestBuildDisclaimers:
    def test_returns_exactly_five_items(self):
        items = _build_disclaimers("llama3", 5, 0.7)
        assert len(items) == 5

    def test_all_items_are_non_empty_strings(self):
        items = _build_disclaimers("llama3", 5, 0.7)
        assert all(isinstance(item, str) and len(item) > 0 for item in items)

    def test_proxy_model_caveat_present(self):
        combined = "\n".join(_build_disclaimers("llama3", 5, 0.7))
        assert "Proxy model" in combined

    def test_small_n_caveat_present(self):
        combined = "\n".join(_build_disclaimers("llama3", 5, 0.7))
        assert "Small-N" in combined or "N=5" in combined

    def test_non_shift_not_deletion_caveat_present(self):
        combined = "\n".join(_build_disclaimers("llama3", 5, 0.7))
        assert "deletion" in combined.lower() or "Non-shift" in combined

    def test_constructed_task_caveat_present(self):
        """FIX G: 'Constructed task' disclaimer must appear so users know rates aren't base rates."""
        combined = "\n".join(_build_disclaimers("llama3", 5, 0.7))
        assert "Constructed task" in combined

    def test_opt_in_observer_caveat_present(self):
        combined = "\n".join(_build_disclaimers("llama3", 5, 0.7))
        assert "opt-in" in combined.lower() or "Opt-in" in combined
