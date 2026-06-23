---
description: Audit which of your CLAUDE.md rules your agents actually ignore (read-only observer)
---

Run `misfire` over the user's Claude Code configuration and report which prose rules
their agents demonstrably ignore, ranked from run history. misfire is a **deterministic,
zero-LLM observer** — it never deletes a rule, never auto-applies a change, and never
writes `settings.json`. It only reports findings and scaffolds hooks for review.

Optional config root to audit: $ARGUMENTS  (defaults to `~/.claude`)

Steps:

1. The `misfire` CLI must be installed — `pip install misfire` (published on PyPI). A
   Homebrew formula is also available once the public tap is published:
   `brew install ek33450505/misfire/misfire`.
2. Run `misfire audit $ARGUMENTS` via Bash for the static findings (stale paths, token
   rent, conflicts, load-fidelity), then `misfire rank $ARGUMENTS` for the
   evidence-ranked violations reconstructed from run history.
3. Summarize: the audit findings first, then the ranked `enforce_candidate` rules (the
   prose rules being ignored) with their violation counts and rates. Call out explicitly
   that any rule in the `observed_no_violations` bucket is **not** a deletion candidate —
   it may simply be obeyed, enforced another way, or never triggered.
4. If the user wants to enforce a top candidate, run `misfire convert $ARGUMENTS --top`
   and show the scaffolded `PreToolUse`/`PostToolUse` hook plus its `settings.json`
   snippet. Do **not** install the hook or write `settings.json` — converting and
   installing is the human's call. misfire observes and recommends; it never acts.
