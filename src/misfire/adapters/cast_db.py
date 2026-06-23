"""cast_db.py — OPTIONAL, flag-gated cast.db adapter for misfire.

misfire is **portable-first**: its core (static audit + transcript-grounded
ranking) works for ANY Claude Code user with zero database.  This adapter is a
strictly OPTIONAL, CAST-power-user bonus substrate.  It is engaged only when the
caller passes ``--cast-db`` on the CLI — otherwise cast.db is never touched.

Why it exists
-------------
Two CAST output-protocol rule classes cannot be reconstructed from the portable
transcript stream because the violation is about the *shape of an agent's final
message* (a missing ``## Handoff`` block, a missing ``Status:`` line), not about
a tool invocation.  CAST records those in ``cast.db`` at SubagentStop time
(``agent_protocol_violations``).  This adapter reads that ledger so misfire can
rank those output_shape rules with real evidence — but ONLY the ones the user's
own prose actually states (honest under-coverage: a violation signal with no
matching prose rule is reported as *unmapped*, never force-attributed).

Safety / posture
----------------
- **Read-only, always.** The DB is opened via a strict ``mode=ro`` URI
  (``sqlite3.connect("file:...?mode=ro", uri=True)``).  We NEVER write, NEVER
  run a write PRAGMA, and NEVER create the main DB file.  A missing file fails
  closed (``mode=ro`` will not create it).  Caveat: reading a WAL-mode DB causes
  SQLite to materialise ``-shm``/``-wal`` shared-memory side files; this does
  NOT touch the committed data or the main file, so the read-only data-safety
  property holds (see ``_connect_ro`` for the full rationale).
- **stdlib only.** Uses the stdlib ``sqlite3`` module.  We deliberately do NOT
  import ``~/.claude/scripts/cast_db.py`` — that would CAST-lock the package and
  it opens the DB read-write.
- **Observer posture.** Any error (missing DB, missing table, query failure)
  yields an EMPTY result; this module NEVER raises into its caller and NEVER
  crashes the CLI.
- **Privacy.** ``raw_excerpt`` may contain real agent output and embedded
  filesystem paths.  Excerpts are sanitised (``/Users/<name>/`` and
  ``/home/<name>/`` collapsed to ``~/``) and truncated before being surfaced;
  raw rows are never written to any committed file.
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from misfire.classify import CATEGORY_OUTPUT_SHAPE, Classification
from misfire.evidence import ToolAction, _sanitize_path
from misfire.match import RuleViolation
from misfire.parse import Rule, _collapse_home


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# convert_kind marker for cast.db-sourced output-shape violations.  Mirrors the
# CATEGORY_OUTPUT_SHAPE classification; rank.py is agnostic to the value.
CONVERT_OUTPUT_SHAPE = "output_shape"

# The hook these protocol violations are recorded at, per the CAST spec.
_SUBAGENT_STOP_HOOK = "SubagentStop"

# Tables the adapter strictly requires to be useful.
_REQUIRED_TABLES = frozenset({"agent_protocol_violations", "agent_runs"})

# Max characters in a synthesised ToolAction.input_summary.
_INPUT_SUMMARY_MAX = 120

# Embedded absolute-home path collapse (arbitrary user, both macOS and Linux).
# evidence._sanitize_path only collapses the CURRENT user's home prefix when it
# is at the START of the string; raw_excerpt may embed an arbitrary
# /Users/<other>/ path mid-string, so we collapse those explicitly first.
#
# NOTE: there is deliberately NO trailing slash in the pattern.  A bare
# ``/Users/<name>`` with nothing after it (e.g. "cwd: /Users/ed") is one of the
# most natural shapes in an agent's final-message excerpt, and a trailing-slash
# requirement would let that username leak.  The replacement is ``~`` (NOT
# ``~/``) so that ``/Users/alice/secret`` → ``~/secret`` and a bare
# ``/Users/bob`` → ``~`` are BOTH collapsed correctly.
_EMBEDDED_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s]+")

# ---------------------------------------------------------------------------
# Protocol-signal mapping table
# ---------------------------------------------------------------------------
#
# Each signal maps a SET of cast.db ``agent_protocol_violations.violation``
# values to the DISTINCTIVE lowercase phrases that a user's prose rule must
# contain for misfire to attribute the violations to that rule.
#
# Ground truth (probed live 2026-06-22 against the real ~/.claude/cast.db):
#   - The column is ``violation`` (NOT ``violation_type``).
#   - ``handoff_schema_violation`` (168 rows) ← maps to a "## Handoff block …"
#     rule.
#   - ``missing_formality``       (42 rows)  ← maps to an "… end with Status:
#     DONE | …" rule.
#   - ``prose_dispatch``          (2 rows)   ← has NO signal here on purpose:
#     no clean output_shape rule states it, so it stays UNMAPPED (honest
#     under-coverage) rather than being force-attributed to an unrelated rule.
#
# The phrases are intentionally distinctive multi-word substrings so they match
# ONLY the intended rule among the output_shape classifications (and not the
# looser journal / agent-file-spec / maxTurns-symptom rules that also mention
# "status" or "work log").
#
# IMPORTANT (false-uniqueness fix, 2026-06-22): the bare phrase "handoff block"
# is NOT unique — the maxTurns-symptom rule
# (rules/working-conventions.md "…no Status/Handoff block, no SubagentStop…")
# normalises to a text that contains the contiguous substring "handoff block",
# so it matched too.  Correct attribution previously survived only because the
# intended CLAUDE.md rule happened to sort first.  The phrase is tightened to
# "handoff block before" — present in the intended "## Handoff block (MANDATORY
# for chained agents): …MUST include a ## Handoff block before ## Work Log" rule
# but NOT in the maxTurns rule.  ``_match_rule_for_signal`` additionally guards
# against any residual ambiguity by refusing to attribute when >1 rule matches.
_PROTOCOL_SIGNALS: List[Dict] = [
    {
        "key": "handoff",
        "violations": frozenset({"handoff_schema_violation"}),
        "rule_phrases": ["handoff block before"],
        "label": "Handoff block schema",
    },
    {
        "key": "status",
        "violations": frozenset({"missing_formality"}),
        "rule_phrases": ["end with status"],
        "label": "agent Status block",
    },
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CastDbAvailability:
    """Result of an availability probe for a cast.db file.

    ``reason`` is one of ``{"ok", "not_found", "unreadable", "missing_tables"}``.
    ``db_path_rel`` is always home-collapsed — it NEVER contains a raw
    ``/Users/<name>/`` prefix.
    """

    available: bool
    reason: str
    db_path_rel: str


@dataclasses.dataclass(frozen=True)
class ProtocolViolationRow:
    """A normalised ``agent_protocol_violations`` row (no raw DB cursor leaks)."""

    violation: str
    pattern: str
    agent_type: Optional[str]
    timestamp: str
    raw_excerpt: str
    session_id: str


@dataclasses.dataclass(frozen=True)
class CastDbResult:
    """Result of ``find_output_shape_violations``.

    Fields
    ------
    rule_violations
        One ``RuleViolation`` per MAPPED signal (a signal whose violation set
        is attributable to exactly one user output_shape rule).  Empty when the
        DB is unavailable or no signal mapped.
    total_violations_read
        Count of all ``agent_protocol_violations`` rows read.
    mapped_violations
        Count of rows whose ``violation`` value belongs to a MAPPED signal.
    unmapped_by_signal
        ``{violation_value: count}`` for every row NOT attributed to a mapped
        signal — either because no signal claims that value (e.g.
        ``prose_dispatch``) or because the signal had no matching prose rule.
    agent_runs_denominator
        ``count_agent_runs`` — the opportunity denominator.  This is an UPPER
        BOUND on real Handoff/Status opportunities, so the resulting
        ``violation_rate`` in rank output is a conservative LOWER BOUND.
    db_path_rel
        Home-collapsed db path (never ``/Users/<name>/``).
    """

    rule_violations: List[RuleViolation]
    total_violations_read: int
    mapped_violations: int
    unmapped_by_signal: Dict[str, int]
    agent_runs_denominator: int
    db_path_rel: str


# ---------------------------------------------------------------------------
# Read-only connection helper
# ---------------------------------------------------------------------------


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` STRICTLY read-only via a ``mode=ro`` file URI.

    ``mode=ro`` guarantees:
    - the file is never created (a missing file raises instead),
    - the logical data and the main DB file are never written (reads never
      mutate the database).

    Caveat — WAL side files: if the DB is in WAL journal mode (the production
    cast.db, written by better-sqlite3, commonly is), SQLite materialises the
    ``-shm``/``-wal`` shared-memory index files to read it, even under
    ``mode=ro``.  Those side files do NOT alter the committed data or the main
    file's mtime, so the read-only DATA-safety property holds; but the earlier
    "no side files are ever produced" claim was false for WAL and has been
    corrected here.  We intentionally do NOT pass ``immutable=1`` (which would
    suppress the side files) because it skips locking and can read a torn page
    if a writer is concurrently active.

    The path is percent-encoded before interpolation so that a path containing
    URI-special characters (notably ``?`` or ``#``) cannot break out of the URI
    path component and silently drop the ``?mode=ro`` directive (which would let
    SQLite open the truncated path in the default read-write-create mode).

    The path is used as supplied (only ``expanduser``-ed by the caller, never
    ``resolve``-d) so that a relative path stays relative and ``db_path_rel``
    remains portable.  sqlite resolves a relative URI path against the current
    working directory.
    """
    # Percent-encode the path component.  '%' MUST be escaped first so the
    # subsequent '?'/'#' escapes are not themselves re-encoded.
    encoded = (
        str(db_path)
        .replace("%", "%25")
        .replace("?", "%3f")
        .replace("#", "%23")
    )
    uri = f"file:{encoded}?mode=ro"
    return sqlite3.connect(uri, uri=True)


