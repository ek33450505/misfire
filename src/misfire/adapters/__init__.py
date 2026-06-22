"""adapters — substrate readers for the misfire evidence engine.

Each adapter converts a raw evidence source into a stream of
``ToolAction`` objects that the evidence engine can match against
rule predicates.

Available adapters
------------------
- ``transcript``: reads Claude Code transcript JSONL (portable; works
  without cast.db for any Claude Code user).
- ``cast_db``: (Phase 2+) reads cast.db ``agent_protocol_violations`` /
  ``quality_gates`` / ``agent_runs`` for CAST-power-user workflows.
"""
