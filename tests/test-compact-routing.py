"""Small, offline regressions for prompt budgets and local helper routing."""
import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load("compact_hook", "hooks/agy_opportunity_reminder.py")
mcp = load("compact_mcp", "codex/mcp_server.py")


class CompactRoutingTests(unittest.TestCase):
    def test_budget_both_hosts_and_soft_mode(self):
        for tool, key in (("Bash", "command"), ("exec_command", "cmd")):
            for count in (800, 801):
                message = hook._agy_prompt_budget_violation(tool, {key: "agy-delegate '" + "word " * count + "'"})
                self.assertEqual(bool(message), count == 801)
        with patch.object(mcp, "_run_shell") as run:
            with self.assertRaises(ValueError):
                mcp._dispatch("delegate", {"prompt": "word " * 801})
            run.assert_not_called()

    def test_character_budget_and_mcp_schema_are_consistent(self):
        long_low_word_prompt = "x" * 8001
        for tool, key in (("Bash", "command"), ("exec_command", "cmd")):
            message = hook._agy_prompt_budget_violation(
                tool, {key: "agy-delegate '" + long_low_word_prompt + "'"}
            )
            self.assertTrue(message)
        with patch.object(mcp, "_run_shell") as run:
            with self.assertRaises(ValueError):
                mcp._dispatch("delegate", {"prompt": long_low_word_prompt})
            run.assert_not_called()
        delegate = next(tool for tool in mcp.TOOLS if tool["name"] == "delegate")
        prompt_schema = delegate["inputSchema"]["properties"]["prompt"]
        self.assertEqual(prompt_schema["maxLength"], 8000)
        self.assertIn("shortest sufficient", prompt_schema["description"])
        self.assertIn("never more than 800", prompt_schema["description"])

    def test_accumulated_prompt_file_and_powershell(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "agy_task_fixture.md"
            task.write_text("word " * 500, encoding="utf-8")
            command = f"$p = '{task}'\nAdd-Content -Path $p -Encoding utf8 -Value @'\n" + "word " * 301 + "\n'@"
            self.assertTrue(hook._agy_prompt_budget_violation("Bash", {"command": command}))
            self.assertTrue(hook._agy_prompt_budget_violation("Edit", {
                "file_path": str(task), "old_string": "word", "new_string": "word " * 302,
            }))
            command = command.replace("Add-Content", "Set-Content")
            self.assertIsNone(hook._agy_prompt_budget_violation("exec_command", {"cmd": command}))
            self.assertTrue(hook.is_control_plane_exempt("exec_command", {"cmd": command}))
        screenshot = "$p = 'C:\\Users\\Gry\\AppData\\Local\\Temp\\claude\\session\\scratchpad\\ob1.md'\nSet-Content -Path $p -Encoding utf8 -Value @'\nTASK: implement the feature\n'@"
        self.assertTrue(hook.is_control_plane_exempt("Bash", {"command": screenshot}))

    def test_pure_python_probe_allowed_not_repository_work(self):
        for tool, key in (("Bash", "command"), ("exec_command", "cmd")):
            for command in ('python -c "import sys;print(len(sys.argv[1]))" "aaa\nbbb"', 'python -c "print(2+2)"'):
                self.assertTrue(hook.is_control_plane_exempt(tool, {key: command}))
            for command in ('python -c "print(open(\'source.py\').read())"',
                            'python -c "import subprocess; subprocess.run([\'git\',\'push\'])"',
                            'python -c "print(1)"; git push',
                            'python -c "print(1)" "$(git push)"'):
                self.assertFalse(hook.is_control_plane_exempt(tool, {key: command}))

    def test_helper_only_turn_does_not_require_dummy_agent(self):
        state = hook._default_state()
        state.update(mode="strict", is_substantive=True)
        with patch.object(hook, "_write_state"), contextlib.redirect_stdout(io.StringIO()) as output:
            hook.handle_pre_tool_use({"tool_name": "exec_command", "tool_input": {"cmd": 'python -c "print(2+2)"'}}, state, "test")
            hook.handle_stop({}, state, "test")
        self.assertEqual(output.getvalue(), "")
        state["denied_categories"] = ["implementation"]
        with patch.object(hook, "_write_state"), contextlib.redirect_stdout(io.StringIO()) as output:
            hook.handle_stop({}, state, "test")
        self.assertTrue(output.getvalue())

    def test_delegate_stdin_cannot_bypass_limit(self):
        bash = shutil.which("bash")
        if not bash and Path("C:/Program Files/Git/bin/bash.exe").is_file():
            bash = "C:/Program Files/Git/bin/bash.exe"
        if not bash:
            self.skipTest("Git Bash unavailable")
        # Dry-run rejects before model discovery or any real AGY invocation.
        for count in (800, 801):
            result = subprocess.run([bash, "scripts/agy-delegate.sh", "--model", "Fixture", "--print-command", "-"],
                                    input="word " * count, text=True, capture_output=True, cwd=ROOT, timeout=10)
            self.assertEqual(result.returncode, 0 if count == 800 else 1, result.stderr)


if __name__ == "__main__":
    unittest.main()
