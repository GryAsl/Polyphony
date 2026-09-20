#!/usr/bin/env bash
#
# agy-delegate.sh — robust headless wrapper around the Antigravity CLI (`agy`).
# Part of Polyphony.
#
# Purpose: let Claude Code (the orchestrator) hand a single, well-scoped subtask
# to an Antigravity (Gemini) agent via `agy --print`, and get clean text back on
# stdout — for delegation, cross-model checks, or offloading bulk work.
#
# Why a wrapper instead of calling `agy` directly:
#   * `agy --print` silently drops stdout when stdin is a non-TTY -> we always
#     redirect `< /dev/null` so it never blocks waiting for input.
#   * agy v1.0.x has NO `--output-format json`, so callers must parse plain text.
#     This wrapper guarantees: non-empty stdout on success, non-zero exit on
#     failure or empty output.
#   * Human-friendly tiers (flash-medium / flash / pro) instead of exact model strings.
#
# Usage:
#   agy-delegate.sh [options] "the task prompt"
#   echo "long prompt" | agy-delegate.sh [options] -      # read prompt from stdin
#
# Options:
#   -t, --tier <flash-medium|flash|pro>  Model tier (default: flash / High)
#   -d, --dir  <path>                Add a workspace dir (repeatable)
#       --timeout <dur>              Print-mode timeout, e.g. 45m (default: 30m)
#       --idle-timeout <secs>        Native Windows no-output timeout. Default: just
#                                    above the hard timeout, so JSON work may finish
#       --yolo                       Auto-approve all tool permissions (DANGEROUS)
#       --sandbox                    Run agent with terminal sandbox restrictions
#       --digest                     Append a digest-only output contract to the prompt
#                                    (ingest digests, not raw dumps — the biggest cost lever)
#       --mode <accept-edits|plan>   agy execution mode (agy >= 1.1.0). accept-edits is NOT a
#                                    write grant: measured on agy 1.1.13, where the flag is
#                                    applied at all (1.1.12 fixed it being ignored headless),
#                                    the write is denied exactly like one without it. Use a
#                                    permissions.allow rule or --yolo. plan: strategize only.
#   -c, --continue                   Resume the most recent agy conversation (stateful)
#       --conversation <id>          Resume a specific agy conversation by ID (stateful)
#   -m, --model <exact name>         Use an exact agy model (any from `agy models`: Gemini/Claude/GPT…)
#       --print-command              Print the resolved agy command and exit (dry run)
#   -h, --help                       Show this help
#
# Exit codes: 0 ok | 1 usage | 2 agy failed | 3 empty | 10 quota | 11 auth | 12 timeout
#             | 13 agy missing | 14 model unavailable (--model / tier remap not in `agy models`)
#             | 15 permission denied — a tool needed permission headless. BOTH shapes:
#             |    agy 1.1.3's soft deny (rc 0, empty stdout) and 1.1.13's hard error
#             |    (rc 1, "user denied permission"). Add a permissions.allow rule, or --yolo
#             | 16 Windows ConPTY bridge/Python unavailable
#             | 19 selected Gemini model has no server capacity (503)
#
# On a classifiable failure, a machine-readable line is printed to stderr so
# orchestrators (e.g. agy-job.sh) can react without scraping prose:
#   AGY_SIGNAL {"status":"QUOTA_DECISION_REQUIRED","reason":"...","model":"...","retry":"ask-user"}
#
# AGY_USAGE / AGY_SIGNAL go to stderr. If you are MEASURING cost, also set
# AGY_USAGE_LOG=/path/to/log: stderr is easily lost (`2>&1 | tail -N` keeps the
# digest and drops the usage line — see tee_usage below), a named file is not.
#
# agy is multi-model: tiers map to Gemini by default, but you can point delegation at any
# model `agy models` lists (e.g. Claude/GPT on plans that expose them). Defaults via plugin
# userConfig (env): CLAUDE_PLUGIN_OPTION_DEFAULT_TIER, _TIMEOUT, _IDLE_TIMEOUT,
# _ALWAYS_YOLO,
# _DEFAULT_MODEL (exact name), _USAGE_LOG, and per-tier remaps _TIER_FLASH_MEDIUM /
# _TIER_FLASH / _TIER_PRO. Explicit flags win; AGY_BRIDGE_IDLE_TIMEOUT wins
# over _IDLE_TIMEOUT; AGY_USAGE_LOG wins over _USAGE_LOG.
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

TIER="${CLAUDE_PLUGIN_OPTION_DEFAULT_TIER:-flash}"
TIMEOUT="${CLAUDE_PLUGIN_OPTION_TIMEOUT:-30m}"
IDLE_TIMEOUT="${CLAUDE_PLUGIN_OPTION_IDLE_TIMEOUT:-}"
IDLE_TIMEOUT_EXPLICIT=0
TIER_EXPLICIT=0
MODEL=""
YOLO_RAW="${AGY_ALWAYS_YOLO:-${CLAUDE_PLUGIN_OPTION_ALWAYS_YOLO:-off}}"
case "$(printf '%s' "$YOLO_RAW" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  1|on|true|yes|enabled) YOLO=1 ;;
  *)                     YOLO=0 ;;
esac
SANDBOX=0
DIGEST=0
MODE=""
ADD_DIRS=()
PROMPT=""
PROMPT_FROM_STDIN=0
CONTINUE=0
CONV_ID=""
PRINT_CMD=0
BASE_PROMPT=""
ACCOUNT_WORKER_ID="${AGY_ACCOUNT_WORKER_ID:-polyphony-$$}"
ACCOUNT_WORKER_REGISTERED=0

die() { echo "agy-delegate: $*" >&2; exit 1; }
# $1 = remaining argc ($#). Fail with a friendly message if an option has no value
# (avoids `shift 2` aborting under `set -e` with a cryptic "shift count" error).
need() { [ "$1" -ge 2 ] || die "option '$2' needs a value"; }

# Optional side channel for AGY_USAGE / AGY_SIGNAL.
#
# WHY this exists: these lines go to stderr so they never pollute the conductor's
# context — but this skill also tells the conductor to keep its context lean, and
# the natural way to do that is `agy-delegate ... 2>&1 | tail -N`. stdout (the
# digest) is written after the usage line, so `tail` keeps the digest and throws
# the usage line away. Measured in the wild: a benchmark harness lost most of its
# Gemini-side cost data exactly this way, which made the hybrid look cheaper than
# it was. A file the caller names cannot be truncated by a pipe.
#
# Set AGY_USAGE_LOG=/path/to/log (or the plugin option `usage_log`). Appended to,
# never truncated; failure to write is non-fatal (measurement must not break work).
USAGE_LOG="${AGY_USAGE_LOG:-${CLAUDE_PLUGIN_OPTION_USAGE_LOG:-}}"
tee_usage() { # $1 = the full line, already formatted
  [ -n "$USAGE_LOG" ] || return 0
  # `2>/dev/null` FIRST: redirections apply left to right, so with `>>"$f" 2>/dev/null`
  # the append is attempted while stderr is still the real stderr — an unwritable path
  # then leaks a bash redirection error on every single call. Order matters here.
  printf '%s\n' "$1" 2>/dev/null >>"$USAGE_LOG" || true
}

# Emit a one-line machine-readable failure signal to stderr. $1=status $2=reason.
# Quota decisions are owned by the user, not by an automatic model fallback.
signal() {
  local status="$1" reason="$2" retry="" line
  [ "$status" = "QUOTA_DECISION_REQUIRED" ] && retry="ask-user"
  [ "$status" = "QUOTA_WAITING" ] && retry="10m"
  # sanitize reason so the JSON stays single-line and valid (no quotes/backslashes/newlines)
  reason="$(printf '%s' "$reason" | tr '\n\r\t' '   ' | tr -d '"\\' | cut -c1-200)"
  line="$(printf 'AGY_SIGNAL {"status":"%s","reason":"%s","model":"%s","retry":"%s"}' \
    "$status" "$reason" "${MODEL:-}" "$retry")"
  printf '%s\n' "$line" >&2
  tee_usage "$line"
}

