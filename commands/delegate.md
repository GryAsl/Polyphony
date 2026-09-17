---
description: Delegate a well-scoped subtask to Antigravity (agy/Gemini) under cost discipline, then verify.
argument-hint: "[--tier flash-medium|flash|pro] <task>"
---

Delegate the following task to Antigravity (`agy` / Gemini) via the plugin wrapper,
following the `antigravity` skill's **Cost discipline** and **Verification gates**.

Task: $ARGUMENTS

Do this:
1. Default to `flash` (High). You may explicitly pick `flash-medium` for a clearly
   simple/routine/mechanical task when that is beneficial. Use High for complex reasoning,
   architecture, concurrency/security, ambiguous multi-file behavior, difficult debugging,
   adversarial review, or a materially incomplete Medium result. Both track the newest
   Gemini Flash family. Reserve
   `pro` for exceptional escalation. If the task needs the repo,
   add `--dir <repo-root>` so agy reads the real files (don't paste them into context).
   **If the task WRITES files or uses tools** (web / Vertex AI Search / terminal), it needs
   a grant. For a plain file write the narrower one is a `write_file(<dir>)` entry under
   `permissions.allow` in `~/.gemini/antigravity-cli/settings.json` (recursive beneath
   `<dir>`, no flag needed — substitute a real path for `<dir>`; if a rule is already
   there and the write is still denied, `agy-doctor` checks whether agy can parse it). Otherwise pass **`--yolo`**, which auto-approves all tools and
   is what web / Vertex AI Search / terminal need. Without a grant,
   headless agy leaves your workspace untouched, and only the newest versions admit it (it
   describes / scratch-diverts / soft-denies / fails outright depending on version; issue #10). `--mode
   accept-edits` is not a grant either: measured on agy 1.1.13, where the flag is applied
   at all, the write is denied exactly like one without it. Run
   write tasks on a dedicated branch — `--sandbox` is not containment, it was measured
   doing nothing under `--yolo` — and
   **verify files actually changed** with `git status`. Claude Code may prompt for or block
   `--dangerously-skip-permissions` — approve it or pre-allow it; non-interactive
   (`claude -p`) without that permission can't write/use-tools via agy. (If the wrapper
   returns exit `15`, that's exactly this: agy denied the write. Both shapes land here —
   the soft deny on agy 1.1.3 and the hard error on 1.1.13 — and both take the same
   fix: a `permissions.allow` rule covering the target, or `--yolo`.)
2. Run **synchronously** (you may be headless — do not background-and-wait):
   `agy-delegate --tier <tier> [--dir .] [--yolo] [--digest] "<task>"`
   For read/analysis tasks, add `--digest` — it appends a digest-only output contract so
   agy returns compact bullets instead of raw content.
3. Ingest only the **result/digest** — do NOT re-read the files agy already handled
   (keeps your context lean; that's where the cost savings come from). If the wrapper
   prints a *"looks like a raw dump"* note on stderr, do NOT ingest the raw output —
   re-run with `--digest` or ask agy to summarize it first.
4. **Verify**: actually run/check the output; never trust a self-reported "done".
   Report what you delegated and how you verified it.

Small tasks are explicitly eligible. Do not refuse solely because a task is below the
cost break-even; use one precise synchronous Flash delegation and avoid needless fan-out.

For repeated work from the same main agent, prefer the Polyphony MCP tool
`persistent_delegate` when available. Give it one stable `parent_agent_id` for the
current main agent and reuse only the returned `agent_id`; Polyphony will reject a
different parent or workspace. It serializes one task per subagent, renews the lease,
and rotates to a fresh conversation only when an explicit resume failure is detected.
Use the ordinary `delegate` tool for isolated work or when context reuse is undesirable.

If a Gemini run fails, the wrapper checks both 5h and 7d quota windows. Either window at
or below 2% is depleted and returns exit 10. Ask the user whether to kill stalled workers
and continue with Claude Sonnet 4.6, or keep them alive and check both windows every 10
minutes. Never select either path automatically. Record the explicit answer with
`agy-quota --decision sonnet|wait`; the wrapper routes to Sonnet only after approval.

**Long task, interactive session?** A sync delegation can also hit Claude Code's ~2-min
Bash-tool limit — start it in the background and keep working (this also keeps the prompt
cache warm and frees you to do other turns):
`ID=$(agy-job start --tier pro --dir . "<task>")`
then check `/antigravity:status` and collect with `/antigravity:result <id>`.
(Don't do this when YOU are headless `claude -p` — one-shot, no later turn to collect;
delegate synchronously there.)
