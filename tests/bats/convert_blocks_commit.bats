#!/usr/bin/env bats
# convert_blocks_commit.bats — Phase 3 verification #3.
#
# Proves that a hook emitted by `misfire convert`, installed (executable, with
# its shebang) into an ISOLATED temp HOME and driven by the REAL PreToolUse
# stdin contract (the exact JSON Claude Code feeds a hook), actually DENIES
# `git commit` — while allowing unrelated commands, ignoring a quoted
# occurrence (no naive-substring false positive), and honoring the rule's own
# escape hatch.
#
# Isolation (hard rules): a temp HOME is used throughout — the real $HOME is
# never touched. No GUI side effects (no osascript/notify/open). Hermetic: the
# CLI is invoked with --json (no `claude --version` subprocess) and explicit
# fixture paths, so nothing outside the repo + temp HOME is read or written.

setup() {
  REPO="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  export PYTHONPATH="$REPO/src"

  TMP_HOME="$(mktemp -d)"
  export HOME="$TMP_HOME"
  mkdir -p "$HOME/.claude/hooks"

  # Emit the top evidence-grounded enforce_candidate hook from the portable
  # evidence fixture, then install it the way a user would.
  OUT="$(cd "$REPO" && python3 -m misfire.cli convert proof/evidence-sample/config \
        --projects-dir proof/evidence-sample/projects --top --json)"
  HOOK="$HOME/.claude/hooks/hook.py"
  echo "$OUT" | jq -r '.hook.script' > "$HOOK"
  echo "$OUT" | jq '.hook.settings_snippet' > "$HOME/.claude/settings.json"
  chmod +x "$HOOK"
}

teardown() {
  [ -n "${TMP_HOME:-}" ] && rm -rf "$TMP_HOME"
}

# Run the installed hook (via its shebang) with a PreToolUse Bash payload.
run_hook() {
  jq -nc --arg c "$1" '{tool_name:"Bash",tool_input:{command:$c}}' | "$HOOK"
}

@test "settings snippet registers a PreToolUse Bash hook (well-formed JSON)" {
  run jq -e '.hooks.PreToolUse[0].matcher == "Bash"' "$HOME/.claude/settings.json"
  [ "$status" -eq 0 ]
}

@test "installed hook DENIES git commit" {
  run run_hook "git commit -m wip"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"permissionDecision": "deny"'* ]]
}

@test "installed hook ALLOWS git status" {
  run run_hook "git status"
  [[ "$output" != *'"permissionDecision": "deny"'* ]]
}

@test "installed hook IGNORES a quoted git commit (no naive-substring FP)" {
  run run_hook 'echo "git commit"'
  [[ "$output" != *'"permissionDecision": "deny"'* ]]
}

@test "installed hook HONORS the rule escape hatch" {
  run run_hook "CAST_COMMIT_AGENT=1 git commit -m ok"
  [[ "$output" != *'"permissionDecision": "deny"'* ]]
}
