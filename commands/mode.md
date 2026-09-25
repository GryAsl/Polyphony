---
description: Read or change Polyphony's authoritative workspace routing mode.
argument-hint: [strict|soft]
---

This is a local control-plane operation. Never delegate it to an Agy worker.

- With `strict` or `soft`, call the `routing_mode` MCP tool with `action=set`, that mode, and the
  current workspace directory.
- With no argument, call `routing_mode` with `action=get` and the current workspace directory.
- If MCP is unavailable, use `agy-routing set strict|soft --directory <workspace>` or
  `agy-routing get --directory <workspace>` locally. This fallback is also control-plane work.
- Treat a successful receipt as immediately authoritative for the active session and future
  sessions. Do not inspect files, probe the hook, or ask for a restart.
- Reply with one brief confirmation containing the resulting mode.
