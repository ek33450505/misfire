"""test_scaffold.py — unit tests for the Phase 3 hook scaffolder.

Covers, per convert_kind:
- the correct ladder rung / event / matcher / settings-snippet shape,
- byte-faithful embedding of misfire's structural matcher,
- the *runtime* behaviour of the emitted hook (run as a subprocess against the
  real PreToolUse / PostToolUse stdin contract) — the strongest unit proof,
- the honesty surfaces (KEEP for safety/judgment/output-shape/non-directive),
- escape-hatch exemption, branch guard, version feature-detection, and PII
  sanitization.

Stdlib + pytest only. No network, no DB.
"""

from __future__ import annotations

import json
import py_compile
import re
import subprocess
import sys
from pathlib import Path

import pytest

from misfire import match
from misfire import scaffold as scaffold_mod
from misfire.classify import (
    CATEGORY_CONVERTIBLE,
    CATEGORY_JUDGMENT_KEEP,
    CATEGORY_NON_DIRECTIVE,
    CATEGORY_OUTPUT_SHAPE,
    CATEGORY_SAFETY_KEEP,
    Classification,
    CONVERT_AFTER_ACTION,
    CONVERT_BEFORE_ACTION,
    CONVERT_NEVER_COMMAND,
    CONVERT_TOOL_SUBSTITUTION,
)
from misfire.scaffold import (
    EVENT_POST,
    EVENT_PRE,
    RUNG_ENFORCE,
    RUNG_KEEP,
    detect_claude_version,
    event_support_note,
    scaffold_hook,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk(
    category: str,
    convert_kind=None,
    predicate=None,
    *,
    rule_id: str = "abc123def456",
    is_safety: bool = False,
) -> Classification:
    return Classification(
        rule_id=rule_id,
        category=category,
        convert_kind=convert_kind,
        predicate=predicate,
        is_safety=is_safety,
        confidence="high",
        rationale="test",
    )


def _run_hook(script: str, payload: dict) -> tuple[int, str]:
    """Write *script* to a temp file, run it with *payload* on stdin."""
    d = Path(tempfile_mkdtemp())
    hp = d / "hook.py"
    hp.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(hp)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout.strip()


def tempfile_mkdtemp() -> str:
    import tempfile

    return tempfile.mkdtemp()


def _compiles(script: str) -> bool:
    """True iff *script* is syntactically valid Python (catches codegen bugs)."""
    p = Path(tempfile_mkdtemp()) / "h.py"
    p.write_text(script, encoding="utf-8")
    try:
        py_compile.compile(str(p), doraise=True)
        return True
    except py_compile.PyCompileError:
        return False


def _denied(stdout: str) -> bool:
    if not stdout:
        return False
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    return (
        data.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


# ---------------------------------------------------------------------------
# never_command (Bash)
# ---------------------------------------------------------------------------


def test_never_command_bash_shape() -> None:
    cl = _mk(
        CATEGORY_CONVERTIBLE,
        CONVERT_NEVER_COMMAND,
        {"tool": "Bash", "match": "git commit", "decision": "deny"},
    )
    sc = scaffold_hook(cl, "Never raw git commit")
    assert sc.rung == RUNG_ENFORCE
    assert sc.event == EVENT_PRE
    assert sc.matcher == "Bash"
    assert sc.is_skeleton is False
    assert sc.hook_filename == "misfire-never-command-abc123de.py"
    assert "FORBIDDEN = 'git commit'" in sc.hook_script
    # settings snippet shape (the exact 3-level nesting Claude Code requires)
    snip = sc.settings_snippet
    entry = snip["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["type"] == "command"
    assert entry["hooks"][0]["command"].startswith("${CLAUDE_PROJECT_DIR}/.claude/hooks/")
    assert entry["hooks"][0]["command"].endswith(sc.hook_filename)


def test_never_command_bash_runtime_denies_and_allows() -> None:
    cl = _mk(
        CATEGORY_CONVERTIBLE,
        CONVERT_NEVER_COMMAND,
        {"tool": "Bash", "match": "git commit", "decision": "deny"},
    )
    sc = scaffold_hook(cl, "Never raw git commit")
    # blocks an actual invocation
    rc, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}})
    assert rc == 0
    assert _denied(out)
    # allows an unrelated command
    _, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": "git status"}})
    assert not _denied(out)
    # non-Bash tool is ignored
    _, out = _run_hook(sc.hook_script, {"tool_name": "Edit", "tool_input": {"file_path": "x"}})
    assert not _denied(out)


