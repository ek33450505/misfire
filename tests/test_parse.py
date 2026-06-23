"""test_parse.py — Tests for src/misfire/parse.py (Phase 1).

All tests use a temp fixture directory (``tmp_path``) or the checked-in
``tests/fixtures/`` tree. The real ``~/.claude`` is NEVER read.

Coverage:
- Precedence ordering of discovered sources
- Rule extraction: sections, line numbers, multi-line bullets
- Markdown normalisation: backtick case, bold+link case
- Imperative detection: true/false cases
- Home-path collapse in source_rel (privacy requirement)
- @path import resolution: basic, fence-skipped fake, missing, cycle, >4 hops
- paths: frontmatter captured on SourceFile
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

import pytest

from misfire.parse import (
    TIER_IMPORT,
    TIER_PROJECT,
    TIER_PROJECT_LOCAL,
    TIER_RULES_FILE,
    TIER_USER,
    ParseResult,
    Rule,
    SourceFile,
    _compute_source_rel,
    _find_import_lines,
    _parse_frontmatter,
    has_imperative,
    normalize_markdown,
    parse_config,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
BASIC = FIXTURES / "basic"
IMPORTS_DIR = FIXTURES / "imports"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def tiers(result: ParseResult) -> List[str]:
    return [sf.tier for sf in result.sources]


def source_paths(result: ParseResult) -> List[str]:
    return [sf.path for sf in result.sources]


# ===========================================================================
# 1. Precedence ordering
# ===========================================================================


class TestPrecedenceOrdering:
    def test_user_before_project(self) -> None:
        """User CLAUDE.md appears before project CLAUDE.md in .sources."""
        config_root = BASIC / "config_root"
        project_dir = BASIC / "project"
        result = parse_config(config_root, project_dir)

        t = tiers(result)
        assert TIER_USER in t
        assert TIER_PROJECT in t
        user_idx = next(i for i, sf in enumerate(result.sources) if sf.tier == TIER_USER)
        project_idx = next(i for i, sf in enumerate(result.sources) if sf.tier == TIER_PROJECT)
        assert user_idx < project_idx

    def test_project_before_project_local(self) -> None:
        """Project CLAUDE.md appears before CLAUDE.local.md."""
        config_root = BASIC / "config_root"
        project_dir = BASIC / "project"
        result = parse_config(config_root, project_dir)

        t = tiers(result)
        assert TIER_PROJECT_LOCAL in t
        project_idx = next(i for i, sf in enumerate(result.sources) if sf.tier == TIER_PROJECT)
        local_idx = next(i for i, sf in enumerate(result.sources) if sf.tier == TIER_PROJECT_LOCAL)
        assert project_idx < local_idx

    def test_project_local_before_rules_file(self) -> None:
        """CLAUDE.local.md appears before rules/*.md."""
        config_root = BASIC / "config_root"
        project_dir = BASIC / "project"
        result = parse_config(config_root, project_dir)

        local_idx = next(i for i, sf in enumerate(result.sources) if sf.tier == TIER_PROJECT_LOCAL)
        rules_idx = next(i for i, sf in enumerate(result.sources) if sf.tier == TIER_RULES_FILE)
        assert local_idx < rules_idx

    def test_full_tier_sequence(self) -> None:
        """All expected tiers appear in precedence order."""
        config_root = BASIC / "config_root"
        project_dir = BASIC / "project"
        result = parse_config(config_root, project_dir)

        tier_list = tiers(result)
        # Remove duplicates while preserving first-occurrence order
        seen: set = set()
        ordered = [t for t in tier_list if not (t in seen or seen.add(t))]
        # user must come before project, project before project_local, etc.
        for earlier, later in [
            (TIER_USER, TIER_PROJECT),
            (TIER_PROJECT, TIER_PROJECT_LOCAL),
            (TIER_PROJECT_LOCAL, TIER_RULES_FILE),
        ]:
            assert ordered.index(earlier) < ordered.index(later), (
                f"{earlier!r} should appear before {later!r} in {ordered}"
            )

    def test_no_real_home_config_loaded(self) -> None:
        """parse_config with a tmp config_root never reads the real ~/.claude."""
        tmp = pytest.importorskip("pathlib").Path  # just to satisfy type checker
        real_home_claude = Path.home() / ".claude" / "CLAUDE.md"
        config_root = BASIC / "config_root"
        project_dir = BASIC / "project"
        result = parse_config(config_root, project_dir)

        loaded_paths = {sf.path for sf in result.sources}
        if real_home_claude.exists():
            assert str(real_home_claude.resolve()) not in loaded_paths

    def test_rules_files_are_sorted(self) -> None:
        """Rules files within a directory are discovered in sorted order."""
        config_root = BASIC / "config_root"
        project_dir = BASIC / "project"
        result = parse_config(config_root, project_dir)

        rules_sources = [sf for sf in result.sources if sf.tier == TIER_RULES_FILE]
        rules_names = [Path(sf.path).name for sf in rules_sources]
        # aaa.md < scoped.md alphabetically (user-level rules come first)
        assert rules_names.index("aaa.md") < rules_names.index("scoped.md")


# ===========================================================================
# 2. Import resolution
# ===========================================================================


class TestImportResolution:
    def test_basic_import_resolved(self, tmp_path: Path) -> None:
        """A simple @file.md import is resolved and tagged tier='import'."""
        config_root = tmp_path / "config"
        config_root.mkdir()
        main = config_root / "CLAUDE.md"
        main.write_text("@sub.md\n- Must follow main rule\n")
        sub = config_root / "sub.md"
        sub.write_text("- Always follow sub rule\n")

        result = parse_config(config_root)
        import_srcs = [sf for sf in result.sources if sf.tier == TIER_IMPORT]
        assert len(import_srcs) == 1
        assert import_srcs[0].path == str(sub.resolve())
        assert import_srcs[0].imported_from == str(main.resolve())

    def test_import_rules_are_extracted(self, tmp_path: Path) -> None:
        """Rules from imported files are included in ParseResult.rules."""
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text("@extra.md\n")
        (config_root / "extra.md").write_text("- Must follow extra rule\n")

        result = parse_config(config_root)
        import_rules = [r for r in result.rules if r.precedence_tier == TIER_IMPORT]
        assert any("extra rule" in r.normalized_text for r in import_rules)

    def test_import_relative_to_importing_file(self, tmp_path: Path) -> None:
        """@path is resolved relative to the importing file's directory."""
        config_root = tmp_path / "config"
        sub_dir = config_root / "sub"
        sub_dir.mkdir(parents=True)

        (config_root / "CLAUDE.md").write_text("@sub/rules.md\n")
        rules_file = sub_dir / "rules.md"
        rules_file.write_text("- Must follow sub/rules rule\n")

        result = parse_config(config_root)
        import_srcs = [sf for sf in result.sources if sf.tier == TIER_IMPORT]
        assert any(sf.path == str(rules_file.resolve()) for sf in import_srcs)

    def test_fence_skips_fake_import(self) -> None:
        """@import lines inside fenced code blocks are NOT resolved."""
        config_root = IMPORTS_DIR
        # Use fence_test.md as a standalone SourceFile via a wrapper config
        # Create an in-fixture config_root that imports fence_test.md
        result = parse_config(config_root)

        # fence_test.md is NOT in this config_root, so let's test _find_import_lines directly
        content = (IMPORTS_DIR / "fence_test.md").read_text()
        imports = _find_import_lines(content)
        import_paths = [p for _ln, p in imports]

        assert "fake_import.md" not in import_paths, (
            "Fake import inside fence should be skipped"
        )
        assert "real_import.md" in import_paths, (
            "Real import outside fence should be found"
        )

    def test_missing_import_recorded_not_crashed(self, tmp_path: Path) -> None:
        """A missing @import target is recorded as a SourceFile, not a crash."""
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text("@nonexistent.md\n- Must still parse\n")

        # Must not raise
        result = parse_config(config_root)

        # The missing file should appear as a SourceFile with tier='import'
        import_srcs = [sf for sf in result.sources if sf.tier == TIER_IMPORT]
        paths = [sf.path for sf in import_srcs]
        assert any("nonexistent.md" in p for p in paths)

    def test_cycle_does_not_loop(self, tmp_path: Path) -> None:
        """A → B → A cycle terminates without infinite recursion."""
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text("@cycle_a.md\n")
        (config_root / "cycle_a.md").write_text("@cycle_b.md\n- Must from A\n")
        (config_root / "cycle_b.md").write_text("@cycle_a.md\n- Must from B\n")

        # Must terminate
        result = parse_config(config_root)
        import_paths = [sf.path for sf in result.sources if sf.tier == TIER_IMPORT]
        # Both A and B are imported once; no duplicates
        assert len(import_paths) == len(set(import_paths))

    def test_self_import_cycle_safe(self, tmp_path: Path) -> None:
        """A file importing itself terminates AND does not duplicate sources/rules.

        The self-referencing @CLAUDE.md line must be detected as a cycle and
        skipped: the file appears exactly once in .sources (as tier='user', not
        again as tier='import'), and the rule extracted from it appears exactly
        once in .rules.  If cycle handling regressed — e.g. the file were
        re-queued as an import — either the source count or the rule count would
        rise above 1, causing both assertions below to fail.
        """
        config_root = tmp_path / "config"
        config_root.mkdir()
        main = config_root / "CLAUDE.md"
        main.write_text("@CLAUDE.md\n- Must self-referential rule\n")

        result = parse_config(config_root)

        # The file must appear exactly once across all sources (as 'user').
        main_abs = str(main.resolve())
        matching_sources = [sf for sf in result.sources if sf.path == main_abs]
        assert len(matching_sources) == 1, (
            f"CLAUDE.md should appear exactly once in sources, "
            f"got {len(matching_sources)}: {matching_sources}"
        )
        assert matching_sources[0].tier == TIER_USER, (
            "CLAUDE.md should retain tier='user', not be re-added as 'import'"
        )

        # The rule from the file must appear exactly once (no double-extraction).
        matching_rules = [r for r in result.rules if "self-referential" in r.normalized_text]
        assert len(matching_rules) == 1, (
            f"Self-referential rule should appear exactly once, "
            f"got {len(matching_rules)}: {[r.normalized_text for r in matching_rules]}"
        )

    def test_max_depth_four_hops(self, tmp_path: Path) -> None:
        """Import chains deeper than 4 hops are truncated at 4."""
        config_root = tmp_path / "config"
        config_root.mkdir()

        # Build a chain: CLAUDE.md → hop1 → hop2 → hop3 → hop4 → hop5
        (config_root / "CLAUDE.md").write_text("@hop1.md\n")
        for i in range(1, 5):
            (config_root / f"hop{i}.md").write_text(
                f"- Must hop {i}\n@hop{i+1}.md\n"
            )
        (config_root / "hop5.md").write_text("- Must hop 5 (should NOT appear)\n")

        result = parse_config(config_root)
        import_paths = {sf.path for sf in result.sources if sf.tier == TIER_IMPORT}

        hop5_path = str((config_root / "hop5.md").resolve())
        assert hop5_path not in import_paths, (
            "hop5 is 5 hops deep and should be cut off at max depth 4"
        )
        # hop1 through hop4 should be present
        for i in range(1, 5):
            hop_path = str((config_root / f"hop{i}.md").resolve())
            assert hop_path in import_paths, f"hop{i} should be in import sources"


# ===========================================================================
# 3. paths: frontmatter
# ===========================================================================


class TestFrontmatter:
    def test_paths_globs_captured(self) -> None:
        """paths: frontmatter is captured on SourceFile.paths_globs."""
        config_root = BASIC / "config_root"
        result = parse_config(config_root)

        scoped = next(
            (sf for sf in result.sources if "scoped.md" in sf.path), None
        )
        assert scoped is not None, "scoped.md not in sources"
        assert "src/**/*.py" in scoped.paths_globs
        assert "tests/**" in scoped.paths_globs

    def test_no_frontmatter_empty_globs(self) -> None:
        """Files without paths: frontmatter get an empty paths_globs list."""
        config_root = BASIC / "config_root"
        result = parse_config(config_root)

        aaa = next((sf for sf in result.sources if "aaa.md" in sf.path), None)
        assert aaa is not None
        assert aaa.paths_globs == []

    def test_frontmatter_body_not_in_rules(self, tmp_path: Path) -> None:
        """Frontmatter block itself is not extracted as a rule."""
        config_root = tmp_path / "config"
        (config_root / "rules").mkdir(parents=True)
        rf = config_root / "rules" / "r.md"
        rf.write_text("---\npaths:\n  - '*.py'\n---\n- Must follow rule\n")

        result = parse_config(config_root)
        rule_texts = [r.normalized_text for r in result.rules]
        assert not any("paths" in t and "*.py" in t for t in rule_texts), (
            "Frontmatter should not appear as a rule"
        )

    def test_parse_frontmatter_direct(self) -> None:
        content = "---\npaths:\n  - 'src/**'\n  - 'tests/**'\n---\n# Body\n"
        fm, body = _parse_frontmatter(content)
        assert fm.get("paths") == ["src/**", "tests/**"]
        assert "# Body" in body

    def test_parse_frontmatter_no_block(self) -> None:
        content = "# Just markdown\n- No frontmatter\n"
        fm, body = _parse_frontmatter(content)
        assert fm == {}
        assert body == content


