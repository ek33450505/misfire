"""classify.py — Phase 1 convertible/judgment classifier for misfire.

Classifies each ``Rule`` (from ``parse.py``) into one of five categories:

    non_directive   — metadata/provenance note; no actionable directive signal
    safety_keep     — destructive/irreversible marker; KEPT as prose regardless
    output_shape    — agent output-protocol rule (Handoff block, Status, Work Log)
    convertible     — machine-checkable predicate → hook recommendation
    judgment_keep   — style/altitude/judgment rule; KEPT as prose

Classification is **deterministic, ordered, and conservative**: when the
evidence is ambiguous the classifier defaults to ``judgment_keep`` rather
than erroneously proposing a hook.  This is the "honesty line" from
``docs/framing.md`` Guardrail 9.

Public API::

    classify_rule(rule: Rule) -> Classification
    classify_rules(rules: list[Rule]) -> list[Classification]
"""

from __future__ import annotations

import dataclasses
import re
from typing import Dict, List, Optional

from misfire.parse import Rule


# ---------------------------------------------------------------------------
# Categories and convert-kind constants
# ---------------------------------------------------------------------------

CATEGORY_NON_DIRECTIVE = "non_directive"
CATEGORY_SAFETY_KEEP = "safety_keep"
CATEGORY_OUTPUT_SHAPE = "output_shape"
CATEGORY_CONVERTIBLE = "convertible"
CATEGORY_JUDGMENT_KEEP = "judgment_keep"

CONVERT_NEVER_COMMAND = "never_command"
CONVERT_TOOL_SUBSTITUTION = "tool_substitution"
CONVERT_BEFORE_ACTION = "before_action"
CONVERT_AFTER_ACTION = "after_action"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Classification:
    """Classification of a single ``Rule``.

    ``category`` is exactly one of the five CATEGORY_* constants.

    ``convert_kind`` is only populated when ``category == 'convertible'``
    (one of the CONVERT_* constants); ``None`` otherwise.

    ``predicate`` holds machine-checkable structured bits even for
    ``safety_keep`` rules that happen to also be machine-checkable,
    so a later scaffolder tier can offer "keep prose + optionally enforce".

    ``is_safety`` is ``True`` only for ``safety_keep`` rules.

    ``confidence`` reflects how cleanly the predicate matched:
    ``high`` = tight named-tool + command, ``medium`` = inferred,
    ``low`` = no clean match (conservative default).
    """

    rule_id: str
    category: str
    convert_kind: Optional[str]
    predicate: Optional[Dict]
    is_safety: bool
    confidence: str
    rationale: str


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# Strong-directive words: modals + common imperative verbs.
# Excludes ``before``/``after`` deliberately — those words alone are
# insufficient to identify an actionable directive (they appear descriptively
# in provenance notes such as "Recreated … after the ~/.claude wipe").
_STRONG_DIRECTIVE_RE = re.compile(
    r"\b(never|must|always|mandatory|prefer|avoid|ensure|keep|dispatch|emit|"
    r"verify|check|route|run|do)\b"
    r"|\bdo\s+not\b"
    r"|\buse\b.{0,80}?\bnot\b",
    re.IGNORECASE,
)

# Provenance / meta note keywords at the start of normalised text.
_PROVENANCE_RE = re.compile(
    r"^(Recreated|Moved|See\s+memory|See\s+also|Written|Updated|Amended|"
    r"TODO|Note:|Rationale|Reference|Added)\b",
    re.IGNORECASE,
)

# Safety: destructive / irreversible action markers.
_SAFETY_RE = re.compile(
    r"\bforce.?push\b"
    r"|push.*--force\b"
    r"|\brm\s+-rf?\b"
    r"|\brmtree\b"
    r"|\bpkill\b"
    r"|\bkillall\b"
    r"|\bdestructive\b"
    r"|\birreversible\b"
    r"|\bschema\s+migration\b"
    r"|\bdb\b.{0,20}?\bprun"
    r"|\bdb\b.{0,20}?\bdelete\b"
    r"|\bprun.{0,20}?\bdb\b"
    r"|\bdrop\s+table\b"
    r"|\$HOME\b"
    r"|\btemp.?home\b"
    r"|\bsetup_temp_home\b"
    r"|\bback\s*up\s*or\s*abort\b"
    r"|\bfail.?closed\b"
    r"|\bblast\s+radius\b",
    re.IGNORECASE,
)

