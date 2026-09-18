# Strict routing: action gate, not conversation gate

Soft is the default. An explicit strict/soft choice is stored per workspace and restored for new
sessions. Active sessions retain their own known mode; resume/compact never creates a new question.
Legacy pending or damaged state falls back to soft. Numeric text alone is not a mode command unless
the host actually presented a routing question.

## Substantive evidence

UserPromptSubmit resets turn evidence but does not infer substantive work from prose. PreToolUse
marks actual worker calls or denied substantive native actions; terminal PostToolUse results decide
completion. Greetings, explanations, acknowledgement and text-only clarification therefore need
no worker. A launcher acknowledgement is pending, not success or empty-output failure.

The only clarification override comes from the host's question tool, not a question mark or an NLP
guess. It cannot erase pending or failed Agy work, and a subsequent substantive action resets it.
Routing selection receipts likewise preserve pending/failure evidence. Failure guidance has a
bounded continuation budget to prevent a notification loop, never permission to claim success.

## Narrow native exceptions

- mode/quota/job/doctor/trace management, policy reading and prompt plumbing;
- host question/skill/MCP discovery and session administration;
- exact Polyphony host update commands after authorization, host version/list checks, and the
  bundled read-only `scripts/polyphony_update.py` checker;
- pure Python argv/text/arithmetic probes and cwd/Git status/HEAD checks;
- at most three bounded operations on one literal small, non-sensitive file per turn: a read
  of at most 200 lines (or a file no larger than 32 KiB), capped grep with at most 50 results,
  or a replacement with at most 800 combined old/new characters.

Secrets, policy edits, permission/security files, hooks, manifests and Git internals are not micro
edit/read exceptions (intentional bootstrap policy reading is separate). Broad discovery, builds,
tests, research, Git mutations and arbitrary scripts remain substantive. A host-only external
capability stays advisory unless an equivalent Agy access path has been established. Allowlisting
an update command does not authorize it; the host must obtain the user's approval.

## Portable entry points

Both plugin manifests explicitly reference `claude/hooks/hooks.json`. The removed default
`hooks/hooks.json` stays absent, preserving the 0.31.61 duplicate-loading fix. Commands use CLAUDE_PLUGIN_ROOT,
also supplied by Codex for compatibility. Native Windows Codex selects commandWindows and the
PowerShell launcher; Claude's Bash launcher can fall back to the same native Python discovery.
Targets are resolved relative to the launcher's own file, never Git Bash's cwd.

Both launchers require working Python 3.9+, honor AGY_BRIDGE_PYTHON and preserve UTF-8. Missing
Python exits non-blocking with a daily warning that enforcement is inactive. MCP bootstrap still
requires the documented Python-on-PATH setup; route selection does not repair a broken server.
Persistence failures must produce an error receipt, not a success claim.

## Proportional verification

Exercise the changed routing/control-plane and launcher regressions, manifest syntax and one real
MCP initialization. No full worker, quota, bridge or persistent-runtime suite is necessary when
those components are unchanged. Real Claude/Codex fresh-session UI checks remain the final host
integration check; unit tests do not prove host loading/trust or eliminate every external failure.
