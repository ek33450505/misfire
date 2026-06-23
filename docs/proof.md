# Proof — byte-reproducible, no database

[← Back to README](../README.md)

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

Every claim misfire makes about your rules is reproducible from committed fixtures.
This page gives the exact one-command repro, the golden file it must match
byte-for-byte, and the test that asserts that equality — for each proof. It
complements [`proof/README.md`](../proof/README.md) (the in-tree fixture notes) and
stays consistent with it.

## Guarantees

- **No database required (core).** The first three proofs (`audit`, `rank`,
  `convert`) and the BATS end-to-end read only markdown and JSONL. The single
  cast.db proof rebuilds a *synthetic* SQLite DB from committed SQL and opens it
  **strictly read-only** (`mode=ro`) — it is the ONLY proof that touches a database,
  and it never mutates one.
- **No network.** Nothing fetches. There is no Anthropic SDK call and no LLM in the
  deterministic core. (The only LLM use anywhere is an opt-in local-Ollama ablation
  that is not yet shipped and is not exercised here.)
- **Stdlib-only.** Zero runtime dependencies (`dependencies = []`). The proofs run
  with nothing but the Python standard library.
- **Runs in CI on Python 3.9–3.12.** GitHub Actions matrix on ubuntu-latest;
  installs with `pip install -e ".[dev]"` and runs `pytest`.
- **Deterministic, byte-stable output.** Every command supports `--json` with
  `sort_keys` and a stable byte layout, and all output is machine-path sanitized
  (`/Users/<name>/` and `/home/<name>/` collapse to `~/`; paths under `config_root`
  are made relative — no usernames leak). That is what makes a golden-file equality
  check possible.

## The proofs at a glance

| # | Command | Golden file | Asserting test | Touches a DB? |
|---|---------|-------------|----------------|---------------|
| 1 | `misfire audit` | `proof/expected_audit.json` | `tests/test_proof.py` | no |
| 2 | `misfire rank` | `proof/expected_rank.json` | `tests/test_proof_rank.py` | no |
| 3 | `misfire convert --top` | `proof/expected_convert.json` | `tests/test_proof_convert.py` | no |
| 4 | `misfire rank --cast-db` | `proof/castdb-sample/expected_castdb_rank.json` | `tests/test_proof_castdb.py` | yes (synthetic, read-only) |
| 5 | installed hook (end-to-end) | — (behavioral assertions) | `tests/bats/convert_blocks_commit.bats` | no |

Run everything from the repo root.

---

## 1. Static audit → `expected_audit.json`

```sh
misfire audit proof/sample-config --json
```

The output must equal `proof/expected_audit.json` byte-for-byte.
`tests/test_proof.py` asserts this on every run.

**What the fixture exercises.** `proof/sample-config/` is a small sanitized config
tree (no personal data, no real `/Users/...` paths) crafted to deterministically
trigger **exactly one finding of each of the 4 finding kinds** the audit knows about:

| Finding kind | Triggered by |
|--------------|--------------|
| `stale_path` | a `~/nonexistent-...` path referenced in `CLAUDE.md` |
| `token_rent` | a `rules/verbose.md` that exceeds 200 lines |
| `conflict` | two rules forbidding the same tool via different replacements (`use misfire not cat` vs `use bat not cat`) |
| `load_fidelity` | a broken `@nonexistent-import.md` import target |

The same fixture also spreads **all 5 classification categories** across its rules —
`convertible`, `safety_keep`, `judgment_keep`, `output_shape`, and `non_directive` —
so a single golden file pins both the audit and the classifier. The static audit is a
table-stakes feature here, not the headline; it lives under the evidence-ranking
story (see [`docs/framing.md`](framing.md)).

---

## 2. Evidence ranking → `expected_rank.json`

This is the wedge: a trace-grounded ranking of which of *your* prose rules your agents
demonstrably ignore. No database — it reads Claude Code transcript JSONL.

```sh
misfire rank proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects --json
```

The output must equal `proof/expected_rank.json` byte-for-byte.
`tests/test_proof_rank.py` asserts this on every run.

**What the fixture exercises.** `proof/evidence-sample/config/CLAUDE.md` holds two
`never_command` rules; the projects dir holds one 35-action synthetic transcript with
no PII or absolute paths:

| Rule | violations | sanctioned (excluded) | opportunities | rate | bucket |
|------|-----------:|----------------------:|--------------:|-----:|--------|
| never `git commit` | 5 | 2 | 35 | 14.3% | `enforce_candidate` |
| never `git push` | 3 | 0 | 35 | 8.6% | `enforce_candidate` |

