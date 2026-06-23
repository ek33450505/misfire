#!/usr/bin/env bats
# ablate_skips_without_ollama.bats — Phase 4 offline smoke test.
#
# Proves that `misfire ablate` exits 0 and prints a clear skip notice when
# Ollama is unreachable, and that output never leaks a /Users/<name>/ path.
#
# Isolation (hard rules):
#   - temp HOME used throughout; the real $HOME is never touched.
#   - No GUI side effects (no osascript / open / terminal-notifier).
#   - No real network: --ollama-url http://127.0.0.1:1 (port 1 = guaranteed
#     ECONNREFUSED on all platforms — no timeout required).

setup() {
  REPO="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  export PYTHONPATH="$REPO/src"

  TMP_HOME="$(mktemp -d)"
  export HOME="$TMP_HOME"
  mkdir -p "$HOME/.claude"

  # Seed a minimal CLAUDE.md with one convertible tool_substitution rule so
  # parse_config + classify_rules produce at least one usable classification.
  cat > "$HOME/.claude/CLAUDE.md" <<'EOF'
# Search conventions

- Always use `rg` instead of `grep` for searching in source files.
EOF
}

teardown() {
  [ -n "${TMP_HOME:-}" ] && rm -rf "$TMP_HOME"
}

@test "ablate exits 0 when Ollama is unreachable (observer posture)" {
  run python3 -m misfire.cli ablate "" --ollama-url "http://127.0.0.1:1"
  [ "$status" -eq 0 ]
}

@test "ablate output contains Ollama not-reachable skip notice" {
  run python3 -m misfire.cli ablate "" --ollama-url "http://127.0.0.1:1"
  [[ "$output" == *"Ollama not reachable"* ]] || [[ "$output" == *"not reachable"* ]]
}

@test "ablate output never leaks a /Users/ path" {
  run python3 -m misfire.cli ablate "" --ollama-url "http://127.0.0.1:1"
  [[ "$output" != */Users/* ]]
}
