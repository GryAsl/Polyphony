#!/usr/bin/env python3
import tempfile
import time
import os
import json
import subprocess
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from polyphony_runtime import Busy, Runtime, NotFound, workspace_key  # noqa: E402


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "runtime.db"
        self.workspace = Path(self.tmp.name) / "repo"
        self.workspace.mkdir()
        self.runtime = Runtime(self.db)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def claim(self, parent="main", summary="bounded task", **kwargs):
        return self.runtime.claim_task(parent, str(self.workspace), summary, **kwargs)

    def test_init_is_idempotent_and_parent_only_reuse(self):
        self.runtime.init_db()
        first = self.claim()
        self.assertIsNone(first["conversation_id"])
        self.assertTrue(self.runtime.finish_task(first["task_id"], first["lease_token"]))
        second = self.claim()
        self.assertEqual(first["agent_id"], second["agent_id"])
        self.assertEqual(first["agent_id"], second["agent_id"])
        self.runtime.finish_task(second["task_id"], second["lease_token"])
        with self.assertRaises(NotFound):
            self.claim("different-parent", agent_id=first["agent_id"])

    def test_one_active_task_is_atomic_for_two_callers(self):
        first = self.claim()
        self.assertIsNotNone(first["task_id"])
        with self.assertRaises(Busy):
            self.claim(agent_id=first["agent_id"])

    def test_fresh_agent_and_workspace_isolation(self):
        first = self.claim()
        self.runtime.finish_task(first["task_id"], first["lease_token"])
        fresh = self.claim(fresh_agent=True)
        self.assertNotEqual(first["agent_id"], fresh["agent_id"])
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.runtime.finish_task(fresh["task_id"], fresh["lease_token"])
        foreign = self.runtime.claim_task("main", str(other), "foreign")
        with self.assertRaises(NotFound):
            self.runtime.set_conversation(first["agent_id"], "main", str(other), "wrong")
        self.runtime.finish_task(foreign["task_id"], foreign["lease_token"])

    def test_windows_style_and_msys_workspace_names_are_equivalent(self):
        if os.name != "nt":
            self.skipTest("MSYS path normalization is Windows-specific")
        path = str(self.workspace)
        drive, tail = path[0], path[2:].replace("\\", "/")
        self.assertEqual(workspace_key(path), workspace_key(f"/{drive.lower()}{tail}"))

    def test_heartbeat_and_stale_recovery(self):
        task = self.claim(lease_seconds=1)
        self.assertTrue(self.runtime.heartbeat(task["task_id"], task["lease_token"], 10))
        self.assertTrue(self.runtime.finish_task(task["task_id"], task["lease_token"]))
        stale = self.claim(lease_seconds=1)
        self.runtime.conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() - 1, stale["task_id"]))
        self.assertEqual(1, self.runtime.recover_stale())
        replacement = self.claim()
        self.assertEqual(stale["agent_id"], replacement["agent_id"])

    def test_messages_wait_and_timeout(self):
        sender = self.claim("parent-a")
        self.runtime.finish_task(sender["task_id"], sender["lease_token"])
        recipient = self.claim("parent-a", fresh_agent=True)
        self.runtime.finish_task(recipient["task_id"], recipient["lease_token"])
        self.runtime.send(str(self.workspace), sender["agent_id"], recipient["agent_id"], "hello", task_id=sender["task_id"], message_type="request")
        received = self.runtime.wait(str(self.workspace), recipient["agent_id"], timeout=0)
        self.assertEqual("hello", received[0]["content"])
        self.assertEqual([], self.runtime.wait(str(self.workspace), recipient["agent_id"], timeout=0))
        started = time.monotonic()
        self.assertEqual([], self.runtime.wait(str(self.workspace), recipient["agent_id"], timeout=0.05))
        self.assertLess(time.monotonic() - started, 1)

        other_sender = self.claim("parent-a", fresh_agent=True)
        self.runtime.finish_task(other_sender["task_id"], other_sender["lease_token"])
        self.runtime.send(str(self.workspace), other_sender["agent_id"], recipient["agent_id"], "other", task_id=other_sender["task_id"])
        self.runtime.send(str(self.workspace), sender["agent_id"], recipient["agent_id"], "wanted", task_id=sender["task_id"])
        filtered = self.runtime.wait(str(self.workspace), recipient["agent_id"], from_agent=sender["agent_id"], timeout=0)
        self.assertEqual(["wanted"], [message["content"] for message in filtered])

    def test_hop_and_fanout_limits(self):
        root = self.claim()
        child = self.claim(parent_task_id=root["task_id"], fresh_agent=True, max_hops=1)
        with self.assertRaises(Busy):
            self.claim(parent_task_id=child["task_id"], fresh_agent=True, max_hops=1)
        self.runtime.finish_task(child["task_id"], child["lease_token"])
        self.runtime.finish_task(root["task_id"], root["lease_token"])

    def test_resume_failure_rotates_generation_with_checkpoint(self):
        task = self.claim()
        self.runtime.finish_task(task["task_id"], task["lease_token"])
        self.runtime.set_conversation(task["agent_id"], "main", str(self.workspace), "conv-1")
        rotated = self.runtime.resume_fallback(task["agent_id"], "main", str(self.workspace), "facts: use UTF-8")
        self.assertEqual(2, rotated["conversation_generation"])
        self.assertIsNone(rotated["conversation_id"])
        self.assertEqual("facts: use UTF-8", rotated["checkpoint"])

    def test_runtime_cli_emits_json(self):
        script = ROOT / "scripts" / "polyphony_runtime.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--db", str(self.db), "init"],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(1, payload["schema_version"])

    def test_handoff_moves_lease_only_to_idle_sibling(self):
        source = self.claim()
        self.runtime.finish_task(source["task_id"], source["lease_token"])
        target = self.claim(fresh_agent=True)
        self.runtime.finish_task(target["task_id"], target["lease_token"])
        live = self.claim(agent_id=source["agent_id"])
        with self.assertRaises(Busy):
            self.runtime.handoff_task(live["task_id"], source["agent_id"], target["agent_id"], "wrong-token")
        moved = self.runtime.handoff_task(live["task_id"], source["agent_id"], target["agent_id"], live["lease_token"])
        self.assertEqual(target["agent_id"], moved["to_agent_id"])
        with self.assertRaises(Busy):
            self.runtime.handoff_task(live["task_id"], target["agent_id"], source["agent_id"], moved["lease_token"], max_hops=0)
        self.assertTrue(self.runtime.finish_task(live["task_id"], moved["lease_token"]))

    def test_regression_stale_recovery_clears_conversation_and_sets_checkpoint_fallback(self):
        task1 = self.claim(summary="initial job")
        self.runtime.set_conversation(task1["agent_id"], "main", str(self.workspace), "conv-active-1")
        self.assertTrue(self.runtime.finish_task(task1["task_id"], task1["lease_token"]))

        task2 = self.claim(agent_id=task1["agent_id"], lease_seconds=1, summary="crashed heavy calculation")
        self.runtime.conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() - 10, task2["task_id"]))
        recovered = self.runtime.recover_stale()
        self.assertEqual(1, recovered)

        agent_row = dict(self.runtime.conn.execute("SELECT * FROM agents WHERE id=?", (task1["agent_id"],)).fetchone())
        self.assertEqual("idle", agent_row["status"])
        self.assertIsNone(agent_row["current_task_id"])
        self.assertIsNone(agent_row["conversation_id"])
        self.assertEqual(2, agent_row["conversation_generation"])
        self.assertIn("crashed heavy calculation", agent_row["checkpoint"])

        task3 = self.claim()
        self.assertEqual(task1["agent_id"], task3["agent_id"])
        self.assertIsNone(task3["conversation_id"])
        self.assertEqual(2, task3["conversation_generation"])
        self.assertIn("crashed heavy calculation", task3["checkpoint"])
        self.assertTrue(self.runtime.finish_task(task3["task_id"], task3["lease_token"]))

    def test_regression_set_conversation_rejects_already_owned_conversation(self):
        agent1 = self.claim()
        self.runtime.finish_task(agent1["task_id"], agent1["lease_token"])
        self.runtime.set_conversation(agent1["agent_id"], "main", str(self.workspace), "conv-exclusive-123")

        # Owner re-setting its own conversation is allowed
        self.runtime.set_conversation(agent1["agent_id"], "main", str(self.workspace), "conv-exclusive-123")

        # Sibling agent under same parent/workspace cannot steal it
        agent2 = self.claim(fresh_agent=True)
        self.runtime.finish_task(agent2["task_id"], agent2["lease_token"])
        with self.assertRaises(Busy):
            self.runtime.set_conversation(agent2["agent_id"], "main", str(self.workspace), "conv-exclusive-123")

        # Agent under different parent cannot claim it
        agent3 = self.claim("other-parent")
        self.runtime.finish_task(agent3["task_id"], agent3["lease_token"])
        with self.assertRaises(Busy):
            self.runtime.set_conversation(agent3["agent_id"], "other-parent", str(self.workspace), "conv-exclusive-123")

        # Agent in another workspace cannot claim it
        other_ws = Path(self.tmp.name) / "other_repo"
        other_ws.mkdir()
        agent4 = self.runtime.claim_task("main", str(other_ws), "foreign")
        self.runtime.finish_task(agent4["task_id"], agent4["lease_token"])
        with self.assertRaises(Busy):
            self.runtime.set_conversation(agent4["agent_id"], "main", str(other_ws), "conv-exclusive-123")

    def test_regression_finish_task_stale_completion_does_not_clear_reassigned_agent(self):
        task1 = self.claim()
        self.runtime.conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() - 10, task1["task_id"]))
        self.runtime.recover_stale()

        task2 = self.claim(agent_id=task1["agent_id"])
        self.assertEqual(task1["agent_id"], task2["agent_id"])

        self.runtime.conn.execute(
            "UPDATE tasks SET status='running', lease_token='stale-token' WHERE id=?", (task1["task_id"],)
        )
        self.assertTrue(self.runtime.finish_task(task1["task_id"], "stale-token"))

        agent_row = dict(self.runtime.conn.execute("SELECT * FROM agents WHERE id=?", (task1["agent_id"],)).fetchone())
        self.assertEqual("active", agent_row["status"])
        self.assertEqual(task2["task_id"], agent_row["current_task_id"])
        self.assertIsNotNone(agent_row["lease_expires_at"])

        self.assertTrue(self.runtime.finish_task(task2["task_id"], task2["lease_token"]))
        agent_row_after = dict(self.runtime.conn.execute("SELECT * FROM agents WHERE id=?", (task1["agent_id"],)).fetchone())
        self.assertEqual("idle", agent_row_after["status"])
        self.assertIsNone(agent_row_after["current_task_id"])

    def test_regression_send_message_transaction_safe_and_committed(self):
        sender = self.claim()
        self.runtime.finish_task(sender["task_id"], sender["lease_token"])
        recipient = self.claim(fresh_agent=True)
        self.runtime.finish_task(recipient["task_id"], recipient["lease_token"])

        msg = self.runtime.send(str(self.workspace), sender["agent_id"], recipient["agent_id"], "safe message")
        self.assertEqual("safe message", msg["content"])

        self.runtime.conn.execute("BEGIN IMMEDIATE")
        self.runtime.conn.execute("COMMIT")

        inbox = self.runtime.inbox(str(self.workspace), recipient["agent_id"], mark_read=True)
        self.assertEqual(1, len(inbox))
        self.assertEqual(msg["id"], inbox[0]["id"])

        r2 = Runtime(self.db)
        try:
            r2_inbox = r2.inbox(str(self.workspace), recipient["agent_id"], unread_only=False)
            self.assertEqual(1, len(r2_inbox))
            self.assertEqual(msg["id"], r2_inbox[0]["id"])
            self.assertIsNotNone(r2_inbox[0]["read_at"])
        finally:
            r2.close()

    def test_regression_claim_task_validates_parent_ownership_active_status_and_rejects_dead_agent(self):
        t1 = self.runtime.claim_task("parent-alpha", str(self.workspace), "parent task")

        with self.assertRaises(NotFound):
            self.runtime.claim_task("parent-beta", str(self.workspace), "child task", parent_task_id=t1["task_id"])

        self.assertTrue(self.runtime.finish_task(t1["task_id"], t1["lease_token"], "completed"))
        with self.assertRaises(Busy):
            self.runtime.claim_task("parent-alpha", str(self.workspace), "child task", parent_task_id=t1["task_id"])

        t2 = self.claim(fresh_agent=True)
        self.runtime.finish_task(t2["task_id"], t2["lease_token"])
        self.runtime.conn.execute("UPDATE agents SET status='dead' WHERE id=?", (t2["agent_id"],))

        with self.assertRaises(Busy):
            self.claim(agent_id=t2["agent_id"])

        row = self.runtime.conn.execute("SELECT status FROM agents WHERE id=?", (t2["agent_id"],)).fetchone()
        self.assertEqual("dead", row["status"])

    def test_lightweight_contention_and_concurrency(self):
        sender = self.claim()
        self.runtime.finish_task(sender["task_id"], sender["lease_token"])
        recipient = self.claim(fresh_agent=True)
        self.runtime.finish_task(recipient["task_id"], recipient["lease_token"])

        errors = []

        def sender_loop():
            r = Runtime(self.db)
            try:
                for i in range(20):
                    r.send(str(self.workspace), sender["agent_id"], recipient["agent_id"], f"msg-{i}")
            except Exception as e:
                errors.append(e)
            finally:
                r.close()

        def reader_loop():
            r = Runtime(self.db)
            try:
                for _ in range(20):
                    r.inbox(str(self.workspace), recipient["agent_id"], mark_read=True)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)
            finally:
                r.close()

        def claim_loop():
            r = Runtime(self.db)
            try:
                for _ in range(10):
                    t = r.claim_task("main", str(self.workspace), "concurrent job", fresh_agent=True)
                    r.heartbeat(t["task_id"], t["lease_token"], 10)
                    r.finish_task(t["task_id"], t["lease_token"])
            except Exception as e:
                errors.append(e)
            finally:
                r.close()

        threads = [
            threading.Thread(target=sender_loop),
            threading.Thread(target=reader_loop),
            threading.Thread(target=claim_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
