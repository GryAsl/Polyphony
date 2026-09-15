#!/usr/bin/env bash
#
# UserPromptSubmit hook: a cheap, deterministic nudge toward delegation when the
# user's prompt LOOKS like bulk work. Small tasks remain eligible through the
# global/session policy; this heuristic stays conservative to avoid context spam.
#
# Design principle: this supplies judgment MATERIAL — the DECISION stays with
# Claude. It never fires the wrapper itself; the fixed note only reminds Claude
# that the plugin is available.
#
# Heuristic is deliberately conservative (volume/fan-out phrases, EN + JA), and
# the nudge text is a FIXED string — the user's prompt is never echoed back into
# the context (no escaping/injection surface).
#
# Toggle via plugin userConfig `delegation_nudge`
# (env CLAUDE_PLUGIN_OPTION_DELEGATION_NUDGE: off/false/0/no/disabled). Default: on.
#
set -uo pipefail

raw="$(printf '%s' "${CLAUDE_PLUGIN_OPTION_DELEGATION_NUDGE:-on}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
case "$raw" in off|false|0|no|disabled) exit 0 ;; esac

IN="$(cat 2>/dev/null || true)"
[ -n "$IN" ] || exit 0

# Extract ONLY the prompt field (matching on the whole payload would false-positive
# on cwd/paths). Git Bash can expose a broken Windows Store `python3` alias, so probe
# the interpreter before using it and fall back to the Windows Python Launcher.
PY_CMD=()
if [ -n "${AGY_BRIDGE_PYTHON:-}" ] && "$AGY_BRIDGE_PYTHON" -c 'import sys' >/dev/null 2>&1; then
  PY_CMD=("$AGY_BRIDGE_PYTHON")
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
  PY_CMD=(python3)
elif command -v py >/dev/null 2>&1 && py -3 -c 'import sys' >/dev/null 2>&1; then
  PY_CMD=(py -3)
elif command -v python >/dev/null 2>&1 && python -c 'import sys' >/dev/null 2>&1; then
  PY_CMD=(python)
else
  exit 0
fi

# Must have a usable session id; if none, emit nothing and exit cleanly
# so it cannot attach state to an unrelated session.
SESSION_ID="$(printf '%s' "$IN" | "${PY_CMD[@]}" -c 'import json,sys
try:
    val = json.load(sys.stdin).get("session_id")
    if isinstance(val, str) and val.strip():
        print(val.strip())
except Exception: pass' 2>/dev/null || true)"
[ -n "$SESSION_ID" ] || exit 0

ACTIVE_MODE="$( "${PY_CMD[@]}" -c 'import hashlib,json,os,sys,tempfile
sid = sys.argv[1]
h = hashlib.sha256(sid.encode("utf-8","replace")).hexdigest()[:24]
d = os.environ.get("AGY_ROUTING_STATE_DIR")
if not d:
    for env_var in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        val = os.environ.get(env_var)
        if val:
            d = os.path.join(val, "agy-routing")
            break
if not d:
    d = os.path.join(tempfile.gettempdir(), "claude-agy-routing")
p = os.path.join(d, f"{h}.json")
try:
    s = json.load(open(p, encoding="utf-8"))
    print(s.get("mode") if s.get("mode") in ("strict", "soft") else "soft")
except Exception:
    print("soft")' "$SESSION_ID" 2>/dev/null || true )"

# When strict mode is active, strict routing is enforced by agy_opportunity_reminder.py.
# Do not emit contradictory "THE JUDGMENT IS YOURS" context; defer to the active mode.
if [ "$ACTIVE_MODE" = "strict" ]; then
  exit 0
fi

PROMPT="$(printf '%s' "$IN" | "${PY_CMD[@]}" -c 'import json,sys
try: print(json.load(sys.stdin).get("prompt",""))
except Exception: pass' 2>/dev/null || true)"
[ -n "$PROMPT" ] || exit 0

# Already delegating explicitly? Stay quiet.
case "$PROMPT" in *antigravity*|*agy-delegate*|*agy-job*) exit 0 ;; esac

shopt -s nocasematch
HIT=0
case "$PROMPT" in
  *"all files"*|*"every file"*|*"across the codebase"*|*"entire codebase"*|*"whole repo"*| \
  *migrate*|*migration*|*"generate tests"*|*"test coverage"*|*"exhaustive test"*| \
  *scaffold*|*boilerplate*|*"deep research"*|*"web search"*| \
  *一括*|*全ファイル*|*すべてのファイル*|*網羅*|*移行*|*大量*|*横断*|*リポジトリ全体*)
    HIT=1 ;;
esac
shopt -u nocasematch
[ "$HIT" -eq 1 ] || exit 0

# Fixed nudge. Note the explicit "the judgment is yours" — this is material, not a mandate.
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"[antigravity plugin] This prompt looks suitable for delegation. Proactively route a bounded unit to antigravity-delegate. Default to `flash` (High); `flash-medium` is an explicit option for clearly simple work. Both track the newest Gemini Flash family. Use a neutral task contract and digest-only output, then verify only as risk requires. Small tasks are eligible. THE JUDGMENT IS YOURS; decide silently and do not mention this notice."}}
JSON
exit 0
