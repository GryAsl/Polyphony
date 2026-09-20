---
description: Check Agy Gemini 5h/7d quota or record the user's explicit depletion choice.
argument-hint: "[sonnet|wait|clear]"
---

Manage the active Agy account's Gemini quota state. All messages to the user must be in English.

- With no argument, run `agy-quota --force` and report both 5h and 7d remaining values.
- If the explicitly enabled account pool has another eligible saved account, the delegate wrapper
  rotates and retries that account first. Offer Sonnet/wait only after the pool is exhausted.
- Run `agy-quota --decision sonnet` only after the user explicitly chooses to kill stalled
  workers and continue with Claude Sonnet 4.6. Then cancel host-managed stalled Agy tasks
  and run `agy-job cancel-all` for plugin-managed jobs before retrying the interrupted work.
- Run `agy-quota --decision wait` only after the user explicitly chooses to keep workers
  alive. Schedule `agy-quota --force` every 10 minutes; do not start Sonnet. Stop the
  schedule and resume Gemini after both windows are above 2%.
- `clear` clears a stored choice; it does not make depleted quota available.

Never choose `sonnet` or `wait` on the user's behalf.
