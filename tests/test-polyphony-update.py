#!/usr/bin/env python3
"""Tests for the throttled, read-only Polyphony update checker."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("polyphony_update", ROOT / "scripts" / "polyphony_update.py")
update = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(update)
HOOK_SPEC = importlib.util.spec_from_file_location("polyphony_hook", ROOT / "hooks" / "agy_opportunity_reminder.py")
hook = importlib.util.module_from_spec(HOOK_SPEC)
assert HOOK_SPEC.loader is not None
HOOK_SPEC.loader.exec_module(hook)


class UpdateCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manifest_root = Path(self.temp.name) / "plugin"
        manifest = self.manifest_root / ".claude-plugin"
        manifest.mkdir(parents=True)
        (manifest / "plugin.json").write_text(json.dumps({"version": "0.31.40"}), encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "POLYPHONY_UPDATE_STATE_FILE": str(Path(self.temp.name) / "state.json"),
            "POLYPHONY_UPDATE_CHECK_INTERVAL_SECONDS": "86400",
            # tests/run-tests.sh exports POLYPHONY_UPDATE_CHECK=off so the rest of the
            # suite never reaches the network. These tests are ABOUT the update notice
            # and mock the fetch themselves, so they must pin the switch on: inheriting
            # it made the hook return an empty context and the file pass alone but fail
            # in the suite.
            "POLYPHONY_UPDATE_CHECK": "on",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_new_release_is_reported_once_and_check_is_throttled(self):
        with mock.patch.object(update, "_latest_release", return_value=("0.31.41", "https://example.test/release")) as fetch:
            first = update.check_for_update(self.manifest_root, now=100)
            second = update.check_for_update(self.manifest_root, now=101)
        self.assertTrue(first["available"])
        self.assertTrue(first["notify"])
        self.assertFalse(second["checked"])
        self.assertTrue(second["available"])
        fetch.assert_called_once()

    def test_equal_release_clears_stale_notification(self):
        with mock.patch.object(update, "_latest_release", return_value=("0.31.40", "https://example.test/release")):
            result = update.check_for_update(self.manifest_root, now=100)
        self.assertFalse(result["available"])
        state = json.loads((Path(self.temp.name) / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["notified_version"], "")

    def test_network_failure_is_silent_status_and_persists_timestamp(self):
        with mock.patch.object(update, "_latest_release", side_effect=RuntimeError("offline")):
            result = update.check_for_update(self.manifest_root, now=100)
        self.assertFalse(result["available"])
        self.assertTrue(result["checked"])
        self.assertIn("last_checked_at", json.loads((Path(self.temp.name) / "state.json").read_text(encoding="utf-8")))

    def test_network_failure_uses_short_retry_window(self):
        with mock.patch.object(update, "_latest_release", side_effect=RuntimeError("offline")):
            first = update.check_for_update(self.manifest_root, now=100)
        self.assertTrue(first["checked"])

        with mock.patch.object(update, "_latest_release") as fetch:
            throttled = update.check_for_update(self.manifest_root, now=101)
            self.assertFalse(throttled["checked"])
            fetch.assert_not_called()

        with mock.patch.object(update, "_latest_release", return_value=("0.31.41", "https://example.test/release")) as fetch:
            retried = update.check_for_update(
                self.manifest_root,
                now=100 + update.DEFAULT_FAILURE_RETRY_SECONDS,
            )
        self.assertTrue(retried["checked"])
        self.assertTrue(retried["available"])
        fetch.assert_called_once()

    def test_network_failure_retry_window_is_configurable(self):
        with mock.patch.dict(os.environ, {"POLYPHONY_UPDATE_FAILURE_RETRY_SECONDS": "30"}):
            with mock.patch.object(update, "_latest_release", side_effect=RuntimeError("offline")):
                update.check_for_update(self.manifest_root, now=100)

            with mock.patch.object(update, "_latest_release") as fetch:
                throttled = update.check_for_update(self.manifest_root, now=129)
                self.assertFalse(throttled["checked"])
                fetch.assert_not_called()

            with mock.patch.object(update, "_latest_release", return_value=("0.31.41", "https://example.test/release")):
                retried = update.check_for_update(self.manifest_root, now=130)
        self.assertTrue(retried["checked"])
        self.assertTrue(retried["available"])

    def test_hook_context_requires_approval_and_explains_reload(self):
        with mock.patch.object(hook, "check_for_update", return_value={
            "notify": True,
            "available": True,
            "current": "0.31.40",
            "latest": "0.31.41",
            "url": "https://github.com/GryAsl/Polyphony/releases/tag/v0.31.41",
        }):
            context = hook._polyphony_update_context()
        self.assertIn("Ask the user exactly one concise question", context)
        self.assertIn("Do not update without explicit approval", context)
        self.assertIn("claude plugin update antigravity@polyphony -y", context)
        self.assertIn("codex plugin marketplace upgrade polyphony", context)
        self.assertIn("new Codex task", context)


if __name__ == "__main__":
    unittest.main()
