# Usage

[← Back to README](../README.md)

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

This is the complete reference for misfire's four-command surface: `audit`, `rank`,
`evidence`, and `convert`. Every example below uses a committed proof fixture, so you
can run each command verbatim from a clone and see the same output.

---

## Install and invocation

misfire is stdlib-only with **zero runtime dependencies** and requires Python **>=3.9**
(CI runs the suite on 3.9, 3.10, 3.11, and 3.12).

The PyPI name is reserved, but the first real release is still pending — the in-repo
version is a `0.0.0` placeholder. **Do not `pip install misfire` yet.** A PyPI release
will arrive with **v0.1.0**; until then, install from source:

```sh
git clone https://github.com/ek33450505/misfire
cd misfire
pip install -e .          # add ".[dev]" to also pull the test toolchain
```

Once installed, the CLI is invoked as `misfire <command>`:

```sh
misfire --version         # prints: misfire <version>
misfire audit             # audit the default config root (~/.claude)
misfire rank
misfire evidence
misfire convert
```

`--version` is the only top-level flag; it short-circuits before any subcommand. With
no subcommand, misfire prints help and exits 0.

---

## The observer contract

misfire is an **observer and recommender**. It prints findings, ranked violation lists,
and hook scaffolds for you to review. It never auto-deletes a rule, never auto-applies a
change, and **never writes `settings.json`**.

Three invariants hold across every command:

- **Nothing is ever written.** All output goes to stdout (with provenance/advisory notes
  on stderr). misfire never mutates your config, your run history, or — when the optional
  cast.db substrate is engaged — your database (it is opened strictly read-only).
- **Exit 0 regardless of findings.** A noisy config and a pristine one both exit 0. The
  *only* non-zero exit is `evidence` or `convert` given an explicit `--rule PREFIX` that
  matches no rule (exit 1). Do not script misfire as a pass/fail gate on findings — read
  the output instead.
- **`--json` is byte-stable.** Every command supports `--json`, emitted with
  `indent=2, sort_keys=True`. The same command on the same input produces byte-identical
  output across machines, which is how the proof fixtures are checked in CI.

