# Security Policy

> Linters tell you your rules are messy; misfire tells you which rules your agents
> actually ignore — and converts only those into hooks, keeping safety rules.

misfire is a deterministic, local-first CLI and Python library. It reads your Claude
Code instruction files and your own run history, and prints recommendations, ranked
violation lists, and hook scaffolds for you to review. It is an observer/recommender:
it never auto-applies a change and never writes your configuration. The notes below
describe how to report a vulnerability and what the tool's security posture actually is.

For the project's full scope and honesty boundaries, see [`docs/framing.md`](docs/framing.md)
and the Assumptions & Limitations section of [`README.md`](README.md).

## Supported Versions

misfire is pre-release. The current in-repo version is a `0.0.0` placeholder; the first
real release will be `>=0.1.0`. Until then, the only supported target is the **latest
commit on `main`**.

| Version | Supported |
| --- | --- |
| Latest release (once `v0.1.0` ships) | Yes |
| `main` (pre-release) | Yes — current development target |
| `0.0.0` placeholder / older | No |

Once `v0.1.0` is published, security fixes will land on the **latest minor release**.
There are no separate long-term-support branches for a solo-maintained, pre-1.0 project.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security reports.** Public disclosure
before a fix is available puts other users at risk.

Instead, report privately through GitHub Security Advisories:

- **Preferred:** [Open a private security advisory](https://github.com/ek33450505/misfire/security/advisories/new)
- **Fallback:** email the maintainer at **edward.kubiak.dev@gmail.com**

If you use email, please include `misfire security` in the subject line so the report is
not missed.

### What to include

A good report lets the fix start immediately. Please include:

- **misfire version** — run `misfire --version`.
- **Environment** — your operating system and Python version (`python --version`).
- **The exact command and flags** you ran (for example, `misfire rank ... --cast-db ...`).
- **Repro steps** — a minimal, deterministic sequence that reproduces the issue. Because
  misfire is deterministic and reads local files, a small sample config or transcript
  fixture is ideal.
- **Impact** — what an attacker could do, and under what assumptions.

Please redact any private paths or content; note that misfire's own output is already
machine-path sanitized (`/Users/<name>/` and `/home/<name>/` collapse to `~/`), but
hand-written repro notes are not.

## Response Timeline

These targets are best-effort for a solo-maintained project. They describe intent, not a
contractual SLA.

| Severity | Acknowledgement | Fix target |
| --- | --- | --- |
| Critical | 48 hours | 14 days |
| High | 48 hours | 30 days |
| Medium / Low | 5 business days | Next release |

We will keep you updated as a fix progresses and will credit you in the advisory unless
you ask to remain anonymous.

## Security Posture

misfire is built to have a small, honest attack surface. The following are true of the
deterministic core:

- **Stdlib-only, zero runtime dependencies.** `dependencies = []` in `pyproject.toml` —
  there are no third-party runtime packages, so there is no third-party supply-chain
  surface to compromise. (The only optional extra is a not-yet-shipped, opt-in local
  LLM ablation behind an explicit flag; the deterministic core never calls an LLM.)
- **No network calls.** misfire reads local files and prints to stdout. It does not phone
  home, fetch remote content, or transmit your rules or run history anywhere.
- **The optional cast.db is opened strictly read-only.** The `--cast-db` substrate (a
  flag-gated accelerant for CAST power-users, off by default) is opened in SQLite
  read-only mode (`mode=ro`) and is never mutated.
- **misfire never writes your configuration.** It does not write `settings.json`, never
  auto-deletes a rule, and never auto-applies a change. It only reads and prints —
  recommendations, ranked violation lists, and hook scaffolds for you to review. The
  generated `settings.json` snippet is printed for you to apply yourself; misfire does not
  apply it.
- **Output is machine-path sanitized.** All text and `--json` output collapses
  `/Users/<name>/` and `/home/<name>/` to `~/`, and paths under your config root are made
  relative, so usernames do not leak into output you might paste into a report or share.

For the surrounding design — the static parse, the trace-grounded violation engine, and
the hook scaffolder — see [`docs/architecture.md`](docs/architecture.md) and
[`docs/adapters.md`](docs/adapters.md).

## Out of Scope

The following are not vulnerabilities in misfire and should be reported to their
respective owners:

- **The Claude API and Claude Code itself** — report to Anthropic. misfire reads Claude
  Code's transcript files and instruction files; it does not control the model, the CLI,
  or the hook runtime.
- **Third-party tools** — bugs in tools your rules reference (git, your shell, external
  CLIs), or in any hook you author from a misfire scaffold after editing it, belong
  upstream with those projects.
- **The contents of your own rules and hooks** — misfire reports on your rules and
  scaffolds hooks for your review; you remain responsible for reviewing and applying
  anything it emits before it runs in your environment.

If you are unsure whether something is in scope, report it privately anyway and we will
help route it.

## Responsible Disclosure

We ask that you give us a reasonable opportunity to investigate and ship a fix before any
public disclosure. In return, we commit to acknowledging your report within the timelines
above, keeping you informed, and crediting you in the published advisory unless you prefer
to remain anonymous. We will not pursue legal action against good-faith security research
that respects this policy and does not access, modify, or exfiltrate other people's data.

---

Back to [`README.md`](README.md).
