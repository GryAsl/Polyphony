<div align="center">

# Polyphony

**Multi-model orchestration for Claude Code and Codex, powered by Antigravity.**

![Polyphony — Claude and Codex orchestrate, Gemini executes](docs/hero.png)

Claude Code or Codex conducts the workflow. Gemini handles delegated execution through
a shared, verifiable toolchain.

</div>

---

## About

Polyphony gives Claude Code and Codex the same Antigravity (`agy`) worker toolkit. It
reduces main-agent context use while keeping execution scoped, observable, and verifiable.

- scoped implementation and general delegation
- read-only repository scouting and independent diff review
- web research and media analysis
- background jobs, account-aware 5h/7d quota control, traces, diagnostics, migration, Cloud debugging, and cost comparison
- optional local Agy account profiles with encrypted Windows credential switching and bounded failover
- non-blocking reminders when a task could be delegated to Gemini

On Windows, the bundled `agy-headless-bridge` v1.2.1 runs headless workers through ConPTY;
no separate bridge installation is required.

## Requirements

- [Antigravity CLI](https://antigravity.google/docs/cli-using) (`agy`), installed and authenticated, plus [Claude Code](https://docs.anthropic.com/en/docs/claude-code), Codex, or both
- Python 3.9 or newer, with [pywinpty](https://pypi.org/project/pywinpty/) on Windows
- On Windows: Git Bash, included with [Git for Windows](https://git-scm.com/download/win), for the Bash wrappers

On native Windows, install `pywinpty` and authenticate Antigravity once:

```powershell
py -3 -m pip install -U pywinpty
agy
agy models
```

## Install for Claude Code

Run these commands inside Claude Code:

```text
/plugin marketplace add GryAsl/Polyphony
/plugin install antigravity@polyphony
/antigravity:setup
```

This installs the slash commands, skill, custom agent, wrappers, and optional delegation reminders.

## Install for Codex

```powershell
codex plugin marketplace add https://github.com/GryAsl/Polyphony
codex plugin add antigravity@polyphony
```

Start a new Codex task after installation. The plugin provides direct MCP tools for delegation, scouting, review, research, media, jobs, quota control, traces, diagnostics, migration, Cloud debugging, and cost comparison. Note that Codex plugin hooks must be reviewed and trusted whenever their definitions change.

## Routing modes

Polyphony defaults to **soft**, with optional strict routing:

- **Strict:** Substantive Agy-capable work is delegated to Agy/Gemini; tiny local helpers,
  bounded small-file operations, and host-only tools remain available. A strict turn needs a
  completed successful Agy result after substantive work starts. Conversation and clarification
  alone never require a worker; pending or failed workers cannot be disguised as success.
- **Soft:** Delegation reminders are advisory and native execution remains allowed.

<p align="center">
  <img src="docs/agy-routing-modes.png" alt="Polyphony soft and strict routing mode selection" width="900">
</p>

**Session start:** No mode question is required. Explicit workspace preferences persist across
new conversations, reboots, reconnects and resume; missing or damaged preferences fall back to soft.

**Control-plane exceptions:** Mode changes, quota/job/doctor/trace management, approved host plugin
updates, version checks, skill/MCP discovery, session management, bounded conductor checks and user
interaction remain native. Editing the code that implements those features is still repository work.

**Manual switching:** Use "switch Agy mode to strict", "set Agy mode to soft", or "soft moda geç".
The host must receive a successful persistence receipt before claiming a preference was saved.

### Shared workflow

Both hosts receive the same lean execution policy from the plugin; a custom global `CLAUDE.md`
or `AGENTS.md` is not required. Clear, low-risk tasks use one scoped worker end-to-end, including
discovery, implementation, proportional checks and authorized publishing. Independent review is
optional for routine work and required when risk or an explicit request justifies it. See the
[shared workflow](docs/WORKFLOW.md) for planning, parallel ownership and compact receipts.

## Model routing

Calls default to High. Choose Medium explicitly only for a clearly simple task:

| Tier | Use it for | Model selection |
| --- | --- | --- |
| `flash` | Default; especially complex reasoning, architecture, concurrency/security, difficult debugging, ambiguous multi-file work, or adversarial review | Newest available Gemini Flash, High effort |
| `flash-medium` | Explicit option for simple, routine, mechanical, or tightly bounded work | Newest available Gemini Flash, Medium effort |
| `pro` | Exceptional escalation only | Configured Gemini Pro model |

There is no Low tier. Both Flash tiers query `agy models` and follow the newest available Gemini Flash family. If discovery is unavailable, they fall back to Gemini 3.8 Flash at the selected effort level. Exact models can still be supplied with `--model` or plugin configuration overrides.

## Usage

Claude Code examples:

```text
/antigravity:delegate --tier flash-medium "Add the missing unit tests"
/antigravity:delegate --tier flash "Diagnose this cross-module concurrency bug"
/antigravity:review
/antigravity:research "Research this topic and include source URLs"
/antigravity:quota
```

Codex uses the corresponding `antigravity` MCP tools; both hosts call the same wrappers and
preserve stdout, stderr, and exact exit codes.

The wrappers are also available from Git Bash:

```text
agy-delegate --tier flash-medium --dir "C:\path\to\repo" --digest "Implement this bounded change"
agy-scout --tier flash-medium --dir "C:\path\to\repo" "Trace the request flow"
agy-review --tier flash --dir "C:\path\to\repo" --staged --goal "Implement feature X"
agy-job start --tier flash --dir "C:\path\to\repo" "Complete this long-running task"
agy-quota --force
agy-account list
```

Routine calls default to 30 minutes; use `--timeout 45m` or `--timeout 60m` for broad,
build-heavy work. Keep authored prompts at 200–500 words and always below 800; use source paths
instead of pasting code. The Windows idle timeout normally follows the hard deadline.

**Strict-mode exceptions:** Tiny orchestration helpers (pure Python argument/text/arithmetic probes, working-directory or Git status/HEAD checks, temporary Agy prompt preparation) run locally. Host-only tools without equivalent Agy access remain advisory. Substantive discovery, implementation, review, tests and Git mutations still require Agy; a short command or the word `python` alone does not make substantive work exempt.

### Persistent subagents

The `persistent_delegate` MCP tool and `polyphony-agent` CLI provide a small SQLite-backed
registry/message bus for repeated subagent work. A stable `parent_agent_id` may reuse only the
subagent it created in the same workspace; agents never enter a global pool. Each agent has one
active task at a time, with leases, heartbeat/stale recovery, bounded delegation, short messages,
handoff, and a fresh-conversation fallback when resume fails. Claude and Codex use the same Python
runtime; ordinary `delegate` remains available when isolation is preferred.

## Local Agy account pool

On native Windows, Polyphony can save the Agy login that already exists in Credential Manager
under `gemini:antigravity`. Saved records are encrypted for the current Windows user with DPAPI;
OAuth material is never written as plaintext, printed, or passed on a command line.

Starting Claude Code or Codex installs a small `agy-account.cmd` launcher into an existing
user-owned PATH directory (normally `~/.local/bin` or Agy's own `bin` directory). The commands
below therefore work directly in PowerShell and Command Prompt; Polyphony never edits PATH.

```powershell
# Account A is currently logged in
agy-account add personal

# Log into Account B once with the normal Agy flow, then save it
agy-account add work

agy-account list
agy-account switch personal

# Automatic sticky failover is explicit opt-in
agy-account enable --pool
```

The active healthy account remains selected until a definite quota or authentication failure.
Polyphony then marks that account unavailable, safely switches to one eligible saved account,
starts a new Agy process, and retries the interrupted call. It never round-robins requests or tries
one account twice in a failover chain. Switching is blocked while another unrelated Agy worker is
active, and credential drift is backed up before any overwrite. Disable automatic rotation with
`agy-account disable --pool`; users without saved profiles keep the previous behavior unchanged.

Quota caches and persistent Agy conversation IDs are isolated per account. If every enabled account
is exhausted, the existing Sonnet-or-wait user decision remains the final fallback. Use only accounts
you are authorized to operate and confirm that this workflow is allowed by the terms applicable to
those accounts.

## Gemini quota control

Polyphony tracks both Gemini quota windows (**5h** and **7d**). After a failed, empty, or timed-out
call, it checks them immediately. An explicitly enabled account pool tries another eligible Gemini
account first; if the pool is absent or exhausted and either window is at **2% or less**, Polyphony
pauses without switching models automatically.

<p align="center">
  <img src="docs/gemini-quota-control.png" alt="Polyphony Gemini quota depleted decision dialog" width="760">
</p>

The user chooses either to stop stalled workers and continue with direct Sonnet 4.6, or keep
workers alive and wait while both windows are checked every 10 minutes. No fallback occurs
without that choice; waiting resumes Gemini only above 2%. Sonnet fallback also requires a direct
completion receipt and concrete evidence. One-time notices appear at 75%, 50%, 25%, and 10%.

## Polyphony update checks

While Polyphony is active, its hooks automatically perform a read-only GitHub latest-release check
once per day by default. If a newer version exists, the host asks for explicit approval and then
shows the Claude- or Codex-specific update command; it never installs silently. After approval,
verify the version and reload Claude/start a new Codex task so the new plugin is loaded.

The successful-check interval and network timeout are configurable with
`POLYPHONY_UPDATE_CHECK_INTERVAL_SECONDS` and `POLYPHONY_UPDATE_CHECK_TIMEOUT_SECONDS`;
failed network checks retry after 15 minutes by default and can be tuned with
`POLYPHONY_UPDATE_FAILURE_RETRY_SECONDS`. Set `POLYPHONY_UPDATE_CHECK=off` to disable checks.
The manual `/antigravity:update` command follows the same approval rule.

## Permissions and troubleshooting

Contributor checks are offline and require no API keys or cloud setup. Run
`bash tests/run-tests.sh` for the short smoke suite; extended tests are opt-in with `--full`.
Documentation-only changes do not trigger automatic CI. See [Contributing](CONTRIBUTING.md).

Run write-capable tasks on a trusted branch. `--yolo` grants the worker broad access to
files, commands, network access, and process-visible credentials; it remains explicit
unless enabled in plugin or environment settings.

To opt into automatic `--yolo` for write-capable delegates, set Claude's `always_yolo` plugin
option to `on`, or use the cross-host environment setting:

```powershell
# Windows: current shell and future desktop/CLI processes
$env:AGY_ALWAYS_YOLO = '1'
[Environment]::SetEnvironmentVariable('AGY_ALWAYS_YOLO', '1', 'User')
```

```bash
# macOS/Linux: export from the environment that launches your host
export AGY_ALWAYS_YOLO=1
```

Restart Claude/Codex after changing persistent environment settings. Remove the variable or set it
to `0` to disable this override. Scouting remains read-only and does not inherit automatic yolo.
This is an opt-in permission setting, not authorization to push, delete data or touch unrelated files.

Hooks resolve Python independently of the shell's working directory and fall back from
`AGY_BRIDGE_PYTHON` to installed interpreters. For a custom Python installation, point
`AGY_BRIDGE_PYTHON` at its executable. The MCP server also requires `python` on the host's PATH;
enable that option when installing Python and restart the host. Missing hook Python produces a
rate-limited setup warning and disables enforcement instead of flooding every tool call with errors.

If a call fails, run `/antigravity:setup` in Claude Code, the `doctor` MCP tool in Codex, or `agy-doctor` from Git Bash. More diagnostics are in [Troubleshooting](docs/TROUBLESHOOTING.md).

## Why Polyphony uses ConPTY on Windows

[ConPTY](https://learn.microsoft.com/en-us/windows/console/pseudoconsoles) is the Windows
pseudoconsole system developed and maintained by Microsoft. It provides a bidirectional,
UTF-8 terminal channel for console applications—the Windows counterpart to a Unix PTY.
Polyphony uses ConPTY because headless Antigravity sessions still expect real terminal
semantics; ordinary redirected pipes can lose output, stall permission/tool flows, or
misrepresent an active process as hung. The bundled bridge hosts `agy` inside a genuine
Windows pseudoconsole while preserving structured output, Unicode, timeouts, and exit
codes for Claude Code and Codex.
