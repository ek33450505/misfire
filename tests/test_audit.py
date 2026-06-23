"""test_audit.py — Tests for src/misfire/audit.py (Phase 1, Unit 3).

All fixtures are built in ``tmp_path`` — the real ``~/.claude`` is NEVER read.

Coverage:
- stale_path: non-existent absolute path flagged; existing path NOT flagged;
              home-relative path that exists NOT flagged;
              bare-relative token (no base_dir) NOT flagged (key regression);
              URL token NOT flagged;
              placeholder token (<agent>, *) NOT flagged (key regression);
              backtick path INSIDE code fence NOT flagged (key regression);
              PermissionError on stat NOT flagged (key regression);
              tilde-less /.claude duplicate NOT emitted (key regression)
- token_rent: >200-line file flagged; short file NOT flagged;
              aggregate info finding always present; heuristic documented
- conflict:   real convertible-predicate conflict (different prefer values) flagged;
              compatible unrelated rules NOT flagged;
              two "never run X" rules NOT flagged (same-polarity no conflict);
              a rule with both "always gate (never run)" NOT flagged (single rule)
- load_fidelity: broken @import flagged; working import NOT flagged;
                 paths: glob matching nothing flagged when project_dir given;
                 paths: check SKIPPED when no project_dir;
                 paths: glob matching file NOT flagged
"""

from __future__ import annotations

import os
import textwrap
import unittest.mock
from pathlib import Path
from typing import List

import pytest

from misfire.audit import (
    Finding,
    KIND_CONFLICT,
    KIND_LOAD_FIDELITY,
    KIND_STALE_PATH,
    KIND_TOKEN_RENT,
    SEVERITY_INFO,
    SEVERITY_WARN,
    _extract_path_tokens,
    _is_template_token,
    _path_definitively_missing,
    audit_all,
    audit_conflicts,
    audit_load_fidelity,
    audit_stale_paths,
    audit_token_rent,
)
from misfire.parse import parse_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _findings_of_kind(findings: List[Finding], kind: str) -> List[Finding]:
    return [f for f in findings if f.kind == kind]


def _make_config(tmp_path: Path, claude_md_content: str) -> Path:
    """Create a minimal config_root with the given CLAUDE.md content."""
    config_root = tmp_path / "config_root"
    config_root.mkdir(exist_ok=True)
    (config_root / "CLAUDE.md").write_text(claude_md_content, encoding="utf-8")
    return config_root


# ===========================================================================
# Unit tests for _extract_path_tokens
# ===========================================================================


class TestExtractPathTokens:
    def test_absolute_path_extracted(self) -> None:
        tokens = _extract_path_tokens("See /etc/hosts for DNS")
        assert "/etc/hosts" in tokens

    def test_home_path_extracted_with_tilde(self) -> None:
        tokens = _extract_path_tokens("Check ~/.claude/CLAUDE.md first")
        assert any(t.startswith("~/") for t in tokens)
        # Must NOT emit the tilde-less duplicate starting with /.claude
        assert not any(t.startswith("/.claude") for t in tokens)

    def test_tildeless_duplicate_not_emitted(self) -> None:
        """Core regression: ~/.claude/rules/ must NOT also emit /.claude/rules/."""
        tokens = _extract_path_tokens("Config lives at `~/.claude/rules/`")
        tilde_less = [t for t in tokens if t.startswith("/.claude")]
        assert not tilde_less, f"Tilde-less duplicates emitted: {tilde_less}"

    def test_placeholder_token_excluded(self) -> None:
        """Tokens with <, >, *, ?, {, } are template/globs — never returned."""
        tokens = _extract_path_tokens(
            "~/.claude/agent-memory-local/<agent>/ is the path"
        )
        placeholder = [t for t in tokens if "<" in t or ">" in t]
        assert not placeholder, f"Placeholder tokens leaked: {placeholder}"

    def test_glob_star_token_excluded(self) -> None:
        tokens = _extract_path_tokens("Pattern: /some/path/*.md")
        star_tokens = [t for t in tokens if "*" in t]
        assert not star_tokens, f"Glob tokens leaked: {star_tokens}"

    def test_url_not_extracted(self) -> None:
        tokens = _extract_path_tokens("See https://docs.example.com/path/to/page")
        url_tokens = [t for t in tokens if "https" in t]
        assert not url_tokens

    def test_backtick_in_fence_not_extracted(self) -> None:
        """Backtick-wrapped paths inside triple-backtick fences are excluded."""
        text = textwrap.dedent("""\
            Normal rule here.
            ```
            `scripts/fake_path.py` shown as example
            ```
            End of rule.
        """)
        tokens = _extract_path_tokens(text)
        fence_tokens = [t for t in tokens if "fake_path" in t]
        assert not fence_tokens, (
            "Path inside a code fence must not be extracted as a token"
        )

    def test_backtick_outside_fence_extracted(self) -> None:
        tokens = _extract_path_tokens("Use `scripts/real_tool.py` for this")
        assert any("real_tool.py" in t for t in tokens)


