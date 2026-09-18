#!/usr/bin/env bash
#
# Install Polyphony's engineering rules into the Antigravity CLI's config so every
# `agy` worker starts with them, however it is invoked — our wrappers, a bare `agy`,
# or an agy session the user opens themselves.
#
# WHY a file copy instead of prompt text. The delegation prompt is not the place for
# standing instructions: scripts/agy-delegate.sh hard-fails a prompt of 800 words or
# more, and says in that message that files and stdin do not bypass the limit. A
# ~460-word standing preamble would eat most of that budget on every call. Rules are
# loaded by agy itself, cost the caller nothing, and apply to sessions we never see.
#
# WHAT agy needs, measured on 1.2.6 by comparing prompt token counts across runs:
#
#   * A plugin.json manifest beside rules/. Without it agy does not treat the
#     directory as a plugin and never reads rules/ — no error, no warning, and the
#     rules ship dead. `name` and `description` are enough; no version is needed.
#   * Frontmatter carrying `trigger: always_on` in each rule, or agy ignores that
#     file, equally silently.
#
# Registering the plugin in ~/.gemini/config/config.json is NOT needed — the manifest
# alone is enough — so this never edits the user's config.
#
# GEMINI_HOME relocates where WE write. agy itself was measured to ignore it and to
# always read $HOME/.gemini, so honouring it here serves the tests, not relocation.
#
# Never fails the session. Every failure path warns on stderr and exits 0.
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$HERE/../agy/rules"

# Honour a relocated Gemini home the same way agy does.
GEMINI_ROOT="${GEMINI_HOME:-$HOME/.gemini}"
PLUGIN_DIR="$GEMINI_ROOT/config/plugins/polyphony"
DEST_DIR="$PLUGIN_DIR/rules"

[ -d "$SRC_DIR" ] || exit 0

# No agy config tree means agy was never run here; installing rules for a CLI that
# may not exist would litter the disk. check-agy.sh already warns about a missing agy.
[ -d "$GEMINI_ROOT/config" ] || exit 0

if ! mkdir -p "$DEST_DIR" 2>/dev/null; then
  echo "[polyphony] could not create $DEST_DIR — agy engineering rules not installed" >&2
  exit 0
fi

# Kept byte-for-byte stable: it is compared against what is already on disk, and a
# manifest that differed per run would rewrite the file on every session start.
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
  # Copy only on first install or a real content change, so an unchanged session start
  # does no disk writes and a user's own edits are not rewritten on every prompt.
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
