#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "install_windows_account_launcher",
    ROOT / "scripts" / "install_windows_account_launcher.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WindowsAccountLauncherTests(unittest.TestCase):
    def test_installs_idempotent_launcher_into_explicit_path_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw)
            with mock.patch.dict("os.environ", {"POLYPHONY_LAUNCHER_DIR": raw}, clear=False):
                first = MODULE.install()
                second = MODULE.install()

            self.assertEqual(first, target / "agy-account.cmd")
            self.assertEqual(second, first)
            self.assertEqual((target / "agy-account.cmd").read_bytes(), (ROOT / "bin" / "agy-account.cmd").read_bytes())
            self.assertEqual(
                (target / "polyphony-agy-account.py").read_bytes(),
                (ROOT / "scripts" / "agy_account.py").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
