#!/usr/bin/env bash
# Contract tests for agy-scout / agy-review. No network or real model calls.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REVIEW="$ROOT/scripts/agy-review.sh"
SCOUT="$ROOT/scripts/agy-scout.sh"
DELEGATE_REAL="$ROOT/scripts/agy-delegate.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0

ok() { echo "ok: $1"; PASS=$((PASS+1)); }
bad() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }
has() { grep -qF -- "$2" "$1"; }
lacks() { ! grep -qF -- "$2" "$1"; }

STUB="$TMP/agy-delegate"
cat >"$STUB" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$AGY_CAPTURE.args"
printf 'call\n' >>"$AGY_CAPTURE.calls"
pwd >"$AGY_CAPTURE.cwd"
cat >"$AGY_CAPTURE.stdin"
if [ "${AGY_STUB_OVERSIZE:-0}" = 1 ]; then
  printf 'VERDICT: CONFIRMED\nFINDINGS:\n- none\nTEST_GAPS:\n- none\nCOMMIT_SUBJECT: test: fixture\n'
  awk 'BEGIN { for (i=0; i<9000; i++) printf "x" }'
  printf '\nDIGEST: oversized\n'
elif printf '%s\n' "$@" | grep -q -- '--mode'; then
  printf 'FINDINGS:\n- fixture.cs:1 — evidence\nRISKS_OR_GAPS:\n- none\nNEXT_LIKELY_GAP: none\nDIGEST: scout saw the fixture\n'
else
  printf 'VERDICT: CONFIRMED\nFINDINGS:\n- none\nTEST_GAPS:\n- none\nCOMMIT_SUBJECT: test: review fixture\nDIGEST: patch matches the goal\n'
fi
STUB
chmod +x "$STUB"

REPO="$TMP/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q
git -C "$REPO" config user.name Test
git -C "$REPO" config user.email test@example.invalid
printf 'base-a\n' >"$REPO/a.txt"
printf 'base-b\n' >"$REPO/b.txt"
git -C "$REPO" add a.txt b.txt
git -C "$REPO" -c commit.gpgSign=false commit -qm baseline
printf 'STAGED_SENTINEL_41A7\n' >>"$REPO/a.txt"
for i in $(seq 1 30); do printf 'staged-padding-%02d\n' "$i" >>"$REPO/a.txt"; done
git -C "$REPO" add a.txt
printf 'WORKTREE_SENTINEL_92BC\n' >>"$REPO/b.txt"
printf 'UNTRACKED_SENTINEL_F00D\n' >"$REPO/new.txt"

CAP="$TMP/worktree"
if AGY_DELEGATE="$STUB" AGY_CAPTURE="$CAP" "$REVIEW" --dir "$REPO" --goal "only append fixture lines" >"$CAP.out" 2>"$CAP.err"; then
  has "$CAP.stdin" STAGED_SENTINEL_41A7 && has "$CAP.stdin" WORKTREE_SENTINEL_92BC \
    && lacks "$CAP.stdin" UNTRACKED_SENTINEL_F00D && ok "worktree patch goes to agy; untracked contents do not" \
    || bad "worktree payload scope"
  has "$CAP.args" flash && has "$CAP.args" --digest && has "$CAP.args" 30m \
    && has "$CAP.out" 'VERDICT: CONFIRMED' \
    && lacks "$CAP.out" STAGED_SENTINEL_41A7 && ok "Claude-facing output is only the compact verdict" \
    || bad "compact review output"
  has "$CAP.err" 'untracked contents are excluded' && ok "untracked review gap is explicit" \
    || bad "untracked warning"
  [ "$(cat "$CAP.cwd")" = "$REPO" ] && ok "review stays in the selected repository" \
    || bad "review working directory"
  lacks "$CAP.args" --idle-timeout \
    && ok "review inherits the hard-deadline idle policy" || bad "review idle timeout"
else
  bad "worktree review exits zero"
fi

