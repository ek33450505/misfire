"""evidence.py — Phase 2 data model for the misfire evidence engine.

``ToolAction`` is the normalized unit that the transcript adapter produces and
that Unit 2 (rule-predicate matching) consumes.  It is intentionally minimal:
no matching / ranking / DB logic lives here.

Privacy invariant: ``transcript_rel`` and ``cwd_rel`` MUST NOT contain a raw
``/Users/<name>/`` prefix.  Use ``_sanitize_path`` (or the imported
``_collapse_home`` from ``parse.py``) before setting those fields.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Optional

# Reuse the home-collapse helper from parse.py — single source of truth,
# no divergent copy.
from misfire.parse import _collapse_home  # noqa: PLC2701 (private-import is intentional)


# ---------------------------------------------------------------------------
# Path sanitiser (string variant that wraps _collapse_home)
# ---------------------------------------------------------------------------


def _sanitize_path(s: str) -> str:
    """Collapse ``/Users/<name>/...`` → ``~/...`` in a plain string.

    Wraps ``_collapse_home`` so callers need not construct a ``Path`` first.
    Safe for already-collapsed strings (``~/...``) — they are returned
    unchanged because they don't start with ``str(Path.home())``.
    """
    if not s:
        return s
    return _collapse_home(Path(s))


# ---------------------------------------------------------------------------
# ToolAction — the normalised evidence atom
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ToolAction:
    """A single tool invocation extracted from a Claude Code transcript.

    Fields
    ------
    session_id
        The ``sessionId`` from the transcript record.
    timestamp
        ISO 8601 timestamp string as stored in the record (e.g.
        ``"2026-06-22T10:00:00.000Z"``).  Not parsed — kept as-is.
    tool_name
        The ``name`` field of the ``tool_use`` content block (e.g. ``"Bash"``,
        ``"Read"``, ``"Write"``, ``"Edit"``).
    command
        For ``Bash`` tool calls: the ``.input.command`` string.
        Empty string for all other tools.
    input_summary
        A compact, privacy-safe summary of the tool input:

        - ``Read`` / ``Write`` / ``Edit``: the ``.input.file_path``
          (home-collapsed).
        - ``Agent``: the ``.input.subagent_type`` or leading
          80 chars of ``.input.description``.
        - Other tools: leading 80 chars of the first string input value.
        - Empty string when no suitable field is found.
    is_sidechain
        ``True`` when the record's ``isSidechain`` field is truthy (i.e.
        the action occurred inside a subagent run).
    agent_type
        The ``attributionAgent`` value from the record when present (e.g.
        ``"code-writer"``, ``"commit"``).  ``None`` for main-session records.
    transcript_rel
        Path to the source ``.jsonl`` file — **home-collapsed** (never
        contains ``/Users/<name>/``).
    cwd_rel
        The ``cwd`` field from the record — **home-collapsed**.
        Empty string when the record has no ``cwd``.
    git_branch
        The ``gitBranch`` field from the record, or ``None`` when absent /
        empty.
    """

    session_id: str
    timestamp: str
    tool_name: str
    command: str
    input_summary: str
    is_sidechain: bool
    agent_type: Optional[str]
    transcript_rel: str
    cwd_rel: str
    git_branch: Optional[str]