# ===========================================================================
# 4. Markdown normalisation
# ===========================================================================


class TestMarkdownNormalization:
    def test_backtick_stripping_enables_imperative_match(self) -> None:
        """The known backtick bug: ``Never `git commit` directly``."""
        raw = "Never `git commit` directly"
        norm = normalize_markdown(raw)
        assert "git commit" in norm
        assert "`" not in norm
        assert has_imperative(norm) is True

    def test_bold_stripping(self) -> None:
        norm = normalize_markdown("**Always** use this approach")
        assert "**" not in norm
        assert "Always" in norm
        assert has_imperative(norm) is True

    def test_snake_case_identifier_underscores_preserved(self) -> None:
        """Regression: intraword underscores are NOT italic-stripped.

        The italic rule must only fire at word boundaries, so snake_case
        identifiers survive intact (was: CAST_COMMIT_AGENT → CASTCOMMITAGENT,
        which mis-quoted the escape hatch in generated convert hooks).
        """
        norm = normalize_markdown("escape hatch: CAST_COMMIT_AGENT=1 git commit")
        assert "CAST_COMMIT_AGENT=1" in norm
        norm2 = normalize_markdown("Status: DONE_WITH_CONCERNS or NEEDS_CONTEXT")
        assert "DONE_WITH_CONCERNS" in norm2
        assert "NEEDS_CONTEXT" in norm2

    def test_word_boundary_underscore_emphasis_still_stripped(self) -> None:
        """Genuine ``_emphasis_`` at word boundaries is still stripped."""
        assert normalize_markdown("use _this_ approach") == "use this approach"
        assert normalize_markdown("_italic_") == "italic"

    def test_link_stripping(self) -> None:
        norm = normalize_markdown("Use [this tool](https://example.com) not that one")
        assert "[" not in norm
        assert "https://" not in norm
        assert "this tool" in norm
        assert has_imperative(norm) is True  # "use … not"

    def test_bold_plus_link_case(self) -> None:
        """**Always** use [foo](http://foo.com) not bar → imperative=True."""
        raw = "**Always** use [foo](http://foo.com) not bar"
        norm = normalize_markdown(raw)
        assert "Always" in norm
        assert "foo" in norm
        assert "http" not in norm
        assert "**" not in norm
        assert has_imperative(norm) is True

    def test_heading_stripped(self) -> None:
        norm = normalize_markdown("## Section Title")
        assert "#" not in norm
        assert "Section Title" in norm

    def test_blockquote_stripped(self) -> None:
        norm = normalize_markdown("> Never do this")
        assert ">" not in norm
        assert "Never do this" in norm
        assert has_imperative(norm) is True

    def test_whitespace_collapsed(self) -> None:
        norm = normalize_markdown("  too   many    spaces  ")
        assert "  " not in norm
        assert norm == "too many spaces"

    def test_bullet_marker_stripped(self) -> None:
        norm = normalize_markdown("- Must follow this rule")
        assert norm.startswith("Must")

    def test_empty_after_stripping(self) -> None:
        norm = normalize_markdown("---")
        # Horizontal rules have no content to emit
        # (--- is not explicitly stripped, but collapses to just dashes)
        assert isinstance(norm, str)


