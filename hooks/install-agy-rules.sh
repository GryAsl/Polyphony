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
# Rules are read from <plugin>/rules/ and — verified against agy 1.2.3 — load WITHOUT
# the plugin being registered in ~/.gemini/config/config.json, so this never edits the
# user's config. Dropping the directory is enough. Frontmatter MUST carry
# `trigger: always_on` or agy ignores the file silently: no error, no warning.
#
# Never fails the session. Every failure path warns on stderr and exits 0.
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$HERE/../agy/rules"

# Honour a relocated Gemini home the same way agy does.
GEMINI_ROOT="${GEMINI_HOME:-$HOME/.gemini}"
DEST_DIR="$GEMINI_ROOT/config/plugins/polyphony/rules"

[ -d "$SRC_DIR" ] || exit 0

# No agy config tree means agy was never run here; installing rules for a CLI that
# may not exist would litter the disk. check-agy.sh already warns about a missing agy.
[ -d "$GEMINI_ROOT/config" ] || exit 0

if ! mkdir -p "$DEST_DIR" 2>/dev/null; then
  echo "[polyphony] could not create $DEST_DIR — agy engineering rules not installed" >&2
  exit 0
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