The 2 *sanctioned* `git commit` actions carry the rule's own escape-hatch marker
(`CAST_COMMIT_AGENT=1`); misfire excludes them from the violation count, matching how
the rule itself defines an exception. Both rules clear the default support floor
(`--min-support 30`, here 35 opportunities) and land at `confidence=medium`.

The text rendering of the same run (sanitized; `<projects-dir>` stands in for the
supplied path):

```
misfire rank — proof/evidence-sample/config
Projects dir: <projects-dir>
Active rules: 2

Thresholds: min_support=30  min_violations=1

=== enforce_candidate (2) ===

  1. CLAUDE.md  [never_command]  confidence=medium
     rule_id: d84c9954a86f
     violations: 5  opportunities: 35  rate: 14.3%  excluded (sanctioned): 2
     "MANDATORY: Never use raw git commit directly — always route through the commit agent. Escape hatch …"

  2. CLAUDE.md  [never_command]  confidence=medium
     rule_id: 8fb701ad4c67
     violations: 3  opportunities: 35  rate: 8.6%
     "MANDATORY: Never dispatch git push to remote directly."

=== insufficient_evidence (0) ===
  (none)

=== observed_no_violations (0) ===
  (none)
```

To drill into the top rule's individual violations:

```sh
misfire evidence proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects
```

