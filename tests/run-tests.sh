#!/usr/bin/env bash
# Critical regression suite only. Deep diagnostics remain available as
# standalone tests; routine verification must finish quickly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PY=()
if [ -n "${AGY_BRIDGE_PYTHON:-}" ] && "$AGY_BRIDGE_PYTHON" -c 'import sys' >/dev/null 2>&1; then
  PY=("$AGY_BRIDGE_PYTHON")
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
  PY=(python3)
elif command -v py >/dev/null 2>&1 && py -3 -c 'import sys' >/dev/null 2>&1; then
  PY=(py -3)
elif command -v python >/dev/null 2>&1 && python -c 'import sys' >/dev/null 2>&1; then
  PY=(python)
else
  echo "critical-tests: Python 3 not found" >&2
  exit 1
fi

run() {
  printf '\n== %s ==\n' "$1"
  shift
  "$@"
}

case "${1:-}" in
  --full) FULL=1 ;;
  '') FULL=0 ;;
  *) echo 'Usage: bash tests/run-tests.sh [--full]' >&2; exit 2 ;;
esac
export POLYPHONY_UPDATE_CHECK=off

if [ "$FULL" -eq 1 ]; then
run "vendored ConPTY bridge" "${PY[@]}" "$ROOT/tests/test-vendored-bridge.py"
run "Windows bridge contract" "${PY[@]}" "$ROOT/tests/test-windows-bridge.py"
run "Flash model routing" "${PY[@]}" "$ROOT/tests/test-flash-routing.py"
run "quota state machine" "${PY[@]}" "$ROOT/tests/test-quota.py"
run "strict/soft hook enforcement" "${PY[@]}" "$ROOT/tests/test-opportunity-hook.py"
run "Polyphony update checker" "${PY[@]}" "$ROOT/tests/test-polyphony-update.py"
run "compact prompts and local helpers" "${PY[@]}" "$ROOT/tests/test-compact-routing.py"
run "Codex MCP adapter" "${PY[@]}" "$ROOT/tests/test-codex-mcp.py"
run "persistent agent runtime" "${PY[@]}" "$ROOT/tests/test-runtime.py"
run "lean wrapper smoke tests" "$ROOT/tests/test-lean-wrappers.sh"
else
run "routing and host portability" "${PY[@]}" "$ROOT/tests/test-opportunity-hook.py" \
  OpportunityHookTests.test_session_start_initializes_soft_without_question \
  OpportunityHookTests.test_turkish_mode_switch_and_numeric_nonselection \
  OpportunityHookTests.test_strict_conversation_clarification_and_host_controls \
  OpportunityHookTests.test_mode_save_failure_never_claims_success \
  OpportunityHookTests.test_ask_user_question_result_persists_mode_before_next_tool \
  OpportunityHookTests.test_host_background_agy_result_is_pending_until_task_output \
  HookManifestPortabilityTests.test_session_start_compact_exclusion_and_fork_inclusion \
  HookManifestPortabilityTests.test_manifest_interpreter_commands_and_launcher_portability \
  HookManifestPortabilityTests.test_native_windows_launcher_carries_utf8_payload
run "MCP transport and version" "${PY[@]}" "$ROOT/tests/test-codex-mcp.py" \
  McpAdapterTests.test_server_version_matches_manifests \
  McpAdapterTests.test_stdin_backed_shell_call_does_not_pass_pipe_and_input_together \
  McpAdapterTests.test_exit_code_stdout_and_stderr_are_preserved
run "quota user-choice safeguard" "${PY[@]}" "$ROOT/tests/test-quota.py" \
  QuotaTests.test_either_decimal_window_at_or_below_two_is_depleted \
  QuotaTests.test_user_choice_persists_only_until_recovery
run "persistent ownership safeguard" "${PY[@]}" "$ROOT/tests/test-runtime.py" \
  RuntimeTests.test_one_active_task_is_atomic_for_two_callers \
  RuntimeTests.test_fresh_agent_and_workspace_isolation
fi

run "agy rules installer" "$ROOT/tests/test-agy-rules-install.sh"

run "manifest JSON" "${PY[@]}" - "$ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for rel in (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    ".mcp.json",
    "codex/.mcp.json",
    "claude/hooks/hooks.json",
    "hooks/policy-context.json",
):
    with (root / rel).open(encoding="utf-8") as handle:
        json.load(handle)
print("manifest JSON is valid")
PY

for script in \
  "$ROOT/scripts/agy-delegate.sh" \
  "$ROOT/scripts/agy-scout.sh" \
  "$ROOT/scripts/agy-review.sh" \
  "$ROOT/hooks/nudge-delegation.sh" \
  "$ROOT/hooks/run-opportunity-hook.sh" \
  "$ROOT/bin/polyphony-agent"; do
  run "syntax: ${script#$ROOT/}" bash -n "$script"
done

printf '\ncritical regression suite passed\n'