# The write-without-grant failure (issue #10) has changed shape three times, and the
# LAST change silently killed this branch. agy 1.1.3 soft-denied: rc=0, empty stdout,
# "auto-denied" on stderr. By 1.1.13 it is a HARD error — rc=1, and the diagnostic reads
# `permission check failed for write_file "...": user denied permission for
# write_file(...)`, which contains none of the old anchors and lands in the rc != 0
# branch, above the soft-deny check entirely. So the most documented failure in this
# plugin came back as a bare "agy exited 1" with none of the guidance below.
#
# 0.22.5 checked that the old anchors were still present in the agy binary and concluded
# exit 15 was intact. The strings were there; the ROUTE was not. Measured on 1.1.13:
# both a plain write and `--mode accept-edits` produce the hard error.
#
# One function, called from both branches, so the two shapes cannot drift apart again.
permission_denied() {   # $1 = "shown" when the caller already echoed $ERR
  # The rc != 0 path dumps $ERR before it classifies, so echoing it again here printed
  # agy's diagnostic twice on the plain-stderr shape. Both reviewers caught it.
  # An `if`, not `cond || { ...; }`. The group is the LAST element of that list, so a
  # failure inside it is NOT exempt from `set -e` — and this file runs `set -euo pipefail`
  # (line 60). With $ERR empty the group returns 1, the shell exits, and the guidance and
  # the AGY_SIGNAL below never run. Only the callers keep that from happening today.
  #
  # The first version of this comment said the file uses `set -uo pipefail` and called the
  # risk theoretical. That was copied from doctor.sh, which really has no `-e`. A reviewer
  # checked the line instead of believing the sentence.
  if [ "${1:-}" != shown ] && [ -s "$ERR" ]; then
    cat "$ERR" >&2
  fi
  echo "agy-delegate: agy denied a tool that needs permission (headless can't prompt) — no work was done. For a FILE WRITE, the narrower fix is a permissions.allow rule covering the target in ~/.gemini/antigravity-cli/settings.json — write_file(<dir>) matches recursively beneath <dir> — which needs no flag; --yolo also works but auto-approves ALL tools. Other tools (web / Vertex AI Search / terminal) need --yolo unless a rule covers them. \`--mode accept-edits\` is NOT a write grant: measured on agy 1.1.13 it is denied exactly like a plain write. agy's own message above names the specific permission it wanted. If a rule is ALREADY in place and you are still reading this, suspect the rule: run agy-doctor, because an entry agy cannot parse grants nothing. (A command(...) rule naming no command ALSO auto-approved everything before agy 1.1.11; a mistyped write_file() never did.)" >&2
  signal PERMISSION_DENIED "agy denied a permissioned tool in headless — add a permissions.allow rule or pass --yolo"
  exit 15
}

# Print the header comment between "# Usage:" and "# Exit codes:" (anchored to
# content, not line numbers, so it never desyncs when the header changes).
usage() { sed -n '/^# Usage:/,/^# Exit codes:/p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# --- map a tier to an exact agy model name (see `agy models`) ---
# Defaults are Gemini, but each tier is remappable to any agy model via userConfig
# (env), so non-Vertex/non-Gemini plans (Claude/GPT) work without code changes.
model_for_tier() {
  case "$1" in
    flash-medium)
      if [ -n "${CLAUDE_PLUGIN_OPTION_TIER_FLASH_MEDIUM:-}" ]; then
        echo "$CLAUDE_PLUGIN_OPTION_TIER_FLASH_MEDIUM"
      else
        resolve_flash_model medium "Gemini 3.8 Flash (Medium)"
      fi ;;
    flash)
      if [ -n "${CLAUDE_PLUGIN_OPTION_TIER_FLASH:-}" ]; then
        echo "$CLAUDE_PLUGIN_OPTION_TIER_FLASH"
      else
        resolve_flash_model high "Gemini 3.8 Flash (High)"
      fi ;;
    pro)      echo "${CLAUDE_PLUGIN_OPTION_TIER_PRO:-Gemini 3.1 Pro (High)}" ;;
    *) die "unknown tier '$1' (use flash-medium | flash | pro)" ;;
  esac
}

resolve_flash_model() { # $1 = medium|high, $2 = stable fallback
  local effort="$1" fallback="$2" resolved=""
  if resolve_bridge_python; then
    resolved="$("${BRIDGE_PY[@]}" "$HERE/resolve-flash-model.py" \
      --effort "$effort" --fallback "$fallback" 2>/dev/null || true)"
  fi
  [ -n "$resolved" ] && printf '%s\n' "$resolved" || printf '%s\n' "$fallback"
}

# True if $1 is a Gemini Flash model (display name or slug).
is_gemini_flash_model() {
  local m
  m="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "$m" in
    *gemini*flash*|*flash*gemini*) return 0 ;;
    *) return 1 ;;
  esac
}

is_gemini_model() {
  local m
  m="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "$m" in *gemini*) return 0 ;; *) return 1 ;; esac
}

# Sonnet 4.6 is used only after the user explicitly approves quota fallback.
# Its agent runtime may otherwise try to hand the task to another agent and return
# before that nested work finishes. Give it a direct-execution contract and require
# a deterministic receipt so rc=0 alone can never masquerade as completed work.
is_sonnet_46_model() {
  local m
  m="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d ' ._-')"
  case "$m" in *claude*sonnet*46*) return 0 ;; *) return 1 ;; esac
}

# Run the zero-token /usage quota helper. Tests may replace it with one executable.
quota_run() {
  if [ -n "${AGY_QUOTA_COMMAND:-}" ]; then
    "$AGY_QUOTA_COMMAND" "$@"
    return $?
  fi
  if resolve_bridge_python; then
    "${BRIDGE_PY[@]}" "$HERE/agy-quota.py" "$@"
    return $?
  fi
  return 2
}

# Account-pool calls use the same Python resolution as the Windows bridge. They
# never receive credential bytes; the manager talks to Credential Manager itself.
account_run() {
  resolve_bridge_python || return 127
  "${BRIDGE_PY[@]}" "$HERE/agy_account.py" --json "$@"
}

account_json_field() { # $1=json, $2=top-level key
  local payload="$1" key="$2"
  resolve_bridge_python || return 1
  printf '%s' "$payload" | "${BRIDGE_PY[@]}" -c 'import json,sys; d=json.load(sys.stdin); v=d.get(sys.argv[1]); print((str(v).lower() if isinstance(v,bool) else v) if v is not None else "")' "$key" 2>/dev/null
}

account_pool_enabled() {
  local pool_json enabled
  set +e
  pool_json="$(account_run list 2>/dev/null)"
  set -e
  enabled="$(account_json_field "$pool_json" pool_enabled 2>/dev/null || true)"
  [ "$enabled" = true ]
}

account_retry_on_current() {
  local tried="$1"; shift
  local -a retry_args=(--model "$MODEL" --timeout "$TIMEOUT")
  [ "$IDLE_TIMEOUT_EXPLICIT" -eq 0 ] || retry_args+=(--idle-timeout "$IDLE_TIMEOUT")
  [ "$YOLO" -eq 0 ] || retry_args+=(--yolo)
  [ "$SANDBOX" -eq 0 ] || retry_args+=(--sandbox)
  [ "$DIGEST" -eq 0 ] || retry_args+=(--digest)
  # Structured output is selected from the inherited plugin option and the
  # child's own capability probe; --json is not a delegate-wrapper CLI flag.
  [ -z "$MODE" ] || retry_args+=(--mode "$MODE")
  for d in "${ADD_DIRS[@]:-}"; do [ -z "$d" ] || retry_args+=(--dir "$d"); done
  # Conversation IDs and --continue are intentionally dropped: they belong to
  # the prior account. The fresh invocation reports its own conversation ID.
  printf '%s' "$BASE_PROMPT" | AGY_ACCOUNT_FAILOVER_TRIED="$tried" \
    AGY_ACCOUNT_WORKER_ID="$ACCOUNT_WORKER_ID" "$HERE/agy-delegate.sh" "${retry_args[@]}" -
}