# Output-shape: agent output-protocol rules that map to a SubagentStop ledger.
_OUTPUT_SHAPE_RE = re.compile(
    r"\bhandoff\s*block\b"
    r"|\bwork\s+log\b"
    r"|\bstatus\s+block\b"
    r"|\bstatus\s+line\b"
    r"|\bstatus:\s*(done|done_with_concerns|blocked|needs_context)\b"
    r"|\bSubagentStop\b"
    r"|\bend\s+with\b.{0,60}?\b(block|status|handoff|log)\b"
    r"|\b(emit|include)\b.{0,50}?\b(handoff|status\s*block|work\s+log)\b"
    r"|\bfrontmatter\s+fields?\b"
    r"|\b(required|mandatory)\b.{0,40}?\bfrontmatter\b",
    re.IGNORECASE,
)

# Judgment / style / altitude rules.
_JUDGMENT_RE = re.compile(
    r"\b(YAGNI|DRY|TDD)\b"
    r"|\bconcise\b"
    r"|\bconcisely\b"
    r"|\bceremony\b"
    r"|\bscope\s+discipline\b"
    r"|\bstep.by.step\b"
    r"|\baltitude\b"
    r"|\bprefer\s+existing\b"
    r"|\bmatch.{0,20}?\btask\s+size\b"
    r"|\bprefer\b.{0,40}?\b(pattern|convention|existing)\b",
    re.IGNORECASE,
)

# --- Convertible sub-patterns ---

# never_command: "never [raw] git commit", "never push [to main]", etc.
_NEVER_GIT_COMMIT_RE = re.compile(
    r"\bnever\b.{0,40}?(raw\s+)?git\s+commit\b",
    re.IGNORECASE,
)
_NEVER_FORCE_PUSH_RE = re.compile(
    r"\bnever\b.{0,60}?\b(force.?push|git\s+push.{0,20}?--force|push.{0,20}?force)\b",
    re.IGNORECASE,
)
_NEVER_PUSH_MAIN_RE = re.compile(
    r"\bnever\b.{0,60}?\b(git\s+)?push\b.{0,30}?\b(main|master)\b",
    re.IGNORECASE,
)
_NEVER_PUSH_RE = re.compile(
    r"\bnever\b.{0,20}?\b(git\s+)?push\b",
    re.IGNORECASE,
)
_NEVER_TOUCH_RE = re.compile(
    r"\bnever\b.{0,15}?\btouch\b\s+(\S+)",
    re.IGNORECASE,
)
_NEVER_RUN_RE = re.compile(
    r"\bnever\b.{0,15}?\brun\b\s+(\w[\w/-]*)",
    re.IGNORECASE,
)

# tool_substitution: "use A not B" or "use A instead of B"
_TOOL_SUB_RE = re.compile(
    r"\buse\b\s+(\w[\w-]*)\b.{0,40}?\b(not|instead\s+of)\b\s+(\w[\w-]*)",
    re.IGNORECASE,
)

# Validation helpers for tool_substitution guard.
# Shape: shell command name — lowercase start, alphanumeric/underscore/hyphen, ≤31 chars.
# Kills TitleCase (Jest, Vitest), ALLCAPS (EXISTS, CREATE), dotted hostnames (github.com).
_COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")

# Stoplist: hosting services and obvious non-CLI tokens that pass shape check.
_TOOL_STOPLIST = frozenset({
    "github", "gitlab", "bitbucket", "sourceforge",
})

# Backtick-wrapped single-token extractor (no whitespace inside backticks).
# A genuine "use `rg` not `grep`" backticks its commands; prose does not.
_BACKTICK_TOKEN_RE = re.compile(r"`([^`\s]+)`")

