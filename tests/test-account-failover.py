#!/usr/bin/env python3
"""Focused end-to-end tests for delegate account failover."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DELEGATE = ROOT / "scripts" / "agy-delegate.sh"
ACCOUNT = ROOT / "scripts" / "agy_account.py"


class AccountFailoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.count = self.root / "agy-count.txt"
        self.mode = self.root / "agy-mode.txt"
        self.mode.write_text("quota-once", encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": str(self.fake_bin) + os.pathsep + self.env.get("PATH", ""),
                "POLYPHONY_ACCOUNTS_DIR": str(self.root / "accounts"),
                "POLYPHONY_CREDENTIAL_BACKEND": "fake",
                "AGY_QUOTA_STATE_DIR": str(self.root / "quota"),
                "AGY_BRIDGE_PYTHON": Path(sys.executable).as_posix(),
                "AGY_TEST_FORCE_POSIX": "1",
                "CLAUDE_PLUGIN_OPTION_STRUCTURED_OUTPUT": "off",
                "AGY_QUOTA_COMMAND": str(self.root / "fake-quota.sh"),
                "TEST_AGY_COUNT": str(self.count),
                "TEST_AGY_MODE": str(self.mode),
            }
        )
        self._write_executable(
            self.fake_bin / "agy",
            """#!/usr/bin/env bash
if [ "${1:-}" = --help ]; then echo 'fake agy'; exit 0; fi
n=0; [ ! -f "$TEST_AGY_COUNT" ] || n="$(cat "$TEST_AGY_COUNT")"
n=$((n+1)); printf '%s' "$n" > "$TEST_AGY_COUNT"
mode="$(cat "$TEST_AGY_MODE")"
if [ "$mode" = quota-always ] || { [ "$mode" = quota-once ] && [ "$n" -eq 1 ]; }; then
  echo 'RESOURCE_EXHAUSTED (code 429): Individual quota reached' >&2
  exit 1
fi
if [ "$mode" = syntax ]; then echo 'invalid configuration syntax' >&2; exit 1; fi
echo "success-from-process-$n"
""",
        )
        self._write_executable(
            self.root / "fake-quota.sh",
            """#!/usr/bin/env bash
if printf '%s\n' "$*" | grep -q -- '--mark-depleted'; then
  echo 'AGY_QUOTA {"status":"DEPLETED","decision":null}'
  exit 10
fi
echo 'AGY_QUOTA {"status":"AVAILABLE","decision":null}'
exit 0
""",
        )
        self._set_active(b"token-a")
        self._account("add", "personal")
        self._set_active(b"token-b")
        self._account("add", "work")
        self._account("switch", "personal")
        self._account("enable", "--pool")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def _set_active(self, value: bytes) -> None:
        active = Path(self.env["POLYPHONY_ACCOUNTS_DIR"]) / ".fake_active.bin"
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_bytes(value)

    def _account(self, *args: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(ACCOUNT), "--json", *args],
            env=self.env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _delegate(self) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        if not bash and os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
            if candidate.exists():
                bash = str(candidate)
        if not bash:
            self.skipTest("Git Bash/bash is required")
        return subprocess.run(
            [bash, str(DELEGATE), "--model", "Gemini Test Flash", "--timeout", "30s", "small read-only task"],
            cwd=ROOT,
            env=self.env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=90,
        )

    def test_explicit_quota_failure_rotates_and_retries_in_new_process(self) -> None:
        completed = self._delegate()
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("success-from-process-2", completed.stdout)
        self.assertIn("AGY_ACCOUNT_FAILOVER", completed.stderr)
        self.assertEqual("2", self.count.read_text(encoding="utf-8"))
        self.assertEqual("work", self._account("current")["current"])

    def test_non_quota_failure_does_not_rotate(self) -> None:
        self.mode.write_text("syntax", encoding="utf-8")
        completed = self._delegate()
        self.assertEqual(2, completed.returncode)
        self.assertNotIn("AGY_ACCOUNT_FAILOVER", completed.stderr)
        self.assertEqual("personal", self._account("current")["current"])

    def test_each_account_is_tried_once_before_existing_quota_fallback(self) -> None:
        self.mode.write_text("quota-always", encoding="utf-8")
        completed = self._delegate()
        self.assertEqual(10, completed.returncode)
        self.assertEqual("2", self.count.read_text(encoding="utf-8"))
        self.assertIn("QUOTA_DECISION_REQUIRED", completed.stderr)


if __name__ == "__main__":
    unittest.main()
