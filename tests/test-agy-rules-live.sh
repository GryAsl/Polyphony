#!/usr/bin/env bash
#
# Does a real agy worker actually SEE the engineering rules?
#
# tests/test-agy-rules-install.sh can only assert what is on disk, and that is how
# this feature first shipped dead: the rules were installed, every offline check
# passed, and agy never read them because no plugin.json sat beside rules/. This is
# the check that would have caught it.
#
# Opt-in, and deliberately NOT wired into tests/run-tests.sh: it spends real Gemini
# quota, needs agy authenticated, and needs the rules already installed into the
# user's own Gemini home. Run it after changing the installer, the rule file, or
# after an agy upgrade:
#
#   POLYPHONY_LIVE_AGY=1 bash tests/test-agy-rules-live.sh
#
# agy was measured to ignore GEMINI_HOME and always read $HOME/.gemini, so this reads
# the real installed rules. It only asks agy a question; it writes nothing.
#
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

# A sentence that appears nowhere but the rule, so a model cannot produce it from
# general knowledge of how good engineers talk. If the rule is not loaded, agy is
# told to say so rather than guess.
PROMPT="Do not use tools. Your instructions may contain a section titled 'Engineering standards'. If they do, complete this sentence VERBATIM from it: 'A test that passes against the unfixed code is ...'. If they contain no such section, reply exactly: ABSENT."

echo "asking a live agy worker what rules it can see..."
out="$(bash "$ROOT/scripts/agy-delegate.sh" --tier flash-medium --timeout 5m "$PROMPT" 2>/dev/null)" \
  || fail "agy delegation failed — cannot tell whether the rules load"

printf '%s\n' "$out" | grep -qi 'not a regression test' \
  || fail "agy did not quote the rule, so no worker is reading it. It replied: $out"

echo "  ok a live agy worker quotes the installed rule"
echo
echo "agy rules live check: 1 check passed"
