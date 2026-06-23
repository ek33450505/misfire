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

## Phase 2 (Optional) — cast.db Adapter Proof

This proof exercises the OPTIONAL, flag-gated `--cast-db` substrate (CAST power
users only). misfire works fully without it; this proves that when engaged, it
reconstructs output-shape (Handoff / Status) rule violations honestly.

### Reproduce in one command

From the repo root, first (re)build the synthetic DB from the committed SQL, then run:

```sh
rm -f proof/castdb-sample/generated.db   # idempotent rebuild — seed.sql is not re-runnable over an existing DB
python3 -c "import sqlite3; c=sqlite3.connect('proof/castdb-sample/generated.db'); \
    c.executescript(open('proof/castdb-sample/seed.sql').read()); c.commit(); c.close()"
misfire rank proof/castdb-sample/config \
    --cast-db proof/castdb-sample/generated.db \
    --projects-dir "$(mktemp -d)" --json
```

The output must match `proof/castdb-sample/expected_castdb_rank.json` byte-for-byte.
`tests/test_proof_castdb.py` asserts this on every run.

### What the fixture exercises

`proof/castdb-sample/seed.sql` builds a synthetic `cast.db` (the three real
tables, no PII) with 6 `agent_protocol_violations` rows + 40 non-running
`agent_runs` (+ 2 running, excluded):

| cast.db `violation`        | rows | maps to prose rule                  | outcome           |
|----------------------------|------|-------------------------------------|-------------------|
| `handoff_schema_violation` | 3    | `## Handoff block …`                | enforce_candidate |
| `missing_formality`        | 2    | `… end with Status: DONE \| …`      | enforce_candidate |
| `prose_dispatch`           | 1    | (no clean rule)                     | UNMAPPED (honest) |

The `agent_runs` count (40, running excluded) is the opportunity denominator —
an UPPER BOUND, so the reported violation rate is a conservative LOWER BOUND.

### Portability + binary-DB note

`generated.db` is gitignored and rebuilt from `seed.sql` on every run — only the
SQL text is committed, never the binary DB. `config_root` and the
`cast_db.db_path_rel` are echoed as the relative strings supplied, so the
committed fixture contains ZERO `/Users/`, `/home/`, `/private/`, or `Projects/`
substrings.

---

## Phase 3 — Convert-to-hook Proof

This proves the wedge end-to-end: from the **evidence** for a rule your agents
demonstrably ignore, misfire emits a deterministic hook that actually enforces
it — reusing the same portable `evidence-sample` fixture (no DB, no PII).

### Reproduce in one command

From the repo root:

```sh
misfire convert proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects --top --json
```

The output must match `proof/expected_convert.json` byte-for-byte.
`tests/test_proof_convert.py` asserts this on every run.

### What the fixture exercises

`misfire convert --top` runs the full pipeline (parse → classify → rank) and
selects the **top evidence-grounded `enforce_candidate`** — here the
`never git commit` rule (5 violations / 35 opportunities). For it, misfire emits:

- a self-contained `PreToolUse` hook script that **embeds misfire's own
  structural matcher** (so a quoted `echo "git commit"` is NOT blocked — no
  naive-substring false positive),
- the hook **honors the rule's escape hatch** (`CAST_COMMIT_AGENT=1`), matching
  misfire's violation accounting,
- the `settings.json` registration snippet (using `${CLAUDE_PROJECT_DIR}`).

misfire **never writes `settings.json`** — the scaffold is printed for review.

### Honesty guard (observer posture)

- `misfire convert --rule <safety|judgment rule>` → **KEEP** verdict, no hook.
- `misfire convert --rule <convertible rule with 0 observed violations>` →
  `recommended: false`, shown for reference only.
- `misfire convert --top` with no qualifying rule → **nothing to convert**.

(Covered by `tests/test_cli_convert.py` and `tests/test_scaffold.py`.)

### Strongest proof — the installed hook blocks `git commit` (BATS)

`tests/bats/convert_blocks_commit.bats` installs the emitted hook (executable,
with its shebang) into an **isolated temp HOME** and drives it with the **real
PreToolUse stdin contract**. It asserts the installed hook denies `git commit`,
allows `git status`, ignores a quoted occurrence, and honors the escape hatch:

```sh
bats tests/bats/convert_blocks_commit.bats
```

---

## No database required (core)

The Phase 1 and Phase 2 (transcript) proofs are purely static or
transcript-based — they read markdown and JSONL files and apply deterministic
logic. No `cast.db`, no Anthropic SDK, no network calls. The cast.db proof above
is the ONLY proof that touches a database, and that database is synthetic,
local, and opened strictly read-only. All proofs run in CI with stdlib-only
dependencies.
