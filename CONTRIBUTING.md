# Contributing

Thanks for your interest! This is an early-stage, MIT-licensed community project —
issues, PRs, and even a ⭐ all genuinely help shape where it goes.

**Not sure where to start?** Look for the
[`good first issue`](https://github.com/GryAsl/Polyphony/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
label.

## What's especially welcome

- **More A/B data points** — repeat the measured runs (n>1) for tighter confidence ([`docs/AB-RESULTS.md`](docs/AB-RESULTS.md)).
- **New SDLC recipes** — when-to-delegate patterns in [`skills/antigravity/SKILL.md`](skills/antigravity/SKILL.md).
- **Support for other CLIs / models** — the delegation wrapper is intentionally thin.
- **Real Vertex prices** — keep [`prices.json`](prices.json) accurate.

## Dev setup

Offline checks need only Python 3.9+, Bash and Git. On native Windows, use Git Bash.
No API key, cloud project, GitHub App, authenticated `agy`, Claude subscription or Codex
installation is needed to contribute or run CI. Real worker smoke tests are optional and
require your own installed/authenticated Antigravity CLI and host.

```bash
git clone https://github.com/GryAsl/Polyphony ~/Polyphony
cd ~/Polyphony

# load the plugin live from your working tree ($CLAUDE_PLUGIN_ROOT resolves):
claude --plugin-dir ~/Polyphony
```

The scripts also run standalone — handy for quick iteration:

```bash
scripts/agy-delegate.sh --tier flash-medium "Summarize this in 3 bullets: ..."
```

## Before you open a PR

```bash
bash tests/run-tests.sh          # short offline smoke suite; no API keys
# Optional, only for broader changes:
bash tests/run-tests.sh --full
```

- You do not need to run the full suite for every PR. CI runs short, deterministic checks;
  pure README/contributor-guide/docs changes skip automatic CI. Skills and runtime policy changes
  still run it. Extended tests stay available for platform/security/concurrency changes.
- CI never invokes a paid AI reviewer. Review is human or explicitly requested by a maintainer;
  missing review credentials cannot fail a contributor's checks.
- ShellCheck errors are checked when available; Bash syntax is the fallback. See
  [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
- If you touch a manifest, `python3 -c "import json; json.load(open('.claude-plugin/plugin.json'))"` (and `marketplace.json`, `prices.json`) still parse.
- **Keep the skill honest.** [`skills/antigravity/SKILL.md`](skills/antigravity/SKILL.md) is the plugin's brain — if behavior changes, update it. Don't claim a capability the code doesn't have.
- **Cost numbers are estimates.** If you quote figures, say so and point at `prices.json`.
- Add a line to [`CHANGELOG.md`](CHANGELOG.md) under "Unreleased".

## Conventions

- Small, focused PRs. Describe *what changed and why*; link the issue.
- Match the surrounding style — POSIX-ish bash, `set -euo pipefail`, quote expansions.
- **Target bash 3.2.** macOS still ships `/bin/bash` 3.2.57 (GPLv3 is why), and macOS is a
  supported platform, so `declare -A`, `readarray`/`mapfile`, `${var^^}` and friends are out.
  Same for GNU-only flags on `sed`, `date` and `grep` — BSD userland is the floor. Testing on
  Linux only will not catch these.
- New scripts get a `usage()` and a test in `tests/run-tests.sh`.

## Reporting bugs / ideas

Open an issue with what you ran (`agy --version`, the command, OS) and what you
expected vs. saw. Feature ideas welcome too — even half-formed ones.

By contributing you agree your work is licensed under the project's [MIT License](LICENSE).
