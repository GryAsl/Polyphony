#!/usr/bin/env bash
#
# hooks/install-agy-rules.sh contract. Offline: no agy, no network.
#
# The installer writes into the user's real Gemini home, so every case here points
# GEMINI_HOME at a temp dir. A test that forgot to would silently edit the developer's
# own agy config and still pass.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/hooks/install-agy-rules.sh"
SRC_DIR="$ROOT/agy/rules"
PASS=0

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { PASS=$((PASS + 1)); printf '  ok %s\n' "$1"; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

fresh_home() {
  local h="$TMPROOT/home.$RANDOM.$RANDOM"
  mkdir -p "$h/.gemini/config"
  printf '%s' "$h"
}

RULES="$SRC_DIR/coding-quality.md"
[ -f "$RULES" ] || fail "agy/rules/coding-quality.md is missing"

# agy ignores a rule whose frontmatter lacks this, silently — no error, no warning.
# Without this assertion the rule could ship dead and every other test still pass.
head -5 "$RULES" | grep -q '^trigger: always_on$' \
  || fail "coding-quality.md frontmatter lacks 'trigger: always_on'"
ok "rule declares trigger: always_on"

# scripts/agy-delegate.sh rejects a prompt of 800+ words. The rule is not sent as a
# prompt, but it is written to the same budget so it can be pasted into one if needed.
words="$(wc -w < "$RULES" | tr -d '[:space:]')"
[ "$words" -lt 800 ] || fail "rule is $words words; keep it under 800"
ok "rule is $words words (< 800)"

# --- fresh install -----------------------------------------------------------
H="$(fresh_home)"
out="$(GEMINI_HOME="$H/.gemini" HOME="$H" bash "$INSTALLER" 2>&1)" || fail "installer exited non-zero"
DEST="$H/.gemini/config/plugins/polyphony/rules/coding-quality.md"
[ -f "$DEST" ] || fail "rule was not installed to $DEST"
cmp -s "$RULES" "$DEST" || fail "installed rule differs from source"
printf '%s' "$out" | grep -q 'installed 1' || fail "installer did not report the install: $out"
ok "fresh install copies the rule and reports it"

# --- idempotence -------------------------------------------------------------
# A SessionStart hook runs on every session; re-copying each time would churn the
# disk and clobber a user's local edits on every prompt.
before="$(stat -c %Y "$DEST" 2>/dev/null || stat -f %m "$DEST")"
out2="$(GEMINI_HOME="$H/.gemini" HOME="$H" bash "$INSTALLER" 2>&1)" || fail "second run exited non-zero"
[ -z "$out2" ] || fail "unchanged rule should install silently, got: $out2"
after="$(stat -c %Y "$DEST" 2>/dev/null || stat -f %m "$DEST")"
[ "$before" = "$after" ] || fail "unchanged rule was rewritten"
ok "unchanged rule is not rewritten and prints nothing"

# --- content change reinstalls ----------------------------------------------
printf 'local edit\n' >> "$DEST"
out3="$(GEMINI_HOME="$H/.gemini" HOME="$H" bash "$INSTALLER" 2>&1)" || fail "third run exited non-zero"
cmp -s "$RULES" "$DEST" || fail "changed rule was not restored from source"
printf '%s' "$out3" | grep -q 'installed 1' || fail "changed rule reinstall was not reported"
ok "a changed rule is restored from source"

# --- no agy config tree ------------------------------------------------------
# agy was never run on this machine. Creating the tree ourselves would litter the
# disk for someone who does not use agy at all.
H2="$TMPROOT/noagy"; mkdir -p "$H2"
out4="$(GEMINI_HOME="$H2/.gemini" HOME="$H2" bash "$INSTALLER" 2>&1)" || fail "missing-config run exited non-zero"
[ ! -d "$H2/.gemini" ] || fail "installer created a Gemini home that did not exist"
[ -z "$out4" ] || fail "missing agy config should be silent, got: $out4"
ok "no agy config tree: does nothing, silently"

# --- unwritable destination --------------------------------------------------
# A SessionStart hook must never fail the session, whatever the filesystem says.
H3="$(fresh_home)"
mkdir -p "$H3/.gemini/config/plugins"
chmod 500 "$H3/.gemini/config/plugins" 2>/dev/null || true
# Git Bash on Windows accepts chmod and ignores it, so the directory stays writable
# and this case would assert nothing. Probe first and skip rather than pass falsely.
if [ "$(id -u)" != "0" ] && ! touch "$H3/.gemini/config/plugins/.probe" 2>/dev/null; then
  set +e
  out5="$(GEMINI_HOME="$H3/.gemini" HOME="$H3" bash "$INSTALLER" 2>&1)"; rc=$?
  set -e
  [ "$rc" -eq 0 ] || fail "unwritable destination must still exit 0, got $rc"
  printf '%s' "$out5" | grep -qi 'not installed' || fail "unwritable destination did not warn: $out5"
  ok "unwritable destination warns and still exits 0"
else
  rm -f "$H3/.gemini/config/plugins/.probe" 2>/dev/null || true
  printf '  skip unwritable-destination case (permissions not enforced here)\n'
fi
chmod 700 "$H3/.gemini/config/plugins" 2>/dev/null || true

printf '\nagy rules installer: %d checks passed\n' "$PASS"