# ---------------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------------


def castdb_available(db_path: Path) -> CastDbAvailability:
    """Probe whether ``db_path`` is a usable, read-only cast.db.

    Never raises.  Returns ``available=False`` (with a reason) when the file is
    missing, cannot be opened read-only, or lacks the required tables.
    """
    db_path_rel = _collapse_home(db_path)

    try:
        if not db_path.exists() or not db_path.is_file():
            return CastDbAvailability(False, "not_found", db_path_rel)
    except OSError:
        return CastDbAvailability(False, "not_found", db_path_rel)

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect_ro(db_path)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        tables = {r[0] for r in rows}
    except sqlite3.Error:
        return CastDbAvailability(False, "unreadable", db_path_rel)
    finally:
        if conn is not None:
            conn.close()

    if not _REQUIRED_TABLES.issubset(tables):
        return CastDbAvailability(False, "missing_tables", db_path_rel)

    return CastDbAvailability(True, "ok", db_path_rel)


# ---------------------------------------------------------------------------
# Queries (parameterised / fixed SQL only — no untrusted interpolation)
# ---------------------------------------------------------------------------


def count_agent_runs(db_path: Path, *, include_running: bool = False) -> int:
    """Return ``COUNT(*)`` of ``agent_runs`` — the opportunity denominator.

    By default rows with ``status = 'running'`` are EXCLUDED: a still-running
    agent has not yet had the chance to emit a Handoff/Status block, so it is
    not a fair opportunity.  Rows with a NULL status are counted (they are not
    'running').

    This count is an UPPER BOUND on the true number of Handoff/Status
    opportunities (not every run is a chained agent that owes a Handoff), so the
    ``violation_rate`` derived downstream is a conservative LOWER BOUND.

    Returns ``0`` on any error (observer posture).
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect_ro(db_path)
        if include_running:
            row = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM agent_runs "
                "WHERE status IS NULL OR status <> 'running'"
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except (sqlite3.Error, ValueError, TypeError):
        return 0
    finally:
        if conn is not None:
            conn.close()


def read_protocol_violations(db_path: Path) -> List[ProtocolViolationRow]:
    """Read all ``agent_protocol_violations`` rows as normalised records.

    Fixed SQL, ordered by ``id`` for deterministic output.  Returns ``[]`` on
    any error (observer posture).
    """
    conn: Optional[sqlite3.Connection] = None
    out: List[ProtocolViolationRow] = []
    try:
        conn = _connect_ro(db_path)
        cursor = conn.execute(
            "SELECT violation, pattern, agent_type, timestamp, raw_excerpt, "
            "session_id FROM agent_protocol_violations ORDER BY id"
        )
        for r in cursor:
            out.append(
                ProtocolViolationRow(
                    violation=r[0] or "",
                    pattern=r[1] or "",
                    agent_type=r[2] if r[2] else None,
                    timestamp=r[3] or "",
                    raw_excerpt=r[4] or "",
                    session_id=r[5] or "",
                )
            )
        return out
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _sanitize_excerpt(text: str) -> str:
    """Collapse embedded home paths and current-home prefix → ``~/``.

    ``evidence._sanitize_path`` only collapses the CURRENT user's home when it
    is the WHOLE string / its prefix; ``raw_excerpt`` can embed an arbitrary
    ``/Users/<other>/`` path mid-string, so we collapse those first with a
    regex before delegating to ``_sanitize_path``.
    """
    if not text:
        return text
    collapsed = _EMBEDDED_HOME_PATH_RE.sub("~", text)
    return _sanitize_path(collapsed)


def _synthesize_action(row: ProtocolViolationRow) -> ToolAction:
    """Build a ``ToolAction`` evidence atom from a protocol-violation row.

    The cast.db ledger row is not a tool invocation, so ``tool_name`` and
    ``command`` are empty and ``transcript_rel`` is the literal marker
    ``"cast.db"`` (a clear non-path source).  ``input_summary`` carries the
    structured pattern plus a SANITISED, truncated excerpt (sanitise FIRST, then
    truncate, per the privacy invariant).
    """
    summary = _sanitize_excerpt(f"[{row.pattern}] {row.raw_excerpt}")[:_INPUT_SUMMARY_MAX]
    return ToolAction(
        session_id=row.session_id or "",
        timestamp=row.timestamp or "",
        tool_name="",
        command="",
        input_summary=summary,
        is_sidechain=True,
        agent_type=row.agent_type or None,
        transcript_rel="cast.db",
        cwd_rel="",
        git_branch=None,
    )


def _output_shape_candidates(
    classifications: List[Classification],
    rules_by_id: Dict[str, Rule],
) -> List[Rule]:
    """Resolve the output_shape classifications to their ``Rule`` objects.

    ONLY ``CATEGORY_OUTPUT_SHAPE`` classifications are eligible — safety_keep,
    convertible, judgment_keep, and non_directive rules are NEVER candidates.
    """
    candidates: List[Rule] = []
    for cl in classifications:
        if cl.category != CATEGORY_OUTPUT_SHAPE:
            continue
        rule = rules_by_id.get(cl.rule_id)
        if rule is not None:
            candidates.append(rule)
    return candidates


def _match_rule_for_signal(signal: Dict, candidates: List[Rule]) -> Optional[Rule]:
    """Return the single output_shape rule that matches ALL of ``signal`` phrases.

    A candidate matches when its ``normalized_text`` (lowercased) contains every
    phrase in ``signal["rule_phrases"]``.

    - Zero matches → ``None`` (the signal's violations stay unmapped).
    - Exactly one match → that rule.
    - More than one match → ``None`` (AMBIGUITY GUARD).  We refuse to
      force-attribute the violations to a sort-order-chosen rule, because a
      confident-but-wrong attribution is exactly the failure mode the module's
      honest-under-coverage posture promises to avoid.  The rows stay unmapped
      (surfaced as under-coverage) rather than being silently mis-assigned.
    """
    phrases = signal["rule_phrases"]
    matches = [
        rule
        for rule in candidates
        if all(phrase in rule.normalized_text.lower() for phrase in phrases)
    ]
    if len(matches) != 1:
        return None
    return matches[0]


# ---------------------------------------------------------------------------
# Public API — core
# ---------------------------------------------------------------------------


def find_output_shape_violations(
    classifications: List[Classification],
    rules_by_id: Dict[str, Rule],
    *,
    db_path: Path,
    include_running: bool = False,
) -> CastDbResult:
    """Reconstruct output_shape rule violations from the cast.db ledger.

    For each protocol-signal (Handoff, Status) that maps to exactly one of the
    user's own output_shape prose rules, emit a ``RuleViolation`` whose
    ``violations`` are synthesised ``ToolAction`` evidence atoms (one per ledger
    row) and whose ``opportunity_count`` is the ``agent_runs`` denominator.

    Signals with no matching prose rule, and ledger rows belonging to no signal
    at all (e.g. ``prose_dispatch``), are reported in ``unmapped_by_signal`` and
    are NEVER force-attributed to an unrelated rule (honest under-coverage).

    Observer posture: ANY error returns an EMPTY ``CastDbResult`` (never raises).
    """
    db_path_rel = _collapse_home(db_path)

    try:
        availability = castdb_available(db_path)
        if not availability.available:
            return CastDbResult(
                rule_violations=[],
                total_violations_read=0,
                mapped_violations=0,
                unmapped_by_signal={},
                agent_runs_denominator=0,
                db_path_rel=availability.db_path_rel,
            )

        denominator = count_agent_runs(db_path, include_running=include_running)
        rows = read_protocol_violations(db_path)
        candidates = _output_shape_candidates(classifications, rules_by_id)

        # Group rows by their cast.db violation value (preserves id order).
        rows_by_value: Dict[str, List[ProtocolViolationRow]] = {}
        for row in rows:
            rows_by_value.setdefault(row.violation, []).append(row)

        rule_violations: List[RuleViolation] = []
        mapped_values: set = set()

        for signal in _PROTOCOL_SIGNALS:
            rule = _match_rule_for_signal(signal, candidates)
            if rule is None:
                # No prose rule states this; its rows stay unmapped.
                continue

            sig_rows: List[ProtocolViolationRow] = []
            for value in sorted(signal["violations"]):
                sig_rows.extend(rows_by_value.get(value, []))
                mapped_values.add(value)

            actions = [_synthesize_action(r) for r in sig_rows]
            rule_violations.append(
                RuleViolation(
                    rule_id=rule.rule_id,
                    predicate={"hook": _SUBAGENT_STOP_HOOK, "signal": signal["key"]},
                    convert_kind=CONVERT_OUTPUT_SHAPE,
                    violations=actions,
                    violation_count=len(actions),
                    opportunity_count=denominator,
                    excluded_by_exception=0,
                )
            )

        unmapped_by_signal: Dict[str, int] = {
            value: len(group)
            for value, group in rows_by_value.items()
            if value not in mapped_values
        }
        mapped_violations = sum(
            len(rows_by_value[v]) for v in mapped_values if v in rows_by_value
        )

        return CastDbResult(
            rule_violations=rule_violations,
            total_violations_read=len(rows),
            mapped_violations=mapped_violations,
            unmapped_by_signal=unmapped_by_signal,
            agent_runs_denominator=denominator,
            db_path_rel=db_path_rel,
        )
    except Exception:
        # Observer posture: never crash the caller.
        return CastDbResult(
            rule_violations=[],
            total_violations_read=0,
            mapped_violations=0,
            unmapped_by_signal={},
            agent_runs_denominator=0,
            db_path_rel=db_path_rel,
        )
