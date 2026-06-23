"""scaffold.py — Phase 3 deterministic hook scaffolder for misfire.

Zero-LLM, templated.  Turns a ``Classification`` (from ``classify.py``) into a
recommendation on the **3-tier ladder** and, for the ENFORCE rung, a
self-contained Claude Code hook script plus the ``settings.json`` registration
snippet needed to install it.

The 3-tier ladder (``docs/framing.md`` Architecture §4):

    KEEP     — judgment / safety / output-shape / non-directive rules.  Stay as
               prose.  Safety rules are KEPT even when machine-checkable (the
               safety invariant): a destructive-action guard is not something to
               silently template away.
    ELEVATE  — a convertible rule that should move to a path-scoped
               ``.claude/rules`` (``paths:`` frontmatter) or ``--append-system-
               prompt`` rather than a hook.  Surfaced as advice; misfire does
               not rewrite config.  (Reserved; ``scaffold_hook`` itself routes
               convertibles to ENFORCE — the CLI decides ELEVATE-vs-ENFORCE
               from evidence.)
    ENFORCE  — a convertible rule with a machine-checkable predicate → a
               ``PreToolUse`` / ``PostToolUse`` hook scaffold.

Posture (``docs/framing.md`` guardrails 2 & 18):
- Observer / recommender.  The output is **printed for the user to review and
  install**; misfire NEVER writes ``settings.json``.
- The emitted hook **denies with a suggested-alternative reason** — never a
  silent ``updatedInput`` rewrite.  The observer-not-actor stance is carried
  into the generated artifact itself.

Matcher fidelity (the differentiator):
- For Bash-command rules the generated hook **embeds misfire's own structural
  command matcher** (``match.command_invokes``, via ``inspect.getsource``), so a
  converted rule does NOT regress to the ~80% naive-substring false-positive
  rate the whole tool exists to measure.  A quoted occurrence such as
  ``echo "git commit"`` is correctly NOT blocked.

Public API::

    scaffold_hook(classification: Classification, excerpt: str = "") -> HookScaffold
    detect_claude_version(runner=None) -> Optional[str]
    event_support_note(event: str, version: Optional[str]) -> Optional[str]
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

from misfire import match
from misfire.classify import (
    CATEGORY_CONVERTIBLE,
    CATEGORY_NON_DIRECTIVE,
    CATEGORY_OUTPUT_SHAPE,
    CATEGORY_SAFETY_KEEP,
    Classification,
    CONVERT_AFTER_ACTION,
    CONVERT_BEFORE_ACTION,
    CONVERT_NEVER_COMMAND,
    CONVERT_TOOL_SUBSTITUTION,
)


# ---------------------------------------------------------------------------
# Ladder rungs + event constants
# ---------------------------------------------------------------------------

RUNG_KEEP = "keep"
RUNG_ELEVATE = "elevate"
RUNG_ENFORCE = "enforce"

EVENT_PRE = "PreToolUse"
EVENT_POST = "PostToolUse"

# Documented, stable hook events misfire is willing to target deterministically.
# Used by event_support_note for the version feature-detection advisory.
STABLE_EVENTS = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "SessionStart",
        "Stop",
        "SubagentStop",
    }
)

# Conventional install location for a hook script, relative to the project root.
_HOOK_DIR_REL = ".claude/hooks"

# Collapse /Users/<name>/ or /home/<name>/ → ~/ in any text that lands in an
# emitted artifact (defense-in-depth; callers already sanitize the excerpt).
_USER_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s]+")

# Version-string extractor for `claude --version` output ("2.1.170 (Claude Code)").
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HookScaffold:
    """The scaffolder's verdict for a single rule.

    Fields
    ------
    rule_id
        The ``Classification.rule_id`` (= ``Rule.rule_id``).
    rung
        One of ``RUNG_KEEP`` / ``RUNG_ELEVATE`` / ``RUNG_ENFORCE``.
    convert_kind
        The ``Classification.convert_kind`` when ENFORCE; ``None`` otherwise.
    event
        ``"PreToolUse"`` / ``"PostToolUse"`` for ENFORCE scaffolds; ``None`` for
        KEEP/ELEVATE.
    matcher
        The settings ``matcher`` string (``"Bash"``, ``"Edit|Write"``); ``None``
        for KEEP/ELEVATE.
    hook_filename
        Suggested filename for the hook script; ``None`` for KEEP/ELEVATE.
    hook_script
        Full, self-contained hook-script text; ``None`` for KEEP/ELEVATE.
    settings_snippet
        The ``settings.json`` fragment to merge to register the hook; ``None``
        for KEEP/ELEVATE.
    reason
        Human-readable explanation: for ENFORCE this is the deny reason embedded
        in the hook; for KEEP/ELEVATE it explains why no hook was produced.
    caveats
        Tuple of honesty caveats (e.g. "no violation evidence — ordering not
        reconstructible", "cannot detect current-branch pushes").  Always a
        tuple (possibly empty).
    is_skeleton
        ``True`` when the ENFORCE scaffold is an incomplete template the user
        must finish (``before_action`` / ``after_action``).
    """

    rule_id: str
    rung: str
    convert_kind: Optional[str]
    event: Optional[str]
    matcher: Optional[str]
    hook_filename: Optional[str]
    hook_script: Optional[str]
    settings_snippet: Optional[Dict]
    reason: str
    caveats: Tuple[str, ...]
    is_skeleton: bool


# ---------------------------------------------------------------------------
# Embedded matcher (DRY: the generated hook reuses misfire's own matcher)
# ---------------------------------------------------------------------------


def _embedded_matcher_source() -> str:
    """Return the source of ``_strip_quoted_spans`` + ``command_invokes``.

    Extracted from ``match.py`` via ``inspect.getsource`` so the generated hook
    is byte-faithful to misfire's own structural matcher — no drift, no
    hand-maintained copy.  The functions carry only a function-local
    ``result: List[str]`` annotation (never evaluated at runtime) and signature
    annotations of builtins (``str`` / ``bool``), so the embedded copy runs in a
    standalone script importing only ``re`` — no ``typing`` dependency.

    Raises:
        RuntimeError: if the source cannot be read (e.g. a zipped install).
    """
    try:
        strip_src = inspect.getsource(match._strip_quoted_spans)
        invokes_src = inspect.getsource(match.command_invokes)
    except (OSError, TypeError) as exc:  # pragma: no cover - only on frozen installs
        raise RuntimeError(
            "misfire could not read its own matcher source to embed in the "
            "generated hook (is misfire installed from a zip/frozen build?)"
        ) from exc
    return strip_src + "\n\n" + invokes_src


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _sanitize(text: str) -> str:
    """Collapse any ``/Users/<name>/`` → ``~/`` (defense-in-depth)."""
    return _USER_PATH_RE.sub("~", text)


def _safe_comment(text: str, limit: int = 120) -> str:
    """Make *text* safe for a single-line ``#`` comment in the generated script.

    Collapses all whitespace (including newlines) to single spaces, sanitizes
    home paths, and truncates to *limit* characters.
    """
    collapsed = " ".join(_sanitize(text).split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "…"
    return collapsed


def _short_id(rule_id: str) -> str:
    """First 8 chars of the rule_id, for a stable, readable filename."""
    return rule_id[:8]


def _hook_filename(convert_kind: str, rule_id: str) -> str:
    """Deterministic hook filename: ``misfire-<kind>-<shortid>.py``."""
    kind = convert_kind.replace("_", "-")
    return f"misfire-{kind}-{_short_id(rule_id)}.py"


def _settings_snippet(event: str, matcher: str, filename: str) -> Dict:
    """Build the ``settings.json`` fragment registering the hook.

    Uses ``${CLAUDE_PROJECT_DIR}`` so the path is portable across machines (the
    documented placeholder that expands to the project root at hook runtime).
    """
    return {
        "hooks": {
            event: [
                {
                    "matcher": matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"${{CLAUDE_PROJECT_DIR}}/{_HOOK_DIR_REL}/{filename}",
                        }
                    ],
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# Script templates (sentinel-replacement — avoids brace-escaping hazards)
# ---------------------------------------------------------------------------

_HEADER = """#!/usr/bin/env python3
# misfire-generated __EVENT__ hook -- DO NOT EDIT BY HAND.
# Enforces rule __RULE_ID__: "__EXCERPT__"
# Generated by `misfire convert` (Phase 3). Zero-LLM, deterministic.
#
# misfire is an observer: this hook DENIES with a reason (your own rule text),
# never silently rewrites the command.
"""

# Bash deny script: embeds misfire's structural matcher so a quoted occurrence
# (e.g.  echo "git commit") is NOT treated as an invocation (no naive-substring
# false positive).
_BASH_TEMPLATE = (
    _HEADER
    + """#
# This hook embeds misfire's own structural command matcher, so it does NOT
# regress to naive-substring matching: a quoted occurrence is not blocked.
import json
import re
import sys

FORBIDDEN = __FORBIDDEN_REPR__
REASON = __REASON_REPR__
# Escape hatch the rule itself names (sanctioned usage); "" if none. A command
# containing this marker is allowed through, matching misfire's own accounting.
EXCEPTION = __EXCEPTION_REPR__


__MATCHER_SRC__


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        # Fail-open: a generated convenience hook never blocks on a parse error.
        sys.exit(0)
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    command = (data.get("tool_input") or {}).get("command", "") or ""
    if __CONDITION__:
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            }
        }
        sys.stdout.write(json.dumps(decision))
    sys.exit(0)