A non-trigger is never evidence for deletion. A safety rule with zero observed violations
is *kept* — it may simply have worked. See
[Assumptions & Limitations](framing.md#assumptions--limitations) for what passive traces
can and cannot tell you.

---

## `misfire audit [CONFIG_ROOT]`

Static parse plus deterministic audit of your instruction files — zero run history, zero
LLM. This is the table-stakes layer (stale paths, token rent, conflicts); the evidence
ranking in `rank` is the headline.

| Flag | Default | Description |
|---|---|---|
| `CONFIG_ROOT` (positional) | `~/.claude` | Config root directory to audit |
| `--project-dir DIR` | none | Project directory for project-scoped sources and load-fidelity checks |
| `--base-dir DIR` | none | Base directory for resolving bare-relative path tokens (stale_path audit) |
| `--json` | off | Deterministic JSON (byte-stable; `sort_keys=True`) |

`audit` reports **four finding kinds**:

| Finding kind | What it flags |
|---|---|
| `stale_path` | A path referenced by a rule that does not exist on disk |
| `token_rent` | Files over the length threshold and total config size (per `/memory` guidance, files >200 lines reduce adherence) |
| `conflict` | Contradictory rules that both load with no precedence winner |
| `load_fidelity` | Broken `@imports` and load-precedence problems |

It also prints a **classification summary** — every parsed rule is sorted into one of
**five categories**: `convertible`, `safety_keep`, `judgment_keep`, `output_shape`,
`non_directive`. Only `convertible` rules can earn a hook; judgment and style rules are
kept as prose.

### Worked example

```sh
misfire audit proof/sample-config
```

```text
misfire audit — proof/sample-config
Sources: 4 | Rules: 9

=== stale_path (1 finding) ===
  CLAUDE.md:13 — Path does not exist: '~/nonexistent-misfire-fixture/logs'

=== token_rent (2 findings) ===
  rules/verbose.md — 'rules/verbose.md' is 208 lines (threshold: 200). Per /memory guidance, files >200 lines reduce adherence.
  (aggregate) — Total config: 228 lines / ~635 tokens (heuristic: chars/4) across 3 source file(s).

=== conflict (1 finding) ===
  CLAUDE.md:15 — Conflicting tool-substitution rules for 'cat': CLAUDE.md:15 prefers 'misfire' but rules/tools.md:3 prefers 'bat'.

=== load_fidelity (1 finding) ===
  nonexistent-import.md — Broken @import: 'nonexistent-import.md' does not exist (imported from 'CLAUDE.md').

Classification summary:
  convertible    3
  judgment_keep  3
  non_directive  1
  output_shape   1
  safety_keep    1

Convertible candidates (3) — Phase 2 will rank these:
  d8fa990b  CLAUDE.md:5  "Never use raw git commit directly — always route through the commit ag…"  [never_command]
  07b6c811  CLAUDE.md:15  "Use misfire not cat for reading configuration files."  [tool_substitution]
  2e407656  rules/tools.md:3  "Use bat not cat for viewing files in the terminal."  [tool_substitution]
```

For the byte-stable JSON form, `misfire audit proof/sample-config --json` reproduces
[`proof/expected_audit.json`](../proof/expected_audit.json) exactly (verified in
`tests/test_proof.py`).

---

## `misfire rank [CONFIG_ROOT]`

The evidence layer: which of your prose rules your agents demonstrably ignore, ranked
from your own run history. `rank` parses your config, classifies the rules, scans your
Claude Code transcript JSONL, and joins tool actions to convertible-rule predicates with
a structural command matcher — then ranks each rule by observed violations against
observed opportunities.

| Flag | Default | Description |
|---|---|---|
| `CONFIG_ROOT` (positional) | `~/.claude` | Config root directory |
| `--projects-dir DIR` | `~/.claude/projects` | Claude Code projects directory (transcript JSONL) |
| `--min-support N` | `30` | Minimum opportunity count for a trusted ranking |
| `--min-violations N` | `1` | Minimum violation count to recommend enforcement |
| `--cast-db [PATH]` | OFF | Optional read-only cast.db substrate (see [adapters](adapters.md)) |
| `--json` | off | Deterministic JSON (byte-stable; `sort_keys=True`) |

Rules are grouped into **three recommendation buckets**:

| Bucket | Meaning |
|---|---|
| `enforce_candidate` | Sufficient support **and** at least one observed violation — a genuine convert-to-hook candidate |
| `insufficient_evidence` | Below the support floor (`min_support`); not enough observations to trust a ranking yet |
| `observed_no_violations` | Enough observations, zero violations — the rule is obeyed. **Not** a deletion signal |

Each ranked rule carries a **confidence label** (`high` / `medium` / `low` /
`insufficient_data`), assigned by a pure function of the counts:

| Label | Assigned when |
|---|---|
| `insufficient_data` | `opportunity_count == 0` (no chance to observe the rule at all) |
| `low` | Below the support floor, or a weak signal above it |
| `medium` | `>=3` violations **or** a `>=5%` violation rate |
| `high` | `>=10` violations **and** a `>=10%` rate; **or** 0 violations across enough observations (the rule is *obeyed* — high confidence it works, not a deletion cue) |

### Worked example

```sh
misfire rank proof/evidence-sample/config --projects-dir proof/evidence-sample/projects
```

```text
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

Read this as: the `git commit` rule was violated 5 times across 35 opportunities
(14.3%), with 2 further matches excluded because they used the rule's own sanctioned
escape hatch → `enforce_candidate`; the `git push` rule, 3 of 35 (8.6%) → also an
`enforce_candidate`. Both land at `confidence=medium`. A trailing disclaimer (omitted
above) restates that a zero-violation rule is never a deletion recommendation.

`misfire rank … --json` reproduces [`proof/expected_rank.json`](../proof/expected_rank.json)
byte-for-byte (verified in `tests/test_proof_rank.py`).

---

## `misfire evidence [CONFIG_ROOT]`

Drill into one rule: show the actual violating tool actions behind its counts.

| Flag | Default | Description |
|---|---|---|
| `CONFIG_ROOT` (positional) | `~/.claude` | Config root directory |
| `--rule RULE_ID` | top-ranked rule | `rule_id` prefix to drill into |
| `--projects-dir DIR` | `~/.claude/projects` | Claude Code projects directory |
| `--limit N` | `20` | Maximum violating actions to show |
| `--cast-db [PATH]` | OFF | Optional read-only cast.db substrate (see [adapters](adapters.md)) |
| `--json` | off | Deterministic JSON (byte-stable; `sort_keys=True`) |

With no `--rule`, `evidence` drills into the top-ranked rule. With `--rule PREFIX`, it
matches by `rule_id` prefix; if the prefix matches no rule, `evidence` exits **1** (the
only non-zero exit on this command).

### Worked example

```sh
misfire evidence proof/evidence-sample/config --projects-dir proof/evidence-sample/projects
```

```text
misfire evidence — proof/evidence-sample/config
Rule: d84c9954a86f  [never_command]  confidence=medium
"MANDATORY: Never use raw git commit directly — always route through the commit agent. Escape hatch …"
Violations: 5  Opportunities: 35  Rate: 14.3%  Excluded (sanctioned): 2

--- 5 violating actions (showing 5 of 5) ---

  2026-06-22T10:00:00.000Z  git commit -m 'add feature A'
  transcript: ~/…/proof/evidence-sample/projects/proj-sample/sess-evidence-0001.jsonl  [main]  agent: main-session

  2026-06-22T10:01:00.000Z  git commit -m 'fix bug B'
  transcript: ~/…/proof/evidence-sample/projects/proj-sample/sess-evidence-0001.jsonl  [main]  agent: main-session

  …
```

Each entry shows the timestamp, the actual command (path-sanitized), the source
transcript, whether it ran on the main session or a sidechain, and the agent type.

---

## `misfire convert [CONFIG_ROOT]`

Scaffold a deterministic hook for an evidence-grounded convertible rule and print it for
review. `convert` is **surface-only**: it prints the hook script and a `settings.json`
snippet you merge yourself. **It never writes `settings.json`.**

| Flag | Default | Description |
|---|---|---|
| `CONFIG_ROOT` (positional) | `~/.claude` | Config root directory |
| `--rule RULE_ID` | top enforce_candidate | `rule_id` prefix to convert |
| `--top` | default when no `--rule` | Convert the top `enforce_candidate` from `rank` |
| `--projects-dir DIR` | `~/.claude/projects` | Claude Code projects directory for evidence |
| `--min-support N` | `30` | Minimum opportunity count for a trusted ranking |
| `--min-violations N` | `1` | Minimum violation count to recommend enforcement |
| `--json` | off | Deterministic JSON (byte-stable; `sort_keys=True`) |

Behavior:

- **Default / `--top`** converts the top evidence-grounded `enforce_candidate`. If no
  rule qualifies (none has both sufficient support *and* observed violations), `convert`
  prints an honest "nothing to convert" and exits 0.
- **`--rule` on a safety or judgment rule** returns a `KEEP` verdict with **no hook** —
  the honesty guard: safety, judgment, output-shape, and non-directive rules stay as
  prose.
- **`--rule` on a convertible rule with 0 observed violations** prints the scaffold for
  reference only, with `recommended=false`. A non-triggered rule is never a conversion
  signal.
- **`before_action` / `after_action` rules** carry no violation evidence (ordering is not
  reconstructible from passive traces). They are unranked and emit a **skeleton** hook
  with a TODO for you to complete — never an evidence-grounded recommendation.

An explicit `--rule PREFIX` that matches no rule exits **1**; every resolved target exits
0. Before emitting a hook, `convert` feature-detects your installed Claude Code version
(a stderr advisory, text mode only) — misfire does not assume a fixed CC version.

### Worked example

```sh
misfire convert proof/evidence-sample/config --projects-dir proof/evidence-sample/projects --top
```

```text
misfire convert — proof/evidence-sample/config
Rule: d84c9954a86f  [never_command]  (CLAUDE.md)
"MANDATORY: Never use raw git commit directly — always route through the commit agent. Escape hatch …"

Evidence: enforce_candidate  violations=5  opportunities=35  rate=14.3%
Verdict: ENFORCE  recommended=true
Evidence-grounded: 5 observed violation(s) across 35 opportunities (14.3%).

=== ENFORCE: PreToolUse hook (matcher: Bash) ===

--- save as: .claude/hooks/misfire-never-command-d84c9954.py (chmod +x) ---
#!/usr/bin/env python3
# misfire-generated PreToolUse hook -- DO NOT EDIT BY HAND.
# Enforces rule d84c9954a86f: "MANDATORY: Never use raw git commit directly …"
…
FORBIDDEN = 'git commit'
EXCEPTION = 'CAST_COMMIT_AGENT=1'   # the rule's own escape hatch, honored

# … misfire's structural command matcher (command_invokes / _strip_quoted_spans)
# is inlined here, so a quoted `echo "git commit"` is NOT blocked — no naive
# substring false positive …

def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    command = (data.get("tool_input") or {}).get("command", "") or ""
    if command_invokes(command, FORBIDDEN) and not (EXCEPTION and EXCEPTION in command):
        decision = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }}
        sys.stdout.write(json.dumps(decision))
    sys.exit(0)

