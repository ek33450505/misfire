# misfire Proof Directory

This directory contains byte-reproducible proofs for the static audit (Phase 1)
and the evidence ranking pipeline (Phase 2).

---

## Phase 1 — Static Audit Proof

### Reproduce in one command

From the repo root:

```sh
misfire audit proof/sample-config --json
```

The output must match `proof/expected_audit.json` byte-for-byte.

The automated test `tests/test_proof.py` asserts this equality on every run.

### What the fixture exercises

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

---

## Phase 2 — Evidence Ranking Proof

### Reproduce in one command

From the repo root:

```sh
misfire rank proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects --json
```

The output must match `proof/expected_rank.json` byte-for-byte.

The automated test `tests/test_proof_rank.py` asserts this equality on every run.

To drill into the top-ranked rule's violations:

```sh
misfire evidence proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects
```

### What the fixture exercises

`proof/evidence-sample/config/CLAUDE.md` is a two-rule config with:
- A `never_command` rule for `git commit` with a clearly-stated escape hatch
  (`CAST_COMMIT_AGENT=1`) — demonstrating honest exception exclusion.
- A `never_command` rule for `git push` with no escape hatch.

`proof/evidence-sample/projects/proj-sample/sess-evidence-0001.jsonl` is a
35-action synthetic transcript with no PII or absolute paths:

| Action type                        | Count | Rule matched         | Outcome          |
|------------------------------------|-------|----------------------|------------------|
| `git commit -m '...'`              | 5     | never git commit     | violation        |
| `CAST_COMMIT_AGENT=1 git commit`   | 2     | never git commit     | sanctioned (excl)|
| `git push origin ...`              | 3     | never git push       | violation        |
| Other Bash commands                | 25    | —                    | opportunity only |

With 35 total Bash actions (opportunity floor met at default `min_support=30`):

| Rule        | violations | excluded | opportunity | rate    | recommendation    |
|-------------|------------|----------|-------------|---------|-------------------|
| git commit  | 5          | 2        | 35          | 14.3%   | enforce_candidate |
| git push    | 3          | 0        | 35          | 8.6%    | enforce_candidate |

### Portability guarantee

`proof/expected_rank.json` contains ZERO machine-specific paths:
- No `/Users/`, `/home/`, `/private/`, or `Projects/` substrings.
- `transcript_rel` fields (which contain the home-collapsed fixture path)
  do NOT appear in rank output — only in `evidence` output.
- `config_root` is the relative string `"proof/evidence-sample/config"`.

---

## No database required

Both proofs are purely static or transcript-based — they read markdown and JSONL
files and apply deterministic logic. No `cast.db`, no Anthropic SDK, no network
calls. Both proofs run in CI with stdlib-only dependencies.
