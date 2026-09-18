#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

skip() { printf 'skip: %s\n' "$*"; exit 0; }
fail() { echo "FAIL: $*" >&2; exit 1; }

[ "${POLYPHONY_LIVE_AGY:-0}" = "1" ] || skip "set POLYPHONY_LIVE_AGY=1 to run (spends Gemini quota)"
command -v agy >/dev/null 2>&1 || skip "agy is not installed"

GEMINI_ROOT="$HOME/.gemini"
PLUGIN_DIR="$GEMINI_ROOT/config/plugins/polyphony"
[ -f "$PLUGIN_DIR/rules/coding-quality.md" ] \
  || skip "rules are not installed here — run hooks/install-agy-rules.sh first"
[ -f "$PLUGIN_DIR/plugin.json" ] \
  || fail "rules are installed but $PLUGIN_DIR/plugin.json is missing — agy will not read them"

PROMPT="Do not use tools. Your instructions may contain a section titled 'Engineering standards'. If they do, complete this sentence VERBATIM from it: 'A test that passes against the unfixed code is ...'. If they contain no such section, reply exactly: ABSENT."

echo "asking a live agy worker what rules it can see..."
out="$(bash "$ROOT/scripts/agy-delegate.sh" --tier flash-medium --timeout 5m "$PROMPT" 2>/dev/null)" \
  || fail "agy delegation failed — cannot tell whether the rules load"

printf '%s\n' "$out" | grep -qi 'not a regression test' \
  || fail "agy did not quote the rule, so no worker is reading it. It replied: $out"

echo "  ok a live agy worker quotes the installed rule"
echo
echo "agy rules live check: 1 check passed"