--- settings.json (merge this; misfire does NOT write it) ---
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/misfire-never-command-d84c9954.py",
            "type": "command"
          }
        ],
        "matcher": "Bash"
      }
    ]
  }
}
```

(The hook script above is abridged — the real output emits the full `command_invokes` /
`_strip_quoted_spans` matcher inline, plus a caveat noting the honored escape hatch.) The
emitted hook **embeds misfire's own structural command matcher**, so a quoted
`echo "git commit"` is *not* blocked, **honors the rule's escape hatch**
(`CAST_COMMIT_AGENT=1`), and **denies with your own rule text as the reason**. The
`settings.json` snippet uses `${CLAUDE_PROJECT_DIR}` and is yours to merge.

`misfire convert … --top --json` reproduces
[`proof/expected_convert.json`](../proof/expected_convert.json) byte-for-byte (verified in
`tests/test_proof_convert.py`). The strongest end-to-end check installs the emitted hook
into an isolated temp HOME and drives it with the real PreToolUse stdin contract —
asserting it denies `git commit`, allows `git status`, ignores a quoted
`echo "git commit"`, and honors the escape hatch
([`tests/bats/convert_blocks_commit.bats`](../tests/bats/convert_blocks_commit.bats)).

---

## Privacy and sanitization

All output — text and `--json` alike — is machine-path sanitized at the output boundary:

- `/Users/<name>/` and `/home/<name>/` collapse to `~/`.
- Paths under the config root are made relative to it (e.g. `CLAUDE.md`,
  `rules/tools.md`).
- Bash command excerpts in `evidence` output are sanitized the same way.

No usernames leak. Run misfire output through review or paste it into an issue without
scrubbing it first.

---

## Optional: the cast.db substrate

`rank` and `evidence` accept `--cast-db [PATH]` to layer in `output_shape` (Handoff /
Status) violations reconstructed from a CAST `cast.db`. It is **off by default**
(misfire is portable-first and works fully without it), opened **strictly read-only**,
and never mutates the database. With no value the flag uses `~/.claude/cast.db`; with a
path, it uses that DB. This is an accelerant for CAST power users, not a dependency — see
[`docs/adapters.md`](adapters.md) for details.

---

## See also

- [Architecture](architecture.md) — signals → audit → recommendation pipeline
- [Adapters](adapters.md) — the portable transcript adapter and the optional cast.db substrate
- [Convertibility taxonomy](convertibility-taxonomy.md) — the convertible / keep boundary
- [Proof](proof.md) — the byte-reproducible fixtures behind every example here
- [Framing guardrails](framing.md) — differentiation, prior-art comparison, assumptions & limitations
- [← README](../README.md)