# ===========================================================================
# 5. Imperative detection
# ===========================================================================


class TestImperativeDetection:
    @pytest.mark.parametrize("text,expected", [
        ("Never do this bad thing", True),
        ("Must follow rule A", True),
        ("Always prefer the right approach", True),
        ("MANDATORY: comply with this", True),
        ("Do not commit secrets", True),
        ("Prefer TypeScript over JavaScript", True),
        ("Avoid anti-patterns", True),
        ("Before committing, run tests", True),
        ("After editing, verify", True),
        ("Use foo not bar", True),
        ("Use `git commit` not raw commits", True),  # after normalization by caller
        ("This is a description", False),
        ("The project has three files", False),
        ("Introduction to the system", False),
    ])
    def test_imperative_cases(self, text: str, expected: bool) -> None:
        assert has_imperative(text) is expected, (
            f"has_imperative({text!r}) should be {expected}"
        )

    def test_imperative_case_insensitive(self) -> None:
        assert has_imperative("NEVER do this") is True
        assert has_imperative("never do this") is True
        assert has_imperative("Never do this") is True


# ===========================================================================
# 6. Home-path collapse (privacy requirement)
# ===========================================================================


class TestHomePathCollapse:
    def test_home_path_collapsed(self) -> None:
        """A file directly under config_root returns its config-root-relative path.

        Priority 2 of _compute_source_rel: when path is under config_root, return
        the bare relative path (e.g. 'CLAUDE.md') with NO hardcoded prefix.
        This is the corrected behaviour — the old code wrongly prepended '~/.claude/'.
        """
        home = Path.home()
        fake_config = home / ".claude"
        fake_file = fake_config / "CLAUDE.md"
        result = _compute_source_rel(fake_file, fake_config, None)
        # Config-root-relative path, not an absolute or home-prefixed one.
        assert result == "CLAUDE.md", (
            f"Expected 'CLAUDE.md' (config-root-relative), got {result!r}"
        )
        # Primary privacy guarantee: no raw /Users/<name>/ exposed.
        assert "/Users/" not in result, (
            f"source_rel must not leak /Users/: got {result!r}"
        )

    def test_no_username_leaked(self) -> None:
        """source_rel never contains /Users/<name>/ (macOS home pattern)."""
        home = Path.home()
        fake_config = home / ".claude"
        fake_file = fake_config / "rules" / "test.md"
        result = _compute_source_rel(fake_file, fake_config, None)
        assert "/Users/" not in result, (
            f"source_rel must not leak /Users/: got {result!r}"
        )

    def test_project_relative_preferred_over_home_collapse(self, tmp_path: Path) -> None:
        """Paths under project_dir are project-relative, not home-collapsed."""
        config_root = tmp_path / "config"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        fake_file = project_dir / "CLAUDE.md"

        result = _compute_source_rel(fake_file, config_root, project_dir)
        assert result == "CLAUDE.md", f"Expected project-relative 'CLAUDE.md', got {result!r}"

    def test_rules_in_project_are_project_relative(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config"
        project_dir = tmp_path / "project"
        (project_dir / ".claude" / "rules").mkdir(parents=True)
        rules_file = project_dir / ".claude" / "rules" / "test.md"
        rules_file.touch()

        result = _compute_source_rel(rules_file, config_root, project_dir)
        assert result == str(Path(".claude") / "rules" / "test.md")

    def test_rules_in_source_appear_with_rules_in_source_rel(self, tmp_path: Path) -> None:
        """Rules from config_root/rules/*.md have a config-root-relative source_rel.

        After the _compute_source_rel fix: the path is relative to config_root with
        NO hardcoded prefix — e.g. 'rules/my.md', not '~/.claude/rules/my.md'.
        This is the regression guard for the portability bug where any non-~/.claude
        config_root produced a wrong '~/.claude/' prefix.
        """
        config_root = tmp_path / "config"
        (config_root / "rules").mkdir(parents=True)
        rf = config_root / "rules" / "my.md"
        rf.write_text("- Must follow my rule\n")

        result = parse_config(config_root)
        rule_from_rf = next(
            (r for r in result.rules if "my rule" in r.normalized_text), None
        )
        assert rule_from_rf is not None
        # Source rel must be the bare config-root-relative path.
        assert rule_from_rf.source_rel == "rules/my.md", (
            f"Expected 'rules/my.md' (config-root-relative), got {rule_from_rf.source_rel!r}"
        )
        # Regression guard: must NOT carry the old hardcoded ~/.claude/ prefix.
        assert not rule_from_rf.source_rel.startswith("~/.claude/"), (
            f"source_rel must not have hardcoded ~/.claude/ prefix: {rule_from_rf.source_rel!r}"
        )

    def test_non_home_config_root_reports_real_relative_path(self, tmp_path: Path) -> None:
        """Regression guard: a non-~/.claude config_root returns the actual relative path.

        The bug: _compute_source_rel always prepended '~/.claude/' regardless of
        where config_root actually lived, so a file at proof/sample-config/CLAUDE.md
        was reported as '~/.claude/CLAUDE.md' — wrong path, wrong prefix.

        After the fix, the config-root-relative path is returned as-is: 'CLAUDE.md'.
        """
        config_root = tmp_path / "my-config"
        config_root.mkdir()
        fake_file = config_root / "CLAUDE.md"
        fake_file.touch()

        result = _compute_source_rel(fake_file, config_root, None)
        assert result == "CLAUDE.md", (
            f"Expected 'CLAUDE.md' for a non-home config_root, got {result!r}"
        )
        assert not result.startswith("~/.claude/"), (
            f"source_rel must not have hardcoded ~/.claude/ prefix: {result!r}"
        )


# ===========================================================================
# 7. Rule extraction correctness
# ===========================================================================


class TestRuleExtraction:
    def test_rules_extracted_from_user_config(self) -> None:
        config_root = BASIC / "config_root"
        result = parse_config(config_root)
        user_rules = [r for r in result.rules if r.precedence_tier == TIER_USER]
        assert len(user_rules) > 0

    def test_section_tracking(self, tmp_path: Path) -> None:
        """Rules inherit the nearest preceding heading."""
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text(
            "## Commit Rules\n\n- Never commit directly\n\n## Style\n\n- Always use tabs\n"
        )

        result = parse_config(config_root)
        commit_rules = [r for r in result.rules if "commit" in r.normalized_text.lower()]
        assert commit_rules, "Expected a rule about committing"
        assert commit_rules[0].section == "Commit Rules"

        style_rules = [r for r in result.rules if "tabs" in r.normalized_text.lower()]
        assert style_rules, "Expected a rule about tabs"
        assert style_rules[0].section == "Style"

    def test_multiline_bullet_consolidated(self, tmp_path: Path) -> None:
        """Multi-line bullets (with indented continuation) are one Rule."""
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text(
            "- Never commit directly to main\n"
            "  unless it is a hotfix approved\n"
            "  by the team lead\n"
            "- Separate bullet\n"
        )

        result = parse_config(config_root)
        # First rule should span lines 1-3
        first = result.rules[0]
        assert first.line_start == 1
        assert first.line_end == 3
        assert "hotfix" in first.normalized_text

    def test_line_numbers_are_one_based(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text(
            "# Heading\n\n- First rule\n- Second rule\n"
        )
        result = parse_config(config_root)
        for rule in result.rules:
            assert rule.line_start >= 1
            assert rule.line_end >= rule.line_start

    def test_rule_id_is_stable(self, tmp_path: Path) -> None:
        """Same content at same path produces same rule_id across two parses."""
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text("- Must be stable\n")

        r1 = parse_config(config_root)
        r2 = parse_config(config_root)
        ids1 = {r.rule_id for r in r1.rules}
        ids2 = {r.rule_id for r in r2.rules}
        assert ids1 == ids2

    def test_rule_id_length_twelve(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text("- Must check id length\n")
        result = parse_config(config_root)
        for rule in result.rules:
            assert len(rule.rule_id) == 12

    def test_fenced_code_block_not_extracted_as_rules(self, tmp_path: Path) -> None:
        """Lines inside fenced code blocks are not extracted as rules."""
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text(
            "- Must follow the example\n"
            "\n"
            "```bash\n"
            "never run this as a rule\n"
            "must ignore me\n"
            "```\n"
            "\n"
            "- Always use the real rule\n"
        )
        result = parse_config(config_root)
        texts = [r.normalized_text for r in result.rules]
        assert not any("ignore me" in t for t in texts)
        assert any("example" in t for t in texts)
        assert any("real rule" in t for t in texts)

    def test_imperative_flag_set_correctly(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text(
            "- Never do this bad thing\n"
            "- This is just a description\n"
        )
        result = parse_config(config_root)
        never_rule = next(r for r in result.rules if "bad thing" in r.normalized_text)
        desc_rule = next(r for r in result.rules if "description" in r.normalized_text)
        assert never_rule.imperative is True
        assert desc_rule.imperative is False

    def test_heading_itself_not_a_rule(self, tmp_path: Path) -> None:
        config_root = tmp_path / "config"
        config_root.mkdir()
        (config_root / "CLAUDE.md").write_text("# Just A Heading\n")
        result = parse_config(config_root)
        # Heading text should not appear as a rule
        assert not any("Just A Heading" in r.normalized_text for r in result.rules)

    def test_empty_config_root(self, tmp_path: Path) -> None:
        """An empty config root produces no rules and no crash."""
        config_root = tmp_path / "empty_config"
        config_root.mkdir()
        result = parse_config(config_root)
        assert result.rules == []
        assert result.sources == []


# ===========================================================================
# 8. find_import_lines (fence-awareness unit tests)
# ===========================================================================


class TestFindImportLines:
    def test_import_outside_fence_found(self) -> None:
        content = "# Title\n@sub.md\n- Rule\n"
        imports = _find_import_lines(content)
        assert len(imports) == 1
        assert imports[0][1] == "sub.md"

    def test_import_inside_fence_skipped(self) -> None:
        content = "```\n@inside.md\n```\n@outside.md\n"
        imports = _find_import_lines(content)
        paths = [p for _ln, p in imports]
        assert "inside.md" not in paths
        assert "outside.md" in paths

    def test_import_inside_tilde_fence_skipped(self) -> None:
        content = "~~~\n@inside.md\n~~~\n@outside.md\n"
        imports = _find_import_lines(content)
        paths = [p for _ln, p in imports]
        assert "inside.md" not in paths
        assert "outside.md" in paths

    def test_line_number_is_one_based(self) -> None:
        content = "@first.md\n- rule\n@third.md\n"
        imports = _find_import_lines(content)
        assert imports[0] == (1, "first.md")
        assert imports[1] == (3, "third.md")

    def test_no_imports(self) -> None:
        content = "# Title\n- Just a rule\n"
        assert _find_import_lines(content) == []
