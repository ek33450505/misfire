"""test_cli_convert.py — tests for ``misfire convert`` (Phase 3).

Exercises the evidence-grounded honesty guard and the surface-only output:
- ``--top`` / default → the top evidence-grounded enforce_candidate,
- nothing-to-convert when no rule qualifies,
- KEEP (no hook) for safety / judgment rules,
- reference-only (``recommended=false``) for a convertible rule with zero
  observed violations,
- deterministic JSON, no PII, and a generated hook that actually denies.

Calls ``main()`` directly; fixture paths are resolved against the repo root via
``monkeypatch.chdir`` (mirrors test_proof_rank).  Stdlib + pytest only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from misfire.classify import (
    CATEGORY_JUDGMENT_KEEP,
    CATEGORY_SAFETY_KEEP,
    classify_rules,
)
from misfire.cli import main
from misfire.parse import parse_config

_REPO_ROOT = Path(__file__).parent.parent
_EV_CONFIG = "proof/evidence-sample/config"
_EV_PROJECTS = "proof/evidence-sample/projects"
_SAMPLE_CONFIG = "proof/sample-config"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _json_run(capsys, monkeypatch, argv) -> tuple[int, dict, str]:
    monkeypatch.chdir(_REPO_ROOT)
    rc = main(argv)
    cap = capsys.readouterr()
    return rc, json.loads(cap.out), cap.err


def _rule_id_for_category(config_rel: str, category: str) -> str:
    pr = parse_config(_REPO_ROOT / config_rel)
    for cl in classify_rules(pr.rules):
        if cl.category == category:
            return cl.rule_id
    raise AssertionError(f"no {category} rule in {config_rel}")


def _commit_rule_id(config_rel: str) -> str:
    pr = parse_config(_REPO_ROOT / config_rel)
    for cl in classify_rules(pr.rules):
        if (cl.predicate or {}).get("match") == "git commit":
            return cl.rule_id
    raise AssertionError("no git commit rule")


# ---------------------------------------------------------------------------
# --top / default — the evidence-grounded wedge
# ---------------------------------------------------------------------------


def test_convert_top_selects_enforce_candidate(capsys, monkeypatch) -> None:
    rc, d, _ = _json_run(
        capsys,
        monkeypatch,
        ["convert", _EV_CONFIG, "--projects-dir", _EV_PROJECTS, "--top", "--json"],
    )
    assert rc == 0
    assert d["status"] == "enforce"
    assert d["recommended"] is True
    assert d["rule"]["convert_kind"] == "never_command"
    assert d["evidence"]["status"] == "enforce_candidate"
    assert d["evidence"]["violation_count"] >= 1
    hook = d["hook"]
    assert hook["event"] == "PreToolUse"
    assert hook["matcher"] == "Bash"
    assert hook["is_skeleton"] is False
    # exact 3-level settings nesting + portable path placeholder
    entry = hook["settings_snippet"]["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["command"].startswith("${CLAUDE_PROJECT_DIR}/")


def test_convert_default_equals_top(capsys, monkeypatch) -> None:
    """No --rule and no --top → same as --top."""
    rc, d, _ = _json_run(
        capsys, monkeypatch, ["convert", _EV_CONFIG, "--projects-dir", _EV_PROJECTS, "--json"]
    )
    assert rc == 0
    assert d["status"] == "enforce"
    assert d["recommended"] is True


# ---------------------------------------------------------------------------
# honesty guard — nothing to convert
# ---------------------------------------------------------------------------


def test_convert_nothing_when_no_candidate(capsys, monkeypatch, tmp_path) -> None:
    empty = tmp_path / "empty_projects"
    empty.mkdir()
    rc, d, _ = _json_run(
        capsys, monkeypatch, ["convert", _EV_CONFIG, "--projects-dir", str(empty), "--json"]
    )
    assert rc == 0
    assert d["status"] == "nothing_to_convert"
    assert d["recommended"] is False
    assert d["rule"] is None
    assert d["hook"] is None


# ---------------------------------------------------------------------------
# honesty guard — KEEP for non-convertible rules
# ---------------------------------------------------------------------------


def test_convert_rule_judgment_keeps(capsys, monkeypatch) -> None:
    rid = _rule_id_for_category(_SAMPLE_CONFIG, CATEGORY_JUDGMENT_KEEP)
    rc, d, _ = _json_run(
        capsys, monkeypatch, ["convert", _SAMPLE_CONFIG, "--rule", rid[:8], "--json"]
    )
    assert rc == 0
    assert d["status"] == "keep"
    assert d["recommended"] is False
    assert d["hook"] is None


def test_convert_rule_safety_keeps(capsys, monkeypatch) -> None:
    rid = _rule_id_for_category(_SAMPLE_CONFIG, CATEGORY_SAFETY_KEEP)
    rc, d, _ = _json_run(
        capsys, monkeypatch, ["convert", _SAMPLE_CONFIG, "--rule", rid[:8], "--json"]
    )
    assert rc == 0
    assert d["status"] == "keep"
    assert d["recommended"] is False
    assert d["hook"] is None
    assert "Safety rule" in d["reason"]


# ---------------------------------------------------------------------------
# honesty guard — convertible rule with ZERO observed violations
# ---------------------------------------------------------------------------


def test_convert_rule_zero_violations_not_recommended(capsys, monkeypatch, tmp_path) -> None:
    empty = tmp_path / "empty_projects"
    empty.mkdir()
    rid = _commit_rule_id(_EV_CONFIG)
    rc, d, _ = _json_run(
        capsys,
        monkeypatch,
        ["convert", _EV_CONFIG, "--projects-dir", str(empty), "--rule", rid[:8], "--json"],
    )
    assert rc == 0
    assert d["recommended"] is False
    assert d["evidence"]["status"] == "observed_no_violations"
    # the scaffold is still shown (user explicitly targeted it) but flagged
    assert d["hook"] is not None
    assert "honesty guard" in d["reason"]


# ---------------------------------------------------------------------------
# error + posture
# ---------------------------------------------------------------------------


def test_convert_bad_rule_prefix_exits_1(capsys, monkeypatch) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    rc = main(["convert", _SAMPLE_CONFIG, "--rule", "zzzznotreal", "--json"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no rule found" in err


def test_convert_json_has_no_pii(capsys, monkeypatch) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    main(["convert", _EV_CONFIG, "--projects-dir", _EV_PROJECTS, "--top", "--json"])
    out = capsys.readouterr().out
    for marker in ("/Users/", "/home/", "/private/"):
        assert marker not in out, f"PII leak: {marker}"


def test_convert_text_mode_renders(capsys, monkeypatch) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    # Hermetic: stub version detection so the test never spawns a real
    # `claude --version` subprocess (the advisory goes to stderr regardless).
    monkeypatch.setattr("misfire.cli.detect_claude_version", lambda *a, **k: "9.9.9")
    rc = main(["convert", _EV_CONFIG, "--projects-dir", _EV_PROJECTS, "--top"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "misfire convert" in out
    assert "Verdict: ENFORCE" in out
    assert "PreToolUse" in out
    assert "settings.json (merge this" in out


def test_convert_reason_quotes_escape_hatch_faithfully(capsys, monkeypatch) -> None:
    """Regression: the deny reason/hook must quote the rule's escape hatch with
    underscores intact (raw_text source), never the markdown-mangled form."""
    rc, d, _ = _json_run(
        capsys, monkeypatch, ["convert", _EV_CONFIG, "--projects-dir", _EV_PROJECTS, "--top", "--json"]
    )
    assert rc == 0
    script = d["hook"]["script"]
    assert "CASTCOMMITAGENT" not in script  # the mangled form must never appear
    assert "CAST_COMMIT_AGENT=1" in script  # faithful hatch (EXCEPTION + caveat)
    assert "CASTCOMMITAGENT" not in d["rule"]["excerpt"]


def test_tool_substitution_escape_hatch_reachable() -> None:
    """A tool_substitution rule's escape hatch is extracted (not never_command-only)."""
    from misfire.cli import _extract_exceptions
    from misfire.classify import (
        CATEGORY_CONVERTIBLE,
        Classification,
        CONVERT_TOOL_SUBSTITUTION,
    )
    from misfire.parse import Rule

    rid = "abc123abc123"
    raw = "Use `rg` not `grep` (exception: `LEGACY_GREP=1 grep`)."
    rule = Rule(rid, "/x", "x", "user", "", 1, 1, raw, "Use rg not grep", True)
    cl = Classification(
        rid,
        CATEGORY_CONVERTIBLE,
        CONVERT_TOOL_SUBSTITUTION,
        {"tool": "Bash", "forbidden": "grep", "prefer": "rg"},
        False,
        "high",
        "t",
    )
    exc = _extract_exceptions([cl], {rid: rule})
    assert exc.get(rid) == "LEGACY_GREP=1"


# ---------------------------------------------------------------------------
# end-to-end: the hook emitted by the CLI actually denies git commit
# ---------------------------------------------------------------------------


def test_convert_emitted_hook_denies_git_commit(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    main(["convert", _EV_CONFIG, "--projects-dir", _EV_PROJECTS, "--top", "--json"])
    d = json.loads(capsys.readouterr().out)
    script = d["hook"]["script"]
    hp = tmp_path / "hook.py"
    hp.write_text(script, encoding="utf-8")

    def run(cmd: str) -> bool:
        proc = subprocess.run(
            [sys.executable, str(hp)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
            capture_output=True,
            text=True,
        )
        return '"permissionDecision": "deny"' in proc.stdout

    assert run("git commit -m wip") is True
    assert run("git status") is False
    assert run('echo "git commit"') is False  # quoted → not blocked
