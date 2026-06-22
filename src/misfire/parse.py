"""parse.py — Phase 1 static config parser for misfire.

Walk the documented Claude Code precedence chain:
  managed (stub) → user (config_root/CLAUDE.md) → project ancestor chain
  (root-down) → project .claude/CLAUDE.md → CLAUDE.local.md
  → .claude/rules/*.md

Resolve ``@path`` imports (relative to importing file, max 4 hops, cycle-safe,
fence-aware). Capture ``paths:`` frontmatter on ``rules/*.md`` files.

Public API::

    parse_config(config_root: Path, project_dir: Path | None = None) -> ParseResult

Never reads the real ``~/.claude`` — callers supply ``config_root`` so tests
can pass a temp fixture directory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Tier constants
# ---------------------------------------------------------------------------

TIER_MANAGED = "managed"
TIER_USER = "user"
TIER_PROJECT = "project"
TIER_PROJECT_LOCAL = "project_local"
TIER_RULES_FILE = "rules_file"
TIER_IMPORT = "import"

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# Imperative markers (applied to normalised text, case-insensitive).
# "use X not Y" uses a lazy dot-any to avoid crossing clause boundaries.
_IMPERATIVE_RE = re.compile(
    r"\b(never|must|always|mandatory|prefer|avoid|before|after)\b"
    r"|\bdo\s+not\b"
    r"|\buse\b.+?\bnot\b",
    re.IGNORECASE,
)

# @path import: a line consisting only of "@something" (no spaces)
_IMPORT_LINE_RE = re.compile(r"^@(\S+)\s*$")

# Markdown heading
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")

# Bullet-list item (possibly indented): -, *, + or N.
_BULLET_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s")

# Fenced code block opener/closer (``` or ~~~, three or more)
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Rule:
    """A single rule unit extracted from a config source file.

    ``rule_id`` is a stable 12-char SHA-1 prefix of
    ``"{source_rel}::{normalized_text}"`` — deterministic across runs.
    """

    rule_id: str
    source_path: str          # absolute path (internal use)
    source_rel: str           # display path — home-collapsed or project-relative
    precedence_tier: str      # one of the TIER_* constants
    section: str              # nearest preceding markdown heading ("" if none)
    line_start: int           # 1-based
    line_end: int             # 1-based (== line_start for single-line rules)
    raw_text: str             # original markdown as written
    normalized_text: str      # markup-stripped, whitespace-collapsed
    imperative: bool          # contains an imperative marker


@dataclasses.dataclass
class SourceFile:
    """A discovered config source file, in discovery order."""

    path: str                             # absolute path
    tier: str                             # one of the TIER_* constants
    imported_from: Optional[str] = None   # absolute path of importing file
    paths_globs: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ParseResult:
    """Result of ``parse_config``."""

    rules: List[Rule]
    sources: List[SourceFile]


# ---------------------------------------------------------------------------
# Path / display helpers
# ---------------------------------------------------------------------------


def _collapse_home(path: Path) -> str:
    """Collapse ``/Users/<name>/...`` → ``~/...`` (privacy requirement)."""
    s = str(path)
    home = str(Path.home())
    if s == home:
        return "~"
    if s.startswith(home + "/"):
        return "~/" + s[len(home) + 1:]
    return s


def _compute_source_rel(
    path: Path,
    config_root: Path,
    project_dir: Optional[Path],
) -> str:
    """Compute a display-safe relative path for a source file.

    Priority:
    1. project-relative (if ``path`` is under ``project_dir``)
    2. config-root-relative (if ``path`` is under ``config_root``) — returned as a
       bare relative path, e.g. ``CLAUDE.md``, ``rules/tools.md``.  No hardcoded
       prefix is added so that callers supplying a non-``~/.claude`` config_root
       (test fixtures, ``proof/sample-config``, etc.) get accurate paths.
    3. home-collapsed (``~/...``)
    4. absolute path (only when outside home — acceptable in test fixtures)

    Never exposes ``/Users/<name>/``.
    """
    resolved = path.resolve()

    if project_dir is not None:
        try:
            return str(resolved.relative_to(project_dir.resolve()))
        except ValueError:
            pass

    try:
        rel = resolved.relative_to(config_root.resolve())
        return str(rel)
    except ValueError:
        pass

    return _collapse_home(resolved)


def _rule_id(source_rel: str, normalized_text: str) -> str:
    """Stable 12-char SHA-1 hex of ``'{source_rel}::{normalized_text}'``."""
    key = f"{source_rel}::{normalized_text}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Markdown normalisation
# ---------------------------------------------------------------------------


def normalize_markdown(text: str) -> str:
    """Strip markdown markup and collapse whitespace.

    Stripping order (order matters for correctness):
    1. Links ``[text](url)`` → ``text``
    2. Inline code `` `text` `` → ``text``  ← critical: enables imperative
       detection on rules like ``Never `git commit` directly``
    3. Heading markers ``##``
    4. Blockquote markers ``>``
    5. Bold ``**text**`` / ``__text__``
    6. Italic ``*text*`` / ``_text_``
    7. Bullet-list markers ``- `` / ``* `` / ``1. ``
    8. Collapse whitespace

    Returns the normalised string (may be empty for blank/pure-markup lines).
    """
    # 1. Links
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # 2. Inline code (handles single and triple backticks)
    text = re.sub(r"`+([^`]*)`+", r"\1", text)
    # 3. Heading markers
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    # 4. Blockquote markers
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # 5. Bold
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    # 6. Italic
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    # 7. Bullet markers (at line start, optional leading whitespace)
    text = re.sub(r"^[ \t]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*\d+\.\s+", "", text, flags=re.MULTILINE)
    # 8. Collapse
    return re.sub(r"\s+", " ", text).strip()


def has_imperative(normalized: str) -> bool:
    """Return ``True`` if ``normalized`` contains an imperative marker.

    Markers (case-insensitive): never, must, always, mandatory, do not,
    prefer, avoid, before, after, use … not.

    ``normalize_markdown`` MUST be applied before calling this so that
    constructs like ``Never `git commit` directly`` have their backticks
    stripped first, enabling the ``\\bnever\\b`` match.
    """
    return bool(_IMPERATIVE_RE.search(normalized))


# ---------------------------------------------------------------------------
# Frontmatter parsing (YAML-lite, stdlib only)
# ---------------------------------------------------------------------------


def _parse_frontmatter(content: str) -> Tuple[Dict, str]:
    """Parse ``---`` delimited YAML-lite frontmatter.

    Only the ``paths:`` list key is captured; all other keys are ignored.
    Returns ``(fm_dict, body_without_frontmatter)``. If no valid frontmatter
    block is found, returns ``({}, content)``.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content

    end_idx: Optional[int] = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, content

    fm_lines = [ln.rstrip() for ln in lines[1:end_idx]]
    body = "".join(lines[end_idx + 1:])

    fm: Dict = {}
    current_key: Optional[str] = None
    current_list: List[str] = []

    for line in fm_lines:
        if not line:
            continue
        if line.startswith(("  ", "\t")):
            # List item under the current key
            item = line.strip()
            if item.startswith("-"):
                item = item[1:].strip()
            item = item.strip("\"'")
            if item and current_key is not None:
                current_list.append(item)
        elif ":" in line:
            # Save previous accumulated list
            if current_key is not None and current_list:
                fm[current_key] = current_list
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            current_key = key
            current_list = []
            if val:
                fm[key] = val
                current_key = None  # scalar key — no list accumulation
        # else: skip unrecognised lines

    if current_key is not None and current_list:
        fm[current_key] = current_list

    return fm, body