if __name__ == "__main__":
    main()
"""
)

# Edit|Write deny script: plain substring on the target path (file paths are not
# shell commands, so the structural matcher is unnecessary).
_EDITWRITE_TEMPLATE = (
    _HEADER
    + """import json
import sys

PATH_MATCH = __MATCH_REPR__
REASON = __REASON_REPR__
TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if data.get("tool_name") not in TOOLS:
        sys.exit(0)
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if PATH_MATCH and PATH_MATCH in file_path:
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            }
        }
        sys.stdout.write(json.dumps(decision))
    sys.exit(0)


if __name__ == "__main__":
    main()
"""
)

# Skeleton for before_action / after_action: misfire cannot auto-generate the
# specific check (the rule names a trigger but not a machine-checkable
# condition), and these kinds carry NO violation evidence.  Emit an honest,
# clearly-marked template the user completes.
_SKELETON_TEMPLATE = (
    _HEADER
    + """#
# SKELETON -- misfire could NOT auto-generate the check for this rule.
# `before_action` / `after_action` rules have NO violation evidence in misfire's
# flat action stream (action ordering is not reconstructible), and the rule
# states a trigger but not a machine-checkable condition. Complete the TODO.
import json
import sys

REASON = __REASON_REPR__
# Trigger tool(s) this rule is about (edit to taste):
TRIGGER = __TRIGGER_REPR__


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    # TODO: inspect `data["tool_name"]` / `data["tool_input"]` and decide whether
    # this action violates the rule. Until you implement a real condition this
    # hook does nothing (fail-open).
    violated = False  # <-- implement me
    if violated:
        __EMIT__
    sys.exit(0)


