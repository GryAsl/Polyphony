#!/usr/bin/env bash
# Send a Git patch directly to a fresh Gemini reviewer without putting the raw diff
# in Claude's context. This wrapper is intentionally read-only and does not give agy
# repository or tool access.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DELEGATE="${AGY_DELEGATE:-$HERE/agy-delegate.sh}"
DIR="."
GOAL="Review the selected changes for correctness and unintended behavior."
SCOPE="worktree"
SCOPE_SET=0
RANGE=""
TIER="flash"
TIER_EXPLICIT=0
TIMEOUT="30m"
ADVERSARIAL=0
PATHS=()
MAX_BYTES="${AGY_REVIEW_MAX_BYTES:-49152}"
CHUNK_BYTES="${AGY_REVIEW_CHUNK_BYTES:-12000}"
PART_OUTPUT="${AGY_REVIEW_PART_OUTPUT_BYTES:-3500}"
MAX_OUTPUT="${AGY_REVIEW_MAX_OUTPUT_CHARS:-8000}"
REVIEW_IDLE_TIMEOUT="${AGY_REVIEW_IDLE_TIMEOUT:-}"

usage() {
  cat <<'EOF'
Usage: agy-review [options] [goal]
  --dir <repo>          Repository (default: current directory)
  --goal <text>         Short original contract / intended change
  --goal-stdin          Read the short original contract from UTF-8 stdin
  --worktree            Staged + unstaged tracked changes (default)
  --staged              Staged changes only
  --last                Last commit only
  --range <A..B>        Explicit Git revision/range
  --path <pathspec>     Limit scope; repeatable
  --adversarial         Also challenge design and tradeoffs
  --tier <flash-medium|flash|pro>  Tier (default: High)
  --timeout <duration>  Delegation timeout (default: 30m)

The raw diff is sent only to agy. Untracked file contents are deliberately excluded;
stage them or review them by path after adding them to Git.
EOF
}

die() { echo "agy-review: $*" >&2; exit 2; }
need() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 requires a value"; }
set_scope() {
  [ "$SCOPE_SET" -eq 0 ] || die "choose only one of --worktree, --staged, --last, --range"
  SCOPE="$1"
  SCOPE_SET=1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir)         need "$@"; DIR="$2"; shift 2 ;;
    --goal)        need "$@"; GOAL="$2"; shift 2 ;;
    --goal-stdin)  GOAL="$(cat)"; shift ;;
    --worktree)    set_scope "worktree"; shift ;;
    --staged)      set_scope "staged"; shift ;;
    --last)        set_scope "last"; shift ;;
    --range)       need "$@"; set_scope "range"; RANGE="$2"; shift 2 ;;
    --path)        need "$@"; PATHS+=("$2"); shift 2 ;;
    --adversarial) ADVERSARIAL=1; shift ;;
    --tier)        need "$@"; TIER="$2"; TIER_EXPLICIT=1; shift 2 ;;
    --timeout)     need "$@"; TIMEOUT="$2"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    --*)           die "unknown option: $1" ;;
    *)             GOAL="$1"; shift; [ "$#" -eq 0 ] || die "quote the goal as one argument" ;;
  esac
done

GOAL_WORDS="$(printf '%s' "$GOAL" | wc -w | tr -d '[:space:]')"
[ "$GOAL_WORDS" -le 800 ] || die "review goal may contain at most 800 words; rewrite it to the shortest sufficient form, normally 200-500 words, and reference paths instead of pasting implementation detail"

case "$TIER" in flash-medium|flash|pro) ;; *) die "--tier must be flash-medium, flash, or pro" ;; esac
[ "$ADVERSARIAL" -eq 0 ] || [ "$TIER_EXPLICIT" -eq 1 ] || TIER="flash"
case "$MAX_BYTES" in ''|*[!0-9]*) die "AGY_REVIEW_MAX_BYTES must be an integer" ;; esac
case "$CHUNK_BYTES" in ''|*[!0-9]*) die "AGY_REVIEW_CHUNK_BYTES must be an integer" ;; esac
case "$PART_OUTPUT" in ''|*[!0-9]*) die "AGY_REVIEW_PART_OUTPUT_BYTES must be an integer" ;; esac
case "$MAX_OUTPUT" in ''|*[!0-9]*) die "AGY_REVIEW_MAX_OUTPUT_CHARS must be an integer" ;; esac
case "$REVIEW_IDLE_TIMEOUT" in *[!0-9]*) die "AGY_REVIEW_IDLE_TIMEOUT must be an integer" ;; esac
[ "$MAX_BYTES" -gt 0 ] || die "AGY_REVIEW_MAX_BYTES must be greater than zero"
[ "$CHUNK_BYTES" -gt 0 ] || die "AGY_REVIEW_CHUNK_BYTES must be greater than zero"
[ "$PART_OUTPUT" -gt 0 ] || die "AGY_REVIEW_PART_OUTPUT_BYTES must be greater than zero"
[ "$MAX_OUTPUT" -gt 0 ] || die "AGY_REVIEW_MAX_OUTPUT_CHARS must be greater than zero"
[ -z "$REVIEW_IDLE_TIMEOUT" ] || [ "$REVIEW_IDLE_TIMEOUT" -gt 0 ] || die "AGY_REVIEW_IDLE_TIMEOUT must be greater than zero"
[ -d "$DIR" ] || die "directory not found: $DIR"
[ -x "$DELEGATE" ] || die "delegation wrapper is not executable: $DELEGATE"

