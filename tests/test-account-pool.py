#!/usr/bin/env python3
"""Deterministic tests for the Polyphony local account pool manager.

Targets exclusively:
- add / switch / fingerprint
- encryption / no plaintext
- cooldown / disabled selection
- locking
- drift
- bounded rotation
- switch blocked hook point
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agy_account.py"

SPEC = importlib.util.spec_from_file_location("agy_account", SCRIPT)
assert SPEC and SPEC.loader
agy_account = importlib.util.module_from_spec(SPEC)
sys.modules["agy_account"] = agy_account
SPEC.loader.exec_module(agy_account)


class AccountPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)
        self.backend = agy_account.FakeCredentialBackend(
            store_path=self.state_dir / ".fake_active.bin"
        )
        self.manager = agy_account.AccountManager(
            state_dir=self.state_dir,
            backend=self.backend,
        )
        self.env = os.environ.copy()
        self.env["POLYPHONY_ACCOUNTS_DIR"] = str(self.state_dir)
        self.env["POLYPHONY_CREDENTIAL_BACKEND"] = "fake"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def invoke_cli(self, *args: str, stdin_data: bytes | None = None) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(SCRIPT), "--state-dir", str(self.state_dir), "--backend", "fake", *args]
        return subprocess.run(
            cmd,
            input=stdin_data.decode("utf-8") if stdin_data else None,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=self.env,
            check=False,
        )

    # -------------------------------------------------------------------------
    # 1. Add / Switch / Fingerprint
    # -------------------------------------------------------------------------

    def test_add_switch_and_fingerprint(self) -> None:
        secret_a = b"gemini_token_account_alpha_111"
        secret_b = b"gemini_token_account_beta_222"

        # 1. Add account A
        self.backend.write_active(secret_a)
        res_a = self.manager.add("alpha")
        fp_a = agy_account.compute_fingerprint(secret_a)
        self.assertEqual(res_a["status"], "ok")
        self.assertEqual(res_a["alias"], "alpha")
        self.assertEqual(res_a["fingerprint"], fp_a)
        self.assertEqual(res_a["current"], "alpha")

        # Active backend credential now reflects account A
        # 2. Add account B
        self.backend.write_active(secret_b)
        res_b = self.manager.add("beta")
        fp_b = agy_account.compute_fingerprint(secret_b)
        self.assertEqual(res_b["status"], "ok")
        self.assertEqual(res_b["alias"], "beta")
        self.assertEqual(res_b["fingerprint"], fp_b)
        self.assertNotEqual(fp_a, fp_b)

        # Add snapshots the currently logged-in account, so beta becomes current.
        curr = self.manager.current()
        self.assertEqual(curr["current"], "beta")
        self.assertTrue(curr["live_matches"])

        # 3. Switch A -> B
        self.manager.switch("alpha")
        sw = self.manager.switch("beta")
        self.assertEqual(sw["status"], "ok")
        self.assertEqual(sw["previous"], "alpha")
        self.assertEqual(sw["current"], "beta")

        # Verify active credential in backend is now secret_b
        self.assertEqual(self.backend.read_active(), secret_b)

        # Current is now beta
        curr_after = self.manager.current()
        self.assertEqual(curr_after["current"], "beta")
        self.assertTrue(curr_after["live_matches"])

        # CLI test for list --json
        cli_res = self.invoke_cli("--json", "list")
        self.assertEqual(cli_res.returncode, 0)
        list_json = json.loads(cli_res.stdout)
        self.assertEqual(len(list_json["accounts"]), 2)
        aliases = [a["alias"] for a in list_json["accounts"]]
        self.assertIn("alpha", aliases)
        self.assertIn("beta", aliases)

    def test_alias_validation(self) -> None:
        # Valid aliases
        agy_account.validate_alias("valid_alias-123")
        agy_account.validate_alias("WORK_ACCOUNT")

        # Invalid aliases must fail validation
        with self.assertRaises(agy_account.AccountManagerError):
            agy_account.validate_alias("invalid alias with spaces")
        with self.assertRaises(agy_account.AccountManagerError):
            agy_account.validate_alias("invalid@account")
        with self.assertRaises(agy_account.AccountManagerError):
            agy_account.validate_alias("")

    def test_windows_record_round_trip_preserves_metadata(self) -> None:
        packed = agy_account.WindowsCredentialBackend._pack_record(
            b"opaque-token", "user@example.test", "original comment", 3
        )
        blob, username, comment, persist = agy_account.WindowsCredentialBackend._unpack_record(packed)
        self.assertEqual(b"opaque-token", blob)
        self.assertEqual("user@example.test", username)
        self.assertEqual("original comment", comment)
        self.assertEqual(3, persist)

    # -------------------------------------------------------------------------
    # 2. Encryption / No Plaintext
    # -------------------------------------------------------------------------

    def test_encryption_and_no_plaintext_on_disk(self) -> None:
        plaintext_token = b"super_secret_high_entropy_token_xyz_9999"
        self.manager.add("secure_acc", credential_bytes=plaintext_token)

        # Verify pool.json contains NO plaintext secret
        pool_json_content = (self.state_dir / "pool.json").read_bytes()
        self.assertNotIn(plaintext_token, pool_json_content)

        # Inspect all files in state_dir recursively
        for path in self.state_dir.rglob("*"):
            if path.is_file() and path.name != ".fake_active.bin":
                file_bytes = path.read_bytes()
                self.assertNotIn(
                    plaintext_token,
                    file_bytes,
                    f"Plaintext secret found in disk file: {path}",
                )

        # Verify vault file is encrypted and decrypts accurately
        vault_file = self.state_dir / "vault" / "secure_acc.enc"
        self.assertTrue(vault_file.exists())
        self.assertTrue(vault_file.read_bytes().startswith(b"FAKE_DPAPI_ENC:"))

        # Loaded credential matches original
        recovered = self.manager._load_credential_blob("secure_acc")
        self.assertEqual(recovered, plaintext_token)

    # -------------------------------------------------------------------------
    # 3. Cooldown / Disabled Selection
    # -------------------------------------------------------------------------

    def test_cooldown_and_disabled_selection(self) -> None:
        self.manager.add("acc1", credential_bytes=b"token1")
        self.manager.add("acc2", credential_bytes=b"token2")
        self.manager.add("acc3", credential_bytes=b"token3")
        self.backend.write_active(b"token1")
        self.manager.switch("acc1")

        # Enable pool rotation
        self.manager.enable("pool")

        # Disable acc2
        self.manager.disable("acc2")

        # Mark acc3 in cooldown
        self.manager.mark_depleted("acc3", cooldown_seconds=3600, reason="rate_limited")

        state = self.manager.load_state()
        self.assertFalse(state.accounts["acc2"].enabled)
        self.assertEqual(state.accounts["acc3"].status, "cooldown")

        # Rotate away from current acc1 -> only candidate remaining is none (acc2 disabled, acc3 cooldown)
        with self.assertRaises(agy_account.NoEligibleAccountsError):
            self.manager.rotate()

        # Re-enable acc2 -> rotate selects acc2
        self.manager.enable("acc2")
        rot = self.manager.rotate()
        self.assertEqual(rot["current"], "acc2")

        # Sticky selection: ensure-active keeps acc2 while healthy
        ens = self.manager.ensure_active()
        self.assertEqual(ens["current"], "acc2")
        self.assertFalse(ens["restored"])

        # Simulate cooldown expiration on acc3 by manually updating cooldown_until to the past
        past_iso = agy_account.utc_iso(agy_account.utc_now() - agy_account.timedelta(seconds=10))
        state = self.manager.load_state()
        state.accounts["acc3"].cooldown_until = past_iso
        self.manager.write_state(state)

        # Rotate from acc2 with tried=['acc1'] -> acc3 is now refreshed and selected
        rot2 = self.manager.rotate(tried=["acc1"])
        self.assertEqual(rot2["current"], "acc3")
        state_after = self.manager.load_state()
        self.assertEqual(state_after.accounts["acc3"].status, "healthy")

    # -------------------------------------------------------------------------
    # 4. Locking
    # -------------------------------------------------------------------------

    def test_cross_process_locking(self) -> None:
        self.manager.add("lock_a", credential_bytes=b"tok_a")
        self.manager.add("lock_b", credential_bytes=b"tok_b")
        self.backend.write_active(b"tok_a")
        self.manager.switch("lock_a")

        # Hold switch lock externally
        lock = agy_account.GlobalSwitchLock(self.state_dir / "switch.lock", timeout=0.2)
        lock.acquire()
        try:
            # Second locker should fail on timeout
            with self.assertRaises(agy_account.SwitchLockError):
                with agy_account.GlobalSwitchLock(self.state_dir / "switch.lock", timeout=0.2):
                    pass

            # Manager switch should fail with SwitchLockError while lock is held
            mgr_competing = agy_account.AccountManager(
                state_dir=self.state_dir, backend=self.backend
            )
            # Temporarily lower lock timeout on competing call
            with unittest.mock.patch.object(agy_account.GlobalSwitchLock, "__init__", lambda s, p, timeout=0.2: setattr(s, "lock_path", p) or setattr(s, "timeout", 0.2) or setattr(s, "file_handle", None)):
                with self.assertRaises(agy_account.SwitchLockError):
                    mgr_competing.switch("lock_b")
        finally:
            lock.release()

        # After release, switch succeeds cleanly
        sw = self.manager.switch("lock_b")
        self.assertEqual(sw["current"], "lock_b")

    # -------------------------------------------------------------------------
    # 5. Drift
    # -------------------------------------------------------------------------

    def test_credential_drift_auto_backup(self) -> None:
        self.manager.add("primary", credential_bytes=b"original_primary_secret")
        self.manager.add("secondary", credential_bytes=b"original_secondary_secret")
        self.backend.write_active(b"original_primary_secret")
        self.manager.switch("primary")

        # Simulate out-of-band credential modification in the live store
        drifted_secret = b"out_of_band_reauth_secret_777"
        self.backend.write_active(drifted_secret)

        # Before switch, current reports drift
        curr = self.manager.current()
        self.assertEqual(curr["current"], "primary")
        self.assertFalse(curr["live_matches"])

        # Switch to secondary; must detect drift and auto-backup before switching
        sw = self.manager.switch("secondary")
        self.assertEqual(sw["current"], "secondary")
        self.assertIsNotNone(sw["drift_backup"])
        backup_alias = sw["drift_backup"]
        self.assertTrue(backup_alias.startswith("_unsaved-"))

        # Verify backup alias exists in pool and holds drifted secret
        state = self.manager.load_state()
        self.assertIn(backup_alias, state.accounts)
        self.assertEqual(
            state.accounts[backup_alias].fingerprint,
            agy_account.compute_fingerprint(drifted_secret),
        )
        recovered_drift = self.manager._load_credential_blob(backup_alias)
        self.assertEqual(recovered_drift, drifted_secret)

        # Verify active credential is now secondary
        self.assertEqual(self.backend.read_active(), b"original_secondary_secret")

    # -------------------------------------------------------------------------
    # 6. Bounded Rotation & Pool Enablement
    # -------------------------------------------------------------------------

    def test_bounded_rotation_and_pool_requirement(self) -> None:
        self.manager.add("acc_1", credential_bytes=b"sec1")
        self.manager.add("acc_2", credential_bytes=b"sec2")
        self.manager.add("acc_3", credential_bytes=b"sec3")
        self.backend.write_active(b"sec1")
        self.manager.switch("acc_1")

        # Pool is disabled by default; rotate must fail with POOL_DISABLED
        with self.assertRaises(agy_account.PoolDisabledError):
            self.manager.rotate()

        # CLI invocation reflects POOL_DISABLED with exit code 3
        cli_res = self.invoke_cli("--json", "rotate")
        self.assertEqual(cli_res.returncode, agy_account.EXIT_POOL_DISABLED)
        err_json = json.loads(cli_res.stdout)
        self.assertEqual(err_json["error_code"], "POOL_DISABLED")

        # Manual switch works even when pool rotation is disabled
        sw = self.manager.switch("acc_2")
        self.assertEqual(sw["current"], "acc_2")

        # Enable pool
        self.manager.enable("pool")

        # Rotation works once enabled
        rot1 = self.manager.rotate()
        self.assertIn(rot1["current"], ["acc_1", "acc_3"])

        # Bounded rotation with tried list: exclude acc_1 and acc_3
        rot2 = self.manager.rotate(tried=["acc_1", "acc_3"])
        # Only acc_2 was not tried, but acc_2 was current when rotate started
        # With tried preventing acc_1 and acc_3, selection picks acc_2
        self.assertEqual(rot2["current"], "acc_2")

        # Exhaust all accounts in tried list -> Bounded termination
        with self.assertRaises(agy_account.NoEligibleAccountsError):
            self.manager.rotate(tried=["acc_1", "acc_2", "acc_3"])

        # rotate-after-failure marks depleted and moves to next eligible
        rot_fail = self.manager.rotate_after_failure(
            alias="acc_2", tried=["acc_1"], reason="429_quota"
        )
        self.assertEqual(rot_fail["depleted"], "acc_2")
        self.assertEqual(rot_fail["current"], "acc_3")

        # State confirms acc_2 in cooldown
        st = self.manager.load_state()
        self.assertEqual(st.accounts["acc_2"].status, "cooldown")

    # -------------------------------------------------------------------------
    # 7. SWITCH_BLOCKED Hook Point
    # -------------------------------------------------------------------------

    def test_switch_blocked_hook_point(self) -> None:
        self.manager.add("w1", credential_bytes=b"w1_tok")
        self.manager.add("w2", credential_bytes=b"w2_tok")
        self.backend.write_active(b"w1")
        self.manager.switch("w1")

        # 1. Block via state directory active_workers
        worker_file = self.state_dir / "active_workers" / "worker_job_42.json"
        worker_file.write_text(
            json.dumps({"pid": os.getpid(), "task": "long_running_migration"}),
            encoding="utf-8",
        )

        # Switch must fail with SWITCH_BLOCKED
        with self.assertRaises(agy_account.SwitchBlockedError) as ctx:
            self.manager.switch("w2")
        self.assertEqual(ctx.exception.error_code, "SWITCH_BLOCKED")
        self.assertIn("worker_job_42", ctx.exception.extra["active_workers"])

        # Passing force=True bypasses the block
        sw_force = self.manager.switch("w2", force=True)
        self.assertEqual(sw_force["current"], "w2")

        # Clean up worker file
        worker_file.unlink()

        # 2. Block via environment variable
        with mock.patch.dict(
            os.environ,
            {"POLYPHONY_BLOCK_SWITCH": "Active build runner in progress"},
            clear=False,
        ):
            with self.assertRaises(agy_account.SwitchBlockedError):
                self.manager.switch("w1")


if __name__ == "__main__":
    unittest.main()
