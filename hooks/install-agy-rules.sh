#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$HERE/../agy/rules"

GEMINI_ROOT="${GEMINI_HOME:-$HOME/.gemini}"
PLUGIN_DIR="$GEMINI_ROOT/config/plugins/polyphony"
DEST_DIR="$PLUGIN_DIR/rules"

[ -d "$SRC_DIR" ] || exit 0

[ -d "$GEMINI_ROOT/config" ] || exit 0

if ! mkdir -p "$DEST_DIR" 2>/dev/null; then
  echo "[polyphony] could not create $DEST_DIR — agy engineering rules not installed" >&2
  exit 0
fi

MANIFEST='{
  "name": "polyphony",
  "description": "Polyphony engineering rules for agy workers."
}'

manifest_path="$PLUGIN_DIR/plugin.json"
if [ ! -f "$manifest_path" ] || [ "$(cat "$manifest_path" 2>/dev/null)" != "$MANIFEST" ]; then
  if printf '%s\n' "$MANIFEST" > "$manifest_path" 2>/dev/null; then
    echo "[polyphony] wrote $manifest_path — agy does not read rules/ without it" >&2
  else
    echo "[polyphony] could not write $manifest_path — agy engineering rules not installed" >&2
    exit 0
  fi
fi

installed=0
for src in "$SRC_DIR"/*.md; do
  [ -f "$src" ] || continue
  dest="$DEST_DIR/$(basename "$src")"
  if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
    continue
  fi
  if cp "$src" "$dest" 2>/dev/null; then
    installed=$((installed + 1))
  else
    echo "[polyphony] could not write $dest — agy engineering rules not installed" >&2
    exit 0
  fi
done

[ "$installed" -gt 0 ] && echo "[polyphony] installed $installed agy engineering rule(s) into $DEST_DIR" >&2

exit 0