ROOT="$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null)" || die "not a Git repository: $DIR"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/agy-review.XXXXXX")" || die "cannot create temporary directory"
DIFF="$TMP/diff"
PAYLOAD="$TMP/payload"
PREFIX="$TMP/prefix"
cleanup() { rm -f "$TMP"/*; rmdir "$TMP" 2>/dev/null || true; }
trap cleanup EXIT HUP INT TERM

GIT=(git -C "$ROOT" -c core.pager=cat -c diff.external= diff --no-ext-diff --no-textconv)
case "$SCOPE" in
  worktree)
    git -C "$ROOT" rev-parse --verify HEAD >/dev/null 2>&1 || die "--worktree requires an initial commit"
    "${GIT[@]}" HEAD -- "${PATHS[@]}" >"$DIFF" || die "git diff failed"
    ;;
  staged)
    "${GIT[@]}" --cached -- "${PATHS[@]}" >"$DIFF" || die "git diff --cached failed"
    ;;
  last)
    git -C "$ROOT" rev-parse --verify HEAD >/dev/null 2>&1 || die "--last requires a commit"
    if git -C "$ROOT" rev-parse --verify HEAD^ >/dev/null 2>&1; then
      "${GIT[@]}" HEAD^ HEAD -- "${PATHS[@]}" >"$DIFF" || die "last-commit diff failed"
    else
      git -C "$ROOT" -c core.pager=cat -c diff.external= show --format= \
        --no-ext-diff --no-textconv HEAD -- "${PATHS[@]}" >"$DIFF" || die "root-commit diff failed"
    fi
    ;;
  range)
    [ -n "$RANGE" ] || die "--range cannot be empty"
    case "$RANGE" in -*) die "--range cannot begin with '-'" ;; esac
    "${GIT[@]}" "$RANGE" -- "${PATHS[@]}" >"$DIFF" || die "invalid or unreadable Git range: $RANGE"
    ;;
esac

[ -s "$DIFF" ] || die "selected scope has no tracked diff"
BYTES="$(wc -c <"$DIFF" | tr -d '[:space:]')"
[ "$BYTES" -le "$MAX_BYTES" ] || die "diff is ${BYTES} bytes (limit ${MAX_BYTES}); narrow it with --path or review staged logical groups"

UNTRACKED=""
if [ "$SCOPE" = "worktree" ]; then
  UNTRACKED="$(git -C "$ROOT" ls-files --others --exclude-standard | sed -n '1,6p')"
  [ -z "$UNTRACKED" ] || echo "agy-review: note: untracked contents are excluded; stage intended files before final review" >&2
fi

{
  printf '%s\n' "You are a fresh, blind code-review verifier. Review ONLY the supplied Git patch against the stated goal. Do not request repository access, run tools, or repeat the patch."
  printf 'GOAL: %s\n' "$GOAL"
  printf '%s\n' "Check correctness, regressions, security/privacy, performance, accidental or unrelated edits, generated artifacts, missing tests, and whether the patch fully satisfies the goal. Treat test claims as unverified unless the patch itself proves them."
  [ "$ADVERSARIAL" -eq 0 ] || printf '%s\n' "Adversarial mode: also challenge design assumptions and tradeoffs; seek counterexamples."
  if [ -n "$UNTRACKED" ]; then
    printf '%s\n%s\n' "UNTRACKED PATHS EXCLUDED FROM PATCH (review is incomplete until staged):" "$UNTRACKED"
  fi
} >"$PREFIX"

schema() {
  cat <<'EOF'
Return ONLY this compact schema (maximum five findings, most severe first):
VERDICT: CONFIRMED | CHANGES_REQUESTED | INCONCLUSIVE
FINDINGS:
- SEVERITY | file:line | issue and concrete evidence
TEST_GAPS:
- missing deterministic check, or none
COMMIT_SUBJECT: one Conventional Commit subject describing only this patch
DIGEST: one sentence
Use "- none" where appropriate and plain path:line references (no Markdown links).
Never quote large code blocks or the raw diff.
EOF
}

run_review() { # payload file, output file, byte limit
  # Keep the reviewer in the actual repository. Running from the temporary payload
  # directory made models search the whole user profile when they ignored the
  # no-tools instruction, producing hundreds of irrelevant steps and apparent hangs.
  # By default the delegate derives idle protection from the hard deadline, so a
  # quiet reviewer is not killed after three minutes. Operators can still set a
  # review-specific ceiling explicitly with AGY_REVIEW_IDLE_TIMEOUT.
  REVIEW_ARGS=(--tier "$TIER" --digest --timeout "$TIMEOUT")
  [ -z "$REVIEW_IDLE_TIMEOUT" ] || REVIEW_ARGS+=(--idle-timeout "$REVIEW_IDLE_TIMEOUT")
  (cd "$ROOT" && AGY_REVIEW_DATA_PAYLOAD=1 AGY_DELEGATE_READ_ONLY=1 "$DELEGATE" "${REVIEW_ARGS[@]}" - <"$1" >"$2")
  RC=$?
  [ "$RC" -eq 0 ] || { echo "agy-review: delegation failed (exit $RC)" >&2; return "$RC"; }
  SIZE="$(LC_ALL=C wc -c <"$2" | tr -d '[:space:]')"
  [ "$SIZE" -le "$3" ] || { echo "agy-review: review output exceeded $3 bytes; raw response suppressed" >&2; return 2; }
  grep -q '^VERDICT:' "$2" || { echo "agy-review: reviewer omitted VERDICT; response suppressed" >&2; return 2; }
  grep -q '^DIGEST:' "$2" || { echo "agy-review: reviewer omitted DIGEST; response suppressed" >&2; return 2; }
}

if [ "$BYTES" -le "$CHUNK_BYTES" ]; then
  { cat "$PREFIX"; schema; printf '%s\n' '--- PATCH START ---'; cat "$DIFF"; printf '%s\n' '--- PATCH END ---'; } >"$PAYLOAD"
  run_review "$PAYLOAD" "$TMP/final" "$MAX_OUTPUT" || exit $?
else
  # Native Windows ultimately places agy's prompt on a command line. Split on line
  # boundaries before that limit, keep each partial verdict private, then synthesize.
  PARTS="$(LC_ALL=C awk -v dir="$TMP" -v lim="$CHUNK_BYTES" '
    BEGIN { n=1; size=0; file=sprintf("%s/chunk-%04d",dir,n) }
    { bytes=length($0)+1; if (size && size+bytes>lim) { close(file); n++; size=0; file=sprintf("%s/chunk-%04d",dir,n) }
      print >>file; size+=bytes }
    END { close(file); print n }
  ' "$DIFF")" || die "could not split the patch"
  REVIEWS="$TMP/partial-reviews"
  : >"$REVIEWS"
  N=0
  for CHUNK in "$TMP"/chunk-*; do
    N=$((N+1))
    CHUNK_SIZE="$(LC_ALL=C wc -c <"$CHUNK" | tr -d '[:space:]')"
    [ "$CHUNK_SIZE" -le "$CHUNK_BYTES" ] || die "one patch line exceeds the Windows-safe chunk size; narrow the review with --path"
    { cat "$PREFIX"; printf 'This is transport part %d of %d and may begin/end inside a file or hunk. Do not report boundary truncation, missing surrounding syntax, or cross-part context as a code defect; judge only complete visible evidence.\n' "$N" "$PARTS"; schema; printf '%s\n' '--- PATCH PART START ---'; cat "$CHUNK"; printf '%s\n' '--- PATCH PART END ---'; } >"$PAYLOAD"
    run_review "$PAYLOAD" "$TMP/review-$N" "$PART_OUTPUT" || exit $?
    { printf '\n--- PARTIAL REVIEW %d OF %d ---\n' "$N" "$PARTS"; cat "$TMP/review-$N"; } >>"$REVIEWS"
  done
  {
    printf '%s\n' "You are the final blind reviewer. Reconcile the partial reviews below against the full goal. Preserve every concrete code finding and resolve duplicate severity. Transport chunks together form one complete patch: discard findings caused only by a chunk beginning/ending mid-file or mid-hunk. Mark genuinely missing evidence INCONCLUSIVE; do not invent evidence."
    printf 'GOAL: %s\n' "$GOAL"
    [ -z "$UNTRACKED" ] || printf '%s\n%s\n' "UNTRACKED PATHS WERE EXCLUDED (final verdict cannot confirm them):" "$UNTRACKED"
    schema
    cat "$REVIEWS"
  } >"$PAYLOAD"
  run_review "$PAYLOAD" "$TMP/final" "$MAX_OUTPUT" || exit $?
fi

cat "$TMP/final"