# ---------------------------------------------------------------------------
# Import-line discovery (fence-aware)
# ---------------------------------------------------------------------------


def _find_import_lines(content: str) -> List[Tuple[int, str]]:
    """Find ``@path`` import lines that are NOT inside fenced code blocks.

    Returns a list of ``(1-based_line_number, import_path_string)`` tuples.
    Lines inside triple-backtick or triple-tilde fences are skipped, so a
    code example containing ``@some.md`` is never treated as a real import.
    """
    results: List[Tuple[int, str]] = []
    in_fence = False
    fence_marker = ""

    for i, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        fm = _FENCE_RE.match(stripped)
        if fm:
            marker = fm.group(1)[:3]  # first 3 chars define the fence type
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = ""
            continue

        if in_fence:
            continue

        m = _IMPORT_LINE_RE.match(stripped)
        if m:
            results.append((i, m.group(1)))

    return results


# ---------------------------------------------------------------------------
# Rule extraction
# ---------------------------------------------------------------------------


def _extract_rules_from_content(
    content: str,
    source_path: str,
    src_rel: str,
    tier: str,
) -> List[Rule]:
    """Extract ``Rule`` objects from markdown content.

    Rule units:
    - Bullet-list items (possibly multi-line: indented continuation lines are
      merged into the opening bullet until a blank line, new top-level bullet,
      heading, or fence is encountered).
    - Standalone non-blank, non-heading, non-import prose lines.

    Lines inside fenced code blocks are entirely ignored.
    Headings update the ``section`` tracker but are not emitted as rules.
    ``@path`` import lines are skipped here (they are handled separately).
    """
    rules: List[Rule] = []
    lines = content.splitlines()
    n = len(lines)

    current_section = ""
    in_fence = False
    fence_marker = ""

    # Bullet accumulator
    in_bullet = False
    bullet_raw_lines: List[str] = []
    bullet_start: int = 0
    bullet_base_indent: int = 0

    def _flush_bullet(end_line: int) -> None:
        nonlocal in_bullet, bullet_raw_lines, bullet_start, bullet_base_indent
        if not in_bullet:
            return
        raw = "\n".join(bullet_raw_lines)
        norm = normalize_markdown(raw)
        if norm:
            rules.append(Rule(
                rule_id=_rule_id(src_rel, norm),
                source_path=source_path,
                source_rel=src_rel,
                precedence_tier=tier,
                section=current_section,
                line_start=bullet_start,
                line_end=end_line,
                raw_text=raw,
                normalized_text=norm,
                imperative=has_imperative(norm),
            ))
        in_bullet = False
        bullet_raw_lines = []
        bullet_base_indent = 0

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()

        # --- Fence detection ---
        fm = _FENCE_RE.match(stripped)
        if fm:
            marker = fm.group(1)[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
                _flush_bullet(i - 1)
            elif stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = ""
            continue

        if in_fence:
            continue

        # --- Heading ---
        hm = _HEADING_RE.match(line)
        if hm:
            _flush_bullet(i - 1)
            current_section = hm.group(2).strip()
            continue

        # --- Blank line ---
        if not stripped:
            _flush_bullet(i - 1)
            continue

        # --- @import lines (not rule units) ---
        if _IMPORT_LINE_RE.match(stripped):
            _flush_bullet(i - 1)
            continue

        # --- Bullet item ---
        bm = _BULLET_RE.match(line)
        if bm:
            indent = len(bm.group(1))
            if in_bullet and indent > bullet_base_indent:
                # Sub-item or deeper nesting: merge into current bullet
                bullet_raw_lines.append(line)
            else:
                # New top-level (or same-level) bullet
                _flush_bullet(i - 1)
                in_bullet = True
                bullet_raw_lines = [line]
                bullet_start = i
                bullet_base_indent = indent
            continue

        # --- Continuation of current bullet (indented non-bullet line) ---
        leading = len(line) - len(line.lstrip())
        if in_bullet and leading > bullet_base_indent:
            bullet_raw_lines.append(line)
            continue

        # --- Plain prose line ---
        _flush_bullet(i - 1)
        norm = normalize_markdown(line)
        if norm:
            rules.append(Rule(
                rule_id=_rule_id(src_rel, norm),
                source_path=source_path,
                source_rel=src_rel,
                precedence_tier=tier,
                section=current_section,
                line_start=i,
                line_end=i,
                raw_text=line,
                normalized_text=norm,
                imperative=has_imperative(norm),
            ))

    # Flush any trailing bullet
    _flush_bullet(n)

    return rules


# ---------------------------------------------------------------------------
# Source file discovery (non-import sources only)
# ---------------------------------------------------------------------------


def _discover_sources(
    config_root: Path,
    project_dir: Optional[Path],
) -> List[SourceFile]:
    """Discover all non-import source files in precedence order.

    Discovery order
    ~~~~~~~~~~~~~~~
    1. **managed** — hook/stub: managed-policy CLAUDE.md is intentionally not
       resolved in Phase 1 (requires platform-specific lookup). The tier constant
       exists; callers can extend this function to prepend managed-policy files.
    2. **user** — ``config_root/CLAUDE.md``
    3. **project** — ancestor-tree ``CLAUDE.md`` files, root → ``project_dir``
       (root-down), then ``project_dir/.claude/CLAUDE.md``
    4. **project_local** — ``project_dir/CLAUDE.local.md``
    5. **rules_file** — ``config_root/rules/*.md`` (sorted, user-level rules)
    6. **rules_file** — ``project_dir/.claude/rules/*.md`` (sorted, project rules)

    Files that do not exist are silently skipped.
    Duplicate paths (same resolved absolute path) are deduplicated.
    """
    sources: List[SourceFile] = []
    seen: Set[str] = set()

    def _add(p: Path, tier: str, paths_globs: Optional[List[str]] = None) -> bool:
        key = str(p.resolve())
        if key in seen or not p.exists():
            return False
        seen.add(key)
        sources.append(SourceFile(path=key, tier=tier, paths_globs=paths_globs or []))
        return True

    def _add_rules_dir(rules_dir: Path) -> None:
        if not rules_dir.is_dir():
            return
        for rf in sorted(rules_dir.glob("*.md")):
            key = str(rf.resolve())
            if key in seen:
                continue
            content = rf.read_text(encoding="utf-8", errors="replace")
            fm, _ = _parse_frontmatter(content)
            globs = fm.get("paths", [])
            if isinstance(globs, str):
                globs = [globs]
            seen.add(key)
            sources.append(SourceFile(path=key, tier=TIER_RULES_FILE, paths_globs=list(globs)))

    # --- 1. Managed-policy (stub) ---
    # TODO Phase 2+: prepend managed-policy files (e.g. from
    # /Library/Application Support/ClaudeCode/managed-settings.d/ on macOS).
    # The TIER_MANAGED constant is reserved for that tier.

    # --- 2. User ---
    _add(config_root / "CLAUDE.md", TIER_USER)

    # --- 3. Project ancestor chain (root → project_dir, root-down) ---
    if project_dir is not None:
        ancestors: List[Path] = []
        p = project_dir.resolve()
        while True:
            ancestors.append(p / "CLAUDE.md")
            parent = p.parent
            if parent == p:
                break
            p = parent
        for anc in reversed(ancestors):
            _add(anc, TIER_PROJECT)

        # project .claude/CLAUDE.md (may already be covered if project_dir IS
        # .claude, but typically distinct)
        _add(project_dir / ".claude" / "CLAUDE.md", TIER_PROJECT)

        # --- 4. Project-local ---
        _add(project_dir / "CLAUDE.local.md", TIER_PROJECT_LOCAL)

    # --- 5. User-level rules ---
    _add_rules_dir(config_root / "rules")

    # --- 6. Project-level rules ---
    if project_dir is not None:
        _add_rules_dir(project_dir / ".claude" / "rules")

    return sources


# ---------------------------------------------------------------------------
# Import resolution (DFS, max 4 hops, cycle-safe)
# ---------------------------------------------------------------------------

_MAX_IMPORT_DEPTH = 4


def _resolve_imports_recursive(
    sf: SourceFile,
    seen: Set[str],
    depth: int,
    out: List[SourceFile],
    missing: List[Tuple[str, str]],  # (abs_path, imported_from_path)
) -> None:
    """DFS import resolution starting from ``sf``.

    - Stops at depth > _MAX_IMPORT_DEPTH (max 4 hops from any base source).
    - Cycle-safe: ``seen`` tracks all resolved absolute paths; a file that
      imports itself or creates a cycle is skipped without looping.
    - Missing imports are recorded in ``missing`` (not crashed on).
    - Import lines inside fenced code blocks are ignored (see ``_find_import_lines``).
    """
    if depth > _MAX_IMPORT_DEPTH:
        return

    p = Path(sf.path)
    if not p.exists():
        return

    content = p.read_text(encoding="utf-8", errors="replace")
    for _lineno, import_path in _find_import_lines(content):
        resolved = (p.parent / import_path).resolve()
        abs_str = str(resolved)

        if abs_str in seen:
            continue  # cycle or already processed

        if not resolved.exists():
            missing.append((abs_str, sf.path))
            continue

        seen.add(abs_str)
        imported_sf = SourceFile(
            path=abs_str,
            tier=TIER_IMPORT,
            imported_from=sf.path,
        )
        out.append(imported_sf)
        _resolve_imports_recursive(imported_sf, seen, depth + 1, out, missing)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_config(
    config_root: Path,
    project_dir: Optional[Path] = None,
) -> ParseResult:
    """Parse the Claude Code config tree.

    Args:
        config_root: The user config root (e.g. ``~/.claude/``). Pass a temp
                     directory in tests — this function never reads the real
                     ``~/.claude`` directly.
        project_dir: The project directory. When supplied, ancestor CLAUDE.md
                     files, project-local files, and project rules are
                     discovered.

    Returns:
        ``ParseResult`` with ``.rules`` (ordered list of ``Rule`` objects)
        and ``.sources`` (ordered list of ``SourceFile`` objects, imports
        appended after base sources).
    """
    # 1. Discover base (non-import) sources
    base_sources = _discover_sources(config_root, project_dir)

    # 2. Resolve @path imports (DFS, max 4 hops, cycle-safe)
    seen_for_imports: Set[str] = {sf.path for sf in base_sources}
    import_sources: List[SourceFile] = []
    missing_imports: List[Tuple[str, str]] = []

    for sf in base_sources:
        _resolve_imports_recursive(
            sf,
            seen_for_imports,
            depth=1,
            out=import_sources,
            missing=missing_imports,
        )

    all_sources = base_sources + import_sources

    # 3. Extract rules from each source (frontmatter stripped before extraction)
    rules: List[Rule] = []
    for sf in all_sources:
        p = Path(sf.path)
        if not p.exists():
            continue
        content = p.read_text(encoding="utf-8", errors="replace")
        _, body = _parse_frontmatter(content)
        src_rel_str = _compute_source_rel(p, config_root, project_dir)
        rules.extend(
            _extract_rules_from_content(body, sf.path, src_rel_str, sf.tier)
        )

    # 4. Record missing imports as SourceFile entries (path won't exist on disk;
    #    rule extraction skips them gracefully via the p.exists() guard above).
    for abs_path, imported_from in missing_imports:
        all_sources.append(SourceFile(
            path=abs_path,
            tier=TIER_IMPORT,
            imported_from=imported_from,
        ))

    return ParseResult(rules=rules, sources=all_sources)
