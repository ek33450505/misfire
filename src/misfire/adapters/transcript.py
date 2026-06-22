"""transcript.py — portable Claude Code transcript adapter.

Walks every ``*.jsonl`` under ``projects_dir`` (Claude Code's
``~/.claude/projects/`` tree), parses assistant ``tool_use`` records,
and yields normalised ``ToolAction`` objects.

Layout assumed (verified against CC 2.1.170)
--------------------------------------------
- Main session files:  ``<projects_dir>/<slug>/<session-uuid>.jsonl``
- Subagent files:      ``<projects_dir>/<slug>/<session-uuid>/subagents/<id>.jsonl``

Both file types are iterated in deterministic (sorted) order.  In-file
order is preserved.

Robustness
----------
- Malformed JSON lines are silently skipped.
- Missing or unexpected fields tolerate ``None`` / absent keys.
- OSError on file open is silently skipped.
- No external dependencies — stdlib only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional

from misfire.evidence import ToolAction, _sanitize_path
from misfire.parse import _collapse_home


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _input_summary(tool_name: str, inp: dict) -> str:
    """Derive a compact, privacy-safe summary of the tool input.

    - ``Read`` / ``Write`` / ``Edit``: home-collapsed ``file_path``.
    - ``Agent``: ``subagent_type`` or first 80 chars of ``description``.
    - Other tools: first 80 chars of the first string value in ``inp``.
    - Returns ``""`` when no suitable field is found.
    """
    if tool_name in {"Read", "Write", "Edit"}:
        fp = inp.get("file_path", "")
        return _sanitize_path(fp) if fp else ""

    if tool_name == "Agent":
        st = inp.get("subagent_type") or ""
        if st:
            return str(st)[:80]
        desc = inp.get("description") or ""
        return str(desc)[:80]

    # Generic fallback: first string value
    for v in inp.values():
        if isinstance(v, str):
            return v[:80]
    return ""


def _parse_records(
    jf: Path,
    transcript_rel: str,
) -> Iterator[ToolAction]:
    """Yield ``ToolAction`` objects from a single ``.jsonl`` file.

    Skips non-assistant records, non-tool_use content blocks, and any
    line that fails JSON parsing.
    """
    try:
        text = jf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(record, dict):
            continue
        if record.get("type") != "assistant":
            continue

        session_id: str = record.get("sessionId") or ""
        timestamp: str = record.get("timestamp") or ""
        is_sidechain: bool = bool(record.get("isSidechain", False))
        cwd_raw: str = record.get("cwd") or ""
        cwd_rel: str = _sanitize_path(cwd_raw) if cwd_raw else ""
        git_branch_raw: Optional[str] = record.get("gitBranch") or None
        git_branch: Optional[str] = git_branch_raw if git_branch_raw else None
        agent_type: Optional[str] = record.get("attributionAgent") or None

        msg = record.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue

            tool_name: str = block.get("name") or ""
            inp = block.get("input")
            inp = inp if isinstance(inp, dict) else {}

            command: str = ""
            if tool_name == "Bash":
                command = inp.get("command") or ""

            summary = _input_summary(tool_name, inp)

            yield ToolAction(
                session_id=session_id,
                timestamp=timestamp,
                tool_name=tool_name,
                command=command,
                input_summary=summary,
                is_sidechain=is_sidechain,
                agent_type=agent_type,
                transcript_rel=transcript_rel,
                cwd_rel=cwd_rel,
                git_branch=git_branch,
            )


def _walk_transcript_files(
    projects_dir: Path,
    include_subagents: bool,
) -> Iterator[tuple[Path, bool]]:
    """Yield ``(jsonl_path, is_subagent_file)`` in deterministic order.

    Traversal order: sorted slug dirs, then sorted files within each.
    Main session files come before subagent files within the same slug dir.
    """
    try:
        slug_dirs = sorted(p for p in projects_dir.iterdir() if p.is_dir())
    except OSError:
        return

    for slug_dir in slug_dirs:
        # Main session files: direct *.jsonl children of the slug dir
        try:
            main_files = sorted(slug_dir.glob("*.jsonl"))
        except OSError:
            main_files = []
        for jf in main_files:
            yield jf, False

        if not include_subagents:
            continue

        # Subagent files: <slug_dir>/<uuid>/subagents/*.jsonl
        try:
            uuid_dirs = sorted(p for p in slug_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for uuid_dir in uuid_dirs:
            subagents_dir = uuid_dir / "subagents"
            if not subagents_dir.is_dir():
                continue
            try:
                sub_files = sorted(subagents_dir.glob("*.jsonl"))
            except OSError:
                continue
            for jf in sub_files:
                yield jf, True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def iter_tool_actions(
    projects_dir: Path,
    *,
    include_subagents: bool = True,
) -> Iterator[ToolAction]:
    """Walk every ``.jsonl`` under ``projects_dir`` and yield ``ToolAction`` objects.

    Args:
        projects_dir: Path to Claude Code's ``projects/`` directory (e.g.
            ``Path.home() / '.claude' / 'projects'``).  Tests should pass a
            fixture directory here — the real ``~/.claude/projects`` is never
            read by default.
        include_subagents: When ``True`` (default), subagent ``.jsonl`` files
            under ``<slug>/<uuid>/subagents/`` are included.  Set to ``False``
            to process only main-session files.

    Yields:
        ``ToolAction`` objects in deterministic order (slug-sorted, then
        file-sorted within each slug, then in-file order).

    The emitted ``transcript_rel`` and ``cwd_rel`` fields are always
    home-collapsed (``~/...``) — they will never contain ``/Users/<name>/``.
    """
    for jf, is_subagent_file in _walk_transcript_files(projects_dir, include_subagents):
        transcript_rel = _collapse_home(jf)
        yield from _parse_records(jf, transcript_rel)
