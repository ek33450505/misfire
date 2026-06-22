# misfire Phase 1 — Static Audit Proof

This directory contains a byte-reproducible proof that the Phase 1 static
audit pipeline works end-to-end with no database required.

## Reproduce in one command

From the repo root:

```sh
misfire audit proof/sample-config --json
```

The output must match `proof/expected_audit.json` byte-for-byte.

The automated test `tests/test_proof.py` asserts this equality on every run.

## What the fixture exercises

`proof/sample-config/` is a small sanitised config tree (no personal data,
no real `/Users/...` paths in rules) designed to deterministically trigger
one finding of every kind the audit knows about:

| Finding kind    | Triggered by                                                      |
|-----------------|-------------------------------------------------------------------|
| `stale_path`    | `~/nonexistent-misfire-fixture/logs` in `CLAUDE.md` line 13      |
| `token_rent`    | `rules/verbose.md` exceeds 200 lines                             |
| `conflict`      | `use misfire not cat` vs `use bat not cat` — same forbidden tool |
| `load_fidelity` | `@nonexistent-import.md` in `CLAUDE.md` — broken import target   |

The fixture also spreads all five classification categories across its rules:

| Category        | Example rule                                              |
|-----------------|-----------------------------------------------------------|
| `convertible`   | `Never use raw git commit directly` (never_command)       |
| `safety_keep`   | `Avoid irreversible loss` (irreversible marker)           |
| `output_shape`  | `MUST include a Handoff block` (output-protocol rule)     |
| `judgment_keep` | `YAGNI: build only what was asked`                        |
| `non_directive` | Blockquote provenance note                                |

## No database required

The audit is purely static — it reads markdown files, extracts rules, and
applies deterministic heuristics. No `cast.db`, no Anthropic SDK, no
network calls. The proof runs in CI with stdlib-only dependencies.