def test_never_command_bash_ignores_quoted_occurrence() -> None:
    """The differentiator: a quoted 'git commit' is data, not an invocation."""
    cl = _mk(
        CATEGORY_CONVERTIBLE,
        CONVERT_NEVER_COMMAND,
        {"tool": "Bash", "match": "git commit", "decision": "deny"},
    )
    sc = scaffold_hook(cl, "Never raw git commit")
    _, out = _run_hook(
        sc.hook_script,
        {"tool_name": "Bash", "tool_input": {"command": 'echo "git commit is blocked"'}},
    )
    assert not _denied(out), "quoted occurrence must NOT be blocked (no naive-substring FP)"


def test_never_command_bash_failopen_on_bad_stdin() -> None:
    cl = _mk(CATEGORY_CONVERTIBLE, CONVERT_NEVER_COMMAND, {"tool": "Bash", "match": "git commit"})
    sc = scaffold_hook(cl, "x")
    d = Path(tempfile_mkdtemp())
    hp = d / "hook.py"
    hp.write_text(sc.hook_script, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(hp)], input="not json", capture_output=True, text=True)
    assert proc.returncode == 0
    assert not _denied(proc.stdout.strip())


# ---------------------------------------------------------------------------
# Embedded-matcher equivalence (no drift vs misfire's own matcher)
# ---------------------------------------------------------------------------


def test_embedded_matcher_equivalent_to_misfire() -> None:
    cl = _mk(CATEGORY_CONVERTIBLE, CONVERT_NEVER_COMMAND, {"tool": "Bash", "match": "git commit"})
    sc = scaffold_hook(cl, "x")
    ns: dict = {"re": re}
    exec(sc.hook_script.split("def main(")[0], ns)  # defs only, skip main()
    embedded = ns["command_invokes"]
    cases = [
        ("git commit -m x", "git commit"),
        ("git status", "git commit"),
        ('echo "git commit"', "git commit"),
        ('grep "git commit" f', "git commit"),
        ("git commitizen", "git commit"),
        ("CAST_COMMIT_AGENT=1 git commit", "git commit"),
        ("rg foo && grep bar", "grep"),
    ]
    for cmd, tgt in cases:
        assert embedded(cmd, tgt) == match.command_invokes(cmd, tgt), cmd


# ---------------------------------------------------------------------------
# never_command (Edit|Write)
# ---------------------------------------------------------------------------


