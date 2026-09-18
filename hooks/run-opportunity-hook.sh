#!/usr/bin/env bash
#
# Launcher for Claude Code hooks invoking agy_opportunity_reminder.py.
# Resolves AGY_BRIDGE_PYTHON first, then python3, py -3, python.
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$HERE/agy_opportunity_reminder.py"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

if [ -n "${AGY_BRIDGE_PYTHON:-}" ] && "$AGY_BRIDGE_PYTHON" -c 'import sys;sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
  exec "$AGY_BRIDGE_PYTHON" "$TARGET" "$@"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
  exec python3 "$TARGET" "$@"
elif command -v py >/dev/null 2>&1 && py -3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
  exec py -3 "$TARGET" "$@"
elif command -v python >/dev/null 2>&1 && python -c 'import sys;sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
  exec python "$TARGET" "$@"
elif command -v powershell.exe >/dev/null 2>&1; then
  # Fall back to native-install discovery when desktop PATH is stale.
  exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$HERE/run-opportunity-hook.ps1" "$@"
else
  # Daily setup warning, not a nonzero hook error on every event.
  stamp="${LOCALAPPDATA:-${TMPDIR:-/tmp}}/Polyphony/hook-python-missing.warned"
  if [ ! -f "$stamp" ] || [ -n "$(find "$stamp" -mtime +0 -print 2>/dev/null)" ]; then
    if mkdir -p "$(dirname "$stamp")" 2>/dev/null && touch "$stamp" 2>/dev/null; then
      echo '[Polyphony] Hook Python unavailable; routing enforcement is inactive. Install Python 3.9+ or set AGY_BRIDGE_PYTHON, then restart the host.' >&2
    fi
  fi
  exit 0
fi
