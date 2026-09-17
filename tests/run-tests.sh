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