def test_never_command_editwrite_shape_and_runtime() -> None:
    cl = _mk(
        CATEGORY_CONVERTIBLE,
        CONVERT_NEVER_COMMAND,
        {"tool": "Edit|Write", "match": "settings.json", "decision": "deny"},
    )
    sc = scaffold_hook(cl, "Never touch settings.json")
    assert sc.rung == RUNG_ENFORCE
    # matcher must register ALL four file-mutating tools the script handles —
    # not just Edit|Write — or MultiEdit/NotebookEdit silently slip through.
    assert sc.matcher == "Edit|Write|MultiEdit|NotebookEdit"
    assert (
        sc.settings_snippet["hooks"]["PreToolUse"][0]["matcher"]
        == "Edit|Write|MultiEdit|NotebookEdit"
    )
    # runtime: blocks Write/Edit/MultiEdit (file_path) and NotebookEdit (notebook_path)
    _, out = _run_hook(sc.hook_script, {"tool_name": "Write", "tool_input": {"file_path": "/a/settings.json"}})
    assert _denied(out)
    _, out = _run_hook(sc.hook_script, {"tool_name": "MultiEdit", "tool_input": {"file_path": "/a/settings.json"}})
    assert _denied(out)
    _, out = _run_hook(sc.hook_script, {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/a/settings.json"}})
    assert _denied(out)
    # allows an unrelated path
    _, out = _run_hook(sc.hook_script, {"tool_name": "Edit", "tool_input": {"file_path": "/a/main.py"}})
    assert not _denied(out)
    # Bash tool ignored
    _, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": "echo settings.json"}})
    assert not _denied(out)


# ---------------------------------------------------------------------------
# tool_substitution
# ---------------------------------------------------------------------------


def test_tool_substitution_runtime_and_reason() -> None:
    cl = _mk(
        CATEGORY_CONVERTIBLE,
        CONVERT_TOOL_SUBSTITUTION,
        {"tool": "Bash", "forbidden": "grep", "prefer": "rg"},
    )
    sc = scaffold_hook(cl, "use rg not grep")
    assert sc.matcher == "Bash"
    assert "`rg`" in sc.reason and "`grep`" in sc.reason
    _, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": "grep foo file"}})
    assert _denied(out)
    _, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": "rg foo file"}})
    assert not _denied(out)


# ---------------------------------------------------------------------------
# push to main — branch guard
# ---------------------------------------------------------------------------


def test_never_push_main_branch_guard() -> None:
    cl = _mk(
        CATEGORY_CONVERTIBLE,
        CONVERT_NEVER_COMMAND,
        {"tool": "Bash", "match": "git push", "target": "main", "decision": "deny"},
    )
    sc = scaffold_hook(cl, "Never push to main")
    assert any("Branch-scoped" in c for c in sc.caveats)

    def denies(cmd: str) -> bool:
        _, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": cmd}})
        return _denied(out)

    # denies a push that targets main/master as a delimited arg or refspec
    assert denies("git push origin main")
    assert denies("git push origin HEAD:main")
    assert denies("git push origin master --force")
    # does NOT over-block a branch NAME that merely contains main/master
    assert not denies("git push origin my-main-feature")
    assert not denies("git push origin feature/master-plan")
    # does NOT deny a push with no branch token (documented under-block limitation)
    assert not denies("git push")


# ---------------------------------------------------------------------------
# escape-hatch exemption
# ---------------------------------------------------------------------------


def test_escape_hatch_exemption_runtime() -> None:
    cl = _mk(CATEGORY_CONVERTIBLE, CONVERT_NEVER_COMMAND, {"tool": "Bash", "match": "git commit"})
    sc = scaffold_hook(cl, "Never raw git commit", exception_marker="CAST_COMMIT_AGENT=1")
    assert "EXCEPTION = 'CAST_COMMIT_AGENT=1'" in sc.hook_script
    assert any("escape hatch" in c for c in sc.caveats)
    # plain invocation blocked
    _, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}})
    assert _denied(out)
    # sanctioned variants allowed
    for cmd in ("CAST_COMMIT_AGENT=1 git commit -m x", "export CAST_COMMIT_AGENT=1 && git commit"):
        _, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert not _denied(out), cmd


# ---------------------------------------------------------------------------
# before_action / after_action — skeletons
# ---------------------------------------------------------------------------


def test_before_action_skeleton() -> None:
    cl = _mk(
        CATEGORY_CONVERTIBLE,
        CONVERT_BEFORE_ACTION,
        {"hook": "PreToolUse", "action": "run", "before": "commit"},
    )
    sc = scaffold_hook(cl, "run tests before commit")
    assert sc.is_skeleton is True
    assert sc.event == EVENT_PRE
    assert any("No violation evidence" in c for c in sc.caveats)
    assert "TODO" in sc.hook_script
    assert _compiles(sc.hook_script)
    # a skeleton never blocks until completed (violated = False) and exits cleanly
    rc, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": "git commit"}})
    assert rc == 0
    assert not _denied(out)


def test_after_action_skeleton_is_posttooluse() -> None:
    cl = _mk(
        CATEGORY_CONVERTIBLE,
        CONVERT_AFTER_ACTION,
        {"hook": "PostToolUse", "action": "run", "after": "edit"},
    )
    sc = scaffold_hook(cl, "run lint after edit")
    assert sc.is_skeleton is True
    assert sc.event == EVENT_POST
    assert "PostToolUse" in sc.settings_snippet["hooks"]
    assert '"decision": "block"' in sc.hook_script
    # the indentation-sensitive _EMIT_POST sentinel must yield valid Python and
    # run cleanly as a PostToolUse hook (rc 0, no block until completed).
    assert _compiles(sc.hook_script)
    rc, out = _run_hook(sc.hook_script, {"tool_name": "Edit", "tool_input": {"file_path": "/a/x.py"}})
    assert rc == 0
    assert '"decision": "block"' not in out


