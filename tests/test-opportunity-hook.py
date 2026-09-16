#!/usr/bin/env python3
"""Claude and Codex payload tests for Agy routing enforcement and advisory reminder hook."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "agy_opportunity_reminder.py"


class OpportunityHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = os.environ.copy()
        self.env["AGY_QUOTA_STATE_DIR"] = self.temp.name
        self.env["AGY_ROUTING_STATE_DIR"] = self.temp.name
        self.env["POLYPHONY_UPDATE_CHECK"] = "off"

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, payload: dict) -> str:
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            env=self.env,
            input=json.dumps(payload),
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def quota_state(self, decision: str | None = None) -> None:
        Path(self.temp.name, "state.json").write_text(
            json.dumps({
                "depleted": True,
                "decision": decision,
                "windows": {
                    "5h": {"remaining": 1.5},
                    "7d": {"remaining": 40},
                },
            }),
            encoding="utf-8",
        )

    def set_mode(self, session_id: str, choice: str) -> str:
        return self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "prompt": choice,
        })

    # --- 1. SessionStart / Pending Default ---

    def test_session_start_initializes_pending_strict_default(self):
        session = str(uuid.uuid4())
        out = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session,
            "matcher": "startup",
        })
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Always use Agy (strict)", ctx)
        self.assertIn("Use Agy when appropriate (soft)", ctx)

        # Confirm compact does not wipe state.
        self.set_mode(session, "Use Agy when appropriate (soft)")
        compact_out = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session,
            "matcher": "compact",
        })
        self.assertEqual(compact_out, "")
        # Tool call should now be in soft mode (advisory, not denied)
        tool_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        tool_data = json.loads(tool_out)
        self.assertNotIn("permissionDecision", tool_data["hookSpecificOutput"])

    # --- 2. Strict Default Before Answer & Stop Enforcement ---

    def test_strict_default_denies_substantive_tools_before_answer(self):
        session = str(uuid.uuid4())
        # Without answering mode, calling Read should be denied
        output = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        data = json.loads(output)
        hook = data["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertEqual(hook.get("permissionDecision"), "deny")
        self.assertIn("Always use Agy (strict)", hook["permissionDecisionReason"])

    def test_stop_enforces_exact_mode_question_when_pending(self):
        session = str(uuid.uuid4())

        # 1. Stop without presenting choices should block
        blocked = self.invoke({
            "hook_event_name": "Stop",
            "session_id": session,
            "last_assistant_message": "I'm ready to help! What would you like to do?",
        })
        data = json.loads(blocked)
        self.assertEqual(data.get("decision"), "block")
        self.assertIn("Always use Agy (strict)", data.get("reason", ""))
        self.assertIn("Use Agy when appropriate (soft)", data.get("reason", ""))

        # 2. Stop with clearly presented canonical choices should allow
        allowed = self.invoke({
            "hook_event_name": "Stop",
            "session_id": session,
            "last_assistant_message": (
                "Before we begin, please select an Agy routing mode:\n"
                "- Always use Agy (strict)\n"
                "- Use Agy when appropriate (soft)"
            ),
        })
        self.assertEqual(allowed, "")

    def test_explicit_mode_survives_session_end_and_resume_without_reasking(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")
        self.invoke({"hook_event_name": "SessionEnd", "session_id": session})
        resumed = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session,
            "matcher": "resume",
        })
        self.assertNotIn("select", resumed.lower())
        denied = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
        })
        self.assertEqual(
            json.loads(denied)["hookSpecificOutput"].get("permissionDecision"),
            "deny",
        )

    def test_strict_allows_bounded_single_file_work_but_not_broad_work(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")
        small_file = Path(self.temp.name, "small.py")
        small_file.write_text("value = 1\n", encoding="utf-8")

        bounded_calls = [
            ("Read", {"file_path": str(small_file)}),
            ("grep", {"path": str(small_file), "pattern": "value", "head_limit": 10}),
            ("Edit", {
                "file_path": str(small_file),
                "old_string": "value = 1",
                "new_string": "value = 2",
            }),
        ]
        for tool_name, tool_input in bounded_calls:
            with self.subTest(tool=tool_name):
                self.assertEqual(self.invoke({
                    "hook_event_name": "PreToolUse",
                    "session_id": session,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                }), "")

        # Cheap host-side probes are orchestration, not substantive repository
        # work; strict routing must not force them through a worker.
        self.assertEqual(self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {
                "cmd": 'python -c "import sys;print(len(sys.argv[1]))" "aaa bbb"',
            },
        }), "")

        broad = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
        })
        self.assertEqual(
            json.loads(broad)["hookSpecificOutput"].get("permissionDecision"),
            "deny",
        )

    # --- 3. Strict Choice & Strict Denial for Major Categories ---

    def test_strict_choice_and_denial_for_each_major_category(self):
        strict_synonyms = ["Always use Agy (strict)", "strict", "always", "7/24"]
        for syn in strict_synonyms:
            session = str(uuid.uuid4())
            out = self.set_mode(session, syn)
            self.assertIn("strict", out.lower())

        # A question or negation is not an answer to the initial mode choice.
        for ambiguous in ("soft?", "do not use soft"):
            pending = str(uuid.uuid4())
            ambig_ws = Path(self.temp.name, f"ambig_{pending}")
            ambig_ws.mkdir(exist_ok=True)
            self.invoke({
                "hook_event_name": "UserPromptSubmit",
                "session_id": pending,
                "prompt": ambiguous,
                "cwd": str(ambig_ws),
            })
            state_name = hashlib.sha256(pending.encode("utf-8")).hexdigest()[:24] + ".json"
            state = json.loads(Path(self.temp.name, state_name).read_text(encoding="utf-8"))
            self.assertIsNone(state.get("mode"))

        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        categories = [
            ("Read", {"file_path": "src/app.py"}, "agy-scout"),
            ("grep", {"pattern": "def "}, "agy-scout"),
            ("glob", {"pattern": "*.py"}, "agy-scout"),
            ("exec_command", {"cmd": "rg foo"}, "agy-scout"),
            ("Edit", {"file_path": "src/app.py"}, "agy-delegate"),
            ("Write", {"file_path": "src/app.py"}, "agy-delegate"),
            ("apply_patch", {"command": "*** patch"}, "agy-delegate"),
            ("exec_command", {"cmd": "git diff"}, "agy-review"),
            ("exec_command", {"cmd": "git show HEAD"}, "agy-review"),
            ("exec_command", {"cmd": "npm test"}, "agy-delegate"),
            ("exec_command", {"cmd": "pytest tests/"}, "agy-delegate"),
            ("exec_command", {"cmd": "git push origin main"}, "agy-delegate"),
            ("exec_command", {"cmd": "curl https://example.com"}, "agy-delegate"),
            ("websearch", {"query": "python"}, "agy-delegate"),
            ("view_image", {"path": "diagram.png"}, "agy-media"),
            ("agent", {"prompt": "research"}, "agy-scout"),
            ("exec_command", {"cmd": "ls -la"}, "agy-scout"),
            ("exec_command", {"cmd": "tar -czf archive.tar.gz src/"}, "agy-delegate"),
        ]

        for tool_name, tool_input, expected_wrapper in categories:
            with self.subTest(tool=tool_name, cmd=tool_input):
                output = self.invoke({
                    "hook_event_name": "PreToolUse",
                    "session_id": session,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                })
                data = json.loads(output)
                hook = data["hookSpecificOutput"]
                self.assertEqual(hook.get("permissionDecision"), "deny")
                self.assertIn(expected_wrapper, hook.get("permissionDecisionReason", ""))

        # External tools remain advisory (not hard-blocked)
        ext_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "mcp__github__create_issue",
            "tool_input": {"title": "bug"},
        })
        ext_data = json.loads(ext_out)
        self.assertNotIn("permissionDecision", ext_data["hookSpecificOutput"])

    def test_strict_allows_agy_job_with_safe_prompt_plumbing(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")
        commands = (
            "cd '/tmp/repo' && TASK=\"$(cat '/tmp/task.txt')\" && agy-job start --tier flash --dir . --yolo \"$TASK\"",
            "$task = Get-Content -Raw 'C:/tmp/task.txt'; agy-job start --tier flash --dir . --yolo $task",
        )
        for command in commands:
            with self.subTest(command=command):
                output = self.invoke({
                    "hook_event_name": "PreToolUse",
                    "session_id": session,
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": command},
                })
                data = json.loads(output)
                hook = data["hookSpecificOutput"]
                self.assertNotEqual(hook.get("permissionDecision"), "deny")
                self.assertIn("Agy command accepted", hook.get("additionalContext", ""))

    def test_ask_user_question_result_persists_mode_before_next_tool(self):
        """Claude desktop returns the routing answer as PostToolUse, not a user prompt."""
        for answer, expected in (
            ("Always use Agy (strict)", "strict"),
            ({"answers": {"routing": "Use Agy when appropriate (soft)"}}, "soft"),
            ({"answers": {"routing": 1}}, "strict"),
        ):
            with self.subTest(answer=answer):
                session = str(uuid.uuid4())
                self.invoke({
                    "hook_event_name": "SessionStart",
                    "session_id": session,
                    "matcher": "startup",
                })
                response = {"answers": {"routing": answer}} if isinstance(answer, str) else answer
                self.invoke({
                    "hook_event_name": "PostToolUse",
                    "session_id": session,
                    "tool_name": "AskUserQuestion",
                    "tool_input": {"questions": [{"question": "routing", "options": []}]},
                    "tool_response": response,
                })
                state_name = hashlib.sha256(session.encode("utf-8")).hexdigest()[:24] + ".json"
                state = json.loads(Path(self.temp.name, state_name).read_text(encoding="utf-8"))
                self.assertEqual(state.get("mode"), expected)
                self.assertTrue(state.get("user_mode_selection"))

                next_tool = self.invoke({
                    "hook_event_name": "PreToolUse",
                    "session_id": session,
                    "tool_name": "Read",
                    "tool_input": {"file_path": "src/app.py"},
                })
                hook = json.loads(next_tool)["hookSpecificOutput"]
                if expected == "strict":
                    self.assertEqual(hook.get("permissionDecision"), "deny")
                else:
                    self.assertNotIn("permissionDecision", hook)

    # --- 4. Soft Choice & Preserved Once-Per-Category Advisory ---

    def test_soft_choice_and_preserved_once_per_category_advisory(self):
        soft_synonyms = ["Use Agy when appropriate (soft)", "soft", "when appropriate", "2"]
        for syn in soft_synonyms:
            session = str(uuid.uuid4())
            out = self.set_mode(session, syn)
            self.assertIn("soft", out.lower())

        session = str(uuid.uuid4())
        self.set_mode(session, "Use Agy when appropriate (soft)")

        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "turn_id": "turn-1",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git push"},
        }
        # First call in turn-1 emits advisory
        out1 = self.invoke(payload)
        data1 = json.loads(out1)
        self.assertNotIn("permissionDecision", data1["hookSpecificOutput"])
        self.assertIn("Git", data1["hookSpecificOutput"]["additionalContext"])

        # Second call in turn-1 stays quiet
        out2 = self.invoke(payload)
        self.assertEqual(out2, "")

        # Calling in turn-2 warns again
        payload["turn_id"] = "turn-2"
        out3 = self.invoke(payload)
        self.assertTrue(out3)

        # In soft mode, Stop does not require Agy work
        stop_out = self.invoke({
            "hook_event_name": "Stop",
            "session_id": session,
            "last_assistant_message": "All done!",
        })
        self.assertEqual(stop_out, "")

    def test_oversized_agy_prompt_is_rejected_with_split_guidance(self):
        """Inline Bash prompts must not reach the Windows shell size/quoting trap."""
        session = str(uuid.uuid4())
        self.set_mode(session, "Use Agy when appropriate (soft)")
        prompt = "word " * 800
        output = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-delegate --tier flash '" + prompt + "'"},
        })
        data = json.loads(output)
        hook = data["hookSpecificOutput"]
        self.assertEqual(hook.get("permissionDecision"), "deny")
        reason = hook.get("permissionDecisionReason", "")
        self.assertIn("compact instruction budget", reason)
        self.assertIn("200–500 words", reason)
        self.assertIn("stdin do not bypass", reason)

    # --- 5. Control-Plane Exemptions ---

    def test_control_plane_exemptions(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        exempt_tools = [
            ("askuserquestion", {"question": "which file?"}),
            ("request_user_input", {"prompt": "confirm"}),
            ("Read", {"file_path": "CLAUDE.md"}),
            ("exec_command", {"cmd": "agy-quota --force"}),
            ("exec_command", {"cmd": "agy-doctor"}),
            ("exec_command", {"cmd": "agy-trace"}),
            ("exec_command", {"cmd": "agy-job list"}),
            ("exec_command", {"cmd": "agy-job status 123"}),
            ("exec_command", {"cmd": "agy-job cancel 123"}),
            ("mcp__antigravity__doctor", {}),
            ("mcp__antigravity__trace", {}),
            ("mcp__antigravity__job_list", {}),
            ("mcp__antigravity__job_status", {"id": "123"}),
            ("mcp__antigravity__job_cancel", {"id": "123"}),
            ("mcp__antigravity__quota", {"action": "check"}),
        ]

        for tool_name, tool_input in exempt_tools:
            with self.subTest(tool=tool_name, input=tool_input):
                output = self.invoke({
                    "hook_event_name": "PreToolUse",
                    "session_id": session,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                })
                self.assertEqual(output, "")

    # --- 6. Successful vs Failed/Empty/Background/Management Agy Calls ---

    def test_successful_vs_failed_empty_background_management_agy_calls(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        # 1. Successful delegate call
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-delegate --tier flash 'fix bug'"},
            "tool_response": {"exit_code": 0, "stdout": "Fixed the bug in auth.py."},
        })
        # Strict Stop should now succeed
        stop_out = self.invoke({"hook_event_name": "Stop", "session_id": session})
        self.assertEqual(stop_out, "")

        # 2. Failed delegate call (exit nonzero)
        session2 = str(uuid.uuid4())
        self.set_mode(session2, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session2,
            "prompt": "Fix auth.py",
        })
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session2,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-delegate --tier flash 'fix bug'"},
            "tool_response": {"exit_code": 1, "stdout": "", "stderr": "Quota exceeded"},
        })
        stop_blocked = self.invoke({"hook_event_name": "Stop", "session_id": session2})
        data = json.loads(stop_blocked)
        self.assertEqual(data.get("decision"), "block")
        self.assertIn("surface the failure or quota choice", data.get("reason", ""))

        # 3. Empty output call
        session3 = str(uuid.uuid4())
        self.set_mode(session3, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session3,
            "prompt": "Fix auth.py",
        })
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session3,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-delegate --tier flash 'fix bug'"},
            "tool_response": {"exit_code": 0, "stdout": "   "},
        })
        stop_blocked3 = self.invoke({"hook_event_name": "Stop", "session_id": session3})
        data3 = json.loads(stop_blocked3)
        self.assertEqual(data3.get("decision"), "block")

        # 4. Background job start (not completed work)
        session4 = str(uuid.uuid4())
        self.set_mode(session4, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session4,
            "prompt": "Fix auth.py",
        })
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session4,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-job start --tier flash 'fix bug'"},
            "tool_response": {"exit_code": 0, "stdout": "Job started: job-456"},
        })
        stop_blocked4 = self.invoke({"hook_event_name": "Stop", "session_id": session4})
        self.assertEqual(json.loads(stop_blocked4).get("decision"), "block")

        # 5. Completed job result
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session4,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-job result job-456"},
            "tool_response": {"exit_code": 0, "stdout": "Completed job result text"},
        })
        stop_allowed4 = self.invoke({"hook_event_name": "Stop", "session_id": session4})
        self.assertEqual(stop_allowed4, "")

    def test_host_background_agy_result_is_pending_until_task_output(self):
        """An async launcher acknowledgement must not become an empty-output failure."""
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "Implement the requested feature",
        })

        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {
                "command": "agy-delegate --tier flash 'implement feature'",
                "run_in_background": True,
            },
            "tool_response": {
                "exit_code": 0,
                "stdout": "",
                "status": "completed",
                "task_id": "task-123",
            },
        })

        pending_stop = self.invoke({"hook_event_name": "Stop", "session_id": session})
        pending_data = json.loads(pending_stop)
        self.assertEqual(pending_data.get("decision"), "block")
        self.assertIn("still running", pending_data.get("reason", ""))
        self.assertIn("task-123", pending_data.get("reason", ""))
        self.assertNotIn("failed (empty output)", pending_data.get("reason", ""))

        # TaskOutput is a host tool rather than an Agy tool, so the hook must
        # explicitly consume its terminal result to release the strict gate.
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session,
            "tool_name": "TaskOutput",
            "tool_input": {"task_id": "task-123"},
            "tool_response": {
                "status": "completed",
                "task_id": "task-123",
                "output": "Feature implemented successfully.",
            },
        })
        self.assertEqual(
            self.invoke({"hook_event_name": "Stop", "session_id": session}),
            "",
        )

    def test_terminal_empty_task_output_remains_a_failure(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "Implement the requested feature",
        })
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {
                "command": "agy-delegate --tier flash 'implement feature'",
                "run_in_background": True,
            },
            "tool_response": {"exit_code": 0, "stdout": "", "task_id": "task-456"},
        })
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session,
            "tool_name": "TaskOutput",
            "tool_input": {"task_id": "task-456"},
            "tool_response": {
                "status": "completed",
                "task_id": "task-456",
                "output": "",
            },
        })

        stop_out = self.invoke({"hook_event_name": "Stop", "session_id": session})
        data = json.loads(stop_out)
        self.assertEqual(data.get("decision"), "block")
        self.assertIn("empty output", data.get("reason", ""))
        self.assertNotIn("still running", data.get("reason", ""))

    # --- 7. Claude and Codex Nested Response Shapes ---

    def test_claude_and_codex_nested_response_shapes(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        # Claude format with content block list
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session,
            "tool_name": "mcp__antigravity__delegate",
            "tool_input": {"prompt": "do work"},
            "tool_response": {
                "isError": False,
                "content": [{"type": "text", "text": "Task finished."}],
            },
        })
        self.assertEqual(self.invoke({"hook_event_name": "Stop", "session_id": session}), "")

        # Codex format with structuredContent
        session_codex = str(uuid.uuid4())
        self.set_mode(session_codex, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "PostToolUse",
            "session_id": session_codex,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-scout . 'find files'"},
            "tool_response": {
                "structuredContent": {
                    "exit_code": 0,
                    "stdout": "Found 3 files.",
                }
            },
        })
        self.assertEqual(self.invoke({"hook_event_name": "Stop", "session_id": session_codex}), "")

        # PostToolUseFailure event
        session_fail = str(uuid.uuid4())
        self.set_mode(session_fail, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_fail,
            "prompt": "Fix bug",
        })
        self.invoke({
            "hook_event_name": "PostToolUseFailure",
            "session_id": session_fail,
            "tool_name": "mcp__antigravity__delegate",
            "tool_input": {"prompt": "do work"},
            "error": "Process terminated abnormally",
        })
        fail_stop = self.invoke({"hook_event_name": "Stop", "session_id": session_fail})
        self.assertEqual(json.loads(fail_stop).get("decision"), "block")

    # --- 8. Strict Stop Gate & Loop Cap ---

    def test_strict_stop_gate_and_loop_cap(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "Implement authentication",
        })

        # 1. Blocks without Agy work
        b1 = self.invoke({"hook_event_name": "Stop", "session_id": session})
        self.assertEqual(json.loads(b1).get("decision"), "block")

        # 2. Blocks second time
        b2 = self.invoke({"hook_event_name": "Stop", "session_id": session})
        self.assertEqual(json.loads(b2).get("decision"), "block")

        # 3. Blocks third time
        b3 = self.invoke({"hook_event_name": "Stop", "session_id": session})
        self.assertEqual(json.loads(b3).get("decision"), "block")

        # 4. Fourth time hits loop cap (>= 3 continuations) and allows stop
        b4 = self.invoke({"hook_event_name": "Stop", "session_id": session})
        self.assertEqual(b4, "")

        # stop_hook_active also allows stopping immediately
        session2 = str(uuid.uuid4())
        self.set_mode(session2, "Always use Agy (strict)")
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session2,
            "prompt": "Do substantive task",
        })
        active_stop = self.invoke({
            "hook_event_name": "Stop",
            "session_id": session2,
            "stop_hook_active": True,
        })
        self.assertEqual(active_stop, "")

    # --- 9. Mode Switch Wording Safety ---

    def test_mode_switch_wording_safety(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Use Agy when appropriate (soft)")

        # Quoted documentation must NOT switch mode
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": 'The user said: "switch agy mode to strict"',
        })
        # Check that it remained soft mode
        t_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertNotIn("permissionDecision", json.loads(t_out)["hookSpecificOutput"])

        # Ordinary sentence containing strict must NOT switch mode
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "We need strict input validation for the API endpoint",
        })
        t_out2 = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertNotIn("permissionDecision", json.loads(t_out2)["hookSpecificOutput"])

        # Unambiguous command DOES switch mode to strict
        sw_out = self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "switch agy mode to strict",
        })
        self.assertIn("strict", sw_out.lower())
        t_out3 = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertEqual(json.loads(t_out3)["hookSpecificOutput"].get("permissionDecision"), "deny")

    # --- 10. Independent Session & Turn State ---

    def test_independent_session_and_turn_state(self):
        sess_a = str(uuid.uuid4())
        sess_b = str(uuid.uuid4())

        self.set_mode(sess_a, "Always use Agy (strict)")
        self.set_mode(sess_b, "Use Agy when appropriate (soft)")

        # sess_a is strict
        out_a = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": sess_a,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertEqual(json.loads(out_a)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # sess_b is soft
        out_b = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": sess_b,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertNotIn("permissionDecision", json.loads(out_b)["hookSpecificOutput"])

    # --- 11. Corrupt State Recovery ---

    def test_corrupt_state_recovery(self):
        session = str(uuid.uuid4())
        # Write corrupted JSON to session state file
        safe_id = subprocess.run(
            [sys.executable, "-c", f"import hashlib; print(hashlib.sha256('{session}'.encode()).hexdigest()[:24])"],
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        corrupt_file = Path(self.temp.name, f"{safe_id}.json")
        corrupt_file.write_text("{corrupt: json content...", encoding="utf-8")

        # Hook should recover gracefully to default pending state.
        output = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        data = json.loads(output)
        self.assertEqual(data["hookSpecificOutput"].get("permissionDecision"), "deny")

    # --- 12. Quota Behavior Remains Intact ---

    def test_depleted_quota_prompts_for_an_explicit_choice_in_english(self):
        self.quota_state()
        output = self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": str(uuid.uuid4()),
            "prompt": "continue",
        })
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("kill any active Agy workers", context)
        self.assertIn("check both quotas every 10 minutes", context)
        self.assertIn("only the user's explicit choice", context)

    def test_wait_choice_forbids_kill_and_sonnet(self):
        self.quota_state("wait")
        output = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": str(uuid.uuid4()),
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git status"},
        })
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Do not kill active Agy workers", context)
        self.assertIn("every 10 minutes", context)

    def test_sonnet_choice_requests_scoped_cancellation(self):
        self.quota_state("sonnet")
        output = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": str(uuid.uuid4()),
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git status"},
        })
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("user approved", context)
        self.assertIn("agy-job cancel-all", context)

    def test_codex_mcp_choice_recording_is_not_nagged(self):
        self.quota_state()
        output = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": str(uuid.uuid4()),
            "tool_name": "mcp__antigravity__quota",
            "tool_input": {"action": "choose_sonnet"},
        })
        self.assertEqual(output, "")

    # --- 13. Regressions: Claude Bash Shapes ---

    def test_claude_bash_command_shapes(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        # Real Agy wrapper via Claude Bash {command: ...} is allowed
        allowed = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {"command": "agy-delegate --tier flash 'fix bug'"},
        })
        self.assertEqual(allowed, "")

        # Substantive native test command via Claude Bash is denied
        denied_test = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/"},
        })
        hook = json.loads(denied_test)["hookSpecificOutput"]
        self.assertEqual(hook.get("permissionDecision"), "deny")
        self.assertIn("agy-delegate", hook.get("permissionDecisionReason", ""))

        # Substantive git command via Claude bash (lowercase) is denied
        denied_git = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "bash",
            "tool_input": {"command": "git push origin main"},
        })
        hook_git = json.loads(denied_git)["hookSpecificOutput"]
        self.assertEqual(hook_git.get("permissionDecision"), "deny")
        self.assertIn("agy-delegate", hook_git.get("permissionDecisionReason", ""))

    # --- 14. Regressions: Codex Command Shapes ---

    def test_codex_command_shapes(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        # Real Agy scout via Codex exec_command {cmd: ...} is allowed
        allowed = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-scout . 'find files'"},
        })
        self.assertEqual(allowed, "")

        # Substantive test command via exec is denied
        denied_exec = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec",
            "tool_input": {"command": "npm test"},
        })
        hook = json.loads(denied_exec)["hookSpecificOutput"]
        self.assertEqual(hook.get("permissionDecision"), "deny")

        # Substantive discovery via powershell is denied
        denied_ps = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "powershell",
            "tool_input": {"command": "Get-Content src/app.py"},
        })
        hook_ps = json.loads(denied_ps)["hookSpecificOutput"]
        self.assertEqual(hook_ps.get("permissionDecision"), "deny")

    # --- 15. Regressions: Mentions vs Real Call ---

    def test_mentions_vs_real_call(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        # Read on file mentioning agy must NOT be exempt
        read_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "tests/test-agy.py"},
        })
        self.assertEqual(json.loads(read_out)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # Edit with payload mentioning agy must NOT be exempt
        edit_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/service.py", "text": "agy-delegate --tier flash"},
        })
        self.assertEqual(json.loads(edit_out)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # grep searching for agy must NOT be exempt
        grep_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "grep",
            "tool_input": {"pattern": "agy-scout"},
        })
        self.assertEqual(json.loads(grep_out)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # Shell command echoing agy is NOT an Agy call
        echo_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "echo agy-delegate"},
        })
        self.assertEqual(json.loads(echo_out)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # Shell command cat on bin/agy-media is NOT an Agy call
        cat_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "cat bin/agy-media"},
        })
        self.assertEqual(json.loads(cat_out)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # Shell command git diff on agy branch is NOT an Agy call
        git_diff_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git diff agy-feature"},
        })
        self.assertEqual(json.loads(git_diff_out)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # Generic command mentioning --decision must NOT be exempt
        decision_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "pytest --decision"},
        })
        self.assertEqual(json.loads(decision_out)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # Policy files are readable for bootstrap, but native edits are substantive.
        policy_read = self.invoke({
            "hook_event_name": "PreToolUse", "session_id": session,
            "tool_name": "Read", "tool_input": {"file_path": "CLAUDE.md"},
        })
        self.assertEqual(policy_read, "")
        policy_edit = self.invoke({
            "hook_event_name": "PreToolUse", "session_id": session,
            "tool_name": "Edit", "tool_input": {"file_path": "CLAUDE.md", "text": "bypass"},
        })
        self.assertEqual(json.loads(policy_edit)["hookSpecificOutput"].get("permissionDecision"), "deny")

    # --- 16. Regressions: Compound Shell Bypasses ---

    def test_compound_shell_bypasses(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        compound_commands = [
            "agy-scout . && rm -rf /",
            "agy-delegate 'fix' ; git status",
            "agy-quota | cat",
            "agy-scout $(whoami)",
            "agy-scout `whoami`",
            "agy-doctor > result.txt",
            "agy-delegate 'fix' < prompt.txt",
            "agy-scout <(whoami)",
            "echo hello && agy-scout .",
            "agy-scout .\nrm -rf /",
            "agy-scout C:\\\nwhoami",
        ]
        for cmd in compound_commands:
            with self.subTest(cmd=cmd):
                out = self.invoke({
                    "hook_event_name": "PreToolUse",
                    "session_id": session,
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": cmd},
                })
                self.assertEqual(json.loads(out)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # Quoted prompt text with compound characters is allowed as a pure call
        pure_quoted = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "agy-delegate --tier flash \"fix bug && test; foo | bar\""},
        })
        self.assertEqual(pure_quoted, "")

    # --- 17. Regressions: Strict/Soft Transitions ---

    def test_strict_soft_transitions(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Always use Agy (strict)")

        # In strict: Read is denied
        out1 = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertEqual(json.loads(out1)["hookSpecificOutput"].get("permissionDecision"), "deny")

        # Switch to soft
        sw1 = self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "switch mode to soft",
        })
        self.assertIn("soft", sw1.lower())

        # In soft: Read is advisory (not denied)
        out2 = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertNotIn("permissionDecision", json.loads(out2)["hookSpecificOutput"])

        # Switch back to strict
        sw2 = self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "switch to strict mode",
        })
        self.assertIn("strict", sw2.lower())

        # In strict again: Read is denied
        out3 = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertEqual(json.loads(out3)["hookSpecificOutput"].get("permissionDecision"), "deny")

    # --- 18. Regressions: Combined Mode + Work ---

    def test_combined_mode_switch_and_substantive_work(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Use Agy when appropriate (soft)")

        # Combined prompt switches mode to strict AND contains substantive work
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "switch to strict and inspect files",
        })

        # Stop must NOT auto-allow because substantive work is pending without completed Agy work
        stop_out = self.invoke({
            "hook_event_name": "Stop",
            "session_id": session,
            "last_assistant_message": "Switched to strict mode. Checking files now.",
        })
        data = json.loads(stop_out)
        self.assertEqual(data.get("decision"), "block")
        self.assertIn("Strict Agy routing is active", data.get("reason", ""))

        # Questions and negated prose must never change the routing mode.
        for prompt in ("how do I switch to soft?", "switch to soft mode?", "do not switch to soft and inspect files"):
            with self.subTest(prompt=prompt):
                fresh = str(uuid.uuid4())
                self.set_mode(fresh, "Always use Agy (strict)")
                self.invoke({"hook_event_name": "UserPromptSubmit", "session_id": fresh, "prompt": prompt})
                denied = self.invoke({
                    "hook_event_name": "PreToolUse", "session_id": fresh,
                    "tool_name": "Read", "tool_input": {"file_path": "src/app.py"},
                })
                self.assertEqual(json.loads(denied)["hookSpecificOutput"].get("permissionDecision"), "deny")

    # --- 19. Regressions: Compact Preservation ---

    def test_compact_preservation_via_source_and_trigger(self):
        session = str(uuid.uuid4())
        self.set_mode(session, "Use Agy when appropriate (soft)")

        # SessionStart with source=compact preserves soft mode
        res_source = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session,
            "source": "compact",
        })
        self.assertEqual(res_source, "")

        # Still soft mode (Read is allowed)
        tool_out1 = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertNotIn("permissionDecision", json.loads(tool_out1)["hookSpecificOutput"])

        # SessionStart with trigger=compact preserves soft mode
        res_trigger = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session,
            "trigger": "compact",
        })
        self.assertEqual(res_trigger, "")

        # Still soft mode in a new turn
        tool_out2 = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "turn_id": "turn-2",
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        self.assertNotIn("permissionDecision", json.loads(tool_out2)["hookSpecificOutput"])

    # --- 20. Regressions: Quota-Active Strict Denial ---

    def test_quota_active_strict_denial(self):
        session = str(uuid.uuid4())
        self.quota_state()
        self.set_mode(session, "Always use Agy (strict)")

        # Quota depleted must not bypass PreToolUse denial for substantive native tools
        out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        })
        hook = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hook.get("permissionDecision"), "deny")
        # Both denial instruction and quota choice context must be present
        self.assertIn("Strict mode", hook.get("permissionDecisionReason", ""))
        self.assertIn("Agy Gemini quota is depleted", hook.get("additionalContext", ""))

        # Shell native test command also denied with quota context
        out_cmd = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "exec_command",
            "tool_input": {"cmd": "pytest tests/"},
        })
        hook_cmd = json.loads(out_cmd)["hookSpecificOutput"]
        self.assertEqual(hook_cmd.get("permissionDecision"), "deny")
        self.assertIn("Strict mode", hook_cmd.get("permissionDecisionReason", ""))

    # --- 21. Regressions: Nested Response Failures ---

    def test_nested_response_failures(self):
        failure_payloads = [
            {"structuredContent": {"isError": True, "output": "Failed"}},
            {"status": "running", "stdout": "Job is running"},
            {"status": "pending", "stdout": "Job queued"},
            {"status": "async", "stdout": "Async execution started"},
            {"status": "timeout", "stdout": "Execution timed out"},
            {"status": "cancelled", "stdout": "Job was cancelled"},
            {"exit_code": 0, "stdout": "usage: agy-delegate [OPTIONS]"},
            {"exit_code": 0, "stdout": "   ", "stderr": "error: connection reset"},
            {"usage": {"input_tokens": 50, "output_tokens": 0}},
            {"content": [{"type": "error", "text": "Remote process crashed"}]},
            {"exit_code": "-1", "stdout": "apparently complete"},
            "Traceback (most recent call last):\nRuntimeError: bridge failed",
            "AGY_USAGE {\"status\":\"SUCCESS\"}",
            "agy-delegate: agy returned empty output",
            {"status": False, "stdout": "apparently complete"},
            {"status": True, "stdout": "apparently complete"},
            {"exit_code": False, "stdout": "apparently complete"},
        ]

        for resp in failure_payloads:
            with self.subTest(resp=resp):
                sess = str(uuid.uuid4())
                self.set_mode(sess, "Always use Agy (strict)")
                self.invoke({
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": sess,
                    "prompt": "Fix auth issue",
                })
                self.invoke({
                    "hook_event_name": "PostToolUse",
                    "session_id": sess,
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "agy-delegate --tier flash 'fix auth'"},
                    "tool_response": resp,
                })
                stop_out = self.invoke({"hook_event_name": "Stop", "session_id": sess})
                data = json.loads(stop_out)
                self.assertEqual(data.get("decision"), "block")

    # --- 13. Distinct Session IDs, Restart-like State & Workspace Persistence ---

    def test_distinct_session_ids_reuse_persisted_strict_mode(self):
        session1 = str(uuid.uuid4())
        self.set_mode(session1, "Always use Agy (strict)")

        session2 = str(uuid.uuid4())
        start_out = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session2,
            "matcher": "startup",
        })
        data = json.loads(start_out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("select", ctx.lower())
        self.assertIn("Strict routing is active", ctx)

        tool_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session2,
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
        })
        tool_data = json.loads(tool_out)
        self.assertEqual(tool_data["hookSpecificOutput"].get("permissionDecision"), "deny")

    def test_distinct_session_ids_reuse_persisted_soft_mode(self):
        session1 = str(uuid.uuid4())
        self.set_mode(session1, "Use Agy when appropriate (soft)")

        session2 = str(uuid.uuid4())
        start_out = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session2,
            "matcher": "startup",
        })
        data = json.loads(start_out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("select", ctx.lower())
        self.assertIn("Soft routing is active", ctx)

        tool_out = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session2,
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
        })
        tool_data = json.loads(tool_out)
        self.assertNotIn("permissionDecision", tool_data["hookSpecificOutput"])

    def test_restart_missing_ephemeral_state_preserves_strict_and_soft(self):
        session_strict = str(uuid.uuid4())
        self.set_mode(session_strict, "Always use Agy (strict)")

        safe_strict = hashlib.sha256(session_strict.encode("utf-8")).hexdigest()[:24] + ".json"
        ephemeral_strict = Path(self.temp.name, safe_strict)
        if ephemeral_strict.exists():
            ephemeral_strict.unlink()

        resumed_strict = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session_strict,
            "matcher": "startup",
        })
        self.assertNotIn("select", resumed_strict.lower())
        denied = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session_strict,
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
        })
        self.assertEqual(json.loads(denied)["hookSpecificOutput"].get("permissionDecision"), "deny")

        self.set_mode(session_strict, "Use Agy when appropriate (soft)")
        if ephemeral_strict.exists():
            ephemeral_strict.unlink()

        resumed_soft = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session_strict,
            "matcher": "startup",
        })
        self.assertNotIn("select", resumed_soft.lower())
        allowed = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session_strict,
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
        })
        self.assertNotIn("permissionDecision", json.loads(allowed)["hookSpecificOutput"])

    def test_unrelated_workspaces_do_not_bleed(self):
        ws_a = Path(self.temp.name, "workspace_a")
        ws_b = Path(self.temp.name, "workspace_b")
        ws_a.mkdir()
        ws_b.mkdir()

        session_a = str(uuid.uuid4())
        self.invoke({
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_a,
            "prompt": "Always use Agy (strict)",
            "cwd": str(ws_a),
        })

        session_b = str(uuid.uuid4())
        start_b = self.invoke({
            "hook_event_name": "SessionStart",
            "session_id": session_b,
            "matcher": "startup",
            "cwd": str(ws_b),
        })
        data_b = json.loads(start_b)
        ctx_b = data_b["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Always use Agy (strict)", ctx_b)
        self.assertIn("Use Agy when appropriate (soft)", ctx_b)

        tool_b = self.invoke({
            "hook_event_name": "PreToolUse",
            "session_id": session_b,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/main.py"},
            "cwd": str(ws_b),
        })
        hook_b = json.loads(tool_b)["hookSpecificOutput"]
        self.assertEqual(hook_b.get("permissionDecision"), "deny")
        self.assertIn("selection is pending", hook_b.get("permissionDecisionReason", ""))

    def test_malformed_and_stale_persisted_workspace_state_recovers_and_prompts(self):
        ws = Path.cwd().resolve()
        norm = os.path.normcase(str(ws))
        ws_safe_id = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]
        ws_file = Path(self.temp.name, f"ws-{ws_safe_id}.json")

        for bad_content in ("{not valid json", json.dumps({"mode": "invalid_mode"})):
            with self.subTest(bad_content=bad_content):
                ws_file.write_text(bad_content, encoding="utf-8")
                session = str(uuid.uuid4())
                start_out = self.invoke({
                    "hook_event_name": "SessionStart",
                    "session_id": session,
                    "matcher": "startup",
                })
                data = json.loads(start_out)
                ctx = data["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Always use Agy (strict)", ctx)
                self.assertIn("Use Agy when appropriate (soft)", ctx)


class HookManifestPortabilityTests(unittest.TestCase):
    @classmethod
    def _find_bash(cls) -> str | None:
        which_bash = shutil.which("bash")
        if which_bash:
            return which_bash

        candidates: list[Path] = []
        for env_key in ("GIT_INSTALL_ROOT", "GIT_HOME", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            env_val = os.environ.get(env_key)
            if env_val:
                p = Path(env_val)
                candidates.extend([
                    p / "bin" / "bash.exe",
                    p / "usr" / "bin" / "bash.exe",
                    p / "Git" / "bin" / "bash.exe",
                    p / "Git" / "usr" / "bin" / "bash.exe",
                    p / "Programs" / "Git" / "bin" / "bash.exe",
                    p / "Programs" / "Git" / "usr" / "bin" / "bash.exe",
                ])

        git_which = shutil.which("git")
        if git_which:
            git_p = Path(git_which).resolve()
            for parent in git_p.parents:
                candidates.extend([
                    parent / "bin" / "bash.exe",
                    parent / "usr" / "bin" / "bash.exe",
                ])

        system_drive = os.environ.get("SystemDrive", "C:")
        for drive in (system_drive, "C:", "D:"):
            candidates.extend([
                Path(f"{drive}\\Program Files\\Git\\bin\\bash.exe"),
                Path(f"{drive}\\Program Files\\Git\\usr\\bin\\bash.exe"),
                Path(f"{drive}\\Program Files (x86)\\Git\\bin\\bash.exe"),
                Path(f"{drive}\\Program Files (x86)\\Git\\usr\\bin\\bash.exe"),
            ])

        candidates.extend([
            Path("/bin/bash"),
            Path("/usr/bin/bash"),
            Path("/usr/local/bin/bash"),
        ])

        seen: set[Path] = set()
        for cand in candidates:
            try:
                cand_resolved = cand.resolve()
            except Exception:
                cand_resolved = cand
            if cand_resolved in seen:
                continue
            seen.add(cand_resolved)
            try:
                if cand.is_file() and os.access(cand, os.X_OK):
                    return str(cand)
            except OSError:
                continue

        return None

    def setUp(self):
        self.claude_manifest = json.loads((ROOT / "claude" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.codex_manifest = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.bash_bin = self._find_bash()

    def test_session_start_compact_exclusion_and_fork_inclusion(self):
        claude_starts = {
            entry.get("matcher"): [h.get("command") for h in entry.get("hooks", [])]
            for entry in self.claude_manifest["hooks"]["SessionStart"]
        }
        # Claude: compact must only run policy injection, not the state Python hook
        self.assertIn("compact", claude_starts)
        compact_cmds = claude_starts["compact"]
        self.assertTrue(any("inject-policy.sh" in cmd for cmd in compact_cmds))
        self.assertFalse(any("run-opportunity-hook.sh" in cmd or "agy_opportunity_reminder" in cmd for cmd in compact_cmds))

        # Claude: fork must run the state Python hook
        self.assertIn("fork", claude_starts)
        fork_cmds = claude_starts["fork"]
        self.assertTrue(any("run-opportunity-hook.sh" in cmd for cmd in fork_cmds))

        # Codex: SessionStart matcher must be startup|resume|clear (compact and fork excluded)
        codex_starts = self.codex_manifest["hooks"]["SessionStart"]
        self.assertEqual(len(codex_starts), 1)
        codex_matcher = codex_starts[0].get("matcher")
        self.assertEqual(codex_matcher, "startup|resume|clear")
        matchers = codex_matcher.split("|")
        self.assertNotIn("compact", matchers)
        self.assertNotIn("fork", matchers)

    def test_manifest_interpreter_commands_and_launcher_portability(self):
        # Claude: all python hook invocations must use the portable launcher
        for event_name, entries in self.claude_manifest["hooks"].items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    if "agy_opportunity_reminder" in cmd or "run-opportunity-hook" in cmd:
                        self.assertIn("run-opportunity-hook.sh", cmd)
                        self.assertFalse(cmd.startswith("python "))

        # Codex: command uses python3, commandWindows uses python
        for event_name, entries in self.codex_manifest["hooks"].items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    cmd_win = h.get("commandWindows", "")
                    self.assertTrue(cmd.startswith("python3 "), f"Expected python3 in {event_name}: {cmd}")
                    self.assertTrue(cmd_win.startswith("python "), f"Expected python in {event_name}: {cmd_win}")

        # Launcher file must exist, be executable, and resolve python interpreters in order
        launcher = ROOT / "hooks" / "run-opportunity-hook.sh"
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))
        content = launcher.read_text(encoding="utf-8")
        pos_bridge = content.find('exec "$AGY_BRIDGE_PYTHON"')
        pos_py3 = content.find("exec python3 ")
        pos_py_3 = content.find("exec py -3 ")
        pos_py = content.find("exec python ")
        self.assertTrue(pos_bridge != -1 and pos_py3 != -1 and pos_py_3 != -1 and pos_py != -1)
        self.assertTrue(pos_bridge < pos_py3 < pos_py_3 < pos_py)
        self.assertIn("exec", content)

        # Test launcher executes agy_opportunity_reminder.py correctly when invoked via bash
        if not self.bash_bin:
            self.skipTest("No compatible shell exists; skipping shell-execution assertion")
        bash_bin = self.bash_bin
        env = os.environ.copy()
        env["AGY_BRIDGE_PYTHON"] = sys.executable
        res = subprocess.run(
            [bash_bin, str(launcher)],
            env=env,
            input=json.dumps({"hook_event_name": "SessionStart", "session_id": "test-launcher-session", "source": "compact"}),
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

    def test_nudge_delegation_silence_on_missing_or_unusable_session(self):
        if not self.bash_bin:
            self.skipTest("No compatible shell exists; skipping shell-execution assertion")
        nudge_script = ROOT / "hooks" / "nudge-delegation.sh"
        bash_bin = self.bash_bin
        unusable_payloads = [
            {},
            {"prompt": "migrate across the entire codebase"},
            {"session_id": "", "prompt": "migrate across the entire codebase"},
            {"session_id": "   ", "prompt": "migrate across the entire codebase"},
            {"session_id": None, "prompt": "migrate across the entire codebase"},
        ]
        for payload in unusable_payloads:
            with self.subTest(payload=payload):
                completed = subprocess.run(
                    [bash_bin, str(nudge_script)],
                    input=json.dumps(payload),
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout.strip(), "")

    def test_nudge_delegation_preserves_writable_roots(self):
        if not self.bash_bin:
            self.skipTest("No compatible shell exists; skipping shell-execution assertion")
        nudge_script = ROOT / "hooks" / "nudge-delegation.sh"
        bash_bin = self.bash_bin
        with tempfile.TemporaryDirectory() as temp_dir:
            for env_var in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
                state_dir = Path(temp_dir) / env_var / "agy-routing"
                state_dir.mkdir(parents=True, exist_ok=True)
                sess = f"sess-{env_var}"
                h = hashlib.sha256(sess.encode("utf-8")).hexdigest()[:24]
                (state_dir / f"{h}.json").write_text(json.dumps({"mode": "strict"}), encoding="utf-8")

                env = os.environ.copy()
                env.pop("AGY_ROUTING_STATE_DIR", None)
                env[env_var] = str(Path(temp_dir) / env_var)

                # In strict mode stored in env_var/agy-routing, nudge must remain silent
                completed = subprocess.run(
                    [bash_bin, str(nudge_script)],
                    env=env,
                    input=json.dumps({"session_id": sess, "prompt": "migrate across the entire codebase"}),
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout.strip(), "")

    def test_nudge_treats_missing_legacy_state_as_default_soft(self):
        if not self.bash_bin:
            self.skipTest("No compatible shell exists; skipping shell-execution assertion")
        nudge_script = ROOT / "hooks" / "nudge-delegation.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["AGY_ROUTING_STATE_DIR"] = temp_dir
            env["AGY_BRIDGE_PYTHON"] = sys.executable
            completed = subprocess.run(
                [self.bash_bin, str(nudge_script)],
                env=env,
                input=json.dumps({
                    "session_id": "missing-state-default-soft",
                    "prompt": "migrate across the entire codebase",
                }),
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("THE JUDGMENT IS YOURS", completed.stdout)

    def test_nudge_respects_persisted_workspace_strict_mode_without_ephemeral_file(self):
        if not self.bash_bin:
            self.skipTest("No compatible shell exists; skipping shell-execution assertion")
        nudge_script = ROOT / "hooks" / "nudge-delegation.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path.cwd().resolve()
            norm = os.path.normcase(str(ws))
            ws_id = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]
            Path(temp_dir, f"ws-{ws_id}.json").write_text(
                json.dumps({"mode": "strict"}),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["AGY_ROUTING_STATE_DIR"] = temp_dir
            env["AGY_BRIDGE_PYTHON"] = sys.executable
            completed = subprocess.run(
                [self.bash_bin, str(nudge_script)],
                env=env,
                input=json.dumps({
                    "session_id": "new-session-no-ephemeral-file",
                    "prompt": "migrate across the entire codebase",
                }),
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
