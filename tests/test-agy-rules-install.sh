#!/usr/bin/env bash
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

head -5 "$RULES" | grep -q '^trigger: always_on$' \
  || fail "coding-quality.md frontmatter lacks 'trigger: always_on'"
ok "rule declares trigger: always_on"

words="$(wc -w < "$RULES" | tr -d '[:space:]')"
[ "$words" -lt 800 ] || fail "rule is $words words; keep it under 800"
ok "rule is $words words (< 800)"

H="$(fresh_home)"
out="$(GEMINI_HOME="$H/.gemini" HOME="$H" bash "$INSTALLER" 2>&1)" || fail "installer exited non-zero"
PLUGIN_DIR="$H/.gemini/config/plugins/polyphony"
DEST="$PLUGIN_DIR/rules/coding-quality.md"
[ -f "$DEST" ] || fail "rule was not installed to $DEST"
cmp -s "$RULES" "$DEST" || fail "installed rule differs from source"
printf '%s' "$out" | grep -q 'installed 1' || fail "installer did not report the install: $out"
ok "fresh install copies the rule and reports it"

MANIFEST="$PLUGIN_DIR/plugin.json"
[ -f "$MANIFEST" ] || fail "no plugin.json at $MANIFEST — agy will not read rules/"
grep -q '"name": "polyphony"' "$MANIFEST" || fail "plugin.json does not name the plugin: $(cat "$MANIFEST")"
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else PY=""; fi
if [ -n "$PY" ]; then
  "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["name"]=="polyphony", d' "$MANIFEST" \
    || fail "plugin.json is not valid JSON naming polyphony"
fi
printf '%s' "$out" | grep -q 'plugin.json' || fail "manifest write was not reported: $out"
ok "fresh install writes the plugin.json agy needs, and reports it"

[ ! -f "$PLUGIN_DIR/rules/plugin.json" ] || fail "plugin.json was written inside rules/"
ok "manifest sits at the plugin root, not inside rules/"

before="$(stat -c %Y "$DEST" 2>/dev/null || stat -f %m "$DEST")"
m_before="$(stat -c %Y "$MANIFEST" 2>/dev/null || stat -f %m "$MANIFEST")"
out2="$(GEMINI_HOME="$H/.gemini" HOME="$H" bash "$INSTALLER" 2>&1)" || fail "second run exited non-zero"
[ -z "$out2" ] || fail "unchanged rule should install silently, got: $out2"
after="$(stat -c %Y "$DEST" 2>/dev/null || stat -f %m "$DEST")"
m_after="$(stat -c %Y "$MANIFEST" 2>/dev/null || stat -f %m "$MANIFEST")"
[ "$before" = "$after" ] || fail "unchanged rule was rewritten"
[ "$m_before" = "$m_after" ] || fail "unchanged manifest was rewritten"
ok "unchanged rule and manifest are not rewritten, and print nothing"

rm -f "$MANIFEST"
out_up="$(GEMINI_HOME="$H/.gemini" HOME="$H" bash "$INSTALLER" 2>&1)" || fail "upgrade run exited non-zero"
[ -f "$MANIFEST" ] || fail "missing manifest was not restored when the rules were unchanged"
printf '%s' "$out_up" | grep -q 'plugin.json' || fail "manifest repair was not reported: $out_up"
ok "a missing manifest is restored even when no rule changed"

printf 'local edit\n' >> "$DEST"
out3="$(GEMINI_HOME="$H/.gemini" HOME="$H" bash "$INSTALLER" 2>&1)" || fail "third run exited non-zero"
cmp -s "$RULES" "$DEST" || fail "changed rule was not restored from source"
printf '%s' "$out3" | grep -q 'installed 1' || fail "changed rule reinstall was not reported"
ok "a changed rule is restored from source"

printf 'not json\n' > "$MANIFEST"
out4="$(GEMINI_HOME="$H/.gemini" HOME="$H" bash "$INSTALLER" 2>&1)" || fail "manifest repair run exited non-zero"
grep -q '"name": "polyphony"' "$MANIFEST" || fail "corrupted manifest was not restored"
ok "a corrupted manifest is restored from source"

H2="$TMPROOT/noagy"; mkdir -p "$H2"
out5="$(GEMINI_HOME="$H2/.gemini" HOME="$H2" bash "$INSTALLER" 2>&1)" || fail "missing-config run exited non-zero"
[ ! -d "$H2/.gemini" ] || fail "installer created a Gemini home that did not exist"
[ -z "$out5" ] || fail "missing agy config should be silent, got: $out5"
ok "no agy config tree: does nothing, silently"

H3="$(fresh_home)"
mkdir -p "$H3/.gemini/config/plugins"
chmod 500 "$H3/.gemini/config/plugins" 2>/dev/null || true
if [ "$(id -u)" != "0" ] && ! touch "$H3/.gemini/config/plugins/.probe" 2>/dev/null; then
  set +e
  out6="$(GEMINI_HOME="$H3/.gemini" HOME="$H3" bash "$INSTALLER" 2>&1)"; rc=$?
  set -e
  [ "$rc" -eq 0 ] || fail "unwritable destination must still exit 0, got $rc"
  printf '%s' "$out6" | grep -qi 'not installed' || fail "unwritable destination did not warn: $out6"
  ok "unwritable destination warns and still exits 0"
else
  rm -f "$H3/.gemini/config/plugins/.probe" 2>/dev/null || true
  printf '  skip unwritable-destination case (permissions not enforced here)\n'
fi
chmod 700 "$H3/.gemini/config/plugins" 2>/dev/null || true

PS_EXE=""
for c in pwsh powershell.exe powershell; do
  command -v "$c" >/dev/null 2>&1 && { PS_EXE="$c"; break; }
done
if [ -n "$PS_EXE" ]; then
  H4="$(fresh_home)"
  WIN_HOME="$H4"
  command -v cygpath >/dev/null 2>&1 && WIN_HOME="$(cygpath -w "$H4")"
  GEMINI_HOME="$WIN_HOME\\.gemini" "$PS_EXE" -NoProfile -ExecutionPolicy Bypass \
    -File "$ROOT/hooks/install-agy-rules.ps1" >/dev/null 2>&1 \
    || fail "powershell installer exited non-zero"
  PS_MANIFEST="$H4/.gemini/config/plugins/polyphony/plugin.json"
  [ -f "$PS_MANIFEST" ] || fail "powershell installer wrote no manifest to $PS_MANIFEST"
  cmp -s "$MANIFEST" "$PS_MANIFEST" \
    || fail "sh and ps1 manifests differ:$(printf '\n')$(diff "$MANIFEST" "$PS_MANIFEST" || true)"
  ok "sh and ps1 write a byte-identical manifest"
else
  printf '  skip installer-parity case (no powershell here)\n'
fi

printf '\nagy rules installer: %d checks passed\n' "$PASS"
