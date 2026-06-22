"""misfire — trace-grounded CLAUDE.md adherence auditor.

Deterministic, local-first, stdlib-only core. Tells you which of your existing
prose rules your agents demonstrably ignore, ranked from your own run history,
and converts only the violated convertible subset into hook scaffolds — keeping
safety rules as prose.

Observer / recommender only: never auto-deletes, never auto-applies,
never writes settings.json.
"""

__version__ = "0.0.0"