account_try_failover() { # $1=quota|auth; succeeds only by replacing this process result
  [ "${AGY_QUOTA_PROBE:-0}" != 1 ] || return 1
  is_gemini_model "$MODEL" || return 1
  account_pool_enabled || return 1
  local reason="$1" current_json current tried result rc next
  set +e
  current_json="$(account_run current 2>/dev/null)"; rc=$?
  set -e
  [ "$rc" -eq 0 ] || return 1
  current="$(account_json_field "$current_json" current || true)"
  [ -n "$current" ] || return 1
  tried="${AGY_ACCOUNT_FAILOVER_TRIED:-}"
  case ",$tried," in *",$current,"*) return 1 ;; esac
  tried="${tried:+$tried,}$current"
  set +e
  result="$(POLYPHONY_SWITCH_OWNER="$ACCOUNT_WORKER_ID" account_run rotate-after-failure "$current" --tried "$tried" --reason "$reason" 2>/dev/null)"; rc=$?
  set -e
  [ "$rc" -eq 0 ] || return 1
  next="$(account_json_field "$result" current || true)"
  [ -n "$next" ] || return 1
  printf 'AGY_ACCOUNT_FAILOVER {"from":"%s","to":"%s","reason":"%s"}\n' "$current" "$next" "$reason" >&2
  account_unregister_worker
  set +e
  account_retry_on_current "$tried"
  rc=$?
  set -e
  exit "$rc"
}

account_preflight() {
  [ "${AGY_QUOTA_PROBE:-0}" != 1 ] || return 0
  [ "$PRINT_CMD" -ne 1 ] || return 0
  is_gemini_model "$MODEL" || return 0
  local pool_json enabled
  set +e
  pool_json="$(account_run list 2>/dev/null)"
  set -e
  enabled="$(account_json_field "$pool_json" pool_enabled 2>/dev/null || true)"
  [ "$enabled" = true ] || return 0
  set +e
  account_run ensure-active >/dev/null 2>&1
  set -e
  return 0
}

account_register_worker() {
  [ "${AGY_QUOTA_PROBE:-0}" != 1 ] || return 0
  is_gemini_model "$MODEL" || return 0
  account_pool_enabled || return 0
  local current_json current
  set +e
  current_json="$(account_run current 2>/dev/null)"
  set -e
  current="$(account_json_field "$current_json" current 2>/dev/null || true)"
  [ -n "$current" ] || return 0
  account_run worker-start "$ACCOUNT_WORKER_ID" --account "$current" >/dev/null 2>&1 || return 0
  ACCOUNT_WORKER_REGISTERED=1
}

account_unregister_worker() {
  [ "$ACCOUNT_WORKER_REGISTERED" -eq 1 ] || return 0
  account_run worker-stop "$ACCOUNT_WORKER_ID" >/dev/null 2>&1 || true
  ACCOUNT_WORKER_REGISTERED=0
}

quota_status_from() { printf '%s' "$1" | sed -n 's/^AGY_QUOTA .*"status":"\([^"]*\)".*/\1/p'; }
quota_decision_from() { printf '%s' "$1" | sed -n 's/^AGY_QUOTA .*"decision":"\([^"]*\)".*/\1/p'; }

quota_decision_required() { # $1 = AGY_QUOTA line (possibly empty)
  local quota_line="${1:-}"
  if [ -n "$quota_line" ]; then
    printf '%s\n' "$quota_line" >&2
    tee_usage "$quota_line"
  fi
  echo 'agy-delegate: Agy Gemini quota is depleted. Ask the user before taking any fallback action: "Agy Gemini quota is depleted. Would you like me to (1) kill any active Agy workers that are no longer progressing and continue with Claude Sonnet 4.6, or (2) keep the workers alive and wait for the 5h/7d quota to reset while I check both quotas every 10 minutes?"' >&2
  signal QUOTA_DECISION_REQUIRED "Gemini 5h or 7d quota is exhausted / at or below 2%; user must choose Sonnet or 10-minute monitoring"
  exit 10
}

quota_waiting() { # $1 = AGY_QUOTA line
  if [ -n "${1:-}" ]; then
    printf '%s\n' "$1" >&2
    tee_usage "$1"
  fi
  echo "agy-delegate: user selected quota waiting; do not kill workers or start Sonnet. Recheck both Gemini quota windows every 10 minutes." >&2
  signal QUOTA_WAITING "waiting for Gemini 5h/7d quota recovery by user choice"
  exit 10
}

# A cached depleted state prevents repeated doomed Gemini launches. Only an explicit
# user decision recorded by `agy-quota --decision sonnet` permits the model switch.
quota_preflight() {
  [ "${AGY_QUOTA_PROBE:-0}" != 1 ] || return 0
  [ "$PRINT_CMD" -ne 1 ] || return 0
  is_gemini_model "$MODEL" || return 0
  local quota_line status decision notice
  set +e
  # A normal (cached) check refreshes after ten minutes. This makes a previously
  # approved Sonnet fallback self-clear as soon as both Gemini windows recover.
  quota_line="$(quota_run --json 2>/dev/null)"
  set -e
  status="$(quota_status_from "$quota_line")"
  [ "$status" = DEPLETED ] || return 0
  account_try_failover quota || true
  decision="$(quota_decision_from "$quota_line")"
  case "$decision" in
    sonnet)
      notice="$(printf 'AGY_FALLBACK {"reason":"USER_APPROVED_QUOTA_FALLBACK","from":"%s","to":"claude-sonnet-4-6"}' "$MODEL")"
      printf '%s\n' "$quota_line" >&2
      printf '%s\n' "$notice" >&2
      tee_usage "$quota_line"
      tee_usage "$notice"
      MODEL="claude-sonnet-4-6"
      ;;
    wait) quota_waiting "$quota_line" ;;
    *) quota_decision_required "$quota_line" ;;
  esac
}

# On any problematic Gemini run, force-refresh both windows. A <=2% window upgrades
# timeout/empty/generic failures to the quota decision state. An explicit 429/quota
# diagnostic enters that state even if the quota endpoint is temporarily unavailable.
quota_failure_check() { # $1 = 1 for explicit quota diagnostic, otherwise 0
  [ "${AGY_QUOTA_PROBE:-0}" != 1 ] || return 1
  is_gemini_model "$MODEL" || return 1
  local explicit="${1:-0}" quota_line status
  set +e
  quota_line="$(quota_run --force --json 2>/dev/null)"
  set -e
  status="$(quota_status_from "$quota_line")"
  if [ "$status" = DEPLETED ]; then
    account_try_failover quota || true
    quota_decision_required "$quota_line"
  fi
  if [ "$explicit" = 1 ]; then
    set +e
    quota_line="$(quota_run --mark-depleted --json 2>/dev/null)"
    set -e
    account_try_failover quota || true
    quota_decision_required "$quota_line"
  fi
  return 1
}

quota_after_success() {
  [ "${AGY_QUOTA_PROBE:-0}" != 1 ] || return 0
  is_gemini_model "$MODEL" || return 0
  local notices
  set +e
  notices="$(quota_run --alerts-only 2>/dev/null)"
  set -e
  if [ -n "$notices" ]; then
    printf '%s\n' "$notices" >&2
    while IFS= read -r line; do [ -z "$line" ] || tee_usage "$line"; done <<<"$notices"
  fi
}