# ===========================================================================
# Unit tests for _path_definitively_missing
# ===========================================================================


class TestPathDefinitivelyMissing:
    def test_existing_path_not_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "exists.txt"
        p.write_text("hi")
        assert not _path_definitively_missing(p)

    def test_nonexistent_path_is_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "does_not_exist.txt"
        assert _path_definitively_missing(p)

    def test_permission_error_is_not_missing(self, tmp_path: Path) -> None:
        """PermissionError on os.stat → exists-but-unreadable → NOT missing."""
        p = tmp_path / "protected.txt"
        p.write_text("secret")
        with unittest.mock.patch("misfire.audit.os.stat", side_effect=PermissionError("denied")):
            assert not _path_definitively_missing(p), (
                "PermissionError must not be treated as missing"
            )

    def test_generic_oserror_is_not_missing(self, tmp_path: Path) -> None:
        """Other OSError → unknown → conservative → NOT flagged as missing."""
        p = tmp_path / "some.txt"
        with unittest.mock.patch("misfire.audit.os.stat", side_effect=OSError("unknown")):
            assert not _path_definitively_missing(p)


# ===========================================================================
# 1. audit_stale_paths
# ===========================================================================


class TestStalePaths:
    def test_nonexistent_absolute_path_flagged(self, tmp_path: Path) -> None:
        config_root = _make_config(
            tmp_path,
            "- See `/totally/nonexistent/path/file.py` for details\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        assert stale, "Expected at least one stale_path finding"
        assert any("/totally/nonexistent/path/file.py" in f.detail["token"] for f in stale)

    def test_existing_absolute_path_not_flagged(self, tmp_path: Path) -> None:
        existing = str(tmp_path)
        config_root = _make_config(
            tmp_path,
            f"- Always check `{existing}` first\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        assert not any(existing in f.detail.get("token", "") for f in stale)

    def test_home_path_existing_not_flagged(self, tmp_path: Path) -> None:
        """A ~/... path that exists on disk must NOT be flagged."""
        ref_path = Path.home() / ".claude"
        if not ref_path.exists():
            pytest.skip("~/.claude does not exist in this environment")
        ref = "~/.claude"
        config_root = _make_config(tmp_path, f"- Config lives at `{ref}`\n")
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        assert not any(ref in f.detail.get("token", "") for f in stale), (
            f"Existing home path {ref!r} must not be flagged as stale"
        )

    def test_home_path_no_tildeless_duplicate(self, tmp_path: Path) -> None:
        """~/.claude/rules/ must never produce a /.claude/... tilde-less finding."""
        config_root = _make_config(
            tmp_path, "- Rules live at `~/.claude/rules/`\n"
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        bad = [f for f in stale if f.detail.get("token", "").startswith("/.claude")]
        assert not bad, f"Tilde-less /.claude token emitted: {bad}"

    def test_bare_relative_token_not_flagged_without_base_dir(
        self, tmp_path: Path
    ) -> None:
        """Bare relative tokens (docs/foo.md) are NOT flagged when base_dir is None.

        Key cross-repo false-positive guard: resolving 'docs/chain-handoff.md'
        against the config dir almost always yields a missing path → FP.
        """
        config_root = _make_config(
            tmp_path,
            "- See `docs/chain-handoff.md` for the chain spec\n"
            "- Also see `claude-agent-team/docs/work-projects-reference.md`\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr, base_dir=None)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        relative_finds = [
            f for f in stale
            if not f.detail.get("token", "").startswith("/")
            and not f.detail.get("token", "").startswith("~/")
        ]
        assert not relative_finds, (
            f"Bare-relative tokens flagged without explicit base_dir: {relative_finds}"
        )

    def test_placeholder_token_not_flagged(self, tmp_path: Path) -> None:
        """<agent> placeholder must never be flagged as stale."""
        config_root = _make_config(
            tmp_path,
            "- Agent memory: `~/.claude/agent-memory-local/<agent>/`\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        placeholder_finds = [f for f in stale if "<" in f.detail.get("token", "")]
        assert not placeholder_finds, f"Placeholder token flagged: {placeholder_finds}"

    def test_url_token_not_flagged(self, tmp_path: Path) -> None:
        config_root = _make_config(
            tmp_path,
            "- See https://docs.example.com/nonexistent for reference\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        assert not any("https" in f.detail.get("token", "") for f in stale)

    def test_backtick_in_fence_not_flagged(self, tmp_path: Path) -> None:
        """A backtick path inside a triple-backtick fence must NOT be flagged."""
        content = textwrap.dedent("""\
            Here is an example:
            ```
            `scripts/fake_example.py` shown as sample code
            ```
            End of doc.
        """)
        config_root = _make_config(tmp_path, content)
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        fence_finds = [f for f in stale if "fake_example" in f.detail.get("token", "")]
        assert not fence_finds, (
            "Path inside a code fence must not produce a stale_path finding"
        )

    def test_permission_error_not_flagged(self, tmp_path: Path) -> None:
        """When os.stat raises PermissionError, the path must NOT be flagged stale."""
        config_root = _make_config(
            tmp_path,
            "- DB lives at `/protected/cast.db`\n",
        )
        pr = parse_config(config_root)
        with unittest.mock.patch(
            "misfire.audit.os.stat", side_effect=PermissionError("sandbox deny")
        ):
            findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        assert not any("cast.db" in f.detail.get("token", "") for f in stale), (
            "PermissionError must not produce a stale_path finding"
        )

    def test_bare_relative_skipped_by_default(self, tmp_path: Path) -> None:
        """Bare-relative backtick tokens are skipped when base_dir is None."""
        config_root = _make_config(
            tmp_path,
            "- Use `scripts/cast_db.py` for database access\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        relative_finds = [f for f in stale if "cast_db.py" in f.detail.get("token", "")]
        assert not relative_finds, (
            "Bare-relative backtick tokens must be skipped when base_dir is None"
        )

    def test_relative_existing_not_flagged_with_explicit_base_dir(
        self, tmp_path: Path
    ) -> None:
        """With explicit base_dir, a relative path that EXISTS is NOT flagged."""
        config_root = _make_config(
            tmp_path, "- Use `existing_script.py` for this task\n"
        )
        (config_root / "existing_script.py").write_text("# exists\n")
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr, base_dir=config_root)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        assert not any(
            "existing_script.py" in f.detail.get("token", "") for f in stale
        )

    def test_relative_nonexistent_flagged_with_explicit_base_dir(
        self, tmp_path: Path
    ) -> None:
        """With explicit base_dir, a relative token that doesn't exist IS flagged."""
        config_root = _make_config(
            tmp_path, "- Run `missing_script.py` first\n"
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr, base_dir=config_root)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        assert any("missing_script.py" in f.detail.get("token", "") for f in stale)


# ===========================================================================
# 2. audit_token_rent
# ===========================================================================


class TestTokenRent:
    def _big_claude_md(self) -> str:
        lines = ["# Rules\n"]
        for i in range(205):
            lines.append(f"- Rule number {i}: always do the right thing\n")
        return "".join(lines)

    def test_large_file_flagged(self, tmp_path: Path) -> None:
        config_root = _make_config(tmp_path, self._big_claude_md())
        pr = parse_config(config_root)
        findings = audit_token_rent(pr)
        warns = [f for f in findings if f.severity == SEVERITY_WARN and f.kind == KIND_TOKEN_RENT]
        assert warns, "A >200-line file must be flagged as token_rent warn"
        assert any(f.detail["line_count"] > 200 for f in warns)

    def test_short_file_not_flagged_as_warn(self, tmp_path: Path) -> None:
        config_root = _make_config(tmp_path, "- Never do bad things\n- Always be good\n")
        pr = parse_config(config_root)
        findings = audit_token_rent(pr)
        warns = [f for f in findings if f.severity == SEVERITY_WARN and f.kind == KIND_TOKEN_RENT]
        assert not warns, "A short file must NOT produce a token_rent warn"

    def test_aggregate_info_always_present(self, tmp_path: Path) -> None:
        config_root = _make_config(tmp_path, "- A single rule\n")
        pr = parse_config(config_root)
        findings = audit_token_rent(pr)
        infos = [f for f in findings if f.severity == SEVERITY_INFO and f.kind == KIND_TOKEN_RENT]
        assert infos, "Aggregate info finding must always be emitted"
        agg = infos[0]
        assert agg.detail["source_file_count"] >= 1
        assert agg.detail["total_lines"] >= 1

    def test_token_count_heuristic_documented(self, tmp_path: Path) -> None:
        config_root = _make_config(tmp_path, "- Never skip tests\n")
        pr = parse_config(config_root)
        findings = audit_token_rent(pr)
        infos = [f for f in findings if f.severity == SEVERITY_INFO and f.kind == KIND_TOKEN_RENT]
        assert infos[0].detail["token_heuristic"] == "chars/4"


# ===========================================================================
# 3. audit_conflicts
# ===========================================================================


class TestConflicts:
    def test_tool_substitution_conflict_flagged(self, tmp_path: Path) -> None:
        """Two rules 'use `X` not `grep`' and 'use `Y` not `grep`' conflict on prefer.

        Backtick wrapping is required for tool_substitution classification —
        matching the real-world convention for CLI tool swap rules.
        """
        content = textwrap.dedent("""\
            - Use `rg` not `grep` for searching
            - Use `ag` not `grep` when possible
        """)
        config_root = _make_config(tmp_path, content)
        pr = parse_config(config_root)
        findings = audit_conflicts(pr)
        conflicts = _findings_of_kind(findings, KIND_CONFLICT)
        assert conflicts, (
            "Two rules with same forbidden tool but different prefer should be flagged"
        )

    def test_compatible_rules_not_flagged(self, tmp_path: Path) -> None:
        """Unrelated rules produce no conflict."""
        content = textwrap.dedent("""\
            - Never run git commit directly
            - Always prefer the existing pattern
        """)
        config_root = _make_config(tmp_path, content)
        pr = parse_config(config_root)
        findings = audit_conflicts(pr)
        conflicts = _findings_of_kind(findings, KIND_CONFLICT)
        assert not conflicts, "Unrelated rules must NOT produce a conflict finding"

    def test_two_never_run_different_targets_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Two 'never run X' rules on different targets do NOT conflict."""
        content = textwrap.dedent("""\
            - Never run git commit directly
            - Never run npm install in CI
        """)
        config_root = _make_config(tmp_path, content)
        pr = parse_config(config_root)
        findings = audit_conflicts(pr)
        conflicts = _findings_of_kind(findings, KIND_CONFLICT)
        assert not conflicts, "Two unrelated never-run rules must NOT produce a conflict"

    def test_always_gate_never_run_single_rule_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Single rule containing both 'always' and 'never' in different clauses.

        Real false-positive: 'Irreversible ops that always gate (never run ad hoc)'
        The old text-based never-vs-always heuristic flagged this because 'never'
        and 'always'/'must' appeared in the same normalised text, extracting 'run'
        as both the never-object and the always-object.  Structural-only approach
        does not fire on a single rule.
        """
        content = textwrap.dedent("""\
            - Irreversible ops that always gate (never run ad hoc): push, force-merge
        """)
        config_root = _make_config(tmp_path, content)
        pr = parse_config(config_root)
        findings = audit_conflicts(pr)
        conflicts = _findings_of_kind(findings, KIND_CONFLICT)
        assert not conflicts, (
            "A single rule with 'always gate (never run ad hoc)' must NOT be flagged"
        )

    def test_two_never_same_object_not_flagged(self, tmp_path: Path) -> None:
        """Two 'never X' rules on the same object are consistent, NOT conflicting."""
        content = "- Never use grep for searching\n- Never use grep in CI\n"
        config_root = _make_config(tmp_path, content)
        pr = parse_config(config_root)
        findings = audit_conflicts(pr)
        conflicts = _findings_of_kind(findings, KIND_CONFLICT)
        assert not conflicts, "Two 'never X' rules must NOT be flagged as a conflict"


# ===========================================================================
# 4. audit_load_fidelity
# ===========================================================================


class TestLoadFidelity:
    def _setup_importer(self, tmp_path: Path, import_target: str) -> Path:
        config_root = tmp_path / "config_root"
        config_root.mkdir(exist_ok=True)
        (config_root / "CLAUDE.md").write_text(
            f"@{import_target}\n\n- A rule\n", encoding="utf-8"
        )
        return config_root

    def test_broken_import_flagged(self, tmp_path: Path) -> None:
        config_root = self._setup_importer(tmp_path, "nonexistent_import.md")
        pr = parse_config(config_root)
        findings = audit_load_fidelity(pr)
        lf = _findings_of_kind(findings, KIND_LOAD_FIDELITY)
        assert lf, "A broken @import must produce a load_fidelity finding"
        assert any("nonexistent_import.md" in f.message for f in lf)

    def test_working_import_not_flagged(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config_root"
        config_root.mkdir(exist_ok=True)
        (config_root / "extra_rules.md").write_text(
            "- Always verify before shipping\n", encoding="utf-8"
        )
        (config_root / "CLAUDE.md").write_text(
            "@extra_rules.md\n\n- A main rule\n", encoding="utf-8"
        )
        pr = parse_config(config_root)
        findings = audit_load_fidelity(pr)
        lf = _findings_of_kind(findings, KIND_LOAD_FIDELITY)
        assert not any("extra_rules.md" in f.message for f in lf)

    def test_paths_glob_matching_nothing_flagged_with_project_dir(
        self, tmp_path: Path
    ) -> None:
        config_root = tmp_path / "config_root"
        config_root.mkdir()
        rules_dir = config_root / "rules"
        rules_dir.mkdir()
        (rules_dir / "rust_rules.md").write_text(
            "---\npaths:\n  - '**/*.rs'\n---\n- Never use unsafe without justification\n",
            encoding="utf-8",
        )
        (config_root / "CLAUDE.md").write_text("- A global rule\n", encoding="utf-8")

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("print('hello')\n")

        pr = parse_config(config_root, project_dir=project_dir)
        findings = audit_load_fidelity(pr, project_dir=project_dir)
        lf = _findings_of_kind(findings, KIND_LOAD_FIDELITY)
        assert lf, "A paths:-scoped file matching no project files must be flagged"
        assert any("rust_rules.md" in f.message for f in lf)

    def test_paths_glob_check_skipped_without_project_dir(
        self, tmp_path: Path
    ) -> None:
        config_root = tmp_path / "config_root"
        config_root.mkdir()
        rules_dir = config_root / "rules"
        rules_dir.mkdir()
        (rules_dir / "rust_rules.md").write_text(
            "---\npaths:\n  - '**/*.rs'\n---\n- Never use unsafe\n",
            encoding="utf-8",
        )
        (config_root / "CLAUDE.md").write_text("- Global rule\n", encoding="utf-8")

        pr = parse_config(config_root)
        findings = audit_load_fidelity(pr)
        lf = _findings_of_kind(findings, KIND_LOAD_FIDELITY)
        glob_findings = [f for f in lf if "rust_rules.md" in f.message]
        assert not glob_findings, (
            "paths: glob check must be skipped when project_dir is not provided"
        )

    def test_paths_glob_matching_file_not_flagged(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config_root"
        config_root.mkdir()
        rules_dir = config_root / "rules"
        rules_dir.mkdir()
        (rules_dir / "py_rules.md").write_text(
            "---\npaths:\n  - '**/*.py'\n---\n- Never import star\n",
            encoding="utf-8",
        )
        (config_root / "CLAUDE.md").write_text("- Global rule\n", encoding="utf-8")

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("print('hello')\n")

        pr = parse_config(config_root, project_dir=project_dir)
        findings = audit_load_fidelity(pr, project_dir=project_dir)
        lf = _findings_of_kind(findings, KIND_LOAD_FIDELITY)
        assert not any("py_rules.md" in f.message for f in lf)


# ===========================================================================
# 5. audit_all
# ===========================================================================


class TestAuditAll:
    def test_returns_stale_and_token_rent_kinds(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config_root"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text(
            "- See `/definitely/does/not/exist.py` for info\n- Never skip tests\n",
            encoding="utf-8",
        )
        pr = parse_config(config_root)
        findings = audit_all(pr)
        kinds = {f.kind for f in findings}
        assert KIND_STALE_PATH in kinds
        assert KIND_TOKEN_RENT in kinds

    def test_empty_config_does_not_crash(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config_root"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text("", encoding="utf-8")
        pr = parse_config(config_root)
        findings = audit_all(pr)
        assert isinstance(findings, list)

    def test_finding_source_rel_never_leaks_username(self, tmp_path: Path) -> None:
        """source_rel must never contain /Users/<name>/."""
        config_root = tmp_path / "config_root"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text(
            "- See `/nonexistent/path.sh` for more\n", encoding="utf-8"
        )
        pr = parse_config(config_root)
        findings = audit_all(pr)
        home_bare = str(Path.home()).lstrip("/")
        for f in findings:
            assert home_bare not in f.source_rel or f.source_rel.startswith("~/"), (
                f"source_rel leaks home path: {f.source_rel!r}"
            )


# ===========================================================================
# Unit tests for _is_template_token (FIX A helper)
# ===========================================================================


class TestIsTemplateToken:
    """_is_template_token must fire on clear date-template patterns only."""

    def test_yyyy_true(self) -> None:
        """Four consecutive Y (case-insensitive) → True."""
        assert _is_template_token("YYYY-MM-DD")
        assert _is_template_token("yyyy-mm-dd")  # lowercase
        assert _is_template_token("~/Documents/Claude/YYYY-MM/YYYY-MM-DD.md")

    def test_strftime_code_true(self) -> None:
        """strftime percent-codes → True."""
        assert _is_template_token("%Y/%m")
        assert _is_template_token("~/logs/%Y-%m-%d.log")
        assert _is_template_token("%H:%M:%S")

    def test_mm_dd_pattern_true(self) -> None:
        """Adjacent MM-DD / DD/MM template parts → True."""
        assert _is_template_token("MM-DD")
        assert _is_template_token("DD/MM")
        assert _is_template_token("MM_DD")
        assert _is_template_token("DD-MM")

    def test_normal_path_false(self) -> None:
        """Normal path tokens must NOT be mistaken for date templates."""
        assert not _is_template_token("scripts/deploy.sh")
        assert not _is_template_token("MEMORY.md")
        assert not _is_template_token("cast-cost-optimization.md")
        assert not _is_template_token("~/.claude/rules/")
        assert not _is_template_token("/etc/hosts")
        # "MM" or "DD" in isolation — must require adjacency with the counterpart
        assert not _is_template_token("summary.md")
        assert not _is_template_token("README.md")


# ===========================================================================
# Integration tests for FIX A + FIX B (false-positive reduction)
# ===========================================================================


class TestFalsePositiveFixes:
    """audit_stale_paths must not emit findings for the two suppressed FP classes."""

    # ------------------------------------------------------------------
    # FIX A — date-template tokens (_is_template_token / _DATE_TEMPLATE_RE)
    # ------------------------------------------------------------------

    def test_date_template_yyyy_not_flagged(self, tmp_path: Path) -> None:
        """YYYY-MM/YYYY-MM-DD.md is a filename PATTERN — must NOT be flagged."""
        config_root = _make_config(
            tmp_path,
            "- Journal file: `~/Documents/Claude/YYYY-MM/YYYY-MM-DD.md`\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        date_template_finds = [
            f for f in stale if "YYYY" in f.detail.get("token", "")
        ]
        assert not date_template_finds, (
            "Date-template token YYYY-MM-DD must not produce a stale_path finding"
        )

    def test_strftime_token_not_flagged(self, tmp_path: Path) -> None:
        """Backtick-wrapped strftime path must NOT be flagged as stale."""
        config_root = _make_config(
            tmp_path,
            "- Store logs at `~/logs/%Y-%m-%d.log` for rotation\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        strftime_finds = [f for f in stale if "%Y" in f.detail.get("token", "")]
        assert not strftime_finds, (
            "Strftime token %Y-%m-%d must not produce a stale_path finding"
        )

    # ------------------------------------------------------------------
    # FIX B — placeholder fragments (_ABS_PATH_RE lookbehind excludes > and })
    # ------------------------------------------------------------------

    def test_placeholder_fragment_pending_not_flagged(self, tmp_path: Path) -> None:
        """The /memory/_pending fragment from a placeholder path is NOT flagged.

        Raw text: write to `~/.claude/projects/<id>/memory/_pending/<file>.md`

        Without the fix, _ABS_PATH_RE would extract /memory/_pending from the
        ">/" boundary (the ``>`` that closes ``<id>`` preceded the ``/``).
        The fix adds ``>`` and ``}`` to the lookbehind so a /token immediately
        following a placeholder-close char is not extracted as an absolute path.
        """
        config_root = _make_config(
            tmp_path,
            "write to `~/.claude/projects/<id>/memory/_pending/<file>.md` instead\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        pending_finds = [
            f for f in stale if "_pending" in f.detail.get("token", "")
        ]
        assert not pending_finds, (
            "Placeholder-fragment /memory/_pending must NOT produce a stale_path finding; "
            f"got: {pending_finds}"
        )

    # ------------------------------------------------------------------
    # REGRESSION GUARD — ensure suppression is surgical (no false negatives)
    # ------------------------------------------------------------------

    def test_missing_home_path_still_flagged(self, tmp_path: Path) -> None:
        """A home-relative path to a clearly missing target is still flagged."""
        nonexistent_home = "~/misfire_nonexistent_home_dir_xyz_12345678"
        config_root = _make_config(
            tmp_path,
            f"- Data lives at `{nonexistent_home}`\n",
        )
        pr = parse_config(config_root)
        findings = audit_stale_paths(pr)
        stale = _findings_of_kind(findings, KIND_STALE_PATH)
        assert any(
            "misfire_nonexistent_home_dir_xyz" in f.detail.get("token", "")
            for f in stale
        ), (
            f"Missing home-relative path {nonexistent_home!r} must still be flagged"
        )
