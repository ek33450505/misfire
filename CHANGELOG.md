# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-06-23

First public release. misfire is a deterministic, local-first, stdlib-only CLI and
Python library that tells you which of your existing Claude Code prose rules your agents
demonstrably ignore — ranked from your own run history — and scaffolds a deterministic
hook for the violated convertible subset, keeping safety and judgment rules as prose. It
is an observer/recommender: it never auto-deletes a rule, never auto-applies a change,
and never writes `settings.json`.

### Added

- **`misfire audit`** — static, zero-LLM audit of your instruction files
  (`CLAUDE.md`, `.claude/rules/*.md`, `@imports`): `stale_path`, `token_rent`,
  `conflict`, and `load_fidelity` findings, plus a five-category rule classification.
- **`misfire rank`** — reconstructs rule violations from your run history and ranks the
  machine-checkable rules by observed violation rate, with confidence labels and a
  minimum-support floor. A rule with zero observed violations is never a deletion signal.
- **`misfire evidence`** — per-rule drill-down into the actual tool actions that violated
  a rule.
- **`misfire convert`** — deterministic, templated hook scaffolder for the violated
  convertible subset. The generated `PreToolUse`/`PostToolUse` hook embeds misfire's own
  structural command matcher (so a quoted occurrence is not a false positive) and honors
  the rule's escape hatch. Surface-only: prints the hook plus a `settings.json` snippet
  for review and writes nothing.
- **Portable transcript adapter** (default, no database) and an **optional, read-only
  `--cast-db` adapter** for CAST power-users.
- **KEEP / ELEVATE / ENFORCE** recommendation ladder over the convertibility taxonomy.
- Every command supports a deterministic, byte-stable `--json` mode; all output is
  machine-path sanitized.
- **Packaging:** Claude Code plugin manifests (`.claude-plugin/`) with a `/misfire`
  command, a Homebrew formula, and PyPI distribution metadata.
- **Documentation:** README, usage, architecture, adapters, convertibility-taxonomy,
  proof, and the authoritative framing guardrails, plus community-health files.
- **Tests:** 430 pytest tests + 5 BATS tests; CI on Python 3.9–3.12. Byte-reproducible
  proofs for `audit`, `rank`, `convert`, and the optional cast.db substrate, plus an
  end-to-end BATS test that installs a generated hook and confirms it blocks the
  violated command.

[0.1.0]: https://github.com/ek33450505/misfire/releases/tag/v0.1.0