# True when running under WSL (Windows Subsystem for Linux).
on_wsl() { [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; }

# True on native Windows (Git Bash / MSYS / Cygwin) — NOT WSL. On native Windows
# without a real console (ConPTY), agy v1.0.x can hard-hang with a 0-byte log when
# its stdio is redirected (the issue this wall-clock guard defends against).
on_windows_native() {
  [ "${AGY_TEST_FORCE_POSIX:-0}" = 1 ] && return 1
  case "${OSTYPE:-}" in msys*|cygwin*|win32) return 0 ;; esac
  case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac
  return 1
}
# Resolve the Windows interpreter that owns agy-headless-bridge. AGY_BRIDGE_PYTHON
# is an executable path (not a shell command string); the default prefers the Windows
# Python Launcher so the Microsoft Store python3 alias cannot masquerade as Python.
BRIDGE_PY=()
resolve_bridge_python() {
  BRIDGE_PY=()
  if [ -n "${AGY_BRIDGE_PYTHON:-}" ]; then
    if [ -x "$AGY_BRIDGE_PYTHON" ] || command -v "$AGY_BRIDGE_PYTHON" >/dev/null 2>&1; then
      BRIDGE_PY=("$AGY_BRIDGE_PYTHON")
      return 0
    fi
    return 1
  fi
  if command -v py >/dev/null 2>&1 && py -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    BRIDGE_PY=(py -3); return 0
  fi
  if command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    BRIDGE_PY=(python); return 0
  fi
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    BRIDGE_PY=(python3); return 0
  fi
  return 1
}
# Convert a Git Bash/MSYS path before handing it to native Windows Python.
windows_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -aw "$1"; else printf '%s\n' "$1"; fi
}
# Convert an agy duration (5m, 300s, 1h, or bare seconds) to whole seconds.
duration_secs() {
  local d="${1:-30m}" n unit secs
  n="${d%[smh]}"; unit="${d#"$n"}"
  case "$n" in (*[!0-9]*|'') n=1800; unit=s ;; esac
  case "$unit" in
    h) secs=$(( n * 3600 )) ;;
    m) secs=$(( n * 60 )) ;;
    s|'') secs=$(( n )) ;;
    *) secs=$(( n )) ;;
  esac
  printf '%s\n' "$secs"
}

# Resolve a usable `timeout`-style command: GNU coreutils `timeout`, or macOS
# Homebrew's `gtimeout`. Echoes the command name, or empty if neither exists.
# We wrap agy in this so a headless/no-TTY hang (agy never returns) is bounded by
# wall-clock — agy's own --print-timeout can't fire if it hangs before starting.
timeout_cmd() {
  if command -v timeout  >/dev/null 2>&1; then echo timeout;  return 0; fi
  if command -v gtimeout >/dev/null 2>&1; then echo gtimeout; return 0; fi
  return 1
}

# Convert an agy-style duration (e.g. 5m, 300s, 1h, or a bare number=seconds) to
# whole seconds, then add a small head-room margin so the OUTER wall-clock guard
# fires only AFTER agy's own --print-timeout has had its chance. Echoes seconds.
outer_timeout_secs() {
  local secs; secs="$(duration_secs "${1:-30m}")"
  # head-room so the OUTER guard never pre-empts agy's own --print-timeout on a
  # legitimately-slow-but-progressing call: +25% of the budget, min 10s, capped 120s.
  local pad=$(( secs / 4 ))
  [ "$pad" -lt 10 ]  && pad=10
  [ "$pad" -gt 120 ] && pad=120
  echo $(( secs + pad ))
}

while [ $# -gt 0 ]; do
  case "$1" in
    -t|--tier)      need "$#" "$1"; TIER="$2"; TIER_EXPLICIT=1; shift 2 ;;
    -d|--dir)       need "$#" "$1"; ADD_DIRS+=("$2"); shift 2 ;;
    --timeout)      need "$#" "$1"; TIMEOUT="$2"; shift 2 ;;
    --idle-timeout) need "$#" "$1"; IDLE_TIMEOUT="$2"; IDLE_TIMEOUT_EXPLICIT=1; shift 2 ;;
    --yolo)         YOLO=1; shift ;;
    --sandbox)      SANDBOX=1; shift ;;
    --digest)       DIGEST=1; shift ;;               # ask agy for a digest-only reply
    --mode)         need "$#" "$1"; MODE="$2"; shift 2
                    case "$MODE" in accept-edits|plan) ;;
                      *) die "invalid --mode '$MODE' (use accept-edits | plan; agy >= 1.1.0)" ;;
                    esac ;;
    -c|--continue)  CONTINUE=1; shift ;;            # resume most recent agy conversation
    --conversation) need "$#" "$1"; CONV_ID="$2"; shift 2 ;; # resume a specific conversation by ID
    -m|--model)     need "$#" "$1"; MODEL="$2"; shift 2 ;;
    --print-command) PRINT_CMD=1; shift ;;          # dry run: show the resolved agy command
    -h|--help)      usage ;;
    -)              PROMPT="$(cat)"; PROMPT_FROM_STDIN=1; shift ;; # read prompt from stdin
    --)             shift; PROMPT="${*:-}"; break ;;
    -*)             die "unknown option '$1'" ;;
    *)              PROMPT="$*"; break ;;            # rest is the prompt
  esac
done

[ -n "$PROMPT" ] || die "no prompt given (pass a string, or '-' to read stdin)"
# Authored instructions stay compact on every transport, including assembled files.
# Only review's already-validated goal plus machine-generated diff payload is exempt.
if [ "${AGY_REVIEW_DATA_PAYLOAD:-0}" != 1 ] || [ "${AGY_DELEGATE_READ_ONLY:-0}" != 1 ] || [ "$PROMPT_FROM_STDIN" -ne 1 ]; then
  PROMPT_WORDS="$(printf '%s' "$PROMPT" | wc -w | tr -d '[:space:]')"
  [ "$PROMPT_WORDS" -le 800 ] || die "prompt has ${PROMPT_WORDS} words. Do not retry it in chunks. Rewrite the complete contract to the shortest sufficient form, normally 200-500 words and never more than 800; include only objective, paths/scope, non-negotiable constraints, acceptance checks, and a compact receipt. Files, stdin, and multiple writes do not bypass this gate."
fi
BASE_PROMPT="$PROMPT"
# --print-command is a dry run (introspection), so it doesn't require agy on PATH.
# On Windows the bridge also honours AGY_PATH and agy's default install dirs.
if [ "$PRINT_CMD" -ne 1 ] && ! command -v agy >/dev/null 2>&1 \
   && ! on_windows_native; then
  echo "agy-delegate: 'agy' not found on PATH — install the Antigravity CLI first" >&2
  signal AGY_MISSING "agy not on PATH"
  exit 13
fi

# Resolve the executor model. Precedence:
#   --model > explicit --tier > userConfig default_model > default tier (mapped).
# agy is multi-model; tiers default to Gemini but are remappable (see model_for_tier).
if [ -z "$MODEL" ]; then
  if [ "$TIER_EXPLICIT" -eq 1 ]; then
    MODEL="$(model_for_tier "$TIER")"
  elif [ -n "${CLAUDE_PLUGIN_OPTION_DEFAULT_MODEL:-}" ]; then
    MODEL="$CLAUDE_PLUGIN_OPTION_DEFAULT_MODEL"
  else
    # default tier from userConfig; a bad value shouldn't make every call die.
    case "$TIER" in
      flash-medium|flash|pro) ;;
      *) echo "agy-delegate: invalid default tier '$TIER' (set CLAUDE_PLUGIN_OPTION_DEFAULT_TIER to flash-medium|flash|pro); using flash" >&2; TIER="flash" ;;
    esac
    MODEL="$(model_for_tier "$TIER")"
  fi
fi

account_preflight
quota_preflight

