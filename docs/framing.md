# Framing Guardrails — misfire

Locked guardrails for consistent, honest framing across all misfire documentation,
blog posts, and public announcements. Reviewers enforce these rules before publication.

**One-liner:**
> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

**Moat statement:**
misfire's defensible asset is *trace-grounded adherence measurement of YOUR existing
prose rules* — specifically, which of your own CLAUDE.md/rules/*.md directives your
agents demonstrably ignore, ranked from your own run history. This moat is unoccupied
in shipping software as of 2026-06-22. The hook scaffold (convertible rules →
deterministic hook stubs) and the static audit (stale paths, token rent, conflicts)
are table-stakes features bundled under that headline, never the headline themselves.

**Observer / recommender posture:**
misfire is an **observer and recommender, never a destructive actor.** It prints
recommendations, ranked violation lists, and hook scaffolds. It never auto-deletes
rules, never auto-applies changes, never writes `settings.json`. The user reviews
a diff and decides. Contrast: `immunity-agent` mines history to grow a *new* policy; misfire ranks violations of your *existing* prose and never auto-applies.

---

## Guardrail 1: The Product Is the Wedge, Not an Omniscient Auditor

**The Claim:** Frame every doc as: *"which of YOUR rules your agents demonstrably
ignore, ranked from YOUR run history, with a hook scaffold for the violated
convertible subset."*

**What we cannot know from passive traces:** A silently-obeyed safety rule and a
rule that was simply never needed are indistinguishable in tool-action logs. misfire
makes no claim about whether an un-triggered rule is important or deletable. Every
doc must include an **Assumptions & Limitations** section (see Guardrail 10) that
states this explicitly.

**Never claim:** "misfire knows which rules are load-bearing." It doesn't — and
claiming otherwise is the omniscient-auditor trap.

---

## Guardrail 2: Never Auto-Delete; Safety Rules Are KEPT

**The Safety Invariant:** A non-triggered rule is **never** evidence for deletion.
Safety rules (rules that prevent harm or irreversible actions) and judgment rules
(YAGNI, "be concise", "match ceremony to task size") are **KEPT as prose**
regardless of observed (non-)trigger frequency.

**Posture:** observer/recommender, never a destructive actor — DNA shared with
`attest` and `looptrip`. Print scaffold + diff; the user decides. Never auto-write
`settings.json`. Never `learn --apply`.

**Convert-to-hook path:** Only rules with a machine-checkable predicate ("never raw
git commit", "use rg not grep", "run X before commit") get a hook recommendation.
All others are surfaced as **keep-as-prose** or **elevate** (move to a path-scoped
`.claude/rules/` file to cut token rent). Judgment and style rules are never
force-converted.

---

## Guardrail 3: Anchor "Why Now" on Anthropic Docs, Not Contested Stats

**Uncontestable anchors (use these):**
- Claude Code best-practices docs: *"Bloated CLAUDE.md files cause Claude to ignore
  your actual instructions"*; *"delete it or convert it to a hook"*; *"hooks are
  deterministic … CLAUDE.md … advisory"*
- `/memory` docs: *"over 200 lines reduces adherence"*; CLAUDE.md is *"context, not
  enforced configuration"*
- Anthropic engineering blog "Effective context engineering for AI agents": *"attention
  budget"*, *"context rot"*

**The honest "why now":** No credible public adherence-rate measurement exists for
CLAUDE.md rules against real agent runs. **misfire producing the first
trace-grounded one from your own history IS the story.** The convert-to-hook idea
is official guidance — that's double-edged: state it and claim the evidence-grounding
that competitors lack, not the idea itself.

---

## Guardrail 4: No Phantom Stats, Ever

**Forbidden claims — never cite, never imply:**
- "CLAUDE.md compliance ~25–40% without an enforcement layer" — confirmed phantom;
  absent from its supposed source.
- "~150–200 reliable instructions" — not in any citable Anthropic source.
- "system prompt ~50 slots" — not in any citable Anthropic source.
- "context files reduce success by N%" — misread of Gloaguen et al. (see Guardrail 5).

**The rule:** if you cannot point to the exact Anthropic doc, preprint, or fixture
that produced the number, do not cite the number. Label extrapolations explicitly.

---

## Guardrail 5: Cite the Direct Study Only With Its Caveats

**Gloaguen et al., "Evaluating AGENTS.md" (arXiv:2602.11988):**
- Dev-written instruction files: **+4%** task success (SWE-bench Verified)
- LLM-generated instruction files: **−3%**
- Dev + LLM combined: **>+20% cost increase**
- **Caveats (MANDATORY when citing):** unreviewed Feb-2026 preprint; near the
  SWE-bench noise floor; Python-skewed; contradicted on cost by Lulla et al.
  (arXiv:2601.20404, −28.6% runtime / −16.6% tokens).
- **Cite as "Gloaguen et al."** — affiliation is inferred, not stated in the paper.
  Do NOT write "ETH Zurich study."
- **Never repeat the misread headline:** "context files reduce success 3%" omits the
  +4% dev-written result entirely. Do not propagate it.
- **Never cite a single cost number as consensus** — the Gloaguen (+20% cost) and
  Lulla (−28.6% runtime) results conflict. Present both.

---

## Guardrail 6: Degradation Papers Are Mechanism Evidence Only

**Papers cited for the attention-budget / token-rent mechanism:**
- IFScale (arXiv:2507.11538): 68% success at 500 instructions
- Lost-in-the-Middle (TACL'24, arXiv:2307.03172): retrieval degradation
- NoLiMa (arXiv:2502.05167): long-context retrieval
- Same-Task-More-Tokens (ACL'24, arXiv:2402.14848): token overhead

**Transfer caveat (MANDATORY):** These papers measure **retrieval** or **synthetic
single-turn instruction stacks**, not persistent CLAUDE.md rule adherence. Using them
to justify *"verbose rules hurt success"* is an inference, not a measurement. Use them
only to justify *why token rent / length matters as a mechanism* — never as direct
evidence of adherence degradation.

**Forbidden attribution:** Do not attribute "curse of instructions" to
arXiv:2509.21051 — that phrase is not in it.

---

## Guardrail 7: Acknowledge Prior Art Honestly and Differentiate Crisply

Name the neighbors in README and public posts. Do not bury or omit them.

| Tool / Work | What it does | misfire's differentiation |
|---|---|---|
| `claudecode-rule2hook` (~405★) | Blind prose → hook conversion (no evidence) | We decide WHICH rules earn conversion, from YOUR trace evidence |
| `PrismorSec/immunity-agent` (v1.7.1, Apache-2.0) | Mines session history to grow a NEW security policy at the hook layer; surfaces recommendations, **no `learn --apply`** | We audit YOUR existing prose rules and rank by observed violations; we never grow a new policy |
| AgentLint (v1.1.13, 41★) | AI-inference flags repeated/ignored rules; "Session mode" is closest to misfire; no ranked output, no convertible-subset filter | We produce ranked output with confidence thresholds and a convertible/judgment split |
| AgentSpec (ICSE'26) | Runtime-enforcement DSL | Static + adherence audit, not a new DSL |
| Offscript (arXiv:2512.10172) | Academic adherence audit: 86.4% conversations deviate / 22.2% material | We ship trace-grounded ranking as a local CLI on YOUR data |
| TRACE (arXiv:2606.13174, Jun 2026) | Mines *user corrections* into enforcement rules | We audit existing prose; we don't derive rules from corrections |
| `agents-lint` / AgentLinter | Static stale-path / conflict detection | We include static audit as table-stakes; the moat is evidence-ranking |

**immunity-agent correction (carry into every doc):** v1.7.1 (2026-06-17) surfaces
recommendations; it has no `learn --apply`. Fix any draft that says otherwise.

---

## Guardrail 8: Static Audit Is a Feature, Not the Headline

Stale-path detection, token-rent / length analysis (flag >200 lines), and
conflict detection (contradictory rules that both inject with no precedence winner)
are **bundled table-stakes**. They are owned ground (`agents-lint`, AgentLint already
ship them). Present them under the evidence-ranking headline, never as novelty.

---

## Guardrail 9: The Convertible / Not-Convertible Boundary Is the Honesty Line

**Convertible (machine-checkable predicate → hook recommendation):**
- "never raw git commit" → `PreToolUse` on Bash, match `git commit`, block
- "use rg not grep" → `PreToolUse` on Bash, match `grep`
- "run X before commit" → `PreToolUse`
- "do X after edit" → `PostToolUse`
- "never touch Z path" → `PreToolUse` on Edit/Write, path filter

**Not convertible (judgment / style / altitude → KEEP as prose, never force-convert):**
- YAGNI, "be concise", "match ceremony to task size", "prefer existing patterns",
  "think step-by-step", "always get permission before destructive action" (judgment)

Rules in the not-convertible class are surfaced as **keep-as-prose** or **elevate**
(move to `.claude/rules/*.md` with `paths:` frontmatter to cut token rent).

---

## Guardrail 10: Preserve the Zero-LLM DNA

misfire's core is **deterministic — zero LLM calls.** Hook scaffolds are templated
from the convertibility taxonomy (pattern matching, not inference). All output is
reproducible: run the same command on the same input, get the same output.

The **only** LLM use is an opt-in local-Ollama ablation for causal probing — behind
an explicit flag, clearly labeled as exploratory. Deterministic core untouched.

This matches the portfolio DNA: `attest` and `looptrip` are also zero-LLM cores with
explicit opt-in extensions.

---

## Assumptions & Limitations

These must appear verbatim or paraphrased in every README, landing page, and dev.to
post. They are non-negotiable for honest framing.

1. **Passive-trace blindspot:** misfire cannot distinguish a *never-needed* rule from a
   *silently-obeyed* rule. A safety rule with zero observed violations is not a
   deletion candidate — it may simply have worked. misfire's output is evidence of
   *violation*, not evidence of *redundancy*.

2. **False-positive rate:** naive string matching of rule predicates against tool
   actions is dominated by false positives — on the one rule tested first-hand in the
   Phase-0 spike (`never raw git commit`), ~80% of naive matches were
   string-match/test noise (the predicate appearing inside a hook-test payload, a PR
   body, or a grep pattern rather than as an executed command); Offscript independently
   measured ~22% material deviations. misfire applies structural command parsing,
   confidence thresholds, and minimum-support floors; rankings are not meaningful until
   a rule has been observed across enough runs.

3. **Structural command parsing is mandatory:** context classification (is a `git commit`
   string a command or a string literal in a test payload?) is load-bearing, not polish.
   misfire's structural matcher strips quoted spans and classifies context before flagging.

4. **CAST vs portable:** the cast.db adapter gives richer, pre-computed signals. The
   portable adapter (transcript JSONL + native `InstructionsLoaded`/`PreToolUse` hook
   ledger) reconstructs equivalent signals for any Claude Code user without cast.db.

5. **Hook schema volatility:** Claude Code's hook event surface is large and still
   growing (16 events were observed wired in the Phase-0 test environment;
   Anthropic's documented set is larger and changes across versions). misfire
   feature-detects the installed CC version before emitting scaffold code. Pin to
   documented stable events; assume schema drift between major CC versions.

6. **Scope:** misfire audits Claude Code instruction files (`CLAUDE.md`,
   `.claude/rules/*.md`, `@imports`). It does not audit AGENTS.md (Cursor) or
   `.cursorrules` in v1; portability to other platforms is a later phase.

7. **Ablation is a proxy measurement, not an authoritative verdict:** The local Ollama
   model used in `misfire ablate` is a stand-in, not your production agent. Behavior
   differences between a generic local model and your deployed Claude model are expected
   and significant. Treat ablation results as suggestive signal, not ground truth.

8. **Small-N / temperature variance:** Ablation runs N trials per condition (default 5)
   at a fixed temperature (default 0.7). With small N, results may not be statistically
   robust. A single ablation run is a directional probe, not a measurement.

9. **Non-shift is never a deletion recommendation:** A zero or negative shift (ablated
   violation rate ≤ present rate) does NOT mean the rule is safe to delete. The rule may
   be obeyed in both conditions, redundant, or simply untestable with the given
   task/model — the omniscient-auditor trap applies here too. `misfire ablate` never
   recommends deletion.

10. **Ablation coverage is limited to convertible rules:** Only rules with `convert_kind`
    in `{never_command, tool_substitution}` can be probed. Safety, judgment, output-shape,
    and non-directive rules are outside scope.

11. **Ablation is opt-in and off the default deterministic path:** `misfire ablate`
    requires an explicit invocation and a running Ollama instance. The deterministic core
    (`audit`, `rank`, `evidence`, `convert`) never calls a live model. CI test suites
    should never call `misfire ablate` against a live Ollama — tests use the injectable
    `ChatClient` stub instead.

12. **Absolute ablation rates are not real-world base rates:** The representative task is
    deliberately constructed to *elicit* the candidate violation, so the present/ablated
    violation rates are inflated by design and must not be read as adherence estimates.
    Only the **shift** between the two conditions is the measured signal.

---

## Enforcement

These guardrails apply to:
- README and all `.md` files under `docs/`
- Blog posts, dev.to articles, and Show HN announcements
- Claude Code plugin marketplace description

Consistency reviewers will flag violations *before publication*. Corrections are
expected. If a number, stat, or claim cannot be sourced to an Anthropic doc, a
committed fixture, or a citable preprint with its caveats — cut it.
