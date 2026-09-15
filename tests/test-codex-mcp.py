#!/usr/bin/env python3
"""Contract tests for the thin Codex-to-wrapper MCP adapter."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "antigravity_codex_mcp", ROOT / "codex" / "mcp_server.py"
)
assert SPEC and SPEC.loader
mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp)


class McpAdapterTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.old_run = mcp.subprocess.run
        self.old_bash = mcp._bash
        mcp._bash = lambda: "bash"

        def fake_run(argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return types.SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

        mcp.subprocess.run = fake_run

    def tearDown(self):
        mcp.subprocess.run = self.old_run
        mcp._bash = self.old_bash

    def wrapper(self):
        return Path(self.calls[-1][0][1]).name

    def test_all_declared_tools_dispatch_to_existing_wrappers(self):
        cases = [
            ("delegate", {"prompt": "p", "tier": "flash", "yolo": True}, "agy-delegate.sh"),
            ("scout", {"question": "q"}, "agy-scout.sh"),
            ("review", {"goal": "g", "scope": "staged"}, "agy-review.sh"),
            ("research", {"query": "q"}, "agy-delegate.sh"),
            ("media", {"file": "x.png"}, "agy-media.sh"),
            ("job", {"action": "list"}, "agy-job.sh"),
            ("quota", {"action": "check"}, "agy-quota.py"),
            ("trace", {"action": "last"}, "agy-trace.sh"),
            ("doctor", {}, "doctor.sh"),
            ("migrate", {"arguments": ["--help"]}, "agy-migrate.py"),
            ("cloud_debug", {"service": "svc", "print_command": True}, "cloud-debug.sh"),
            ("cost", {"prompt": "p"}, "agy-cost-compare.sh"),
        ]
        self.assertEqual([tool["name"] for tool in mcp.TOOLS], [case[0] for case in cases])
        for name, args, expected in cases:
            with self.subTest(name=name):
                mcp._dispatch(name, args)
                self.assertEqual(self.wrapper(), expected)

    def test_packaged_timeout_policy_allows_long_codex_and_claude_calls(self):
        # The two hosts read different files and resolve paths differently, so the
        # script path is host-specific. Both keep cwd "." so the server still gets the
        # caller's workspace as the fallback --dir for delegation.
        claude = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        server = claude["mcpServers"]["antigravity"]
        # Claude Code resolves .mcp.json paths against the session cwd, not the
        # plugin, so a "./..." arg makes the server fail to start in every session.
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(
            server["args"], ["${CLAUDE_PLUGIN_ROOT}/codex/mcp_server.py"]
        )
        for arg in server["args"]:
            self.assertFalse(arg.startswith("./") or arg.startswith("../"), arg)
        self.assertGreaterEqual(server["tool_timeout_sec"], 2100)

        # Codex resolves cwd "." to the plugin root and runs relative args against it,
        # but it does NOT substitute ${CLAUDE_PLUGIN_ROOT} and does not export it, so
        # the Claude form would reach python as a literal and never start.
        codex_manifest = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(codex_manifest["mcpServers"], "./codex/.mcp.json")
        codex_path = ROOT / "codex" / ".mcp.json"
        codex_raw = codex_path.read_text(encoding="utf-8")
        self.assertNotIn("CLAUDE_", codex_raw)
        codex_server = json.loads(codex_raw)["mcpServers"]["antigravity"]
        self.assertEqual(codex_server["cwd"], ".")
        self.assertEqual(codex_server["args"], ["./codex/mcp_server.py"])
        self.assertGreaterEqual(codex_server["tool_timeout_sec"], 2100)

        # Both hosts must end up launching the same script with the same limits.
        self.assertEqual(
            Path(server["args"][0]).name, Path(codex_server["args"][0]).name
        )
        self.assertEqual(server["command"], codex_server["command"])
        self.assertEqual(
            server["startup_timeout_sec"], codex_server["startup_timeout_sec"]
        )
        self.assertTrue((ROOT / "codex" / "mcp_server.py").is_file())

        claude_manifest = json.loads(
            (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(claude_manifest["userConfig"]["timeout"]["default"], "30m")
        for script, expected in (
            ("agy-delegate.sh", 'CLAUDE_PLUGIN_OPTION_TIMEOUT:-30m'),
            ("agy-delegate.sh", 'n=1800; unit=s'),
            ("agy-scout.sh", 'TIMEOUT="30m"'),
            ("agy-review.sh", 'TIMEOUT="30m"'),
            ("agy-media.sh", 'TIMEOUT="30m"'),
        ):
            with self.subTest(script=script):
                text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_mcp_responses_are_written_as_utf8_bytes_on_windows(self):
        class Cp1252Stdout:
            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, value):
                value.encode("cp1252")

            def flush(self):
                pass

        old_stdout = sys.stdout
        fake_stdout = Cp1252Stdout()
        try:
            sys.stdout = fake_stdout
            mcp._send({"status": "✓"})
        finally:
            sys.stdout = old_stdout
        self.assertEqual(json.loads(fake_stdout.buffer.getvalue()), {"status": "✓"})

    def test_delegate_forwards_model_permissions_timeouts_and_dirs(self):
        mcp._dispatch(
            "delegate",
            {
                "prompt": "do it",
                "directory": str(ROOT),
                "add_dirs": [str(ROOT / "tests")],
                "tier": "flash-medium",
                "model": "Exact Model",
                "timeout": "7m",
                "idle_timeout": 430,
                "yolo": True,
                "sandbox": True,
                "digest": True,
                "mode": "accept-edits",
                "conversation": "abc",
            },
        )
        argv = self.calls[-1][0]
        for expected in (
            "--tier", "flash-medium", "--model", "Exact Model", "--timeout", "7m",
            "--idle-timeout", "430", "--yolo", "--sandbox", "--digest",
            "--mode", "accept-edits", "--conversation", "abc", "-",
        ):
            self.assertIn(expected, argv)
        self.assertEqual(argv.count("--dir"), 2)
        self.assertEqual(self.calls[-1][1]["input"], "do it")

    def test_authored_unicode_prompts_use_utf8_stdin_not_windows_argv(self):
        cases = (
            ("delegate", {"prompt": "Türkçe 🏰 görev"}, "-"),
            ("scout", {"question": "Dosyayı çözümle 🏰"}, "-"),
            ("review", {"goal": "değişikliği incele 🏰"}, "--goal-stdin"),
            ("research", {"query": "güncel araştırma 🏰"}, "-"),
            ("media", {"file": "fixture.png", "focus": "görseli incele 🏰"}, "--focus-stdin"),
            ("job", {"action": "start", "prompt": "arka plan görevi 🏰"}, "-"),
            ("cost", {"prompt": "maliyet ölçümü 🏰"}, "-"),
        )
        for name, args, marker in cases:
            with self.subTest(name=name):
                mcp._dispatch(name, args)
                argv, kwargs = self.calls[-1]
                self.assertIn(marker, argv)
                self.assertIn("🏰", kwargs.get("input", ""))
                self.assertNotIn("🏰", " ".join(argv))

    def test_only_medium_high_and_pro_tiers_are_exposed(self):
        self.assertEqual(mcp.TIER["enum"], ["flash-medium", "flash", "pro"])
        scout = next(tool for tool in mcp.TOOLS if tool["name"] == "scout")
        self.assertEqual(
            scout["inputSchema"]["properties"]["tier"]["enum"],
            ["flash-medium", "flash"],
        )
        self.assertNotIn("flash-lo", json.dumps(mcp.TOOLS))

    def test_scout_forwards_an_explicit_effort_tier(self):
        mcp._dispatch("scout", {"question": "q", "tier": "flash"})
        self.assertIn("--tier", self.calls[-1][0])
        self.assertIn("flash", self.calls[-1][0])

    def test_quota_actions_and_cancel_all_are_exactly_mapped(self):
        mcp._dispatch("quota", {"action": "check", "force": True})
        self.assertEqual(self.calls[-1][0][-2:], ["--json", "--force"])
        mcp._dispatch("quota", {"action": "choose_sonnet"})
        self.assertEqual(self.calls[-1][0][-3:], ["--decision", "sonnet", "--json"])
        mcp._dispatch("quota", {"action": "choose_wait"})
        self.assertEqual(self.calls[-1][0][-3:], ["--decision", "wait", "--json"])
        mcp._dispatch("quota", {"action": "clear"})
        self.assertEqual(self.calls[-1][0][-3:], ["--decision", "clear", "--json"])
        mcp._dispatch("job", {"action": "cancel_all"})
        self.assertEqual(self.calls[-1][0][-1], "cancel-all")

    def test_server_version_matches_manifests(self):
        response = mcp.handle_request({"id": 1, "method": "initialize", "params": {}})
        self.assertEqual(response["result"]["serverInfo"]["version"], "0.31.40")
        negotiated = mcp.handle_request({
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        })
        self.assertEqual(negotiated["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            data = json.loads((ROOT / manifest).read_text(encoding="utf-8"))
            self.assertEqual(data["version"], "0.31.40")

    def test_exit_code_stdout_and_stderr_are_preserved(self):
        def failed(argv, **kwargs):
            return types.SimpleNamespace(returncode=12, stdout="partial", stderr="TIMEOUT")

        mcp.subprocess.run = failed
        response = mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "delegate", "arguments": {"prompt": "p"}},
            }
        )
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["exit_code"], 12)
        self.assertEqual(result["structuredContent"]["stdout"], "partial")
        self.assertEqual(result["structuredContent"]["stderr"], "TIMEOUT")
        self.assertEqual(json.loads(result["content"][0]["text"])["exit_code"], 12)

    def test_range_review_requires_a_range(self):
        with self.assertRaisesRegex(ValueError, "requires range"):
            mcp._dispatch("review", {"goal": "g", "scope": "range"})


if __name__ == "__main__":
    unittest.main()