if __name__ == "__main__":
    main()
"""
)

# Emit blocks for the skeleton, per event.
_EMIT_PRE = """decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            }
        }
        sys.stdout.write(json.dumps(decision))"""

_EMIT_POST = """decision = {"decision": "block", "reason": REASON}
        sys.stdout.write(json.dumps(decision))"""


def _render(template: str, replacements: Dict[str, str]) -> str:
    """Replace ``__SENTINEL__`` tokens in *template*.

    Sentinel replacement (not ``str.format``) avoids escaping every ``{`` / ``}``
    in the embedded JSON-emitting code.
    """
    out = template
    for token, value in replacements.items():
        out = out.replace(token, value)
    return out


# ---------------------------------------------------------------------------
# Reason / condition builders
# ---------------------------------------------------------------------------


def _deny_reason(classification: Classification, excerpt: str) -> str:
    """Build the human-readable deny reason (the suggested-alternative text)."""
    safe_excerpt = _safe_comment(excerpt, limit=160) if excerpt else ""
    predicate = classification.predicate or {}
    if classification.convert_kind == CONVERT_TOOL_SUBSTITUTION:
        prefer = predicate.get("prefer", "the preferred tool")
        forbidden = predicate.get("forbidden", "this command")
        base = f"Use `{prefer}` instead of `{forbidden}` (misfire-generated)."
    else:
        base = "Blocked by your rule (misfire-generated)."
    if safe_excerpt:
        return f'{base} Rule: "{safe_excerpt}"'
    return base


def _keep_reason(classification: Classification) -> str:
    """Explain why a non-convertible rule stays as prose."""
    category = classification.category
    if category == CATEGORY_SAFETY_KEEP:
        return (
            "Safety rule — KEEP as prose. Destructive/irreversible guards are "
            "kept as prose by design (the safety invariant), even when "
            "machine-checkable. Convert deliberately, not automatically."
        )
    if category == CATEGORY_OUTPUT_SHAPE:
        return (
            "Output-shape rule (Handoff / Status / Work Log) — KEEP as prose. "
            "These are enforced via a SubagentStop ledger (the evidence layer), "
            "not a PreToolUse deny hook."
        )
    if category == CATEGORY_NON_DIRECTIVE:
        return "Not an actionable directive (provenance / meta note) — nothing to convert."
    # judgment_keep and any fallthrough
    return (
        "Judgment / style / altitude rule — KEEP as prose. No machine-checkable "
        "predicate; a hook would be brittle or wrong."
    )


# ---------------------------------------------------------------------------
# ENFORCE builders (one per convert_kind)
# ---------------------------------------------------------------------------


def _build_bash_enforce(
    classification: Classification, excerpt: str, exception_marker: str = ""
) -> HookScaffold:
    """ENFORCE scaffold for never_command (Bash) and tool_substitution.

    ``exception_marker`` is the rule's own escape-hatch literal (e.g.
    ``CAST_COMMIT_AGENT=1``), extracted by the CLI.  When present, the generated
    hook lets a command containing it through — so the hook never blocks usage
    the rule itself sanctions (consistent with misfire's violation accounting).
    """
    predicate = classification.predicate or {}
    convert_kind = classification.convert_kind
    rule_id = classification.rule_id

    if convert_kind == CONVERT_TOOL_SUBSTITUTION:
        forbidden = predicate.get("forbidden", "")
    else:
        forbidden = predicate.get("match", "")

    reason = _deny_reason(classification, excerpt)
    caveats: List[str] = []

    # Build the deny condition incrementally.
    parts = ["command_invokes(command, FORBIDDEN)"]

    # Branch guard for "never push to main/master": only deny when a main/master
    # token is present in the command — we cannot see the checked-out branch.
    if convert_kind == CONVERT_NEVER_COMMAND and predicate.get("target") in (
        "main",
        "master",
    ):
        parts.append('re.search(r"\\b(main|master)\\b", command)')
        caveats.append(
            "Branch-scoped: this hook can only block a push when 'main'/'master' "
            "appears in the command. A push from a checked-out main branch with no "
            "branch argument is NOT detected — keep the prose rule too."
        )

    # Escape-hatch exemption: do not block a command the rule itself sanctions.
    if exception_marker:
        parts.append("not (EXCEPTION and EXCEPTION in command)")
        caveats.append(
            f"Honors the rule's escape hatch ({exception_marker!r}): commands "
            "containing that marker are allowed through, matching misfire's "
            "violation accounting."
        )

    condition = " and ".join(parts)

    script = _render(
        _BASH_TEMPLATE,
        {
            "__EVENT__": EVENT_PRE,
            "__RULE_ID__": rule_id,
            "__EXCERPT__": _safe_comment(excerpt) if excerpt else "(no excerpt)",
            "__FORBIDDEN_REPR__": repr(_sanitize(forbidden)),
            "__REASON_REPR__": repr(reason),
            "__EXCEPTION_REPR__": repr(_sanitize(exception_marker)),
            "__MATCHER_SRC__": _embedded_matcher_source(),
            "__CONDITION__": condition,
        },
    )
    filename = _hook_filename(convert_kind, rule_id)
    return HookScaffold(
        rule_id=rule_id,
        rung=RUNG_ENFORCE,
        convert_kind=convert_kind,
        event=EVENT_PRE,
        matcher="Bash",
        hook_filename=filename,
        hook_script=script,
        settings_snippet=_settings_snippet(EVENT_PRE, "Bash", filename),
        reason=reason,
        caveats=tuple(caveats),
        is_skeleton=False,
    )


def _build_editwrite_enforce(
    classification: Classification, excerpt: str
) -> HookScaffold:
    """ENFORCE scaffold for never_command with an Edit|Write tool (path guard)."""
    predicate = classification.predicate or {}
    rule_id = classification.rule_id
    path_match = predicate.get("match", "")
    reason = _deny_reason(classification, excerpt)

    script = _render(
        _EDITWRITE_TEMPLATE,
        {
            "__EVENT__": EVENT_PRE,
            "__RULE_ID__": rule_id,
            "__EXCERPT__": _safe_comment(excerpt) if excerpt else "(no excerpt)",
            "__MATCH_REPR__": repr(_sanitize(path_match)),
            "__REASON_REPR__": repr(reason),
        },
    )
    filename = _hook_filename(CONVERT_NEVER_COMMAND, rule_id)
    return HookScaffold(
        rule_id=rule_id,
        rung=RUNG_ENFORCE,
        convert_kind=CONVERT_NEVER_COMMAND,
        event=EVENT_PRE,
        matcher="Edit|Write",
        hook_filename=filename,
        hook_script=script,
        settings_snippet=_settings_snippet(EVENT_PRE, "Edit|Write", filename),
        reason=reason,
        caveats=(
            "Path guard uses a substring match on the edited file path; tune "
            "PATH_MATCH if it is too broad or too narrow.",
        ),
        is_skeleton=False,
    )


def _build_skeleton_enforce(
    classification: Classification, excerpt: str
) -> HookScaffold:
    """ENFORCE *skeleton* for before_action / after_action (honest template)."""
    predicate = classification.predicate or {}
    rule_id = classification.rule_id
    convert_kind = classification.convert_kind
    event = predicate.get("hook") or (
        EVENT_PRE if convert_kind == CONVERT_BEFORE_ACTION else EVENT_POST
    )
    trigger = predicate.get("before") or predicate.get("after") or ""
    reason = (
        f'Rule: "{_safe_comment(excerpt, limit=160)}" (misfire-generated skeleton)'
        if excerpt
        else "misfire-generated skeleton — complete the check."
    )
    emit = _EMIT_PRE if event == EVENT_PRE else _EMIT_POST
    # matcher: a sensible default for the trigger word, but the user will tune it.
    matcher = "Bash" if trigger in ("commit", "push", "merge", "deploy") else "Edit|Write"

    script = _render(
        _SKELETON_TEMPLATE,
        {
            "__EVENT__": event,
            "__RULE_ID__": rule_id,
            "__EXCERPT__": _safe_comment(excerpt) if excerpt else "(no excerpt)",
            "__REASON_REPR__": repr(reason),
            "__TRIGGER_REPR__": repr(_sanitize(trigger)),
            "__EMIT__": emit,
        },
    )
    filename = _hook_filename(convert_kind, rule_id)
    return HookScaffold(
        rule_id=rule_id,
        rung=RUNG_ENFORCE,
        convert_kind=convert_kind,
        event=event,
        matcher=matcher,
        hook_filename=filename,
        hook_script=script,
        settings_snippet=_settings_snippet(event, matcher, filename),
        reason=reason,
        caveats=(
            "No violation evidence: before_action/after_action ordering is not "
            "reconstructible from misfire's flat action stream, so this rule is "
            "UNRANKED.",
            "Skeleton only: misfire cannot infer the specific check — you must "
            "implement the `violated` condition before this hook does anything.",
        ),
        is_skeleton=True,
    )


def _keep_scaffold(classification: Classification) -> HookScaffold:
    """A KEEP verdict — no hook emitted."""
    return HookScaffold(
        rule_id=classification.rule_id,
        rung=RUNG_KEEP,
        convert_kind=None,
        event=None,
        matcher=None,
        hook_filename=None,
        hook_script=None,
        settings_snippet=None,
        reason=_keep_reason(classification),
        caveats=(),
        is_skeleton=False,
    )


# ---------------------------------------------------------------------------
# Public API — scaffold_hook
# ---------------------------------------------------------------------------


def scaffold_hook(
    classification: Classification,
    excerpt: str = "",
    exception_marker: str = "",
) -> HookScaffold:
    """Produce a ``HookScaffold`` for a single classified rule.

    Pure and deterministic — depends only on the ``Classification``, the
    (already-sanitized) ``excerpt`` text, and an optional ``exception_marker``.
    Carries NO evidence/violation data; the CLI layers evidence on top to decide
    whether an ENFORCE scaffold is an evidence-grounded *recommendation* (the
    honesty guard lives in the CLI, not here).

    Routing:
    - ``convertible`` + ``never_command`` (Bash)      → ENFORCE PreToolUse (Bash)
    - ``convertible`` + ``never_command`` (Edit|Write)→ ENFORCE PreToolUse (Edit|Write)
    - ``convertible`` + ``tool_substitution``         → ENFORCE PreToolUse (Bash)
    - ``convertible`` + ``before_action``/``after_action`` → ENFORCE skeleton
    - everything else (safety / output_shape / judgment / non_directive) → KEEP

    Args:
        classification: The rule's ``Classification``.
        excerpt: Already-sanitized rule text for the hook comment + deny reason.
        exception_marker: The rule's escape-hatch literal (e.g.
            ``CAST_COMMIT_AGENT=1``).  When present and the rule is a Bash
            convertible, the generated hook exempts commands containing it.

    Returns:
        A ``HookScaffold``.
    """
    if classification.category != CATEGORY_CONVERTIBLE:
        return _keep_scaffold(classification)

    convert_kind = classification.convert_kind
    predicate = classification.predicate or {}

    if convert_kind == CONVERT_NEVER_COMMAND:
        tool = predicate.get("tool", "Bash")
        if "|" in tool:  # Edit|Write
            return _build_editwrite_enforce(classification, excerpt)
        return _build_bash_enforce(classification, excerpt, exception_marker)

    if convert_kind == CONVERT_TOOL_SUBSTITUTION:
        return _build_bash_enforce(classification, excerpt, exception_marker)

    if convert_kind in (CONVERT_BEFORE_ACTION, CONVERT_AFTER_ACTION):
        return _build_skeleton_enforce(classification, excerpt)

    # Unknown convert_kind (defensive): keep as prose rather than guess.
    return _keep_scaffold(classification)


# ---------------------------------------------------------------------------
# Version feature-detection (advisory only — never gates conversion)
# ---------------------------------------------------------------------------


def detect_claude_version(runner: Optional[Callable[[], str]] = None) -> Optional[str]:
    """Return the installed Claude Code version (``"2.1.170"``) or ``None``.

    Args:
        runner: Optional injectable callable returning the raw ``claude
            --version`` stdout (for testing).  When ``None``, ``claude
            --version`` is invoked via ``subprocess`` with a short timeout.

    Never raises: any failure (binary absent, timeout, unparsable) → ``None``.
    """
    try:
        if runner is not None:
            raw = runner()
        else:
            proc = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            raw = proc.stdout or ""
        m = _VERSION_RE.search(raw or "")
        return m.group(1) if m else None
    except Exception:
        return None


def event_support_note(event: str, version: Optional[str]) -> Optional[str]:
    """Return an advisory string when the target event may not be supported.

    Conservative: only warns when the event is outside misfire's documented
    stable set, or when the Claude Code version could not be detected.  Returns
    ``None`` when there is nothing to say (the common case).
    """
    if event not in STABLE_EVENTS:
        return (
            f"Hook event {event!r} is outside misfire's documented-stable set "
            f"({', '.join(sorted(STABLE_EVENTS))}); verify it against your "
            "Claude Code version before installing."
        )
    if version is None:
        return (
            "Could not detect your Claude Code version (`claude` not on PATH?). "
            f"The generated hook targets the documented-stable {event} event; "
            "verify the hook schema against your installed version."
        )
    return None
