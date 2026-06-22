"""test_transcript_adapter.py — Tests for the Phase 2 transcript adapter.

Uses committed synthetic fixtures under ``tests/fixtures/transcripts/``
(sanitised paths only — no real ``/Users/<name>/`` paths, no PII).

The real ``~/.claude/projects`` is NEVER accessed by these tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from misfire.adapters.transcript import iter_tool_actions
from misfire.evidence import ToolAction, _sanitize_path

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "transcripts"

# Real home prefix — used for PII assertions
_HOME = str(Path.home())
_HOME_PREFIX = _HOME + "/"

# Regex that matches a raw /Users/<name>/ path (we reject this in outputs)
_RAW_USERS_RE = re.compile(r"/Users/[^/]+/")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect(projects_dir: Path, **kwargs) -> list[ToolAction]:
    return list(iter_tool_actions(projects_dir, **kwargs))


# ---------------------------------------------------------------------------
# Happy-path: correct count and tool names
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_total_count_with_subagents(self) -> None:
        """3 tool_use in main session + 1 in subagent = 4 total."""
        actions = _collect(FIXTURES_DIR)
        assert len(actions) == 4

    def test_total_count_without_subagents(self) -> None:
        """Only main-session tool_use blocks (3)."""
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        assert len(actions) == 3

    def test_tool_names_main_session(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        names = [a.tool_name for a in actions]
        assert names == ["Bash", "Read", "Write"]

    def test_bash_command_extracted(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        bash_action = actions[0]
        assert bash_action.tool_name == "Bash"
        assert bash_action.command == "git commit -m 'add feature'"

    def test_read_input_summary(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        read_action = actions[1]
        assert read_action.tool_name == "Read"
        assert read_action.command == ""
        assert "foo.py" in read_action.input_summary

    def test_write_input_summary(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        write_action = actions[2]
        assert write_action.tool_name == "Write"
        assert "bar.py" in write_action.input_summary

    def test_git_branch_extracted(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        assert actions[0].git_branch == "feature/test"

    def test_timestamp_extracted(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        assert actions[0].timestamp == "2026-06-22T10:00:00.000Z"

    def test_session_id_extracted(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        assert actions[0].session_id == "session-aaaa"


# ---------------------------------------------------------------------------
# Subagent handling
# ---------------------------------------------------------------------------


class TestSubagentHandling:
    def test_subagent_is_sidechain_true(self) -> None:
        actions = _collect(FIXTURES_DIR)
        subagent_action = actions[-1]  # subagent file yields last (sorted order)
        assert subagent_action.is_sidechain is True

    def test_main_session_is_sidechain_false(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        for a in actions:
            assert a.is_sidechain is False

    def test_subagent_agent_type(self) -> None:
        actions = _collect(FIXTURES_DIR)
        subagent_actions = [a for a in actions if a.is_sidechain]
        assert len(subagent_actions) == 1
        assert subagent_actions[0].agent_type == "code-reviewer"

    def test_main_session_agent_type_none(self) -> None:
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        for a in actions:
            assert a.agent_type is None

    def test_subagent_tool_name(self) -> None:
        actions = _collect(FIXTURES_DIR)
        subagent_actions = [a for a in actions if a.is_sidechain]
        assert subagent_actions[0].tool_name == "Bash"
        assert subagent_actions[0].command == "echo done"


# ---------------------------------------------------------------------------
# Malformed-line robustness
# ---------------------------------------------------------------------------


class TestMalformedLines:
    def test_malformed_lines_skipped(self) -> None:
        """The fixture has one malformed line; it must be silently skipped."""
        actions = _collect(FIXTURES_DIR)
        # We get the expected 4 actions despite the malformed line
        assert len(actions) == 4

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        """An empty projects_dir yields no actions."""
        actions = _collect(tmp_path)
        assert actions == []

    def test_empty_jsonl_file(self, tmp_path: Path) -> None:
        """A slug dir containing only an empty .jsonl file yields no actions."""
        slug = tmp_path / "slug-empty"
        slug.mkdir()
        (slug / "session-empty.jsonl").write_text("")
        actions = _collect(tmp_path)
        assert actions == []

    def test_non_assistant_records_skipped(self, tmp_path: Path) -> None:
        """Only assistant records with tool_use blocks are yielded."""
        slug = tmp_path / "slug-x"
        slug.mkdir()
        (slug / "s.jsonl").write_text(
            '{"type": "user", "message": {"content": []}, "sessionId": "x", "timestamp": "2026-01-01T00:00:00Z"}\n'
            '{"type": "system", "message": {}, "sessionId": "x", "timestamp": "2026-01-01T00:00:00Z"}\n'
        )
        assert _collect(tmp_path) == []

    def test_assistant_without_tool_use_skipped(self, tmp_path: Path) -> None:
        slug = tmp_path / "slug-y"
        slug.mkdir()
        (slug / "s.jsonl").write_text(
            '{"type": "assistant", "isSidechain": false, "message": {"content": [{"type": "text", "text": "hello"}]}, "sessionId": "y", "timestamp": "2026-01-01T00:00:00Z"}\n'
        )
        assert _collect(tmp_path) == []


# ---------------------------------------------------------------------------
# PII / privacy: transcript_rel and cwd_rel must never contain /Users/<name>/
# ---------------------------------------------------------------------------


class TestPrivacySanitisation:
    def _check_no_raw_users(self, s: str, field: str) -> None:
        assert not _RAW_USERS_RE.search(s), (
            f"PII leak in {field}: contains raw /Users/<name>/ prefix: {s!r}"
        )

    def test_transcript_rel_no_raw_home(self) -> None:
        """transcript_rel must not contain /Users/<name>/."""
        actions = _collect(FIXTURES_DIR)
        for a in actions:
            self._check_no_raw_users(a.transcript_rel, "transcript_rel")

    def test_cwd_rel_no_raw_home(self) -> None:
        """cwd_rel must not contain /Users/<name>/."""
        actions = _collect(FIXTURES_DIR)
        for a in actions:
            self._check_no_raw_users(a.cwd_rel, "cwd_rel")

    def test_input_summary_no_raw_home(self) -> None:
        """input_summary (file_path fields) must not contain /Users/<name>/."""
        actions = _collect(FIXTURES_DIR)
        for a in actions:
            self._check_no_raw_users(a.input_summary, "input_summary")

    def test_real_home_path_is_sanitised(self, tmp_path: Path) -> None:
        """A record with a real /Users/<name>/ cwd path is home-collapsed."""
        real_home = str(Path.home())
        slug = tmp_path / "slug-real"
        slug.mkdir()
        record = {
            "type": "assistant",
            "isSidechain": False,
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pwd"}}]},
            "sessionId": "real-session",
            "timestamp": "2026-06-22T00:00:00Z",
            "cwd": real_home + "/Projects/myrepo",
            "gitBranch": "main",
        }
        import json as _json
        (slug / "s.jsonl").write_text(_json.dumps(record) + "\n")
        actions = _collect(tmp_path)
        assert len(actions) == 1
        self._check_no_raw_users(actions[0].cwd_rel, "cwd_rel")
        assert actions[0].cwd_rel == "~/Projects/myrepo"


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    def test_in_file_order_preserved(self) -> None:
        """Actions within a file are yielded in the order they appear."""
        actions = _collect(FIXTURES_DIR, include_subagents=False)
        timestamps = [a.timestamp for a in actions]
        assert timestamps == sorted(timestamps)

    def test_main_session_before_subagent(self) -> None:
        """Main-session actions come before subagent actions (sorted slug/file order)."""
        actions = _collect(FIXTURES_DIR)
        main_actions = [a for a in actions if not a.is_sidechain]
        sub_actions = [a for a in actions if a.is_sidechain]
        if main_actions and sub_actions:
            # All main actions appear before the first subagent action
            last_main_idx = max(i for i, a in enumerate(actions) if not a.is_sidechain)
            first_sub_idx = min(i for i, a in enumerate(actions) if a.is_sidechain)
            assert last_main_idx < first_sub_idx

    def test_sorted_across_two_slug_dirs(self, tmp_path: Path) -> None:
        """Actions from alphabetically earlier slug dirs appear first."""
        import json as _json

        def _make_record(cmd: str) -> str:
            return _json.dumps({
                "type": "assistant",
                "isSidechain": False,
                "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": cmd}}]},
                "sessionId": "s",
                "timestamp": "2026-06-22T00:00:00Z",
                "cwd": "~/tmp",
                "gitBranch": "main",
            })

        slug_a = tmp_path / "aaa-project"
        slug_a.mkdir()
        (slug_a / "session.jsonl").write_text(_make_record("echo aaa") + "\n")

        slug_z = tmp_path / "zzz-project"
        slug_z.mkdir()
        (slug_z / "session.jsonl").write_text(_make_record("echo zzz") + "\n")

        actions = _collect(tmp_path)
        assert len(actions) == 2
        assert actions[0].command == "echo aaa"
        assert actions[1].command == "echo zzz"


# ---------------------------------------------------------------------------
# _sanitize_path unit tests
# ---------------------------------------------------------------------------


class TestSanitizePath:
    def test_empty_string(self) -> None:
        assert _sanitize_path("") == ""

    def test_already_collapsed(self) -> None:
        assert _sanitize_path("~/Projects/foo") == "~/Projects/foo"

    def test_real_home_collapsed(self) -> None:
        real = str(Path.home()) + "/Projects/foo"
        result = _sanitize_path(real)
        assert result == "~/Projects/foo"
        assert not _RAW_USERS_RE.search(result)

    def test_non_home_path_unchanged(self) -> None:
        assert _sanitize_path("/tmp/foo") == "/tmp/foo"

    def test_home_itself(self) -> None:
        result = _sanitize_path(str(Path.home()))
        assert result == "~"