CAP="$TMP/staged"
if AGY_DELEGATE="$STUB" AGY_CAPTURE="$CAP" "$REVIEW" --dir "$REPO" --staged --timeout 7m >"$CAP.out" 2>"$CAP.err"; then
  has "$CAP.stdin" STAGED_SENTINEL_41A7 && lacks "$CAP.stdin" WORKTREE_SENTINEL_92BC \
    && has "$CAP.args" 7m \
    && ok "--staged isolates the index" || bad "--staged scope"
else
  bad "staged review exits zero"
fi

CAP="$TMP/scout"
if AGY_DELEGATE="$STUB" AGY_CAPTURE="$CAP" "$SCOUT" --dir "$REPO" "trace the fixture" >"$CAP.out" 2>"$CAP.err"; then
  has "$CAP.args" flash && has "$CAP.args" plan && has "$CAP.args" 30m && has "$CAP.args" "$REPO" \
    && has "$CAP.stdin" 'READ-ONLY repository investigation' && has "$CAP.stdin" 'NEXT_LIKELY_GAP' \
    && ok "scout builds the fixed read-only Flash contract" || bad "scout contract"
  has "$CAP.out" 'DIGEST: scout saw the fixture' && ok "scout returns only its digest" \
    || bad "scout output"
else
  bad "scout exits zero"
fi

CAP="$TMP/scout-timeout"
if AGY_DELEGATE="$STUB" AGY_CAPTURE="$CAP" "$SCOUT" --dir "$REPO" --timeout 7m "trace the fixture" >"$CAP.out" 2>"$CAP.err" \
  && has "$CAP.args" 7m; then
  ok "scout forwards an explicit timeout override"
else
  bad "scout timeout override"
fi

CAP="$TMP/oversize"
set +e
AGY_DELEGATE="$STUB" AGY_CAPTURE="$CAP" AGY_REVIEW_MAX_OUTPUT_CHARS=200 \
  AGY_STUB_OVERSIZE=1 "$REVIEW" --dir "$REPO" --staged >"$CAP.out" 2>"$CAP.err"
RC=$?
set -e
if [ "$RC" -ne 0 ] && [ ! -s "$CAP.out" ] && has "$CAP.err" 'raw response suppressed'; then
  ok "oversized model output is not leaked to Claude"
else
  bad "oversized output guard"
fi

set +e
AGY_DELEGATE="$STUB" AGY_CAPTURE="$TMP/reject" "$REVIEW" --dir "$REPO" --range '-c' >"$TMP/reject.out" 2>"$TMP/reject.err"
RC=$?
set -e
if [ "$RC" -ne 0 ] && has "$TMP/reject.err" "cannot begin with '-'"; then
  ok "option-shaped Git ranges are rejected"
else
  bad "range option injection guard"
fi

CAP="$TMP/chunked"
if AGY_DELEGATE="$STUB" AGY_CAPTURE="$CAP" AGY_REVIEW_CHUNK_BYTES=200 \
  "$REVIEW" --dir "$REPO" --staged >"$CAP.out" 2>"$CAP.err"; then
  CALLS="$(wc -l <"$CAP.calls" | tr -d '[:space:]')"
  if [ "$CALLS" -gt 1 ] && has "$CAP.out" 'VERDICT: CONFIRMED' \
    && lacks "$CAP.out" STAGED_SENTINEL_41A7 && has "$CAP.stdin" 'discard findings caused only by a chunk'; then
    ok "Windows-sized chunks are privately reviewed and synthesized"
  else
    bad "chunked review synthesis"
  fi
else
  bad "chunked review exits zero"
fi

# Empty-output recovery must be bounded and must never blindly repeat a write task.
EMPTY_BIN="$TMP/empty-bin"
mkdir -p "$EMPTY_BIN"
cat >"$EMPTY_BIN/agy" <<'STUB'
#!/usr/bin/env bash
[ "${1:-}" != "--help" ] || { printf '%s\n' '--output-format'; exit 0; }
[ -z "${AGY_FIXED_OUTPUT:-}" ] || { printf '%s\n' "$AGY_FIXED_OUTPUT"; exit 0; }
n=0; [ ! -f "$AGY_EMPTY_COUNTER" ] || n="$(cat "$AGY_EMPTY_COUNTER")"
n=$((n + 1)); printf '%s' "$n" >"$AGY_EMPTY_COUNTER"
[ -z "${AGY_EMPTY_ARGS:-}" ] || printf '%s\n' "$*" >>"$AGY_EMPTY_ARGS"
if [ "${AGY_EMPTY_STRUCTURED:-0}" = 1 ]; then
  if [ "$n" -eq 1 ]; then
    printf '%s\n' '{"status":"SUCCESS","response":"","conversation_id":"fixture-conversation","usage":{}}'
  else
    printf '%s\n' '{"status":"SUCCESS","response":"DIGEST: recovered from the same conversation","conversation_id":"fixture-conversation","usage":{}}'
  fi
  exit 0