# WSL gotcha: agy reads --add-dir over the /mnt/* Windows mount via a slow 9p bridge,
# so even trivial calls can take 20s+. Warn (don't fail); the fix is to move the repo
# into the WSL Linux filesystem (~).
if on_wsl && [ "${AGY_QUOTA_PROBE:-0}" != 1 ]; then
  for d in "${ADD_DIRS[@]:-}"; do
    [ -n "$d" ] || continue
    case "$d" in
      /mnt/*) echo "agy-delegate: note: --add-dir '$d' is on a Windows mount under WSL; agy reads it over a slow 9p bridge (calls can take 20s+). Move the repo into the Linux FS (~) for ~10x faster I/O." >&2; break ;;
    esac
  done
fi

# Heads-up: a likely write task with no visible write grant. Headless agy's write
# behavior has shifted across versions (describe-only pre-1.1.0; scratch-divert
# 1.1.0-1.1.2; soft-deny on 1.1.3+; hard error by 1.1.13), and without a grant YOUR
# WORKSPACE IS UNTOUCHED while the run still "succeeds" (issue #10).
#
# There are TWO grants, and this used to claim there was one. A `write_file(<dir>)`
# rule under `permissions.allow` in ~/.gemini/antigravity-cli/settings.json grants
# headless writes beneath that path with no --yolo at all — confirmed on agy 1.1.9 by
# a controlled A/B (#37): covered target wrote, uncovered target came back
# PERMISSION_DENIED with the rule as the only variable. The match is a RECURSIVE
# PREFIX, not one directory. agy's own denial text names the rule and offers --yolo as
# the alternative, so the CLI has been saying this for a while and we were not.
#
# We cannot see settings.json from here (it is not ours, and --dir is not where it
# lives), so this stays a warning rather than a check — but it must not assert that
# --yolo is required. It fired immediately before a write that then succeeded.
# (--mode accept-edits is NOT a grant: measured on agy 1.1.13, where the flag is
#  actually applied since 1.1.12, the write is denied exactly like one without it.)
# Best-effort heuristic; warn only. --print-command (dry run) is exempt.
# Lean read-only wrappers set AGY_DELEGATE_READ_ONLY=1 because their payload may quote
# words such as "implement" from a diff even though agy receives no repository/tools.
if [ "$YOLO" -eq 0 ] && [ "$PRINT_CMD" -ne 1 ] && [ "${AGY_DELEGATE_READ_ONLY:-0}" != 1 ] && [ "${AGY_QUOTA_PROBE:-0}" != 1 ]; then
  shopt -s nocasematch
  case "$PROMPT" in
    *implement*|*scaffold*|*migrate*|*refactor*|*"write the file"*|*"create the file"*|*"edit the file"*)
      echo "agy-delegate: note: this looks like a write task and --yolo is not set. Headless agy will NOT touch your workspace without a write grant (it describes / scratch-diverts / soft-denies / fails outright depending on version; the workspace is untouched either way, and only the newest versions admit it; issue #10). Two grants work: a permissions.allow rule matching the target — write_file(<dir>), a recursive prefix, in ~/.gemini/antigravity-cli/settings.json — which is the narrower one and needs no flag; or --yolo, which auto-approves ALL tools. If a rule already covers your target, ignore this — but <dir> is a placeholder, and agy-doctor will tell you whether yours actually parses. Otherwise add one, or pass --yolo on a dedicated branch, and verify with git status." >&2 ;;
  esac
  shopt -u nocasematch
fi

# --digest: append an explicit output contract so agy returns a compact digest
# instead of raw content. Ingesting digests (never dumps) is the plugin's single
# biggest cost lever — it keeps the conductor's context lean (issue #5).
# (Appended AFTER the write-task heuristic so that scans the user's prompt only.)
if [ "$DIGEST" -eq 1 ]; then
  PROMPT="$PROMPT

OUTPUT CONTRACT (digest): reply with ONLY a compact digest — short bullets (findings / decisions / errors, with file:line references where useful). NO full file contents, NO raw logs, NO long code blocks. End with exactly one line: DIGEST: <one-sentence summary>."
fi

if is_sonnet_46_model "$MODEL"; then
  PROMPT="$PROMPT

SONNET 4.6 DIRECT-EXECUTION CONTRACT (mandatory): Complete the assigned task yourself in this worker. Do not create, invoke, or delegate any part of the task to a sub-agent, nested agent, team, background agent, or another conversation. Do not merely announce future work or waiting. Do not claim completion until the requested work and its stated verification are actually finished. If you cannot complete the task directly, report failure instead of success.

End the response with exactly these two machine-readable lines (after any requested DIGEST line):
POLYPHONY_FALLBACK_STATUS: COMPLETED
POLYPHONY_FALLBACK_EVIDENCE: <concise concrete evidence such as changed files and verification results, or read-only findings>
Use POLYPHONY_FALLBACK_STATUS: FAILED when the task was not completed."
fi

# Inline prompts are ultimately embedded in a `bash -c`/CreateProcess command by
# Claude's Bash tool. Keep a conservative transport budget so a valid-looking
# delegation cannot fail before agy starts with `unexpected EOF` or an argv limit.
# The separate instruction-word check above applies to stdin too. This byte guard
# only limits inline transport, after wrapper-owned instructions have been appended.
if [ "$PROMPT_FROM_STDIN" -eq 0 ]; then
  PROMPT_MAX_CHARS="${AGY_PROMPT_MAX_CHARS:-8000}"
  case "$PROMPT_MAX_CHARS" in ''|*[!0-9]*|0) PROMPT_MAX_CHARS=8000 ;; esac
  [ "$PROMPT_MAX_CHARS" -le 8000 ] || PROMPT_MAX_CHARS=8000
  PROMPT_CHARS="$(LC_ALL=C printf '%s' "$PROMPT" | wc -c | tr -d '[:space:]')"
  if [ "$PROMPT_CHARS" -gt "$PROMPT_MAX_CHARS" ]; then
    die "inline prompt exceeds the compact contract budget (${PROMPT_CHARS}; limit ${PROMPT_MAX_CHARS}). Rewrite to the shortest sufficient form, normally 200-500 words, and reference source paths. Stdin, files, and chunks do not bypass the at-most-800-word authored-instruction gate."
  fi
fi

# --- assemble agy args ---
# NOTE: in agy, -p/--print/--prompt TAKES THE PROMPT AS ITS VALUE, so it must come
# last with the prompt attached. Other flags go before it.
ARGS=(--model "$MODEL" --print-timeout "$TIMEOUT")
BRIDGE_EXTRA_ARGS=()
for d in "${ADD_DIRS[@]:-}"; do [ -n "$d" ] && ARGS+=(--add-dir "$d"); done
if [ "$YOLO" -eq 1 ]; then
  ARGS+=(--dangerously-skip-permissions); BRIDGE_EXTRA_ARGS+=(--dangerously-skip-permissions)
fi
if [ -n "$MODE" ]; then
  ARGS+=(--mode "$MODE"); BRIDGE_EXTRA_ARGS+=(--mode "$MODE") # agy >= 1.1.0
fi
if [ "$SANDBOX" -eq 1 ]; then ARGS+=(--sandbox); BRIDGE_EXTRA_ARGS+=(--sandbox); fi
if [ "$CONTINUE" -eq 1 ]; then
  ARGS+=(--continue); BRIDGE_EXTRA_ARGS+=(--continue) # keep context on the Gemini side
fi
if [ -n "$CONV_ID" ]; then
  ARGS+=(--conversation "$CONV_ID"); BRIDGE_EXTRA_ARGS+=(--conversation "$CONV_ID")
fi

# --- structured output (agy >= 1.1.8) -----------------------------------------
# agy gained `--output-format json`, which is strictly better for a wrapper than
# scraping prose: it carries status/error explicitly and a real `usage` object
# (input/output/thinking/cache_read tokens). We use it INTERNALLY and keep the
# stdout contract unchanged — callers still get the model's text on stdout — while
# classification gets the structured error and token usage goes to stderr as an
# AGY_USAGE line (stderr, so it never pollutes the conductor's context).
#
# Gated three ways, falling back to plain text if any is unmet:
#   * agy actually advertises --output-format (older versions don't),
#   * python3 is available to parse (the bash-only path stays dependency-free),
#   * the user hasn't opted out (structured_output=off).
# NOTE: agy 1.1.8 emits a RAW newline inside the "response" string, which strict
# JSON parsers reject — so we parse with strict=False. (Reported upstream.)
# Resolved BEFORE the capability probe below, not just before the main call: the
# probe was the one unbounded `agy` invocation left in this script. Measured, it
# returns in ~0.08s — but doctor's own hint documents a hang this release does NOT
# fix (agy blocking internally while an MCP server never finishes connecting), and
# an unguarded call is an unguarded call.
TO_CMD="$(timeout_cmd || true)"

# One trap for every temp file this script makes, installed before the first one
# exists. Declaring them empty up front means the probe's file is covered too —
# it used to be cleaned by a trailing `rm -f`, which a SIGINT during the probe
# skips. `rm -f ""` is a silent no-op, so the unset ones cost nothing.
HELPF=""; ERR=""; OUTF=""; REQF=""; PROMPTF=""
trap 'account_unregister_worker; rm -f "$HELPF" "$ERR" "$OUTF" "$REQF" "$PROMPTF" 2>/dev/null' EXIT

JSON_MODE=0
JSON_PY=()
raw_so="${CLAUDE_PLUGIN_OPTION_STRUCTURED_OUTPUT:-on}"
case "$(printf '%s' "$raw_so" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  off|false|0|no|disabled) ;;
  *)
    if [ "$PRINT_CMD" -ne 1 ] && on_windows_native; then
      if ! resolve_bridge_python; then
        echo "agy-delegate: Python >= 3.9 not found for the Windows ConPTY bridge" >&2
        signal BRIDGE_UNAVAILABLE "set AGY_BRIDGE_PYTHON to a Python >= 3.9 executable"
        exit 16
      fi
      # Probing `agy --help` directly is the no-ConPTY call that can hang. Native
      # Windows support therefore requires modern agy (>=1.1.8) and enables JSON.
      JSON_PY=("${BRIDGE_PY[@]}")
      JSON_MODE=1
      ARGS+=(--output-format json)
      BRIDGE_EXTRA_ARGS+=(--output-format json)
    elif [ "$PRINT_CMD" -ne 1 ] && command -v python3 >/dev/null 2>&1; then
      # Capability probe. Deliberately NOT `agy --help | grep -q`: `grep -q` exits at
      # the first match and closes the pipe, so `agy --help` can die of SIGPIPE (141)
      # and, under `set -o pipefail`, the whole pipeline reads as "failed" — silently
      # disabling JSON mode. That race actually bit a benchmark run (~75% of calls on a
      # loaded container), and it is indistinguishable from "no delegation happened",
      # which is the worst kind of failure. Capture once, match with a shell glob.
      # Same pipe hazard as the main call (issue #37): route via a temp file so
      # inherited MCP children can never hold the capture pipe open.
      HELPF="$(mktemp "${TMPDIR:-/tmp}/agy-help.XXXXXX")"
      # 15s is ~180x the measured time; it fires only on a real hang, and losing
      # JSON mode is the right failure — the plain-text path still works.
      if [ -n "$TO_CMD" ]; then
        "$TO_CMD" --kill-after=5 15 agy --help >"$HELPF" 2>&1 || true
      else
        agy --help >"$HELPF" 2>&1 || true
      fi
      # `|| true` so the assignment cannot fail under `set -e` and skip the rm.
      agy_help="$(cat "$HELPF" 2>/dev/null || true)"; rm -f "$HELPF"
      case "$agy_help" in
        *--output-format*)
          JSON_MODE=1; JSON_PY=(python3); ARGS+=(--output-format json)
          BRIDGE_EXTRA_ARGS+=(--output-format json) ;;
      esac
    fi ;;
esac

# --- dry run: print the resolved (shell-quoted) agy invocation and exit ---
if [ "$PRINT_CMD" -eq 1 ]; then
  { printf 'agy'; printf ' %q' "${ARGS[@]}" -p "$PROMPT"; printf '\n'; }
  exit 0
fi

account_register_worker

# --- run (always detach stdin so non-TTY stdout is not dropped) ---
# Per-invocation temp file for stderr (mktemp avoids the race + symlink risk of a
# fixed /tmp path when multiple delegations run concurrently). Cleaned up on exit.
ERR="$(mktemp "${TMPDIR:-/tmp}/agy-delegate.XXXXXX")"
# stdout goes to a file too, never a command-substitution pipe: agy's stdio MCP
# children inherit our stdout and can outlive agy, so `$(agy ...)` blocks forever
# waiting for EOF even after `timeout` kills agy itself (issue #37). A regular
# file is inherited harmlessly.
OUTF="$(mktemp "${TMPDIR:-/tmp}/agy-out.XXXXXX")"

# POSIX uses the existing outer wall-clock guard. Native Windows instead runs through
# agy-headless-bridge, whose ConPTY runner owns hard + idle timeouts in-process.
TO_SECS="$(outer_timeout_secs "$TIMEOUT")"

set +e
if on_windows_native; then
  if [ "${#BRIDGE_PY[@]}" -eq 0 ] && ! resolve_bridge_python; then
    echo "agy-delegate: Python >= 3.9 not found for the Windows ConPTY bridge" >"$ERR"
    RC=16
  else
    BASE_SECS="$(duration_secs "$TIMEOUT")"
    BRIDGE_HARD_TIMEOUT="${AGY_BRIDGE_TIMEOUT:-$(( BASE_SECS + 15 ))}"
    case "$BRIDGE_HARD_TIMEOUT" in
      *[!0-9]*|'') echo "agy-delegate: AGY_BRIDGE_TIMEOUT must be positive integer seconds" >"$ERR"; RC=16 ;;
      0) echo "agy-delegate: AGY_BRIDGE_TIMEOUT must be greater than zero" >"$ERR"; RC=16 ;;
      *)
        # Structured print mode may remain completely silent while an agentic turn
        # is doing useful work. A fixed 120s idle limit killed broad repo scouts
        # before their hard print timeout. Unless configured, keep idle just past
        # the hard wall so agy's inner print timeout gets the first chance to fire.
        if [ "$IDLE_TIMEOUT_EXPLICIT" -eq 1 ]; then
          BRIDGE_IDLE_TIMEOUT="$IDLE_TIMEOUT"
        elif [ -n "${AGY_BRIDGE_IDLE_TIMEOUT:-}" ]; then
          BRIDGE_IDLE_TIMEOUT="$AGY_BRIDGE_IDLE_TIMEOUT"
        elif [ -n "$IDLE_TIMEOUT" ]; then
          BRIDGE_IDLE_TIMEOUT="$IDLE_TIMEOUT"
        else
          BRIDGE_IDLE_TIMEOUT="$(( BRIDGE_HARD_TIMEOUT + 1 ))"
        fi
        case "$BRIDGE_IDLE_TIMEOUT" in
          *[!0-9]*|'') echo "agy-delegate: idle timeout must be positive integer seconds" >"$ERR"; RC=16 ;;
          0) echo "agy-delegate: idle timeout must be greater than zero" >"$ERR"; RC=16 ;;
          *)
        REQF="$(mktemp "${TMPDIR:-/tmp}/agy-request.XXXXXX")"
        PROMPTF="$(mktemp "${TMPDIR:-/tmp}/agy-prompt.XXXXXX")"
        printf '%s' "$PROMPT" >"$PROMPTF"
        REQ_WIN="$(windows_path "$REQF")"
        PROMPT_WIN="$(windows_path "$PROMPTF")"
        ADAPTER_WIN="$(windows_path "$HERE/agy-windows-bridge.py")"
        AGY_PATH_WIN=""
        [ -n "${AGY_PATH:-}" ] && AGY_PATH_WIN="$(windows_path "$AGY_PATH")"
        BRIDGE_DIRS=()
        for d in "${ADD_DIRS[@]}"; do BRIDGE_DIRS+=("$(windows_path "$d")"); done

        # Build the UTF-8 request with the selected Windows interpreter. The prompt
        # travels in a file, not argv/environment, so long prompts avoid Win32 limits.
        "${BRIDGE_PY[@]}" - "$REQ_WIN" "$PROMPT_WIN" "$MODEL" "$AGY_PATH_WIN" \
          "$BRIDGE_HARD_TIMEOUT" "$BRIDGE_IDLE_TIMEOUT" "$JSON_MODE" \
          "${#BRIDGE_DIRS[@]}" "${BRIDGE_DIRS[@]}" -- "${BRIDGE_EXTRA_ARGS[@]}" <<'PY' 2>"$ERR"
import json, pathlib, sys
req, prompt_file, model, agy_path, hard, idle, structured, count = sys.argv[1:9]
n = int(count)
dirs = sys.argv[9:9+n]
rest = sys.argv[9+n:]
if rest[:1] == ["--"]:
    rest = rest[1:]
data = {
    "prompt": pathlib.Path(prompt_file).read_text(encoding="utf-8"),
    "model": model or None,
    "agy_path": agy_path or None,
    "add_dirs": dirs,
    "timeout": int(hard),
    "idle_timeout": int(idle),
    "structured_output": structured == "1",
    "extra_args": rest,
}
pathlib.Path(req).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
PY
        if [ $? -ne 0 ]; then
          RC=16
        else
          "${BRIDGE_PY[@]}" "$ADAPTER_WIN" "$REQ_WIN" < /dev/null >"$OUTF" 2>"$ERR"
          RC=$?
        fi ;;
        esac
        ;;
    esac
  fi
elif [ -n "$TO_CMD" ]; then
  # --kill-after sends SIGKILL if agy ignores the initial SIGTERM (defensive).
  "$TO_CMD" --kill-after=10 "$TO_SECS" agy "${ARGS[@]}" -p "$PROMPT" < /dev/null >"$OUTF" 2>"$ERR"
  RC=$?
else
  agy "${ARGS[@]}" -p "$PROMPT" < /dev/null >"$OUTF" 2>"$ERR"
  RC=$?
fi
OUT="$(cat "$OUTF" 2>/dev/null)"
set -e

# --- unwrap the structured envelope (JSON mode) --------------------------------
# Replaces OUT with the model's text so the stdout contract is unchanged, exposes
# the structured error for classification, and reports token usage on stderr.
# Any parse failure falls back to treating OUT as plain text (never fatal).
JSON_STATUS=""; JSON_ERROR=""; JSON_CONVERSATION=""
# Glob, not ${OUT//[...]/}: stripping the whole string to test emptiness is minutes-to-
# hours at tens of KB on macOS /bin/bash 3.2 (n^~2.6); the glob stops at the first hit.
if [ "$JSON_MODE" -eq 1 ] && [[ "$OUT" = *[!$' \t\n\r']* ]]; then
  # The response can be multi-line, so it goes to a temp file; the single-line
  # metadata comes back on stdout. (Command substitution strips NUL bytes, so a
  # NUL-delimited stream is not an option here.)
  RESP="$(mktemp "${TMPDIR:-/tmp}/agy-resp.XXXXXX")"
  # The error text goes to its OWN file, not back out through `meta`. agy's error
  # strings quote the offending value (`--model \"foo\"`), and pulling the field out
  # of `meta` with sed truncates at that first escaped quote — which silently hid the
  # diagnostic from the classifier, so a bad --model/tier remap reported a generic
  # "agy failed" (exit 2) instead of MODEL_UNAVAILABLE (14). Let python, which already
  # has the parsed object, write the raw value out.
  JERR="$(mktemp "${TMPDIR:-/tmp}/agy-err.XXXXXX")"
  JSON_INPUT_PY="$OUTF"; RESP_PY="$RESP"; JERR_PY="$JERR"
  if on_windows_native; then
    JSON_INPUT_PY="$(windows_path "$OUTF")"
    RESP_PY="$(windows_path "$RESP")"
    JERR_PY="$(windows_path "$JERR")"
  fi
  meta="$(AGY_JSON_FILE="$JSON_INPUT_PY" AGY_RESP_FILE="$RESP_PY" AGY_ERR_FILE="$JERR_PY" "${JSON_PY[@]}" - <<'PY' 2>/dev/null || true
import json, os, sys
try:
    with open(os.environ["AGY_JSON_FILE"], encoding="utf-8") as fh:
        raw = fh.read()
    # strict=False: agy 1.1.8 leaves raw newlines inside "response".
    d = json.loads(raw, strict=False)
    if not isinstance(d, dict): raise ValueError
except Exception:
    sys.exit(1)
with open(os.environ["AGY_RESP_FILE"], "w", encoding="utf-8") as fh:
    fh.write(str(d.get("response", "") or ""))
with open(os.environ["AGY_ERR_FILE"], "w", encoding="utf-8") as fh:
    fh.write(" ".join(str(d.get("error", "") or "").split()))
u = d.get("usage") or {}
def n(k):
    v = u.get(k)
    return v if isinstance(v, int) else 0
one_line = lambda s: " ".join(str(s or "").split())
print(json.dumps({
    "status": str(d.get("status", "")),
    "error": one_line(d.get("error", "")),
    "usage": {"input": n("input_tokens"), "output": n("output_tokens"),
              "thinking": n("thinking_tokens"), "cache_read": n("cache_read_tokens"),
              "total": n("total_tokens")},
    "conversation_id": str(d.get("conversation_id", "") or ""),
}))
PY
)"
  if [ -n "$meta" ]; then
    # `status` is a bare enum with no quotes inside it, so sed is safe there.
    JSON_STATUS="$(printf '%s' "$meta" | sed -n 's/.*"status": *"\([^"]*\)".*/\1/p')"
    JSON_CONVERSATION="$(printf '%s' "$meta" | sed -n 's/.*"conversation_id": *"\([^"]*\)".*/\1/p')"
    JSON_ERROR="$(cat "$JERR" 2>/dev/null)"
    OUT="$(cat "$RESP" 2>/dev/null)"
    printf 'AGY_USAGE %s\n' "$meta" >&2
    tee_usage "AGY_USAGE $meta"
    # A structured ERROR is authoritative even if agy exited 0.
    [ "$JSON_STATUS" = "ERROR" ] && [ "$RC" -eq 0 ] && RC=1
  fi
  rm -f "$RESP" "$JERR"
fi

# Agy can occasionally finish a turn (and report usage/conversation metadata) but
# leave the final response empty. Blindly repeating a write task could duplicate
# edits. Recover once by resuming the same conversation and asking only for its
# final digest. If no conversation id exists, a single full retry is allowed only
# for callers that explicitly declared the operation read-only (scout/review).
recover_empty_output() {
  [ "${AGY_EMPTY_RECOVERY:-0}" != 1 ] || return 125
  local recovery_prompt recovery_timeout recovery_idle rc
  local -a retry_args
  recovery_prompt="The previous turn completed without a final response. Do not redo any work. Return only the final compact result/digest now."
  recovery_timeout="${AGY_EMPTY_RECOVERY_TIMEOUT:-5m}"
  recovery_idle="${AGY_EMPTY_RECOVERY_IDLE_TIMEOUT:-180}"
  retry_args=(--model "$MODEL" --timeout "$recovery_timeout" --idle-timeout "$recovery_idle")
  [ "$YOLO" -eq 0 ] || retry_args+=(--yolo)
  [ -z "$MODE" ] || retry_args+=(--mode "$MODE")
  for d in "${ADD_DIRS[@]:-}"; do [ -z "$d" ] || retry_args+=(--dir "$d"); done

  if [ -n "$JSON_CONVERSATION" ]; then
    retry_args+=(--conversation "$JSON_CONVERSATION" --digest)
    AGY_EMPTY_RECOVERY=1 "$HERE/agy-delegate.sh" "${retry_args[@]}" "$recovery_prompt"
    return $?
  fi
  if [ "${AGY_DELEGATE_READ_ONLY:-0}" = 1 ]; then
    [ "$DIGEST" -eq 0 ] || retry_args+=(--digest)
    # Keep large diff/scout payloads out of the Windows argv limit. The original
    # stdin has already been consumed, so replay the in-memory prompt explicitly.
    printf '%s' "$PROMPT" | AGY_EMPTY_RECOVERY=1 "$HERE/agy-delegate.sh" "${retry_args[@]}" -
    return $?
  fi
  return 125
}

# Adapter-owned failures preserve the public wrapper codes and partial timeout output.
if on_windows_native; then
  case "$RC" in
    12)
      [[ "$OUT" = *[!$' \t\n\r']* ]] && printf '%s\n' "$OUT"
      [ -s "$ERR" ] && cat "$ERR" >&2
      quota_failure_check 0 || true
      signal TIMEOUT "Windows ConPTY bridge timeout"
      exit 12 ;;
    13)
      [ -s "$ERR" ] && cat "$ERR" >&2
      signal AGY_MISSING "agy not found by Windows ConPTY bridge"
      exit 13 ;;
    16)
      [ -s "$ERR" ] && cat "$ERR" >&2
      signal BRIDGE_UNAVAILABLE "Python or agy-headless-bridge unavailable"
      exit 16 ;;
    3)
      quota_failure_check 0 || true
      if recover_empty_output; then exit 0; else recovery_rc=$?; [ "$recovery_rc" -eq 125 ] || exit "$recovery_rc"; fi
      [ -s "$ERR" ] && cat "$ERR" >&2
      echo "agy-delegate: agy returned empty output through ConPTY (model='$MODEL')" >&2
      exit 3 ;;
  esac
