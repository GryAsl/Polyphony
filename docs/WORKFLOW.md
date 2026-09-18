# Shared Polyphony workflow

This policy is bundled for Claude Code and Codex. Host instructions and user authorization remain
authoritative; it does not require copying a private global configuration or installing extra hooks.

## One scoped worker for routine work

Use one Flash worker end-to-end when the objective is clear and low risk, including small tasks,
discovery, implementation, code-aware checks, diff review and authorized commit/push. Use a separate
scout when the conductor needs evidence before it can scope, plan or answer. Keep instructions at
200–500 words, strictly below 800, and reference source paths instead of pasted code.

Read compact receipts, not full files, diffs or transcripts. A receipt states touched paths,
checks and exit codes, concrete evidence, uncertainties and remaining gaps. Do not repeat clean
checks or reread the whole result merely because another model produced it. Optional fresh Agy
review is always available. Inspect source natively only for unresolved findings, conflicting
evidence, immediate safety or the user's explicit request; soft routing still permits discretion.

## Risk and parallel work

Use a fresh, independent Agy reviewer for security/auth, secrets, destructive changes, migrations,
concurrency, compatibility boundaries, broad architecture, unexpectedly large diffs, failed checks
or an explicit review request. Request the smallest relevant gates and allow at most one bounded
repair before surfacing an unresolved problem.

For independent workstreams, consider up to three active workers and at most two writers. Give
writers exact, disjoint write ownership and the other writer's DO NOT WRITE paths. Record existing
changes first; forbid shared formatters/generators, Git mutations and recursive delegation. At fan-in,
compare actual changed paths with ownership before one fresh read-only combined verifier. That
verifier also covers risk review when its contract explicitly includes every triggered risk.
Any overlap or unexpected file stops integration; never automatically revert user work. Use
sequential writers or isolated worktrees if disjoint ownership cannot be made safe. These are
conductor/worker safeguards, not a claim that a prompt provides an OS-level filesystem sandbox.

## Planning and publishing

Keep planning research read-only and mostly on Agy scouts when host rules permit. Native exploration
is reserved for genuinely critical/complex questions, with at most one native Explore agent when
host rules permit, never required merely to relay Agy. Keep plans
short, phase large work, and ask concise multiple-choice questions when useful for scope or decisions;
avoid repetitive preference questions and obey the host's question tool contract.

Only publish when authorized. One worker scopes the diff, stages exact task-owned files, writes a
descriptive commit and pushes normally. Its receipt includes branch, hash, subject, remote/ref, push
result and remaining status. The conductor checks HEAD/upstream/status without duplicating the review.
Force-push, history rewriting, tags/releases and unrelated changes need separate authorization.

## Control plane and failures

Soft is the default; strict gates substantive actions, not conversation. Host plugin/skill discovery,
approved plugin updates, mode/quota/job management, clarification and bounded safe checks stay native.
An explicit mode choice must be persisted before reporting success. No reset/reconnect should ask
again. Do not treat a task launcher acknowledgement, exit 0 with empty evidence or nested unfinished
delegation as completion. Pending/failed workers remain pending/failed even when the host asks a question.

Use 30-minute hard timeouts for real work, 45–60 minutes for broad/build-heavy work, and short limits
only for probes. Read failures explicitly; quota depletion requires the user's Sonnet-or-wait choice.
Run changed-area tests only, with parallel independent checks when useful; no routine full-suite loops.
Permissions are opt-in as documented in the README. Never infer write/push authorization from yolo.