fi
[ "$n" -gt 1 ] && printf 'DIGEST: recovered after one empty read-only turn\n'
exit 0
STUB
chmod +x "$EMPTY_BIN/agy"
cat >"$EMPTY_BIN/python3" <<'STUB'
#!/usr/bin/env bash
for name in AGY_JSON_FILE AGY_RESP_FILE AGY_ERR_FILE; do
  value="${!name:-}"
  if [ -n "$value" ] && command -v cygpath >/dev/null 2>&1; then
    value="$(cygpath -aw "$value")"
  fi
  printf -v "$name" '%s' "$value"
  export "$name"
done
exec python "$@"
STUB
chmod +x "$EMPTY_BIN/python3"
cat >"$EMPTY_BIN/timeout" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in --kill-after=*) shift ;; esac
[ "$#" -eq 0 ] || shift
exec "$@"
STUB
chmod +x "$EMPTY_BIN/timeout"

EMPTY_COUNTER="$TMP/empty-counter"
if PATH="$EMPTY_BIN:$PATH" AGY_EMPTY_COUNTER="$EMPTY_COUNTER" AGY_TEST_FORCE_POSIX=1 \
  AGY_DELEGATE_READ_ONLY=1 CLAUDE_PLUGIN_OPTION_STRUCTURED_OUTPUT=off \
  "$DELEGATE_REAL" --model 'Fixture Model' --timeout 1s 'inspect only' >"$TMP/empty.out" 2>"$TMP/empty.err" \
  && [ "$(cat "$EMPTY_COUNTER")" = 2 ] && has "$TMP/empty.out" 'DIGEST: recovered'; then
  ok "read-only empty output gets one bounded recovery"
else
  bad "read-only empty-output recovery"
fi

rm -f "$EMPTY_COUNTER"
set +e
PATH="$EMPTY_BIN:$PATH" AGY_EMPTY_COUNTER="$EMPTY_COUNTER" AGY_TEST_FORCE_POSIX=1 \
  CLAUDE_PLUGIN_OPTION_STRUCTURED_OUTPUT=off \
  "$DELEGATE_REAL" --model 'Fixture Model' --timeout 1s 'edit a file' >"$TMP/write-empty.out" 2>"$TMP/write-empty.err"
RC=$?
set -e
if [ "$RC" -eq 3 ] && [ "$(cat "$EMPTY_COUNTER")" = 1 ]; then
  ok "empty write task is never blindly repeated"
else
  bad "write-task empty-output safety"
fi

rm -f "$EMPTY_COUNTER"
EMPTY_ARGS="$TMP/empty-args"
if PATH="$EMPTY_BIN:$PATH" AGY_EMPTY_COUNTER="$EMPTY_COUNTER" AGY_EMPTY_ARGS="$EMPTY_ARGS" \
  AGY_EMPTY_STRUCTURED=1 AGY_TEST_FORCE_POSIX=1 \
  "$DELEGATE_REAL" --model 'Fixture Model' --timeout 1s 'edit a file once' >"$TMP/conversation.out" 2>"$TMP/conversation.err" \
  && [ "$(cat "$EMPTY_COUNTER")" = 2 ] && has "$EMPTY_ARGS" '--conversation fixture-conversation' \
  && has "$TMP/conversation.out" 'DIGEST: recovered from the same conversation'; then
  ok "empty write result resumes its conversation for digest only"
else
  bad "conversation-based empty-output recovery"
fi