fi

# `timeout` exits 124 (SIGTERM) or 137 (SIGKILL after --kill-after) when it had to
# kill agy. Treat that as our structured TIMEOUT (exit 12), not a generic failure.
if ! on_windows_native && [ -n "$TO_CMD" ] && { [ $RC -eq 124 ] || [ $RC -eq 137 ]; }; then
  echo "agy-delegate: agy hit the wall-clock guard (${TO_SECS}s) and was terminated — likely a headless/no-TTY hang." >&2
  quota_failure_check 0 || true
  signal TIMEOUT "agy wall-clock guard fired after ${TO_SECS}s (headless/no-TTY hang?)"
  exit 12
fi

if [ $RC -ne 0 ]; then
  # Best-effort classification into a structured code (the generic 2 is the safe
  # fallback). Scans agy's diagnostics only — never the model-generated response,
  # which could contain trigger words and misclassify. In JSON mode (agy >= 1.1.8)
  # the diagnostic lives in the envelope's `error` field and stderr is typically
  # empty, so prefer that; otherwise fall back to stderr. Patterns are deliberately
  # specific to avoid false positives on incidental substrings.
  blob="$(cat "$ERR" 2>/dev/null)"
  [ -n "$JSON_ERROR" ] && blob="$JSON_ERROR
