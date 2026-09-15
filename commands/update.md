---
description: Check for and, only after explicit user approval, update Polyphony.
---

Check the Polyphony update notice first. If no newer release is known, run the
throttled check or tell the user that Polyphony is already current. If a newer
release exists, ask the user for explicit confirmation before changing anything.

After approval, use the command for the current host:

- Claude Code: `claude plugin update antigravity@polyphony -y`, then reload/restart
  (inside Claude's UI: `/plugin marketplace update polyphony`, then `/reload-plugins`).
- Codex: `codex plugin marketplace upgrade polyphony`, then
  `codex plugin add antigravity@polyphony`.

Verify the installed plugin version and report the result. Tell the user that
Claude must reload/start a new session and Codex must start a new task before
the updated plugin code is loaded. Never update on a mere status check or on
ambiguous user wording.