# ---------------------------------------------------------------------------
# KEEP rungs (the honesty surfaces)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category,needle",
    [
        (CATEGORY_SAFETY_KEEP, "Safety rule"),
        (CATEGORY_OUTPUT_SHAPE, "SubagentStop"),
        (CATEGORY_JUDGMENT_KEEP, "Judgment"),
        (CATEGORY_NON_DIRECTIVE, "Not an actionable directive"),
    ],
)
def test_non_convertible_categories_keep(category: str, needle: str) -> None:
    cl = _mk(category, is_safety=(category == CATEGORY_SAFETY_KEEP))
    sc = scaffold_hook(cl)
    assert sc.rung == RUNG_KEEP
    assert sc.hook_script is None
    assert sc.settings_snippet is None
    assert sc.event is None
    assert needle in sc.reason


def test_safety_keep_even_when_machine_checkable() -> None:
    """Safety invariant: a hook-able safety rule is still KEPT, never auto-enforced."""
    cl = _mk(
        CATEGORY_SAFETY_KEEP,
        predicate={"tool": "Bash", "match": "git push --force", "decision": "deny"},
        is_safety=True,
    )
    sc = scaffold_hook(cl, "Never force-push")
    assert sc.rung == RUNG_KEEP
    assert sc.hook_script is None


# ---------------------------------------------------------------------------
# PII sanitization
# ---------------------------------------------------------------------------


def test_excerpt_pii_sanitized_in_script() -> None:
    cl = _mk(CATEGORY_CONVERTIBLE, CONVERT_NEVER_COMMAND, {"tool": "Bash", "match": "git commit"})
    sc = scaffold_hook(cl, "Never edit /Users/alice/.claude/settings.json")
    assert "/Users/alice" not in sc.hook_script
    assert "alice" not in sc.reason


# ---------------------------------------------------------------------------
# version feature-detection
# ---------------------------------------------------------------------------


def test_detect_version_parses_stdout() -> None:
    assert detect_claude_version(runner=lambda: "2.1.170 (Claude Code)") == "2.1.170"


def test_detect_version_none_on_garbage() -> None:
    assert detect_claude_version(runner=lambda: "no version here") is None


def test_detect_version_none_on_runner_error() -> None:
    def boom() -> str:
        raise FileNotFoundError("claude")

    assert detect_claude_version(runner=boom) is None


def test_event_support_note() -> None:
    # stable event + known version → no note
    assert event_support_note("PreToolUse", "2.1.170") is None
    # stable event + unknown version → advisory
    assert "Could not detect" in event_support_note("PreToolUse", None)
    # non-stable event → advisory regardless of version
    assert "outside misfire's documented-stable set" in event_support_note("ZorpToolUse", "2.1.170")


def test_detect_version_subprocess_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner=None path parses `claude --version` stdout (no real subprocess)."""
    import types

    monkeypatch.setattr(
        scaffold_mod.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout="2.1.170 (Claude Code)\n", returncode=0),
    )
    assert detect_claude_version() == "2.1.170"


def test_detect_version_subprocess_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(scaffold_mod.subprocess, "run", boom)
    assert detect_claude_version() is None


# ---------------------------------------------------------------------------
# template robustness — sentinel self-injection
# ---------------------------------------------------------------------------


def test_render_sentinel_injection_safe() -> None:
    """A rule excerpt containing an internal sentinel must not corrupt the hook."""
    cl = _mk(CATEGORY_CONVERTIBLE, CONVERT_NEVER_COMMAND, {"tool": "Bash", "match": "git commit"})
    sc = scaffold_hook(cl, "rule mentions __MATCHER_SRC__ and __CONDITION__ and __REASON_REPR__")
    assert _compiles(sc.hook_script), "sentinel in excerpt must not break codegen"
    # and the hook still functions
    _, out = _run_hook(sc.hook_script, {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}})
    assert _denied(out)