# Transient backend failures: structured INTERNAL (rc 0, empty response, sometimes
# no conversation_id) and "stream was interrupted" (rc 1). Bounded, same-conversation
# recovery for writes; full replay only for read-only callers.
TRANSIENT_BIN="$TMP/transient-bin"
mkdir -p "$TRANSIENT_BIN"
cat >"$TRANSIENT_BIN/agy" <<'STUB'
#!/usr/bin/env bash
[ "${1:-}" != "--help" ] || { printf '%s\n' '--output-format'; exit 0; }
n=0; [ ! -f "$AGY_T_COUNTER" ] || n="$(cat "$AGY_T_COUNTER")"
n=$((n + 1)); printf '%s' "$n" >"$AGY_T_COUNTER"
printf '%s\n' "$*" >>"$AGY_T_ARGS"
if [ "$n" -eq 1 ] && [ -n "${AGY_T_BRAIN:-}" ]; then
  # Real agy writes the transcript even when the envelope omits conversation_id.
  mkdir -p "$AGY_T_BRAIN/conv-found/.system_generated/logs"
  printf '{"type":"USER_INPUT","content":"<USER_REQUEST> %s </USER_REQUEST>"}\n' "$AGY_T_PROMPT" \
    >"$AGY_T_BRAIN/conv-found/.system_generated/logs/transcript.jsonl"
fi
if [ "$n" -le "${AGY_T_FAILS:-1}" ]; then
  if [ "${AGY_T_SIGNAL:-0}" = 1 ]; then
    echo 'Error: The stream was interrupted. Please continue the task you were working on.' >&2
    exit 143
  elif [ "${AGY_T_SIGNAL:-0}" = taskkill ]; then
    echo 'Process terminated by taskkill; stream was interrupted.' >&2
    exit 1
  fi
  if [ "${AGY_T_KIND:-internal}" = stream ]; then
    echo 'Error: The stream was interrupted. Please continue the task you were working on.' >&2
    exit 1
  fi
  printf '%s\n' '{"status":"INTERNAL","error":"","response":"","conversation_id":"","usage":{}}'
  exit 0
fi
printf '%s\n' '{"status":"SUCCESS","response":"DIGEST: finished after transient failure","conversation_id":"conv-found","usage":{}}'
STUB
chmod +x "$TRANSIENT_BIN/agy"

run_transient() { # $1 = case name; remaining = extra env assignments
  local name="$1"; shift
  rm -f "$TMP/$name.counter" "$TMP/$name.args"
  set +e
  env PATH="$TRANSIENT_BIN:$EMPTY_BIN:$PATH" AGY_TEST_FORCE_POSIX=1 AGY_TRANSIENT_RETRY_DELAYS='0 0' \
    AGY_T_COUNTER="$TMP/$name.counter" AGY_T_ARGS="$TMP/$name.args" "$@" \
    >"$TMP/$name.out" 2>"$TMP/$name.err"
  RC=$?
  set -e
  touch "$TMP/$name.args"
}

T_PROMPT='rename the fixture project everywhere'
run_transient internal-write AGY_BRAIN_DIR="$TMP/brain1" AGY_T_BRAIN="$TMP/brain1" AGY_T_PROMPT="$T_PROMPT" \
  "$DELEGATE_REAL" --model 'Fixture Model' --timeout 30s "$T_PROMPT"
if [ "$RC" -eq 0 ] && [ "$(cat "$TMP/internal-write.counter")" = 2 ] \
  && has "$TMP/internal-write.args" '--conversation conv-found' \
  && has "$TMP/internal-write.out" 'DIGEST: finished after transient failure'; then
  ok "structured INTERNAL on a write task resumes the discovered conversation"
else
  bad "INTERNAL write-task recovery (rc=$RC)"
fi

run_transient stream-readonly AGY_T_KIND=stream AGY_DELEGATE_READ_ONLY=1 AGY_BRAIN_DIR="$TMP/brain-empty" \
  "$DELEGATE_REAL" --model 'Fixture Model' --timeout 30s 'inspect only'
if [ "$RC" -eq 0 ] && [ "$(cat "$TMP/stream-readonly.counter")" = 2 ] \
  && lacks "$TMP/stream-readonly.args" '--conversation'; then
  ok "stream interruption on a read-only task replays once"
