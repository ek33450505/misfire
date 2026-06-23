# Architecture — signals → audit → recommendation

[← Back to README](../README.md)

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

This is the how-it-works doc. misfire reads your existing Claude Code instruction
files and your own run history, tells you which of your prose rules your agents
demonstrably ignore — ranked from that history — and scaffolds a deterministic hook
for the violated convertible subset only. It is an observer/recommender: it prints
recommendations, ranked violation lists, and hook scaffolds for you to review. It
never auto-deletes a rule, never auto-applies a change, and never writes
`settings.json`.

The whole core is **stdlib-only, zero runtime dependencies, zero LLM calls** —
deterministic and byte-reproducible (see [Zero-LLM determinism](#zero-llm-determinism)).

---

## The pipeline

Two signal sources feed one recommendation. The **static** side parses your
instruction files; the **evidence** side reconstructs what your agents actually did
from run history. They converge on the KEEP / ELEVATE / ENFORCE ladder, and ENFORCE
candidates flow into the hook scaffolder.

```
                          your CLAUDE.md / .claude/rules/*.md / @imports
                                            │
                                     ┌──────▼──────┐
                                     │   parse     │   precedence chain, dir
                                     │  (static)   │   concat, @imports, rules/
                                     └──────┬──────┘
                                            │ Rules
                                     ┌──────▼──────┐
                                     │  classify   │   5 categories
                                     │             │   + 4 convert_kinds
                                     └──────┬──────┘
                          ┌────────────────┼─────────────────┐
                          │                │                 │
                   ┌──────▼──────┐         │          your run history
                   │    audit    │         │        (transcript JSONL,
                   │ 4 findings  │         │         optional cast.db)
                   └──────┬──────┘         │                 │
                          │                │          ┌──────▼──────┐
                          │                │          │  transcript │  adapter →
                          │                │          │   adapter   │  ToolAction
                          │                │          └──────┬──────┘
                          │                │          ┌──────▼──────┐
                          │                │          │    match    │  structural
                          │                │          │             │  matcher
                          │                │          └──────┬──────┘
                          │                │          ┌──────▼──────┐
                          │                │          │    rank     │  confidence +
                          │                │          │             │  min-support
                          │                │          └──────┬──────┘
                          └────────────────┼─────────────────┘
                                     ┌──────▼──────┐
                                     │  recommend  │   KEEP / ELEVATE / ENFORCE
                                     └──────┬──────┘
                                     ┌──────▼──────┐
                                     │  scaffold   │   PreToolUse / PostToolUse
                                     │ (zero-LLM)  │   hook + settings snippet
                                     └─────────────┘   (printed, never written)
```

The static audit and the hook scaffold are **table-stakes features** bundled under
the headline. The headline — the defensible thing — is the evidence path:
trace-grounded adherence measurement of your existing prose rules. No shipping tool
ranks which of your existing prose rules your agents actually ignore.

---

## Static parse

`parse.py` is a stdlib, zero-LLM walk of the documented Claude Code load order. It
extracts individual rules and records where each one came from, so a downstream
finding can point you back to the exact file and line.

**Precedence chain.** It walks the documented order:

1. managed policy (stub)
2. user `~/.claude/CLAUDE.md` (the `config_root` default is `~/.claude`)
3. project `./CLAUDE.md` / `./.claude/CLAUDE.md` (the ancestor chain, root-down)
4. `./CLAUDE.local.md`

**Directory concatenation.** Within the project ancestor chain, files concatenate
**root-down** — outer-directory instructions load before the ones closer to your
working directory, matching how Claude Code assembles context.

**`@path` imports.** An `@path` line pulls another file inline, resolved
**relative to the importing file**. Imports inside fenced code blocks are skipped
(an `@path` in an example is documentation, not a directive).

**`.claude/rules/*.md`.** Rule files load **unconditionally** unless they carry a
`paths:` frontmatter key — in which case they are path-scoped and only apply under
the matching directories. That `paths:` mechanism is exactly what the **ELEVATE**
recommendation uses to cut token rent (see the ladder below).

---

## Classification

`classify.py` sorts every parsed rule into one of **five categories**. The
convertible / not-convertible boundary is the honesty line of the whole tool: only a
rule with a machine-checkable predicate can earn a hook.

| Category | Meaning | Fate |
|---|---|---|
| `convertible` | Machine-checkable predicate ("never X", "use TOOL not TOOL", "run X before commit") | Eligible for ENFORCE — the only category that can become a hook |
| `safety_keep` | Destructive / irreversible-action guardrail | KEEP as prose, always |
| `judgment_keep` | Judgment / style / altitude (YAGNI, "be concise", "match ceremony to task size") | KEEP as prose, always |
| `output_shape` | Agent output-protocol rule (Handoff block, Status line, Work Log) | KEEP as prose; violations only observable via the optional cast.db substrate |
| `non_directive` | Metadata / provenance note, no actionable directive | KEEP as prose |

Convertible rules additionally carry one of **four `convert_kinds`**:

| convert_kind | Shape | Evidence? |
|---|---|---|
| `never_command` | "never run X" → `PreToolUse` deny | Yes — ranked |
| `tool_substitution` | "use TOOL_A not TOOL_B" → `PreToolUse` | Yes — ranked |
| `before_action` | "run X before commit" → `PreToolUse` | **No** — unranked skeleton |
| `after_action` | "do X after edit" → `PostToolUse` | **No** — unranked skeleton |

**`before_action` and `after_action` carry no violation evidence.** Ordering ("did X
happen *before* commit?") is not reconstructible from passive traces, so these rules
are **UNRANKED**: the scaffolder emits a **skeleton hook for you to complete**, never
an evidence-grounded recommendation. This is honesty about what the trace can and
cannot prove, not a gap to be filled later.

The full predicate inventory — every category, convert_kind, and the keep-as-prose
rationale — is in [`convertibility-taxonomy.md`](convertibility-taxonomy.md).

---

## Deterministic audit

`audit.py` is the static, table-stakes side. It runs four sub-audits, each producing
findings keyed back to the source file. These are owned ground (agents-lint, AgentLint
already ship stale-path and conflict detection) — bundled, never the headline. There
are **four finding kinds**:

- **`stale_path`** — a rule references a filesystem path that no longer exists.
- **`token_rent`** — a rule (or file) is large enough to spend attention budget out
  of proportion to how often it applies; a candidate to ELEVATE behind `paths:`.
- **`conflict`** — two rules that both load contradict each other with no precedence
  winner to break the tie.
- **`load_fidelity`** — a mismatch between what the precedence chain says should load
  and what actually resolves (e.g., a broken or skipped `@import`).

**Why length matters** is anchored on Anthropic's own guidance, not invented stats:
the Claude Code best-practices docs note that *"bloated CLAUDE.md files cause Claude
to ignore your actual instructions"*, the `/memory` docs that *"over 200 lines reduces
adherence"* and that CLAUDE.md is *"context, not enforced configuration"*, and
*"Effective context engineering"* frames the *"attention budget"* and *"context rot."*
Public long-context degradation results (IFScale, Lost-in-the-Middle, NoLiMa,
Same-Task-More-Tokens) are used **only to motivate the mechanism** — they measure
retrieval and synthetic single-turn instruction stacks, not persistent CLAUDE.md
adherence, so treating them as direct adherence evidence would be an inference, not a
measurement. The audit flags token rent as a length signal; it does not claim a
success-rate number it cannot source.

---

## The evidence / violation engine

This is the evidence path — the part misfire is built around. It answers: *which of
these rules do your agents demonstrably ignore, and how often?*

### Reconstructing actions

`adapters/transcript.py` reads Claude Code's native transcript JSONL under
`~/.claude/projects/**` and normalizes each tool invocation into a `ToolAction`. This
is **portable** — it works for any Claude Code user with no database. CAST power-users
can additionally pass `--cast-db` (read-only) to add `output_shape` (Handoff / Status)
violations; that substrate is an accelerant, never a dependency. Both adapters and the
read-only contract are documented in [`adapters.md`](adapters.md).

### The structural matcher (and the ~80% naive-FP story)

The naive approach — substring-search the rule's target string in the action stream —
is dominated by false positives. On the one rule tested first-hand
(`never raw git commit`), **~80% of naive matches were noise**: the string
`git commit` appearing inside a hook-test payload, a PR body, or a grep pattern —
i.e., the string was **data**, not an executed command. Offscript independently
measured ~22% material deviations, in the same neighborhood of "most surface matches
don't mean what they look like."

So `match.py` does **structural command parsing**, not substring matching. Its
`command_invokes` / `_strip_quoted_spans` logic strips quoted spans before testing, so
a target string that appears as data — a grep pattern, a PR body, a quoted
`echo "git commit"` — is **not** counted as an executed command. Structural parsing
here is mandatory, not polish: without it the rankings would be ~80% garbage.

### Confidence threshold + minimum-support floor

`rank.py` emits per-rule **violation** and **opportunity** counts and ranks them, but
only after applying a **minimum-support floor** (default `--min-support 30`): a
rule's ranking is not meaningful until it has been observed across enough
opportunities. Each ranked rule carries a confidence label — `high` / `medium` /
`low` / `insufficient_data` — assigned by a pure function:

- `opportunity_count == 0` → `insufficient_data`
- below the support floor → `low`
- **zero violations with enough observations → `high`** (the rule is *obeyed* — this
  is **not** a deletion signal)
- `>= 10` violations **and** `>= 10%` rate → `high`
- `>= 3` violations **or** `>= 5%` rate → `medium`
- otherwise → `low`

Rank sorts rules into three buckets: `enforce_candidate`, `insufficient_evidence`,
and `observed_no_violations`.

### The zero-violation HARD GUARD

The single most important invariant: **a rule with zero observed violations yields no
deletion or convert signal.** A silently-obeyed safety rule and a never-needed rule
are **indistinguishable** in passive traces — misfire's output is evidence of
*violation*, never evidence of *redundancy*. So a high-confidence "zero violations"
result means *"this rule is being obeyed,"* not *"delete this rule."* Safety rules are
KEPT regardless of trigger frequency. misfire is a wedge, not an omniscient auditor;
it never claims to know which rules are load-bearing.

These limits are not caveats bolted on after the fact — they are the design. See the
**Assumptions & Limitations** in the [README](../README.md) and
[`framing.md`](framing.md).

---

## The recommendation ladder

Every rule lands on exactly one rung. Only the ENFORCE rung produces a hook, and only
for a convertible rule with evidence.

| Rung | Applies to | Action |
|---|---|---|
| **KEEP** | `judgment_keep`, `safety_keep`, `output_shape`, `non_directive` | Stays as prose. No change. Safety and judgment rules are never force-converted. |
| **ELEVATE** | Convertible / verbose rules that apply only in some paths | Move to a path-scoped `.claude/rules/*.md` with `paths:` frontmatter, so it only loads where relevant — cutting token rent without losing the rule. |
| **ENFORCE** | A `convertible` rule with an **observed violation record** (the violated convertible subset only) | Scaffold a `PreToolUse` / `PostToolUse` hook. Printed for review — never installed automatically. |

Convert-to-hook is official Anthropic guidance — the best-practices docs say to
*"delete it or convert it to a hook"* precisely because *"hooks are deterministic …
CLAUDE.md … advisory."* misfire takes the un-contested half of that advice (convert)
and adds the missing half: **which** rules earn conversion, decided from your own
trace evidence rather than blindly.

---

## The hook scaffolder

`scaffold.py` is **deterministic, templated, zero-LLM**. Given a classified
convertible rule on the ENFORCE rung, it emits a self-contained Claude Code hook
script plus the `settings.json` registration snippet — and stops there. It prints the
scaffold and a diff; it **never writes `settings.json`** and never installs anything.
The settings snippet it prints uses `${CLAUDE_PROJECT_DIR}` so you can paste it
yourself.

The crucial detail: **the generated hook embeds misfire's own structural matcher**
via source inlining. A blindly-generated `grep "git commit"` hook would re-introduce
the exact ~80% naive-substring false-positive class the ranking engine was built to
avoid. By inlining the structural matcher, the emitted hook honors the same
quoted-span stripping — so, for example, a `never_command` hook for `git commit`:

- **DENIES** an actual `git commit` (with the user's own rule text as the deny reason,
  `permissionDecision: "deny"`),
- **ALLOWS** `git status`,
- **IGNORES** a quoted `echo "git commit"` (data, not a command — no false positive),
  and
- **HONORS** the rule's own escape hatch (e.g. `CAST_COMMIT_AGENT=1`).

Because the hook surface changes across Claude Code versions, the scaffolder
**feature-detects** the installed CC version before emitting hook code rather than
assuming one schema. This behavior is verified end-to-end by
`tests/bats/convert_blocks_commit.bats`, which installs the emitted hook into an
isolated temp HOME and drives it with the real `PreToolUse` stdin contract. See
[`proof.md`](proof.md) for the byte-reproducible proofs.

---

## Zero-LLM determinism

The deterministic core **never calls an LLM**. Same command, same input → same output,
byte-for-byte: every command supports `--json` with sorted keys for stable diffs, and
all output (text and JSON) is machine-path sanitized — `/Users/<name>/` and
`/home/<name>/` collapse to `~/`, paths under `config_root` are made relative, no
usernames leak.

The **only** LLM use anywhere is an opt-in, local-Ollama causal-ablation probe
(Phase 4, **not yet shipped**), gated behind an explicit flag. It never touches the
deterministic path. This matches the portfolio DNA of `attest` and `looptrip`:
zero-LLM cores with explicit opt-in extensions.

---

## Module map

The core is ~5,990 lines across **12 Python modules**, stdlib-only, zero runtime
dependencies, `requires-python >= 3.9`.

| Module | Responsibility |
|---|---|
| `parse.py` | Static parse of the precedence chain, directory concatenation, `@imports`, and `.claude/rules/*.md`; extracts individual rules with provenance. |
| `classify.py` | Sorts each rule into the 5 categories and assigns a convert_kind + machine-checkable predicate to convertibles. |
| `audit.py` | Deterministic static audit — the four finding kinds (`stale_path`, `token_rent`, `conflict`, `load_fidelity`). |
| `evidence.py` | The normalized `ToolAction` data model shared by the adapter and the matcher; carries the path-sanitization privacy invariant. |
| `match.py` | The structural command matcher (`command_invokes` / `_strip_quoted_spans`); joins tool actions to rule predicates, stripping quoted spans. |
| `rank.py` | Per-rule violation + opportunity counts; confidence labels, minimum-support floor, the buckets, and the zero-violation HARD GUARD. |
| `scaffold.py` | The zero-LLM, templated hook scaffolder; emits the hook (with the matcher inlined) + the `settings.json` snippet, never writing it. |
| `cli.py` | The `misfire` entry point — `audit` / `rank` / `evidence` / `convert`, plus `--version`; every command supports `--json`. |
| `adapters/transcript.py` | Portable, default adapter: reads Claude Code transcript JSONL under `~/.claude/projects/**`, yields `ToolAction`s. No DB required. |
| `adapters/cast_db.py` | Optional, flag-gated (`--cast-db`), strictly read-only cast.db adapter; adds `output_shape` violations for CAST power-users. |
| `__init__.py` (package) | Package marker / version for `misfire`. |
| `adapters/__init__.py` | Package marker for the adapters subpackage. |

---

## See also

- [`convertibility-taxonomy.md`](convertibility-taxonomy.md) — the full
  category / convert_kind inventory and keep-as-prose rationale.
- [`adapters.md`](adapters.md) — the portable transcript adapter and the optional
  read-only cast.db substrate.
- [`proof.md`](proof.md) — the byte-reproducible proofs, including the BATS
  hook-blocks-commit end-to-end test.
- [`framing.md`](framing.md) — differentiation statement, prior-art comparison, and the
  full Assumptions & Limitations.
- [`usage.md`](usage.md) — command reference and flags.

[← Back to README](../README.md)
