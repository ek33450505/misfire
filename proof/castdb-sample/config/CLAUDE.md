# CAST Agent Config (cast.db Sample Fixture)

> Not real config. Authored to exercise misfire rank with the OPTIONAL cast.db adapter.

## Handoff

MANDATORY: Every agent in a multi-agent chain MUST include a Handoff block before the Work Log. The Handoff block lists files_changed, status, and blockers.

## Status

MANDATORY: All agents end with Status: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT.

## Commits

MANDATORY: Never use raw git commit directly — always route through the commit agent.
