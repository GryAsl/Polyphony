"""Focused tests for Polyphony's declarative strict-mode capability model."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from polyphony_capabilities import (  # noqa: E402
    mcp_capability,
    registered_shell_commands,
    shell_capability,
)


class ToolCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "polyphony_capability_hook", ROOT / "hooks" / "agy_opportunity_reminder.py"
        )
        cls.hook = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(cls.hook)

    def test_introspection_is_generically_read_only_for_every_registered_command(self):
        for command in registered_shell_commands():
            with self.subTest(command=command):
                self.assertEqual(shell_capability([command, "--help"]), "read-only")
                self.assertTrue(self.hook.is_control_plane_exempt(
                    "Bash", {"command": f"{command} --help"}
                ))

    def test_capabilities_distinguish_work_control_and_unknown_calls(self):
        self.assertEqual(shell_capability(["agy-delegate", "do work"]), "work-producing")
        self.assertEqual(shell_capability(["agy-job", "start", "do work"]), "work-producing")
        self.assertEqual(shell_capability(["agy-job", "status", "123"]), "read-only")
        self.assertEqual(shell_capability(["agy-routing", "set", "soft"]), "control-plane")
        self.assertIsNone(shell_capability(["unregistered-command", "--help"]))
        self.assertEqual(mcp_capability("mcp__antigravity__delegate", {}), "work-producing")
        self.assertEqual(mcp_capability("mcp__antigravity__routing_mode", {"action": "set"}), "control-plane")
        self.assertEqual(mcp_capability("mcp__antigravity__job", {"action": "cancel_all"}), "control-plane")
        self.assertEqual(mcp_capability("mcp__antigravity__persistent_delegate", {"action": "start"}), "work-producing")
        self.assertEqual(mcp_capability("mcp__antigravity__persistent_delegate", {"action": "status"}), "read-only")
        self.assertEqual(mcp_capability("mcp__antigravity__persistent_delegate", {"action": "cancel"}), "control-plane")
        self.assertFalse(self.hook.is_control_plane_exempt(
            "Bash", {"command": "agy-delegate 'do work'"}
        ))
        self.assertFalse(self.hook.is_control_plane_exempt(
            "Bash", {"command": "agy-delegate --help 'do work'"}
        ))
        self.assertFalse(self.hook.is_control_plane_exempt(
            "Bash", {"command": "unregistered-command --help"}
        ))

    def test_routing_wrapper_exposes_help(self):
        bash = shutil.which("bash")
        if not bash and sys.platform == "win32":
            for candidate in (
                Path(r"C:\Program Files\Git\bin\bash.exe"),
                Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
            ):
                if candidate.exists():
                    bash = str(candidate)
                    break
        self.assertIsNotNone(bash, "Git Bash is required for wrapper smoke tests")
        completed = subprocess.run(
            [bash, str(ROOT / "bin" / "agy-routing"), "--help"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("workspace routing mode", completed.stdout)


if __name__ == "__main__":
    unittest.main()