$blob"
  shopt -s nocasematch
  case "$blob" in
    *quota*|*"rate limit"*|*"resource exhausted"*)
      shopt -u nocasematch
      quota_failure_check 1 || true
      ;;
    *)
      shopt -u nocasematch
      quota_failure_check 0 || true ;;
  esac

  echo "agy-delegate: agy exited $RC" >&2
  [ -s "$ERR" ] && cat "$ERR" >&2
  # In JSON mode agy puts the diagnostic in the envelope instead of stderr — relay it
  # so the failure is still visible to a human reading the transcript.
  [ -n "$JSON_ERROR" ] && printf '%s\n' "$JSON_ERROR" >&2
  shopt -s nocasematch
  case "$blob" in
    # FIRST, because these strings are the most specific ones here and must not be
    # shadowed. agy 1.1.13 fails the run outright on a denied tool instead of soft-denying
    # it, so this shape reaches the rc != 0 path and never sees the check further down.
    *"user denied permission"*|*"permission check failed"*|*"auto-denied"*|\
    *"permission that headless"*|*"dangerously-skip-permissions"*)
      shopt -u nocasematch; permission_denied shown ;;
    *quota*|*"rate limit"*|*"resource exhausted"*)
      shopt -u nocasematch; signal QUOTA_EXHAUSTED "non-Gemini agy quota / rate limit"; exit 10 ;;
    *"no capacity available for model"*|*"unavailable (code 503)"*)
      # agy already performs its own bounded API retries (the diagnostic reports
      # the final attempt). Classify exhausted server capacity instead of adding
      # another expensive wrapper retry or confusing it with account quota.
      shopt -u nocasematch
      signal CAPACITY_UNAVAILABLE "Gemini service has no capacity for the selected model; retry later or ask before changing models"
      exit 19 ;;
    *unauthenticated*|*unauthorized*|*"sign in"*|*"please authenticate"*|*reauth*)
      shopt -u nocasematch
      account_try_failover auth || true
      signal AUTH_REQUIRED "agy not authenticated — run \`agy\` once"; exit 11 ;;
    *"timed out"*|*"deadline exceeded"*|*"print-timeout"*)
      shopt -u nocasematch; signal TIMEOUT "agy print-timeout / deadline exceeded"; exit 12 ;;
    *"invalid --model"*|*"is not recognized as a known model"*|*"not a known model"*)
      # agy >= 1.1.2 hard-fails (instead of silently downgrading) when --model can't be
      # resolved — common when a tier_* / default_model remap points at a model this plan
      # doesn't expose. Surface it as its own actionable category.
      shopt -u nocasematch
      echo "agy-delegate: model '$MODEL' is not available on this plan — run \`agy models\`, then fix --model / the tier_* / default_model option." >&2
      signal MODEL_UNAVAILABLE "model not in \`agy models\` (check --model / tier remaps)"; exit 14 ;;
  esac
  shopt -u nocasematch
  signal AGY_FAILED "agy exited $RC"
  exit 2
