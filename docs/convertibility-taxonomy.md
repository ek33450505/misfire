# Convertibility taxonomy — the honesty line

> Back to [README](../README.md) · See also [docs/architecture.md](architecture.md) and [docs/framing.md](framing.md).

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

This document defines the boundary that keeps misfire honest: **which of your prose
rules can earn a deterministic hook, and which must stay as prose.** Only rules with a
machine-checkable predicate are eligible for a hook recommendation. Judgment, style,
safety, and output-protocol rules are **KEPT as prose** (or **ELEVATED** to a
path-scoped file). misfire never force-converts a rule it cannot mechanically check.

The classifier (`classify.py`) sorts every extracted rule into exactly **one of five
categories**. Only one of them — `convertible` — is hook-eligible. The other four are
the honesty line: they describe intent, judgment, or safety that a passive trace
cannot adjudicate, so misfire keeps them in prose where a human stays in the loop.

---

## The principle

A hook is **deterministic enforcement**: a PreToolUse or PostToolUse script that fires
on a tool action and returns an allow/deny decision. That only works when the rule
reduces to a predicate a machine can evaluate against a tool action — "never run this
command", "use tool X instead of tool Y", "run X before committing". If a rule needs
human judgment to apply ("be concise", "build only what was asked", "match ceremony to
task size"), no hook can enforce it without false positives, so misfire leaves it as
prose.

This split is also where misfire's recommendation **ladder** lands each rule:

- **KEEP** — judgment, safety, output-shape, and non-directive rules stay as prose.
- **ELEVATE** — move a rule into a path-scoped `.claude/rules/*.md` file with `paths:`
  frontmatter so it only loads when relevant, cutting token rent without changing
  behavior.
- **ENFORCE** — scaffold a PreToolUse/PostToolUse hook, but **only** for the violated
  convertible subset (the rules with both a machine-checkable predicate *and* observed
  violations in your run history).

The convertible category is necessary but not sufficient for ENFORCE: being
machine-checkable makes a rule *eligible* for a hook; observed violations from your own
traces are what make a hook *recommended*. See [docs/architecture.md](architecture.md)
for how signals flow from static parse to evidence ranking to recommendation.

---

## Convertible kinds (hook-eligible)

A `convertible` rule carries one of four `convert_kind` values. Two of them
(`never_command`, `tool_substitution`) reduce to a tool-action predicate that misfire
can join to your run history, so they can be **ranked by observed violations**. The
other two (`before_action`, `after_action`) describe an *ordering* between actions —
and ordering is not reconstructible from passive traces — so they carry **no violation
evidence**, stay **UNRANKED**, and emit a **skeleton** hook for you to complete rather
than an evidence-grounded recommendation.

| convert_kind | Shape | Hook event | Ranked? |
| --- | --- | --- | --- |
| `never_command` | "never run X" | PreToolUse `Bash` → deny | Yes — violations counted from traces |
| `tool_substitution` | "use X not Y" | PreToolUse `Bash` | Yes — violations counted from traces |
| `before_action` | "run X before commit" | PreToolUse | **No — UNRANKED skeleton** |
| `after_action` | "do X after edit" | PostToolUse | **No — UNRANKED skeleton** |

**Why before/after are skeletons.** A trace records *that* a tool action happened, not
the obligation that should have preceded or followed it. misfire cannot tell from a
passive log whether "run the tests before commit" was satisfied — the absence of a
test run before a commit could mean the rule was ignored, or that this particular
commit legitimately needed none. Rather than invent violation counts it cannot defend,
misfire emits a PreToolUse (`before_action`) or PostToolUse (`after_action`) **hook
skeleton** with the predicate stubbed out, marks it UNRANKED, and leaves the
completion to you. This is the same conservatism that governs the whole tool: when the
evidence is ambiguous, misfire surfaces, it does not assert.

The predicate misfire extracts is structural, not a substring. For `never_command` the
predicate is a tool plus a command match (e.g. tool `Bash`, match `git commit`,
decision `deny`); for `tool_substitution` it is a forbidden tool plus a preferred one
(e.g. tool `Bash`, forbidden `cat`, prefer `bat`). When this becomes a hook, the
emitted script **embeds misfire's own structural command matcher** so a quoted
`echo "git commit"` — where the target string is *data*, not an executed command — is
not blocked. That structural matching is what keeps the convertible path off the
~80% naive-substring false-positive floor measured first-hand; see
[docs/architecture.md](architecture.md) and the proof in
[docs/proof.md](proof.md).

---

## KEEP categories (never hook-eligible)

The remaining four categories are **never converted to a hook.** They are kept as
prose (or elevated to a path-scoped file). The classifier is deliberately conservative:
when a rule's signal is ambiguous it defaults to `judgment_keep` rather than risk
mis-converting a rule it cannot mechanically check.

| Category | What it is | Disposition |
| --- | --- | --- |
| `safety_keep` | Destructive / irreversible markers ("back up before any operation", "avoid irreversible loss") | **KEPT as prose regardless of observed triggers.** A safety rule is never force-converted and never a deletion candidate. |
| `judgment_keep` | Style / altitude / judgment rules (YAGNI, "be concise", "match ceremony to task size") | KEPT as prose — no machine-checkable predicate exists. |
| `output_shape` | Agent output-protocol rules (Handoff block, Status line, Work Log) | KEPT as prose. Violations *can* be evidenced — from subagent JSONL, or from cast.db's `agent_protocol_violations` when the optional `--cast-db` substrate is enabled — but the rule itself stays prose, not a Bash/command hook. |
| `non_directive` | Prose that is not an instruction (provenance notes, blockquotes, metadata) | KEPT as prose — there is nothing to enforce. |

`safety_keep` is the load-bearing one. **Safety wins over convertibility:** if a rule
reads like a command guard *and* carries a destructive/irreversible marker, its
category stays `safety_keep` even though a predicate could be extracted. misfire will
not hand you a hook that auto-denies your own safety rail, and it will not flag a
safety rule for removal because it was never triggered. A non-trigger is not evidence;
see [the safety invariant](#the-safety-invariant) below.

`output_shape` shows the difference between *evidenced* and *enforced*. With the
optional read-only `--cast-db` substrate (or subagent transcript JSONL), misfire can
**rank** how often a Handoff or Status block was missing — that is evidence. But it
still recommends KEEP-as-prose, because an output-protocol convention is judgment about
agent communication, not a Bash command to deny. Evidence informs the recommendation;
it does not promote the rule out of KEEP.

---

## Worked examples (from `proof/sample-config`)

The committed audit fixture `proof/sample-config` is authored to spread all five
categories across nine rules. Running:

```sh
misfire audit proof/sample-config --json
```

produces `proof/expected_audit.json` (verified byte-for-byte by
`tests/test_proof.py`). The classification counts are: `convertible` 3,
`judgment_keep` 3, `safety_keep` 1, `output_shape` 1, `non_directive` 1. Each category
maps to a real line in the fixture:

| Rule text (fixture) | Source | Category | convert_kind |
| --- | --- | --- | --- |
| "Never use raw git commit directly — always route through the commit agent." | `CLAUDE.md:5` | `convertible` | `never_command` |
| "Use `misfire` not `cat` for reading configuration files." | `CLAUDE.md:15` | `convertible` | `tool_substitution` |
| "Use `bat` not `cat` for viewing files in the terminal." | `rules/tools.md:3` | `convertible` | `tool_substitution` |
| "Always back up data before any operation. Avoid irreversible loss." | `CLAUDE.md:11` | `safety_keep` | — (KEPT as prose) |
| "YAGNI: build only what was asked. No speculative extra features." | `CLAUDE.md:9` | `judgment_keep` | — (KEPT as prose) |
| "Every agent MUST include a Handoff block listing all files_changed." | `CLAUDE.md:7` | `output_shape` | — (KEPT as prose) |
| "Not real config. Authored to exercise misfire audit finding kinds." | `CLAUDE.md:3` | `non_directive` | — (KEPT as prose) |

Reading the boundary off this fixture:

- **`never_command`** — "Never use raw git commit directly" reduces to tool `Bash`,
  match `git commit`, decision `deny`. It is hook-eligible, and when it also accrues
  observed violations it becomes an ENFORCE candidate (the
  [convert proof](proof.md) drives exactly this rule end-to-end, including its escape
  hatch).
- **`tool_substitution`** — both `cat` substitutions reduce to a forbidden/preferred
  tool pair. (The fixture deliberately gives `cat` two conflicting preferences,
  `misfire` vs `bat`, which the static audit also reports as a `conflict` finding —
  convertibility and the static audit are independent passes over the same rules.)
- **`safety_keep`** — "Always back up data before any operation" is KEPT. It is never
  scaffolded into a hook and never proposed for deletion, no matter how its trigger
  count looks.
- **`judgment_keep`** — "YAGNI: build only what was asked" has no machine-checkable
  predicate. Kept as prose.
- **`output_shape`** — the Handoff-block rule is evidence-able via subagent JSONL /
  cast.db, but stays prose.
- **`non_directive`** — the blockquote provenance note is not an instruction; nothing
  to enforce.

The fixture's convertibles are all `never_command` / `tool_substitution`, so it
contains no `before_action` / `after_action` example. Those kinds are illustrated by
shape only — "run the tests before commit" (`before_action` → PreToolUse skeleton),
"format the file after edit" (`after_action` → PostToolUse skeleton) — and, as noted
above, they emit **UNRANKED skeletons**, never an evidence-grounded recommendation.

---

## The safety invariant

Two rules govern every disposition misfire makes, and they are the reason the
convertible/not-convertible boundary exists at all:

1. **A non-triggered rule is never deletion evidence.** misfire measures *violations*,
   not *redundancy*. A safety rule with zero observed triggers and a never-needed rule
   are indistinguishable in passive traces — so misfire makes no claim that an
   un-triggered rule is removable. Output is evidence of violation, not of
   redundancy.
2. **Safety rules are never force-converted.** A `safety_keep` rule stays prose
   regardless of its predicate or its trigger count. misfire is an observer and
   recommender: it prints ranked violation lists, hook scaffolds, and diffs. It never
   auto-deletes a rule, never auto-applies a change, and never writes `settings.json`.

These are the product's guardrails, not implementation details. The full set —
including why misfire is a wedge and not an omniscient auditor — is in
[docs/framing.md](framing.md). The convertibility taxonomy on this page is simply that
honesty line made concrete: machine-checkable predicate, and only then a hook;
everything else stays prose, with a human in the loop.

---

> See also: [docs/architecture.md](architecture.md) (signals → audit → recommendation),
> [docs/framing.md](framing.md) (framing guardrails), and the
> [README](../README.md) for the full command surface.