# before_action: "run/check/do X before commit/push/edit/..."
_BEFORE_ACTION_RE = re.compile(
    r"\b(run|check|do|add|stage|ensure|invoke|verify)\b.{0,80}?\bbefore\b"
    r".{0,40}?\b(commit|commits?|committing|push|pushing|edit|editing|merge|deploy)\b",
    re.IGNORECASE,
)

# after_action: "do/run X after edit/commit/..."
_AFTER_ACTION_RE = re.compile(
    r"\b(do|run|check|ensure|verify)\b.{0,80}?\bafter\b"
    r".{0,40}?\b(edit|editing|commit|committing|push|pushing|update)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_non_directive(raw: str, norm: str, imperative: bool) -> bool:
    """Return True when the rule carries no actionable directive signal.

    Three cases (in order):

    1. Blockquote line (starts with ``>``) — meta / provenance note.
    2. Normalised text begins with a provenance keyword (Recreated, Moved, etc.).
    3. ``imperative=True`` was triggered *only* by ``before``/``after`` in a
       descriptive context — there is no strong-directive word (never/must/run/etc.)
       or ``use … not`` pattern present.  Better to call such a rule
       non_directive (→ no recommendation) than to mis-convert it.
    """
    # 1. Blockquote
    if raw.lstrip().startswith(">"):
        return True
    # 2. Provenance keyword
    if _PROVENANCE_RE.match(norm):
        return True
    # 3. Imperative only from descriptive before/after — no strong directive
    if imperative and not _STRONG_DIRECTIVE_RE.search(norm):
        return True
    return False


def _try_never_command(norm: str) -> Optional[tuple]:
    """Return (predicate: dict, confidence: str) if this is a never_command rule.

    Tries the most specific patterns first (git commit, force-push) before
    falling back to generic push / touch / run.

    Returns ``None`` if no never_command pattern matches.
    """
    # git commit (most common case: "never raw git commit")
    if _NEVER_GIT_COMMIT_RE.search(norm):
        return {"tool": "Bash", "match": "git commit", "decision": "deny"}, CONFIDENCE_HIGH

    # force-push (safety: also triggers _SAFETY_RE, but we populate predicate here
    # so the safety branch can call this helper for the predicate dict)
    if _NEVER_FORCE_PUSH_RE.search(norm):
        return {"tool": "Bash", "match": "git push --force", "decision": "deny"}, CONFIDENCE_HIGH

    # push to main/master
    if _NEVER_PUSH_MAIN_RE.search(norm):
        return {
            "tool": "Bash",
            "match": "git push",
            "target": "main",
            "decision": "deny",
        }, CONFIDENCE_HIGH

    # generic push
    if _NEVER_PUSH_RE.search(norm):
        return {"tool": "Bash", "match": "git push", "decision": "deny"}, CONFIDENCE_MEDIUM

    # touch <path>
    m = _NEVER_TOUCH_RE.search(norm)
    if m:
        path = m.group(1).rstrip(".,;)")
        return {"tool": "Edit|Write", "match": path, "decision": "deny"}, CONFIDENCE_MEDIUM

    # run <cmd>
    m = _NEVER_RUN_RE.search(norm)
    if m:
        cmd = m.group(1)
        return {"tool": "Bash", "match": cmd, "decision": "deny"}, CONFIDENCE_MEDIUM

    return None


def _try_tool_substitution(norm: str, raw: str) -> Optional[tuple]:
    """Return (predicate, confidence) for 'use A not B' / 'use A instead of B'.

    All three guards must pass — conservative bias: ambiguous → None → judgment_keep.

    Guard 1 (backtick-wrapped): both tokens must appear as individually
    backtick-wrapped single tokens (no whitespace) in ``raw``.  A genuine CLI
    swap like "use `rg` not `grep`" backticks its commands; prose rules that
    use "not" in passing (e.g. "not Jest", "not GitHub") do not.

    Guard 2 (shape): both tokens must match ``^[a-z][a-z0-9_-]{0,30}$`` —
    lowercase start, no uppercase.  Kills ALLCAPS SQL keywords (EXISTS, CREATE)
    and TitleCase framework names (Jest, Vitest).

    Guard 3 (stoplist): neither token may be a known hosting service or obvious
    non-CLI word (github, gitlab, bitbucket, …).
    """
    m = _TOOL_SUB_RE.search(norm)
    if not m:
        return None

    prefer = m.group(1)
    forbidden = m.group(3)

    # Guard 1: both tokens must be individually backtick-wrapped in raw_text.
    backtick_tokens = set(_BACKTICK_TOKEN_RE.findall(raw))
    if prefer not in backtick_tokens or forbidden not in backtick_tokens:
        return None

    # Guard 2: command-name shape — lowercase start, no uppercase/dots.
    if not _COMMAND_NAME_RE.match(prefer) or not _COMMAND_NAME_RE.match(forbidden):
        return None

    # Guard 3: not a hosting service or other obvious non-CLI token.
    if prefer.lower() in _TOOL_STOPLIST or forbidden.lower() in _TOOL_STOPLIST:
        return None

    return {
        "tool": "Bash",
        "forbidden": forbidden,
        "prefer": prefer,
    }, CONFIDENCE_HIGH


def _try_before_action(norm: str) -> Optional[tuple]:
    """Return (predicate, confidence) for 'run/do X before commit/push/...'."""
    m = _BEFORE_ACTION_RE.search(norm)
    if m:
        action_verb = m.group(1)
        trigger = m.group(2).lower().rstrip("s")  # normalise "commits" → "commit"
        return {
            "hook": "PreToolUse",
            "action": action_verb,
            "before": trigger,
        }, CONFIDENCE_MEDIUM
    return None


def _try_after_action(norm: str) -> Optional[tuple]:
    """Return (predicate, confidence) for 'do/run X after edit/commit/...'."""
    m = _AFTER_ACTION_RE.search(norm)
    if m:
        action_verb = m.group(1)
        trigger = m.group(2).lower().rstrip("s")  # normalise "edits" → "edit"
        return {
            "hook": "PostToolUse",
            "action": action_verb,
            "after": trigger,
        }, CONFIDENCE_MEDIUM
    return None


def _safety_predicate(norm: str) -> Optional[Dict]:
    """Return a machine-checkable predicate if the safety rule is also hook-able.

    Called from the safety branch so that the predicate is populated even
    though the *category* stays ``safety_keep``.
    """
    nc = _try_never_command(norm)
    if nc:
        return nc[0]
    # force-push without "never" (e.g. "avoid force-push")
    if re.search(r"\bforce.?push\b", norm, re.IGNORECASE):
        return {"tool": "Bash", "match": "git push --force", "decision": "deny"}
    # rm -rf
    if re.search(r"\brm\s+-rf?\b|\brmtree\b", norm, re.IGNORECASE):
        return {"tool": "Bash", "match": "rm -rf", "decision": "deny"}
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_rule(rule: Rule) -> Classification:
    """Classify a single ``Rule`` into a ``Classification``.

    Order of evaluation:
    1. non_directive — blockquote / provenance note / no real directive signal
    2. safety_keep  — destructive/irreversible markers (safety wins over convertible)
    3. output_shape — agent output-protocol rules
    4. convertible  — machine-checkable predicate (never_command, tool_substitution,
                       before_action, after_action)
    5. judgment_keep — style / altitude / judgment (matched by keyword)
    6. default       — judgment_keep, confidence=low (conservative fallback)
    """
    norm = rule.normalized_text
    raw = rule.raw_text

    # ------------------------------------------------------------------
    # Step 1: non_directive
    # ------------------------------------------------------------------
    if _is_non_directive(raw, norm, rule.imperative):
        return Classification(
            rule_id=rule.rule_id,
            category=CATEGORY_NON_DIRECTIVE,
            convert_kind=None,
            predicate=None,
            is_safety=False,
            confidence=CONFIDENCE_HIGH,
            rationale="blockquote / provenance note / no real directive signal",
        )

    # ------------------------------------------------------------------
    # Step 2: safety_keep
    # Safety wins over convertible for the category; predicate is still
    # populated when the rule is also machine-checkable so a later tier
    # can offer "keep prose + optionally enforce".
    # ------------------------------------------------------------------
    if _SAFETY_RE.search(norm):
        predicate = _safety_predicate(norm)
        return Classification(
            rule_id=rule.rule_id,
            category=CATEGORY_SAFETY_KEEP,
            convert_kind=None,
            predicate=predicate,
            is_safety=True,
            confidence=CONFIDENCE_HIGH,
            rationale="contains destructive / irreversible safety marker — keep as prose",
        )

    # ------------------------------------------------------------------
    # Step 3: output_shape
    # ------------------------------------------------------------------
    if _OUTPUT_SHAPE_RE.search(norm):
        return Classification(
            rule_id=rule.rule_id,
            category=CATEGORY_OUTPUT_SHAPE,
            convert_kind=None,
            predicate=None,
            is_safety=False,
            confidence=CONFIDENCE_HIGH,
            rationale="agent output-protocol rule (Handoff / Status / Work Log) "
            "— maps to SubagentStop ledger",
        )

    # ------------------------------------------------------------------
    # Step 4: convertible — try sub-kinds in priority order
    # ------------------------------------------------------------------

    # 4a. never_command
    nc_result = _try_never_command(norm)
    if nc_result:
        predicate, confidence = nc_result
        return Classification(
            rule_id=rule.rule_id,
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_NEVER_COMMAND,
            predicate=predicate,
            is_safety=False,
            confidence=confidence,
            rationale="never/do-not command → PreToolUse deny hook",
        )

    # 4b. tool_substitution
    ts_result = _try_tool_substitution(norm, raw)
    if ts_result:
        predicate, confidence = ts_result
        return Classification(
            rule_id=rule.rule_id,
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_TOOL_SUBSTITUTION,
            predicate=predicate,
            is_safety=False,
            confidence=confidence,
            rationale="use A not B → PreToolUse substitution hook",
        )

    # 4c. before_action
    ba_result = _try_before_action(norm)
    if ba_result:
        predicate, confidence = ba_result
        return Classification(
            rule_id=rule.rule_id,
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_BEFORE_ACTION,
            predicate=predicate,
            is_safety=False,
            confidence=confidence,
            rationale="do X before Y → PreToolUse hook",
        )

    # 4d. after_action
    aa_result = _try_after_action(norm)
    if aa_result:
        predicate, confidence = aa_result
        return Classification(
            rule_id=rule.rule_id,
            category=CATEGORY_CONVERTIBLE,
            convert_kind=CONVERT_AFTER_ACTION,
            predicate=predicate,
            is_safety=False,
            confidence=confidence,
            rationale="do X after Y → PostToolUse hook",
        )

    # ------------------------------------------------------------------
    # Step 5: judgment_keep — style / altitude matched by keyword
    # ------------------------------------------------------------------
    if _JUDGMENT_RE.search(norm):
        return Classification(
            rule_id=rule.rule_id,
            category=CATEGORY_JUDGMENT_KEEP,
            convert_kind=None,
            predicate=None,
            is_safety=False,
            confidence=CONFIDENCE_HIGH,
            rationale="style / altitude / judgment rule — keep as prose",
        )

    # ------------------------------------------------------------------
    # Step 6: default — conservative fallback
    # Never default to convertible; when unsure, KEEP.
    # ------------------------------------------------------------------
    return Classification(
        rule_id=rule.rule_id,
        category=CATEGORY_JUDGMENT_KEEP,
        convert_kind=None,
        predicate=None,
        is_safety=False,
        confidence=CONFIDENCE_LOW,
        rationale="no clean pattern match — conservative default: keep as prose",
    )


def classify_rules(rules: List[Rule]) -> List[Classification]:
    """Classify a list of ``Rule`` objects, returning a ``Classification`` per rule."""
    return [classify_rule(r) for r in rules]