else
  bad "read-only stream recovery (rc=$RC)"
fi

run_transient stream-write AGY_T_KIND=stream AGY_BRAIN_DIR="$TMP/brain-empty" \
  "$DELEGATE_REAL" --model 'Fixture Model' --timeout 30s 'edit a file'
if [ "$RC" -eq 20 ] && [ "$(cat "$TMP/stream-write.counter")" = 1 ] \
  && has "$TMP/stream-write.err" 'STREAM_INTERRUPTED'; then
  ok "write task without a resumable conversation is never replayed (exit 20)"
else
  bad "write-task stream safety (rc=$RC)"
fi

run_transient internal-persistent AGY_T_FAILS=9 AGY_BRAIN_DIR="$TMP/brain2" AGY_T_BRAIN="$TMP/brain2" AGY_T_PROMPT="$T_PROMPT" \
  "$DELEGATE_REAL" --model 'Fixture Model' --timeout 30s "$T_PROMPT"
if [ "$RC" -eq 20 ] && [ "$(cat "$TMP/internal-persistent.counter")" = 3 ]; then
  ok "persistent transient failure is bounded by the retry delay list"
else
  bad "bounded transient retries (rc=$RC, calls=$(cat "$TMP/internal-persistent.counter" 2>/dev/null))"
fi

run_transient retry-disabled AGY_T_FAILS=9 AGY_BRAIN_DIR="$TMP/brain-disabled" AGY_T_BRAIN="$TMP/brain-disabled" AGY_T_PROMPT="$T_PROMPT" \
  AGY_TRANSIENT_RETRY_DELAYS='' "$DELEGATE_REAL" --model 'Fixture Model' --timeout 30s "$T_PROMPT"
if [ "$RC" -eq 20 ] && [ "$(cat "$TMP/retry-disabled.counter")" = 1 ]; then
  ok "an explicitly empty transient retry list disables retries"
else
  bad "empty retry-list opt-out (rc=$RC, calls=$(cat "$TMP/retry-disabled.counter" 2>/dev/null))"
fi

run_transient retry-deadline AGY_T_FAILS=9 AGY_BRAIN_DIR="$TMP/brain-deadline" AGY_T_BRAIN="$TMP/brain-deadline" AGY_T_PROMPT="$T_PROMPT" \
  AGY_TRANSIENT_RETRY_DELAYS='2' "$DELEGATE_REAL" --model 'Fixture Model' --timeout 1s "$T_PROMPT"
if [ "$RC" -eq 20 ] && [ "$(cat "$TMP/retry-deadline.counter")" = 1 ]; then
  ok "transient backoff cannot exceed the original invocation deadline"
else
  bad "aggregate transient deadline (rc=$RC, calls=$(cat "$TMP/retry-deadline.counter" 2>/dev/null))"
fi

run_transient retry-cancelled AGY_T_SIGNAL=1 AGY_DELEGATE_READ_ONLY=1 AGY_BRAIN_DIR="$TMP/brain-cancelled" \
  AGY_TRANSIENT_RETRY_DELAYS='0 0' "$DELEGATE_REAL" --model 'Fixture Model' --timeout 30s 'inspect only'
if [ "$RC" -eq 20 ] && [ "$(cat "$TMP/retry-cancelled.counter")" = 1 ]; then
  ok "signal-terminated transient calls are never replayed"
else
  bad "cancelled-call replay guard (rc=$RC, calls=$(cat "$TMP/retry-cancelled.counter" 2>/dev/null))"
fi

run_transient retry-taskkill AGY_T_SIGNAL=taskkill AGY_DELEGATE_READ_ONLY=1 AGY_BRAIN_DIR="$TMP/brain-taskkill" \
  AGY_TRANSIENT_RETRY_DELAYS='0 0' "$DELEGATE_REAL" --model 'Fixture Model' --timeout 30s 'inspect only'
if [ "$RC" -eq 20 ] && [ "$(cat "$TMP/retry-taskkill.counter")" = 1 ]; then
  ok "generic taskkill diagnostics suppress read-only replay"
