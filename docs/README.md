# misfire — Documentation

[← Back to project README](../README.md)

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

This is the index for misfire's documentation. Start with **usage**, then dig into
the architecture and the trace-grounded evidence behind the ranked recommendations.

| Doc | What it covers |
| --- | --- |
| [usage.md](usage.md) | The four CLI commands (`audit`, `rank`, `evidence`, `convert`), flags, `--json` output, and the KEEP / ELEVATE / ENFORCE recommendation ladder. |
| [architecture.md](architecture.md) | The pipeline — static parse → classify → deterministic audit → evidence/violation engine → recommendation → hook scaffolder. Zero-LLM, stdlib-only core. |
| [adapters.md](adapters.md) | The portable transcript adapter (default, no DB) and the optional read-only `--cast-db` adapter for CAST power-users. |
| [convertibility-taxonomy.md](convertibility-taxonomy.md) | The five rule categories and four convert_kinds — which prose rules earn a hook and which stay as prose. |
| [proof.md](proof.md) | The byte-reproducible proofs: committed fixtures, expected JSON, and the BATS end-to-end hook test. |
| [framing.md](framing.md) | **Authoritative honesty guardrails** — the differentiation statement, prior-art comparison, and the Assumptions & Limitations every claim is bound by. Read this to understand what misfire does *not* claim. |

misfire is an observer/recommender: it prints ranked findings, recommendations, and
hook scaffolds for you to review. It never auto-deletes a rule, never auto-applies a
change, and never writes `settings.json`. See [framing.md](framing.md) for the full
limitations.
