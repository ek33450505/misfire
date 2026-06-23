<!-- Thanks for contributing to misfire. Keep changes scoped; see CONTRIBUTING.md. -->

## What & why

<!-- One or two sentences: what this changes and the motivation. Link any issue (#123). -->

## Type

- [ ] Bug fix
- [ ] New feature (command, flag, convertible rule kind, adapter)
- [ ] Docs
- [ ] Refactor / internal

## Checklist

- [ ] Tests pass for touched code (`pytest`, and `bats tests/bats/` if a hook/CLI contract changed)
- [ ] New command / flag / behavior has a test
- [ ] `--json` output stays deterministic (`sort_keys`, byte-stable); golden files in `proof/` were **regenerated**, not hand-edited
- [ ] Core stays **stdlib-only** (no new runtime dependencies) and **zero-LLM**
- [ ] Observer posture preserved — nothing writes `settings.json`, deletes a rule, or auto-applies a change
- [ ] No machine-specific absolute paths (`/Users/...`) in code, tests, or output
- [ ] Docs changes obey the framing guardrails in [`docs/framing.md`](../docs/framing.md) — no phantom stats; sources cited with their caveats

## Notes for reviewers

<!-- Anything that needs a closer look, trade-offs, or follow-ups. -->