**Why these numbers are trustworthy.** A naive substring match for `git commit`
would also count the string where it appears as *data* — inside a grep pattern, a PR
body, or a hook-test payload. On the one rule tested first-hand, ~80% of naive matches
were exactly that kind of noise. misfire's structural matcher strips quoted spans
before joining a tool action to a rule predicate, which is why the count is 5 and not
an inflated number. Rankings are not meaningful until a rule clears the support floor;
a rule with zero observed violations yields **no** deletion or convert signal — a
non-trigger is never deletion evidence. See
[Assumptions & Limitations](../README.md#assumptions--limitations) and
[`docs/adapters.md`](adapters.md).

---

## 3. Convert-to-hook → `expected_convert.json`

This proves the second half of the wedge: from the evidence for a rule your agents
ignore, misfire emits a deterministic hook that actually enforces it — reusing the
same portable `evidence-sample` fixture (no DB, no PII).

```sh
misfire convert proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects --top --json
```

The output must equal `proof/expected_convert.json` byte-for-byte.
`tests/test_proof_convert.py` asserts this on every run.

**What the fixture exercises.** `convert --top` runs the full pipeline
(parse → classify → rank) and selects the **top evidence-grounded
`enforce_candidate`** — here the `never git commit` rule (5 violations / 35
opportunities / 14.3%). For it, misfire emits a self-contained **PreToolUse** hook
(saved as `.claude/hooks/misfire-never-command-d84c9954.py`, matcher `Bash`,
`is_skeleton: false`) that:

- **embeds misfire's own structural command matcher** via source inlining, so the
  hook does not regress to naive-substring matching — a quoted `echo "git commit"` is
  NOT blocked;
- **honors the rule's escape hatch** (`CAST_COMMIT_AGENT=1`), matching misfire's
  violation accounting;
- **DENIES with the user's own rule text as the reason**
  (`permissionDecision: "deny"`);
- prints a `settings.json` registration snippet using `${CLAUDE_PROJECT_DIR}` that
  misfire does **not** write.

The text rendering ends with the verdict:

```
Verdict: ENFORCE  recommended=true
Evidence-grounded: 5 observed violation(s) across 35 opportunities (14.3%).
```

misfire is an observer. The scaffold is printed for you to review; misfire never
writes `settings.json` and never auto-applies a change. Convert-to-hook is official
Anthropic guidance — the differentiator here is the evidence-grounding, not the idea.
A converted rule should usually stay in prose too (defense in depth). Only rules with
a machine-checkable predicate are eligible at all; judgment and safety rules are
KEEP-as-prose. The `before_action`/`after_action` convert kinds carry no violation
evidence (ordering is not reconstructible from passive traces), so they are unranked
and emit a skeleton hook for you to complete — never an evidence-grounded
recommendation. See [`docs/convertibility-taxonomy.md`](convertibility-taxonomy.md).

---

## 4. Optional cast.db substrate → `expected_castdb_rank.json`

This proof exercises the OPTIONAL, flag-gated `--cast-db` substrate (CAST power-users
only). misfire works fully without it; the portable transcript adapter is the default.
When engaged, cast.db reconstructs `output_shape` (Handoff / Status) rule violations
from its `agent_protocol_violations` table, read-only. This is **the only proof that
touches a database**, and that database is synthetic, local, rebuilt from committed
SQL, and opened strictly read-only.

First (re)build the synthetic DB from the committed SQL, then run. The `rm -f` keeps
the rebuild idempotent — `seed.sql` uses plain `CREATE TABLE`, so re-running without
removing the gitignored DB first would error with `table already exists`:

```sh
rm -f proof/castdb-sample/generated.db
python3 -c "import sqlite3; c=sqlite3.connect('proof/castdb-sample/generated.db'); \
    c.executescript(open('proof/castdb-sample/seed.sql').read()); c.commit(); c.close()"
misfire rank proof/castdb-sample/config \
    --cast-db proof/castdb-sample/generated.db \
    --projects-dir "$(mktemp -d)" --json
```

The output must equal `proof/castdb-sample/expected_castdb_rank.json` byte-for-byte.
`tests/test_proof_castdb.py` asserts this on every run.

**What the fixture exercises.** `proof/castdb-sample/seed.sql` builds a synthetic
`cast.db` (the real tables, no PII) with `agent_protocol_violations` rows mapping to
`output_shape` prose rules:

| cast.db violation | maps to prose rule | outcome |
|-------------------|--------------------|---------|
| `handoff_schema_violation` | the `## Handoff` block rule | `enforce_candidate` |
| `missing_formality` | the `end with Status: DONE \| …` rule | `enforce_candidate` |
| `prose_dispatch` | (no clean rule) | UNMAPPED (reported honestly) |

The `agent_runs` count (running rows excluded) is the opportunity denominator — an
upper bound, so the reported violation rate is a conservative lower bound.

**Binary-DB note.** `generated.db` is gitignored and rebuilt from `seed.sql` on every
run — only the SQL text is committed, never a binary DB. `config_root` and the echoed
`cast_db.db_path_rel` are the relative strings supplied, so the committed fixture
contains zero `/Users/`, `/home/`, `/private/`, or `Projects/` substrings. The flag
defaults OFF (portable-first); supplying `--cast-db` with no value would use
`~/.claude/cast.db`. cast.db is a richer accelerant, never a dependency — see
[`docs/adapters.md`](adapters.md).

---

## 5. End-to-end — the installed hook blocks `git commit` (BATS)

The strongest proof. `tests/bats/convert_blocks_commit.bats` takes the hook emitted in
proof #3, installs it (executable, with its shebang) into an **isolated temp HOME**,
and drives it with the **real PreToolUse stdin contract** — the exact JSON Claude Code
feeds a hook. It asserts the installed hook:

- **DENIES** `git commit` (`permissionDecision: "deny"`);
- **ALLOWS** `git status`;
- **IGNORES** a quoted occurrence (`echo "git commit"`) — no naive-substring false
  positive;
- **HONORS** the escape hatch (`CAST_COMMIT_AGENT=1 git commit`).

```sh
bats tests/bats/convert_blocks_commit.bats
```

This is the one proof that runs the emitted hook as a real subprocess rather than
comparing JSON, so it closes the loop: the structural matcher misfire uses to *count*
violations is the same matcher the generated hook uses to *block* them. The test
honors the project isolation rules — a temp HOME throughout (the real `$HOME` is never
touched), no GUI side effects, and the CLI invoked with `--json` and explicit fixture
paths so nothing outside the repo and temp HOME is read or written.

---

## Test inventory

| Proof | Test file |
|-------|-----------|
| audit | `tests/test_proof.py` |
| rank | `tests/test_proof_rank.py` |
| convert | `tests/test_proof_convert.py` |
| cast.db rank | `tests/test_proof_castdb.py` |
| installed hook (end-to-end) | `tests/bats/convert_blocks_commit.bats` |

These five proofs are part of the wider suite — **430 pytest tests** plus **5 BATS
tests** (the single `convert_blocks_commit.bats` file) — all green. The pytest suite
runs in CI across Python 3.9, 3.10, 3.11, and 3.12 (GitHub Actions, ubuntu-latest); the
BATS proof runs locally with `bats`.

---

## See also

- [`docs/usage.md`](usage.md) — command reference and flags
- [`docs/architecture.md`](architecture.md) — signals → audit → recommendation
- [`docs/adapters.md`](adapters.md) — portable transcript adapter vs optional cast.db
- [`docs/convertibility-taxonomy.md`](convertibility-taxonomy.md) — the
  convertible / keep boundary
- [`docs/framing.md`](framing.md) — what misfire claims, and what it does not
- [`proof/README.md`](../proof/README.md) — in-tree fixture notes (kept consistent
  with this page)

[← Back to README](../README.md)
