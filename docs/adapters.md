# Adapters — portable transcript + optional cast.db

[← Back to README](../README.md)

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

misfire needs evidence to rank which of your prose rules your agents actually ignore.
That evidence comes from one of two **substrates**, behind a stable internal interface:

1. **The portable transcript adapter** — the PRIMARY, default substrate. It reads
   Claude Code's own transcript JSONL and needs no setup and no database.
2. **The optional cast.db adapter** — a flag-gated, read-only accelerant for CAST
   power-users. It is strictly optional; misfire works fully without it.

**misfire is portable-first.** If you never pass `--cast-db`, misfire reconstructs
every signal it needs from transcripts alone. The database, when engaged, is opened
**strictly read-only** and is never required.

---

## Portable transcript adapter (PRIMARY, default)

This is the substrate every Claude Code user gets for free. There is nothing to
install, configure, or migrate — if you have run Claude Code, you already have the
evidence.

**What it reads.** Claude Code writes a native transcript for each session under
`~/.claude/projects/**` as JSONL. The adapter walks that tree:

| Signal | Source |
|---|---|
| Main-session tool actions (the `git commit`, `grep`, `rm` calls your rules talk about) | `message.content[].tool_use` entries in the session JSONL |
| Subagent output-shape signals (Handoff / Status protocol adherence) | `<session>/subagents/*.jsonl` |

Tool actions are joined to each rule's machine-checkable predicate by the structural
matcher in `match.py` (`command_invokes` / `_strip_quoted_spans`), which strips quoted
spans so a target string that is *data* — a grep pattern, a PR body, a hook-test
payload — is **not** counted as an executed command. This is what kills the ~80%
naive-substring false-positive class measured first-hand. See
[architecture.md](architecture.md) and [proof.md](proof.md) for the mechanics and the
byte-reproducible proofs.

**Where it looks.** The default config root is `~/.claude` and the default transcript
root is `~/.claude/projects`. Override the latter with `--projects-dir`:

```sh
# Default: reads ~/.claude/projects
misfire rank ~/.claude

# Explicit transcript root (used by every proof fixture)
misfire rank proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects --json
```

**Who it works for.** Anyone running Claude Code, with cast.db absent. This is the
substrate behind the transcript proofs in [proof.md](proof.md) — the static audit,
the `rank` evidence proof, and the `convert` end-to-end proof all run with **no
database at all**.

---

## Optional cast.db adapter (CAST power-users)

CAST (the Claude Agent Specialist Team framework) maintains an observability database
at `~/.claude/cast.db`. If you run CAST, misfire can read pre-computed protocol
signals from it as a **richer accelerant** — but it remains entirely optional.

**How to engage it.** The `--cast-db [PATH]` flag is available on `rank` and
`evidence` only. It is **OFF by default** (portable-first). Passing the flag with no
value uses `~/.claude/cast.db`; passing a path uses that file.

```sh
# Engage the default cast.db
misfire rank ~/.claude --cast-db

# Engage an explicit DB path
misfire evidence ~/.claude --cast-db /path/to/cast.db
```

**Read-only, always.** The database is opened with SQLite `mode=ro`. misfire never
writes, migrates, or mutates the DB — consistent with its observer/recommender
posture (it never writes `settings.json` either; see [framing.md](framing.md)).

**What it adds.** The adapter reconstructs **output_shape** rule violations
(Handoff / Status protocol adherence) from cast.db's `agent_protocol_violations`
table. These are the rules the portable adapter can only see through subagent JSONL;
cast.db has them pre-computed. In the proof fixture the reconstructed violations map
to a `SubagentStop` hook:

| cast.db violation | maps to prose rule | outcome |
|---|---|---|
| `handoff_schema_violation` | "MANDATORY: Every agent … MUST include a Handoff block …" | enforce_candidate |
| `missing_formality` | "MANDATORY: All agents end with Status: DONE \| …" | enforce_candidate |
| `prose_dispatch` | (no clean rule) | UNMAPPED (honest) |

**Conservative by construction.** The opportunity denominator for these signals is the
`agent_runs` count (running rows excluded). `agent_runs` is an **upper bound** on the
number of opportunities a rule had — not every run is an opportunity for every rule —
so the reported `violation_rate` is a **conservative lower bound**. Some cast.db
signals (e.g. `prose_dispatch`) have no clean prose rule to attach to and stay
**honestly unmapped** rather than being force-fit. The `--json` output reports both
sides of this honestly:

```json
"cast_db": {
  "agent_runs_denominator": 40,
  "db_path_rel": "proof/castdb-sample/generated.db",
  "mapped_violations": 5,
  "total_violations_read": 6,
  "unmapped_by_signal": {
    "prose_dispatch": 1
  }
}
```

`mapped_violations` (5) is less than `total_violations_read` (6): one violation stayed
unmapped, and misfire says so rather than inflating a rule's count.

**An accelerant, not a dependency.** cast.db gives richer, pre-computed signals, but
it is never required. CAST power-users only. Everyone else gets the same wedge from
transcripts.

---

## Choosing a substrate

| | Portable transcript | Optional cast.db |
|---|---|---|
| Default | Yes (always on) | No (`--cast-db` to engage) |
| Setup | None | Requires a CAST cast.db |
| Commands | all (`audit`, `rank`, `evidence`, `convert`) | `rank`, `evidence` only |
| Source | `~/.claude/projects/**` JSONL | `~/.claude/cast.db` (`mode=ro`) |
| Main-session tool actions | Yes (`message.content[].tool_use`) | — |
| Output_shape (Handoff / Status) | From `<session>/subagents/*.jsonl` | Pre-computed from `agent_protocol_violations` |
| Mutates anything | No | No (read-only) |
| Who it's for | Any Claude Code user | CAST power-users |

The two are additive: engaging `--cast-db` does not replace the transcript adapter, it
supplements it with pre-computed output-shape signals.

---

## Proof — the optional cast.db substrate (byte-reproducible)

The cast.db path is proven the same way as the core: a committed fixture whose output
must match byte-for-byte. The binary DB is **not** committed — only the SQL text is.
`generated.db` is rebuilt from `seed.sql` on every run, opened read-only, and the
result is asserted by `tests/test_proof_castdb.py`.

From the repo root, rebuild the synthetic DB from the committed SQL, then run `rank`:

```sh
python3 -c "import sqlite3; c=sqlite3.connect('proof/castdb-sample/generated.db'); \
    c.executescript(open('proof/castdb-sample/seed.sql').read()); c.commit(); c.close()"

misfire rank proof/castdb-sample/config \
    --cast-db proof/castdb-sample/generated.db \
    --projects-dir "$(mktemp -d)" --json
```

The output must match `proof/castdb-sample/expected_castdb_rank.json`. The fixture
seeds 6 `agent_protocol_violations` rows and 40 non-running `agent_runs` (2 running
rows excluded), and an empty `--projects-dir` (the `mktemp -d`) so the run isolates the
cast.db contribution. The result ranks the Handoff rule (3 violations / 40 → 7.5%) and
the Status rule (2 / 40 → 5.0%) as `enforce_candidate`, leaving the unmapped
`prose_dispatch` violation out of every rule's count.

For the full proof set (static audit, transcript `rank`, `convert`, and the BATS
hook-blocks-commit proof), see [proof.md](proof.md) and `proof/README.md`.

---

## Privacy

All output — text and `--json`, from either substrate — is machine-path sanitized.
`/Users/<name>/` and `/home/<name>/` collapse to `~/`; paths under the config root are
made relative. No usernames leak. For the cast.db adapter, the `db_path_rel` field is
home-collapsed the same way (a relative `--cast-db` path stays relative; a path under
your home collapses to `~/`) and never carries a raw machine path, so committed
fixtures carry no machine-specific paths.

---

## Assumptions & Limitations (Adapters)

Restating limitation #4 from the project-wide [framing.md](framing.md), which every
doc making product claims must point to:

> **CAST vs portable.** The cast.db adapter gives richer, pre-computed signals. The
> portable adapter (transcript JSONL + native subagent output-shape signals)
> reconstructs equivalent signals for any Claude Code user without cast.db.

This connects to the wider honesty line that governs both substrates: misfire's output
is evidence of **violation**, not of **redundancy**. A rule with zero observed
violations is **not** a deletion candidate — it may be obeyed, enforced by other means,
or simply never triggered. Neither substrate can distinguish a never-needed rule from a
silently-obeyed one, and neither ever auto-deletes a rule or writes `settings.json`.
See [framing.md](framing.md) for the full Assumptions & Limitations and the
KEEP / ELEVATE / ENFORCE recommendation ladder.

---

[← Back to README](../README.md) · [Docs index](README.md)
