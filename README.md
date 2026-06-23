# misfire

[![Tests](https://github.com/ek33450505/misfire/actions/workflows/test.yml/badge.svg)](https://github.com/ek33450505/misfire/actions/workflows/test.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![tests: 430 + 5 BATS](https://img.shields.io/badge/tests-430%20%2B%205%20BATS-brightgreen.svg)](tests/)

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

misfire is a deterministic, local-first CLI and Python library that reads your existing
Claude Code instruction files (`CLAUDE.md`, `.claude/rules/*.md`, `@imports`) and your own
run history, then tells you **which of YOUR prose rules your agents demonstrably ignore,
ranked from YOUR run history**. For the violated, machine-checkable subset only, it
scaffolds a deterministic hook for you to review — keeping safety and judgment rules as
prose. It is an observer and recommender: it prints recommendations, ranked violation
lists, and hook scaffolds. It never auto-deletes a rule, never auto-applies a change, and
never writes `settings.json`.

The static audit (stale paths, token rent, conflicts) and the hook scaffold are
table-stakes features bundled under one headline: trace-grounded adherence measurement of
your existing prose rules. No shipping tool ranks which of your existing prose rules your
agents actually ignore.

## Why now

Convert-to-hook is **Anthropic's own guidance**. The Claude Code best-practices docs say
that bloated `CLAUDE.md` files cause Claude to ignore your actual instructions, and that
the fix is to "delete it or convert it to a hook" — because hooks are deterministic while
`CLAUDE.md` is advisory. The `/memory` docs describe `CLAUDE.md` as "context, not enforced
configuration" and note that going over 200 lines reduces adherence. The "Effective
context engineering" guidance frames the same problem as a finite "attention budget"
subject to "context rot."

So the *idea* of converting rules to hooks is not new, and misfire says so plainly. The
honest why-now is narrower and is the whole story: **no credible public measurement of
CLAUDE.md adherence exists.** misfire producing the first trace-grounded adherence
ranking from your own run history is the differentiator — not the hook scaffold, which is
official guidance, but the evidence-grounding that decides *which* rules earn a hook.

## What misfire does

Four commands, each with a deterministic `--json` mode (sorted keys, byte-stable output):

| Command   | What it does |
|-----------|--------------|
| `audit`   | Static, zero-LLM audit of your instruction files — finds `stale_path`, `token_rent`, `conflict`, and `load_fidelity` issues. Table-stakes. |
| `rank`    | Reconstructs rule violations from your run history and ranks the machine-checkable rules by observed violation rate, with confidence thresholds and a minimum-support floor. This is the wedge. |
| `evidence`| Shows the per-rule violation detail behind a ranking — the actual tool actions that violated a rule. |
| `convert` | Scaffolds a deterministic PreToolUse/PostToolUse hook for the violated convertible subset, prints it plus a `settings.json` snippet for you to review, and writes nothing. |

Observer exit codes: every command exits `0` regardless of findings. The only non-zero
exit is `evidence` or `convert` invoked with an explicit `--rule PREFIX` that matches no
rule (exit `1`).

misfire sorts every rule into one of five categories — `convertible`, `safety_keep`,
`judgment_keep`, `output_shape`, `non_directive` — and recommends along a three-tier
ladder:

- **KEEP** — judgment, safety, output-shape, and non-directive rules stay as prose.
- **ELEVATE** — move a rule into a path-scoped `.claude/rules/*.md` with `paths:`
  frontmatter to cut token rent.
- **ENFORCE** — scaffold a hook, but only for the violated convertible subset.

## Install

misfire is **stdlib-only with zero runtime dependencies** and supports Python 3.9+.

```sh
pip install misfire
# or, with uv:
uv pip install misfire
```

A Homebrew tap is planned — `brew install ek33450505/misfire/misfire` will work once the
`homebrew-misfire` tap is published.

### From source (for development)

```sh
git clone https://github.com/ek33450505/misfire
cd misfire
pip install -e .
# or, with uv:
uv pip install -e .
```

## Quick start — proof in one command

The ranking is byte-reproducible against a committed fixture, with **no database**. From
the repo root:

```sh
misfire rank proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects
```

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

The `git commit` rule was violated 5 times across 35 opportunities (14.3%), with 2
sanctioned uses of its escape hatch excluded honestly; the `git push` rule, 3 of 35
(8.6%). Add `--json` and the output matches `proof/expected_rank.json` byte-for-byte
(test: `tests/test_proof_rank.py`) — purely from the markdown config and the transcript
JSONL, no `cast.db`.

Now turn the top evidence-grounded candidate into a hook:

```sh
misfire convert proof/evidence-sample/config \
    --projects-dir proof/evidence-sample/projects --top
```

This emits a self-contained PreToolUse hook (`matcher: Bash`) for the `never git commit`
rule. The generated hook **embeds misfire's own structural command matcher**, so a quoted
`echo "git commit"` is not blocked (no naive-substring false positive); it **honors the
rule's escape hatch** (`CAST_COMMIT_AGENT=1`); and it denies with your own rule text as the
reason (`permissionDecision: "deny"`). It prints a `settings.json` snippet using
`${CLAUDE_PROJECT_DIR}` that misfire does **not** write. The verdict is evidence-grounded:

```
Verdict: ENFORCE  recommended=true
Evidence-grounded: 5 observed violation(s) across 35 opportunities (14.3%).
```

The strongest proof drives the emitted hook end-to-end: `bats
tests/bats/convert_blocks_commit.bats` installs it into an isolated temp HOME and feeds it
the real PreToolUse stdin contract, asserting it denies `git commit`, allows `git status`,
ignores a quoted `echo "git commit"`, and honors the escape hatch. See
[`docs/proof.md`](docs/proof.md) for every reproducible proof.

## How it's different

misfire owns trace-grounded ranking of your *existing* prose rules. Adjacent tools either
convert rules blindly, grow a new policy, or do static analysis only:

| Tool / work | What it does | misfire's difference |
|-------------|--------------|----------------------|
| rule2hook | Blind prose→hook conversion, no evidence | misfire decides WHICH rules earn conversion, from YOUR trace evidence |
| PrismorSec/immunity-agent | Mines history to grow a NEW security policy; surfaces recommendations (no auto-apply) | misfire audits YOUR EXISTING prose rules and ranks them by observed violations; it never grows a new policy |
| AgentLint | AI-inference flags repeated or ignored rules ("Session mode" is closest) | misfire gives ranked output with confidence thresholds plus a convertible/judgment split |
| AgentSpec (ICSE'26) | Runtime-enforcement DSL | misfire is static plus an adherence audit, not a new DSL |
| Offscript | Academic adherence audit (86.4% of conversations deviate / 22.2% material) | misfire ships trace-grounded ranking as a local CLI on YOUR own data |
| TRACE | Mines USER CORRECTIONS into rules | misfire audits EXISTING prose; it does not derive rules from corrections |
| agents-lint / AgentLinter | Static stale-path / conflict detection | misfire includes static audit as table-stakes; the headline is evidence-ranking |

## Assumptions & Limitations

These are load-bearing. Read them before acting on any recommendation; the full guardrails
live in [`docs/framing.md`](docs/framing.md).

1. **Passive-trace blindspot.** misfire cannot tell a never-needed rule from a
   silently-obeyed one. Output is evidence of *violation*, not of *redundancy*. A safety
   rule with zero violations is **not** a deletion candidate.
2. **False positives dominate naive matching.** On the one rule tested first-hand (never
   raw `git commit`), ~80% of naive string matches were noise — the predicate appearing
   inside a hook-test payload, a PR body, or a grep pattern (Offscript independently
   measured ~22% material deviations). misfire applies structural command parsing,
   confidence thresholds, and minimum-support floors; rankings are not meaningful until a
   rule has enough observations.
3. **Structural command parsing is mandatory, not polish** — it is what kills the
   false-positive class above.
4. **CAST vs portable.** The optional `cast.db` substrate gives richer pre-computed
   signals; the default portable adapter reconstructs equivalent signals for any Claude
   Code user without `cast.db`.
5. **Hook schema volatility.** The Claude Code hook surface is large and changes across
   versions; misfire feature-detects the installed CC version before emitting scaffold
   code. (No required CC version is stated.)
6. **Scope.** v1 audits Claude Code instruction files only (`CLAUDE.md`,
   `.claude/rules/*.md`, `@imports`). Not `AGENTS.md` or `.cursorrules` yet.
7. **Unranked ordering rules.** `before_action` / `after_action` convertible rules carry
   no violation evidence (ordering is not reconstructible from passive traces) — they are
   unranked and emit a skeleton hook for you to complete, never an evidence-grounded
   recommendation.

## Documentation

| Doc | Contents |
|-----|----------|
| [`docs/README.md`](docs/README.md) | Documentation index |
| [`docs/usage.md`](docs/usage.md) | Command reference, flags, defaults, JSON contract |
| [`docs/architecture.md`](docs/architecture.md) | The signals → audit → recommendation pipeline |
| [`docs/adapters.md`](docs/adapters.md) | Portable transcript adapter and optional `cast.db` adapter |
| [`docs/convertibility-taxonomy.md`](docs/convertibility-taxonomy.md) | The 5 categories and 4 convert kinds |
| [`docs/proof.md`](docs/proof.md) | Byte-reproducible proofs (audit, rank, convert, cast.db, BATS) |
| [`docs/framing.md`](docs/framing.md) | Framing guardrails, differentiation statement, prior art, and full assumptions |

## Contributing, Security, License

- Contributing: see [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Security: report vulnerabilities per [`SECURITY.md`](SECURITY.md).
- Conduct: see [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
- License: **Apache-2.0** — see [`LICENSE`](LICENSE).

Maintainer: edward.kubiak.dev@gmail.com · Repo: https://github.com/ek33450505/misfire