else
  bad "taskkill replay guard (rc=$RC, calls=$(cat "$TMP/retry-taskkill.counter" 2>/dev/null))"
fi

# If multiple recent transcripts contain the same request, no conversation can
# be selected safely. Do not start another read-only worker that could duplicate
# one already running remotely.
AMBIGUOUS_BRAIN="$TMP/brain-ambiguous"
for conv in existing-a existing-b; do
  mkdir -p "$AMBIGUOUS_BRAIN/$conv/.system_generated/logs"
  printf '{"type":"USER_INPUT","content":"<USER_REQUEST> %s </USER_REQUEST>"}\n' 'inspect only' \
    >"$AMBIGUOUS_BRAIN/$conv/.system_generated/logs/transcript.jsonl"
done
run_transient retry-ambiguous AGY_T_KIND=stream AGY_DELEGATE_READ_ONLY=1 AGY_BRAIN_DIR="$AMBIGUOUS_BRAIN" \
  AGY_TRANSIENT_RETRY_DELAYS='0 0' "$DELEGATE_REAL" --model 'Fixture Model' --timeout 30s 'inspect only'
if [ "$RC" -eq 20 ] && [ "$(cat "$TMP/retry-ambiguous.counter")" = 1 ]; then
  ok "ambiguous live transcript matches suppress read-only replay"
else
  bad "ambiguous transcript replay guard (rc=$RC, calls=$(cat "$TMP/retry-ambiguous.counter" 2>/dev/null))"
fi

# Sonnet 4.6 quota fallback must execute directly and prove completion. A textual
# promise to delegate is not a successful work result even when agy itself exits 0.
if "$DELEGATE_REAL" --model 'claude-sonnet-4-6' --print-command 'edit the fixture directly' \
  >"$TMP/sonnet-contract.out" 2>"$TMP/sonnet-contract.err" \
  && has "$TMP/sonnet-contract.out" 'Do not create, invoke, or delegate any part of the task to a sub-agent' \
  && has "$TMP/sonnet-contract.out" 'POLYPHONY_FALLBACK_STATUS'; then
  ok "Sonnet receives the direct-execution and completion-receipt contract"
else
  bad "Sonnet direct-execution contract"
fi

set +e
PATH="$EMPTY_BIN:$PATH" AGY_TEST_FORCE_POSIX=1 CLAUDE_PLUGIN_OPTION_STRUCTURED_OUTPUT=off \
  AGY_FIXED_OUTPUT='I will delegate this to a sub-agent and wait for it.' \
  "$DELEGATE_REAL" --model 'claude-sonnet-4-6' --timeout 1s 'edit the fixture directly' \
  >"$TMP/sonnet-incomplete.out" 2>"$TMP/sonnet-incomplete.err"
RC=$?
set -e
if [ "$RC" -eq 2 ] && has "$TMP/sonnet-incomplete.err" 'AGY_INCOMPLETE'; then
  ok "Sonnet exit 0 without direct-completion receipt is rejected"
else
  bad "Sonnet premature-success guard"
fi

SONNET_DONE="$(printf '%s\n' \
  'Changed fixture.txt and ran the requested check.' \
  'POLYPHONY_FALLBACK_STATUS: COMPLETED' \
  'POLYPHONY_FALLBACK_EVIDENCE: fixture.txt updated; targeted check passed')"
if PATH="$EMPTY_BIN:$PATH" AGY_TEST_FORCE_POSIX=1 CLAUDE_PLUGIN_OPTION_STRUCTURED_OUTPUT=off \
  AGY_FIXED_OUTPUT="$SONNET_DONE" \
  "$DELEGATE_REAL" --model 'claude-sonnet-4-6' --timeout 1s 'edit the fixture directly' \
  >"$TMP/sonnet-complete.out" 2>"$TMP/sonnet-complete.err" \
  && has "$TMP/sonnet-complete.out" 'POLYPHONY_FALLBACK_STATUS: COMPLETED'; then
  ok "Sonnet direct-completion receipt is accepted"
else
  bad "Sonnet completion receipt"
fi

echo "wrapper PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
