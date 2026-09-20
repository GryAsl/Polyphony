#!/usr/bin/env bash
#
# SessionStart hook: lightweight check that the Antigravity CLI (`agy`) is usable.
# Warns on stderr but NEVER fails the session (always exits 0). The full health
# check lives in scripts/doctor.sh — this one stays fast (no `agy models` network
# call) so it doesn't slow every session start.
#
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)"

on_windows_native() {
  case "${OSTYPE:-}" in msys*|cygwin*|win32) return 0 ;; esac
  case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac
  return 1
}

agy_available() {
  command -v agy >/dev/null 2>&1 || \
    { [ -n "${AGY_PATH:-}" ] && [ -f "$AGY_PATH" ]; } || \
    { [ -n "${LOCALAPPDATA:-}" ] && [ -f "$LOCALAPPDATA/agy/bin/agy.exe" ]; }
}

if ! agy_available; then
  echo "[antigravity] agy not on PATH — install the Antigravity CLI to enable delegation:" >&2
  echo "[antigravity]   https://antigravity.google/docs/cli-using" >&2
  exit 0
fi

if on_windows_native; then
  # Make the account-pool command usable from ordinary PowerShell/cmd. Install
  # only into a user-owned directory that is already on PATH; never edit PATH.
  if [ -n "${AGY_BRIDGE_PYTHON:-}" ] && "$AGY_BRIDGE_PYTHON" -c 'import sys' >/dev/null 2>&1; then
    "$AGY_BRIDGE_PYTHON" "$ROOT/scripts/install_windows_account_launcher.py" >/dev/null 2>&1 || \
      echo "[antigravity] could not install the agy-account PowerShell launcher." >&2
  elif command -v py >/dev/null 2>&1; then
    py -3 "$ROOT/scripts/install_windows_account_launcher.py" >/dev/null 2>&1 || \
      echo "[antigravity] could not install the agy-account PowerShell launcher." >&2
  elif command -v python >/dev/null 2>&1; then
    python "$ROOT/scripts/install_windows_account_launcher.py" >/dev/null 2>&1 || \
      echo "[antigravity] could not install the agy-account PowerShell launcher." >&2
  fi

  # Never launch agy directly from this headless hook: native Windows needs the
  # ConPTY adapter, and the full end-to-end check belongs to agy-doctor.
  VENDOR="$(cd "$(dirname "$0")/../vendor/agy-headless-bridge/src" 2>/dev/null && pwd)"
  if [ -n "${AGY_BRIDGE_PYTHON:-}" ]; then
    AGY_VENDOR_SRC="$VENDOR" "$AGY_BRIDGE_PYTHON" -c 'import os,sys; sys.path.insert(0, os.environ["AGY_VENDOR_SRC"]); import agy_headless_bridge, winpty' >/dev/null 2>&1 || \
      echo "[antigravity] Vendored Windows ConPTY bridge or pywinpty is not importable by AGY_BRIDGE_PYTHON." >&2
  elif command -v py >/dev/null 2>&1; then
    AGY_VENDOR_SRC="$VENDOR" py -3 -c 'import os,sys; sys.path.insert(0, os.environ["AGY_VENDOR_SRC"]); import agy_headless_bridge, winpty' >/dev/null 2>&1 || \
      echo "[antigravity] Vendored Windows ConPTY bridge needs pywinpty — run: py -3 -m pip install -U pywinpty" >&2
  elif command -v python >/dev/null 2>&1; then
    AGY_VENDOR_SRC="$VENDOR" python -c 'import os,sys; sys.path.insert(0, os.environ["AGY_VENDOR_SRC"]); import agy_headless_bridge, winpty' >/dev/null 2>&1 || \
      echo "[antigravity] Vendored Windows ConPTY bridge or pywinpty is unavailable to the active Python." >&2
  else
    echo "[antigravity] Windows ConPTY bridge needs Python >= 3.9 (or AGY_BRIDGE_PYTHON)." >&2
  fi
elif ! agy --version >/dev/null 2>&1; then
  echo "[antigravity] agy is on PATH but '--version' failed — it may need authentication (run \`agy\` once)." >&2
fi

exit 0
