#!/usr/bin/env bash
# Compact, read-only repository scout. It keeps the repeated policy boilerplate on
# the Gemini side so Claude only has to provide a repository and one precise question.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DELEGATE="${AGY_DELEGATE:-$HERE/agy-delegate.sh}"
DIR="."
TIMEOUT="30m"
TIER="flash"
QUESTION=""
MAX_OUTPUT="${AGY_SCOUT_MAX_OUTPUT_CHARS:-8000}"

usage() {
  cat <<'EOF'
Usage: agy-scout --dir <repo> [--tier flash-medium|flash] [--timeout 30m] "question"

Runs one read-only latest-Gemini-Flash planning scout and returns only a compact,
evidence-based digest. High (`flash`) is the default; Medium is explicitly selectable
for a clearly simple question. It never passes --yolo.
EOF
}
die() { echo "agy-scout: $*" >&2; exit 2; }
need() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 requires a value"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir)     need "$@"; DIR="$2"; shift 2 ;;
    --tier)    need "$@"; TIER="$2"; shift 2 ;;
    --timeout) need "$@"; TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*)       die "unknown option: $1" ;;
    -)         QUESTION="$(cat)"; shift; [ "$#" -eq 0 ] || die "stdin marker must be the final argument" ;;
    *)         QUESTION="$1"; shift; [ "$#" -eq 0 ] || die "quote the question as one argument" ;;
  esac
done

[ -n "$QUESTION" ] || die "a question is required"
case "$TIER" in flash-medium|flash) ;; *) die "--tier must be flash-medium or flash" ;; esac
[ -d "$DIR" ] || die "directory not found: $DIR"
[ -x "$DELEGATE" ] || die "delegation wrapper is not executable: $DELEGATE"
case "$MAX_OUTPUT" in ''|*[!0-9]*) die "AGY_SCOUT_MAX_OUTPUT_CHARS must be an integer" ;; esac
[ "$MAX_OUTPUT" -gt 0 ] || die "AGY_SCOUT_MAX_OUTPUT_CHARS must be greater than zero"

PROMPT="READ-ONLY repository investigation. Do not modify files, install anything, or perform Git mutations. Inspect only the files needed to answer this question:

$QUESTION

Return ONLY a compact evidence digest with these headings:
FINDINGS: ordered bullets with file:line evidence and relevant data/control flow
RISKS_OR_GAPS: uncertainties, conflicting evidence, and checks still needed
NEXT_LIKELY_GAP: the most likely downstream hotspot or follow-up that would otherwise cause a second work round
DIGEST: one-sentence answer
Use at most eight short bullets total and plain path:line references (no Markdown links).
Do not paste full methods, files, raw logs, or propose a patch unless the question explicitly asks for design options."

OUT="$(printf '%s' "$PROMPT" | AGY_DELEGATE_READ_ONLY=1 "$DELEGATE" --tier "$TIER" --mode plan --digest --timeout "$TIMEOUT" --dir "$DIR" -)"
RC=$?
[ "$RC" -eq 0 ] || { echo "agy-scout: delegation failed (exit $RC)" >&2; exit "$RC"; }

OUT_BYTES="$(LC_ALL=C printf '%s' "$OUT" | wc -c | tr -d '[:space:]')"
[ "$OUT_BYTES" -le "$MAX_OUTPUT" ] || die "scout output exceeded ${MAX_OUTPUT} bytes; raw response suppressed"
grep -q '^DIGEST:' <<<"$OUT" || die "scout omitted DIGEST; response suppressed"
printf '%s\n' "$OUT"
