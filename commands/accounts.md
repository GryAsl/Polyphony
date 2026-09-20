---
description: Manage Polyphony's local Agy account pool without exposing credentials.
---

Use `agy-account` for the requested account operation and report its compact result.

- `list` or no argument: run `agy-account list`.
- `current`: run `agy-account current`.
- `add [alias]`: save the Agy login currently present in Windows Credential Manager.
- `switch <alias>`, `remove <alias>`, `enable <alias>`, or `disable <alias>`: run the matching command.
- `enable-pool` / `disable-pool`: run `agy-account enable --pool` or `agy-account disable --pool`.
- `rotate`: run `agy-account rotate`.
- `doctor`: run `agy-account doctor`.

Never request, print, paste, or transport OAuth credential material. Automatic rotation is an
explicit opt-in and only applies to accounts the user is authorized to use. A switch blocked by
another live Agy worker is a safety result; do not kill that worker or bypass the block.