fi
if [[ "$OUT" != *[!$' \t\n\r']* ]]; then   # same glob as above, not the quadratic strip
  # agy >= 1.1.3 soft-denies a tool needing permission in headless mode and returns
  # rc=0 with EMPTY stdout plus an explanatory stderr notice (the evolved issue #10:
  # earlier versions silently wrote to a scratch dir or only described the edit). Detect
  # it so the caller gets an actionable signal instead of a bare "empty output".
  eblob="$(cat "$ERR" 2>/dev/null)"
  shopt -s nocasematch
  case "$eblob" in
    *"auto-denied"*|*"permissions.allow"*|*"permission that headless"*|*"dangerously-skip-permissions"*)
      shopt -u nocasematch
      permission_denied ;;
  esac
  shopt -u nocasematch
  quota_failure_check 0 || true
  if recover_empty_output; then exit 0; else recovery_rc=$?; [ "$recovery_rc" -eq 125 ] || exit "$recovery_rc"; fi
  echo "agy-delegate: agy returned empty output (model='$MODEL')" >&2
  exit 3
fi


# A Sonnet fallback result is not trusted merely because agy returned rc=0 and text.
# The direct-execution receipt proves that this worker itself reached a terminal
# outcome; a promise to delegate/wait therefore becomes a visible non-zero failure.
if is_sonnet_46_model "$MODEL"; then
  if ! printf '%s\n' "$OUT" | grep -Eq '^POLYPHONY_FALLBACK_STATUS:[[:space:]]*COMPLETED[[:space:]]*$' \
     || ! printf '%s\n' "$OUT" | grep -Eq '^POLYPHONY_FALLBACK_EVIDENCE:[[:space:]]*[^[:space:]].*$'; then
    echo "agy-delegate: Sonnet 4.6 returned exit 0 without a valid direct-completion receipt; treating the result as incomplete." >&2
    signal AGY_INCOMPLETE "Sonnet 4.6 did not prove direct completion; nested delegation or premature success is possible"
    exit 2
  fi
fi

# Digest-size guard: the cost saving depends on the conductor ingesting a DIGEST,
# not a raw dump — if the reply is dump-sized, say so on stderr (advisory only;
# stdout passes through untouched). Tune via the digest_warn_chars plugin option
# (env CLAUDE_PLUGIN_OPTION_DIGEST_WARN_CHARS; empty = 8000, 0 = off).
WARN_CHARS="${CLAUDE_PLUGIN_OPTION_DIGEST_WARN_CHARS:-8000}"
case "$WARN_CHARS" in (*[!0-9]*|'') WARN_CHARS=8000 ;; esac
if [ "$WARN_CHARS" -gt 0 ] && [ "${#OUT}" -gt "$WARN_CHARS" ]; then
  echo "agy-delegate: note: output is ${#OUT} chars (> ${WARN_CHARS}) — that looks like a raw dump, not a digest. Don't ingest this into the conductor's context: re-run with --digest, or have agy summarize it first. (plugin option digest_warn_chars tunes this; 0 disables.)" >&2
fi

quota_after_success

printf '%s\n' "$OUT"
