# Contributing to misfire

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

Thanks for your interest in contributing. misfire is an open-source, deterministic,
local-first CLI + Python library for auditing Claude Code instruction files. Before
diving in, read the [README](README.md) for the ecosystem framing and the
[architecture](docs/architecture.md) for how the pieces fit together. misfire is an
**observer / recommender** — it prints ranked violation lists, audit findings, and hook
scaffolds for you to review. It never auto-deletes a rule, never auto-applies a change,
and never writes `settings.json`. Keep that posture in mind for every contribution.

This guide covers setup, tests, project layout, and the conventions reviewers enforce
before merge.

---

## Prerequisites

- **Python 3.9+** — the deterministic core is stdlib-only. CI runs the matrix on 3.9,
  3.10, 3.11, and 3.12.
- **[uv](https://github.com/astral-sh/uv)** (recommended) or **pip** — for the editable
  install and running tests.
- **[bats-core](https://github.com/bats-core/bats-core)** — required only for the BATS
  end-to-end hook test (`tests/bats/`). On macOS: `brew install bats-core`.

---

## Setup

Clone and install the package in editable mode with the dev extra:

```sh
git clone https://github.com/ek33450505/misfire.git
cd misfire
pip install -e ".[dev]"
```

The `dev` extra pulls in `pytest` — and nothing else. The core has **zero runtime
dependencies** (`dependencies = []` in `pyproject.toml`); do not add any (see
[Conventions](#conventions)).

If you prefer uv, the editable install is:

```sh
uv pip install -e ".[dev]"
```

---

## Running tests

### pytest (430 tests)

```sh
pytest
```

If you do not have a local pytest (or want a hermetic, version-pinned run), this uv
one-liner installs everything ephemerally and runs the suite against Python 3.12:

```sh
uv run --with pytest --with-editable . --python 3.12 pytest tests/ -q
```

### BATS (5 tests — the end-to-end hook proof)

```sh
bats tests/bats/
```

The single BATS file (`tests/bats/convert_blocks_commit.bats`) installs a misfire-emitted
PreToolUse hook into an **isolated temp HOME** and drives it with the real Claude Code
stdin contract: it asserts the hook DENIES `git commit`, ALLOWS `git status`, IGNORES a
quoted `echo "git commit"` (no naive-substring false positive), and HONORS the escape
hatch. It never touches your real `$HOME`.

All 430 pytest tests + 5 BATS tests are expected green before a PR merges code that
touches them.

---

## Project layout

```
src/misfire/
├── parse.py             Static parse of the precedence chain, @imports, .claude/rules/*.md
├── classify.py          Sort each rule into 5 categories; assign convert_kind + predicate
├── audit.py             Deterministic static audit (4 finding kinds) — table-stakes
├── evidence.py          Per-rule violation evidence assembly
├── match.py             Structural command matcher (strips quoted spans → kills FPs)
├── rank.py              Violation/opportunity counts, confidence + min-support ranking
├── scaffold.py          Deterministic, templated hook scaffolder (zero-LLM)
├── cli.py               The 4 commands: audit, rank, evidence, convert
└── adapters/
    ├── transcript.py    PRIMARY: portable Claude Code transcript JSONL reader (no DB)
    └── cast_db.py       OPTIONAL: flag-gated, read-only cast.db substrate (CAST users)

tests/                   pytest suite (incl. the byte-reproducible proof tests)
└── bats/                BATS end-to-end hook proof (convert_blocks_commit.bats)
proof/                   Committed fixtures + golden JSON for the byte-reproducible proofs
docs/                    Documentation (architecture, usage, adapters, framing, …)
```

The core is **~5,990 lines across 12 Python modules** (the modules above plus the two
package `__init__.py` files). Stdlib-only.

---

## Conventions

These are the rules reviewers hold the line on.

- **Stdlib-only core — hard rule.** The deterministic core imports nothing beyond the
  Python standard library. `dependencies = []` stays empty. The only dev dependency is
  `pytest`. Do not add a runtime dependency; if you think you need one, open a Discussion
  first.
- **Deterministic / zero-LLM.** The core never calls an LLM. Output is reproducible. The
  only LLM use anywhere is a future opt-in local-Ollama ablation behind an explicit flag
  (Phase 4, not yet shipped) — it does not touch the deterministic path.
- **Observer posture.** misfire prints recommendations, ranked lists, and hook scaffolds.
  It never writes `settings.json`, never auto-deletes a rule, and never auto-applies a
  change. A rule with zero observed violations yields **no** deletion/convert signal — a
  non-trigger is never deletion evidence. Keep this guarantee intact in any code you add.
- **`--json` must stay byte-stable.** Every command supports `--json`, emitted with
  `sort_keys` so output is deterministic and byte-stable. Any change that alters JSON
  output must keep it sorted and stable — and must regenerate the affected golden files
  (below).
- **Privacy: output is path-sanitized.** All text and JSON output collapses
  `/Users/<name>/` and `/home/<name>/` to `~/`, and makes paths under `config_root`
  relative. No usernames leak. New output must preserve this — never emit a raw absolute
  home path.
- **Every new command or flag gets a test.** No exceptions. Cover happy path, edge cases,
  and error states. Note the observer exit-code contract: all commands exit 0 regardless
  of findings; the only non-zero exit is `evidence` / `convert` with an explicit
  `--rule PREFIX` that matches no rule (exit 1).
- **Proofs are byte-reproducible — regenerate goldens deliberately.** The `proof/`
  fixtures back golden-file tests (`tests/test_proof.py`, `tests/test_proof_rank.py`,
  `tests/test_proof_convert.py`, `tests/test_proof_castdb.py`). When a deliberate output
  change requires updating a golden, **regenerate it from the tool** — never hand-edit a
  golden JSON file. A hand-edited golden defeats the entire proof.

---

## Docs PRs

Documentation contributions must obey the framing guardrails in
[docs/framing.md](docs/framing.md). Reviewers enforce these before merge:

- **No phantom stats, ever.** Cite only numbers backed by an Anthropic doc, a committed
  fixture, or a citable preprint *with caveats*. If you cannot point to one of those, cut
  the number.
- **Cite Gloaguen et al. only with caveats** — unreviewed preprint, near the noise floor,
  Python-skewed, contradicted on cost by Lulla et al. Never present one cost number as
  consensus.
- **Hold the observer posture and the wedge framing.** misfire measures violation, not
  redundancy; it cannot tell a silently-obeyed safety rule from a never-needed one. Every
  doc making product claims must point to (or restate) Assumptions & Limitations.

When in doubt, read `docs/framing.md` in full — it is authoritative and reviewers will
reject any violation.

---

## Pull request checklist

Before opening a PR, confirm:

- [ ] **Tests pass for touched code** — `pytest` (and `bats tests/bats/` if you touched
      the hook scaffold or the BATS proof).
- [ ] **No machine-specific absolute paths** in code, tests, fixtures, or output — output
      stays path-sanitized; tests use temp dirs.
- [ ] **No phantom stats** in any docs or comments — every number traces to an Anthropic
      doc, a committed fixture, or a caveated preprint.
- [ ] **Golden files regenerated, not hand-edited** — if output changed, the affected
      `proof/` goldens were regenerated from the tool.
- [ ] **No new runtime dependency** — the core stays stdlib-only.
- [ ] **New command/flag has a test** and preserves the observer exit-code contract.
- [ ] **Docs changes obey `docs/framing.md`.**

---

## Good first issues & Discussions

New here? Look for issues labeled **`good first issue`** on the
[issue tracker](https://github.com/ek33450505/misfire/issues). Strong starting points:
adding a fixture-backed test for an existing command, tightening an edge case in
`match.py`'s quoted-span stripping, or improving a docs page against the framing
guardrails.

Have a design question, want to propose a new finding kind or adapter, or unsure whether
a change fits the observer posture? Open a thread in
[Discussions](https://github.com/ek33450505/misfire/discussions) before writing code —
especially for anything that would add a dependency or change the determinism guarantees.

Maintainer contact: edward.kubiak.dev@gmail.com.

By contributing, you agree your contributions are licensed under the project's
[Apache-2.0 license](LICENSE).
