#!/usr/bin/env python3
"""Local account-manager surface and pool manager for Polyphony.

Manages multiple Gemini/Antigravity credentials on Windows with DPAPI
encryption, Win32 Credential Manager integration, cross-process locking,
sticky selection, cooldown tracking, and automatic drift backup.
"""

from __future__ import annotations

import abc
import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable


# Standard process exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_POOL_DISABLED = 3
EXIT_SWITCH_BLOCKED = 4
EXIT_LOCK_FAILED = 5

DEFAULT_TARGET = "gemini:antigravity"
DEFAULT_COOLDOWN_SECONDS = 5 * 3600  # 5 hours
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
WINDOWS_RECORD_MAGIC = b"POLYPHONY_CREDENTIAL_V1\0"


class AccountManagerError(Exception):
    """Base exception for account manager errors."""

    def __init__(
        self,
        message: str,
        error_code: str = "ERROR",
        extra: dict[str, Any] | None = None,
        exit_code: int = EXIT_ERROR,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.extra = extra or {}
        self.exit_code = exit_code


class PlatformNotSupportedError(AccountManagerError):
    def __init__(
        self,
        message: str = "Native Windows CredentialBackend requires Windows (Advapi32 / Crypt32)",
    ) -> None:
        super().__init__(
            message,
            error_code="PLATFORM_NOT_SUPPORTED",
            exit_code=EXIT_ERROR,
        )


class SwitchBlockedError(AccountManagerError):
    def __init__(
        self,
        reason: str,
        active_workers: list[str] | None = None,
    ) -> None:
        workers = active_workers or []
        super().__init__(
            f"Account switch blocked: {reason}",
            error_code="SWITCH_BLOCKED",
            extra={"reason": reason, "active_workers": workers},
            exit_code=EXIT_SWITCH_BLOCKED,
        )


class PoolDisabledError(AccountManagerError):
    def __init__(
        self,
        message: str = "Pool rotation is disabled; run 'agy-account enable --pool' to enable.",
    ) -> None:
        super().__init__(
            message,
            error_code="POOL_DISABLED",
            exit_code=EXIT_POOL_DISABLED,
        )


class NoEligibleAccountsError(AccountManagerError):
    def __init__(
        self,
        message: str = "No eligible accounts available in pool (exhausted, disabled, or in cooldown).",
    ) -> None:
        super().__init__(
            message,
            error_code="NO_ELIGIBLE_ACCOUNTS",
            exit_code=EXIT_ERROR,
        )


class SwitchLockError(AccountManagerError):
    def __init__(
        self,
        message: str = "Could not acquire global switch lock: locked by another process.",
    ) -> None:
        super().__init__(
            message,
            error_code="SWITCH_LOCKED",
            exit_code=EXIT_LOCK_FAILED,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    target = dt or utc_now()
    return target.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        # Handles 2026-09-19T13:43:00Z and standard ISO
        clean = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except (ValueError, TypeError):
        return None


def validate_alias(alias: str) -> None:
    if not alias or not ALIAS_PATTERN.match(alias):
        raise AccountManagerError(
            f"Invalid alias '{alias}': must match [A-Za-z0-9_-] and not be empty.",
            error_code="INVALID_ALIAS",
            exit_code=EXIT_USAGE,
        )


def compute_fingerprint(blob: bytes) -> str:
    """Non-reversible SHA-256 fingerprint of credential blob."""
    return hashlib.sha256(blob).hexdigest()


# -----------------------------------------------------------------------------
# Credential Backend Interface & Implementations
# -----------------------------------------------------------------------------


class CredentialBackend(abc.ABC):
    """Abstract interface for managing the active credential and data protection."""

    @abc.abstractmethod
    def read_active(self) -> bytes | None:
        """Read credential blob from the system active credential store."""
        ...

    @abc.abstractmethod
    def write_active(self, blob: bytes) -> None:
        """Write credential blob to the system active credential store."""
        ...

    @abc.abstractmethod
    def delete_active(self) -> None:
        """Delete active credential from the system store if it exists."""
        ...

    @abc.abstractmethod
    def protect(self, plaintext: bytes) -> bytes:
        """Encrypt secret bytes with user/system encryption."""
        ...

    @abc.abstractmethod
    def unprotect(self, ciphertext: bytes) -> bytes:
        """Decrypt secret bytes."""
        ...


class WindowsCredentialBackend(CredentialBackend):
    """Windows-first backend using Win32 Credential Manager and DPAPI."""

    def __init__(self, target_name: str = DEFAULT_TARGET) -> None:
        if sys.platform != "win32":
            raise PlatformNotSupportedError()
        self.target_name = target_name
        self._init_windows_apis()

    def _init_windows_apis(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self.DATA_BLOB = DATA_BLOB
        self.CREDENTIALW = CREDENTIALW
        self.PCREDENTIALW = ctypes.POINTER(CREDENTIALW)

        self.advapi32.CredReadW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(self.PCREDENTIALW),
        ]
        self.advapi32.CredReadW.restype = wintypes.BOOL
        self.advapi32.CredWriteW.argtypes = [self.PCREDENTIALW, wintypes.DWORD]
        self.advapi32.CredWriteW.restype = wintypes.BOOL
        self.advapi32.CredDeleteW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.advapi32.CredDeleteW.restype = wintypes.BOOL
        self.advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self.advapi32.CredFree.restype = None
        self.crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        self.crypt32.CryptProtectData.restype = wintypes.BOOL
        self.crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        self.crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _pack_record(blob: bytes, username: str | None, comment: str | None, persist: int) -> bytes:
        payload = json.dumps(
            {
                "blob": base64.b64encode(blob).decode("ascii"),
                "username": username,
                "comment": comment,
                "persist": int(persist),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return WINDOWS_RECORD_MAGIC + payload

    @staticmethod
    def _unpack_record(value: bytes) -> tuple[bytes, str | None, str | None, int]:
        if not value.startswith(WINDOWS_RECORD_MAGIC):
            return value, "gemini", "Polyphony account pool manager", 2
        try:
            payload = json.loads(value[len(WINDOWS_RECORD_MAGIC):].decode("utf-8"))
            return (
                base64.b64decode(payload["blob"], validate=True),
                payload.get("username"),
                payload.get("comment"),
                int(payload.get("persist") or 2),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AccountManagerError("Saved credential record is corrupt", "CREDENTIAL_CORRUPT") from exc

    def read_active(self) -> bytes | None:
        cred_ptr = self.PCREDENTIALW()
        # CRED_TYPE_GENERIC = 1
        res = self.advapi32.CredReadW(self.target_name, 1, 0, self.ctypes.byref(cred_ptr))
        if not res:
            err = self.ctypes.get_last_error()
            if err == 1168:  # ERROR_NOT_FOUND
                return None
            raise AccountManagerError(
                f"CredReadW failed for target '{self.target_name}': error code {err}",
                error_code="CRED_READ_FAILED",
            )
        try:
            blob = self.ctypes.string_at(
                cred_ptr.contents.CredentialBlob,
                cred_ptr.contents.CredentialBlobSize,
            )
            return self._pack_record(
                blob,
                cred_ptr.contents.UserName,
                cred_ptr.contents.Comment,
                cred_ptr.contents.Persist,
            )
        finally:
            self.advapi32.CredFree(cred_ptr)

    def write_active(self, blob: bytes) -> None:
        raw_blob, username, comment, persist = self._unpack_record(blob)
        cred = self.CREDENTIALW()
        cred.Flags = 0
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = self.target_name
        cred.Comment = comment
        cred.CredentialBlobSize = len(raw_blob)
        raw_buffer = self.ctypes.create_string_buffer(raw_blob, len(raw_blob))
        cred.CredentialBlob = self.ctypes.cast(raw_buffer, self.ctypes.POINTER(self.ctypes.c_char))
        cred.Persist = persist
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = username

        res = self.advapi32.CredWriteW(self.ctypes.byref(cred), 0)
        if not res:
            err = self.ctypes.get_last_error()
            raise AccountManagerError(
                f"CredWriteW failed for target '{self.target_name}': error code {err}",
                error_code="CRED_WRITE_FAILED",
            )

    def delete_active(self) -> None:
        # CRED_TYPE_GENERIC = 1
        self.advapi32.CredDeleteW(self.target_name, 1, 0)

    def protect(self, plaintext: bytes) -> bytes:
        # CRYPTPROTECT_UI_FORBIDDEN = 0x1
        blob_in = self.DATA_BLOB(
            len(plaintext),
            self.ctypes.cast(
                self.ctypes.c_char_p(plaintext),
                self.ctypes.POINTER(self.ctypes.c_char),
            ),
        )
        blob_out = self.DATA_BLOB()
        res = self.crypt32.CryptProtectData(
            self.ctypes.byref(blob_in),
            "polyphony:credential",
            None,
            None,
            None,
            0x1,
            self.ctypes.byref(blob_out),
        )
        if not res:
            err = self.ctypes.get_last_error()
            raise AccountManagerError(
                f"CryptProtectData failed: error code {err}",
                error_code="DPAPI_PROTECT_FAILED",
            )
        try:
            return self.ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self.kernel32.LocalFree(blob_out.pbData)

    def unprotect(self, ciphertext: bytes) -> bytes:
        # CRYPTPROTECT_UI_FORBIDDEN = 0x1
        blob_in = self.DATA_BLOB(
            len(ciphertext),
            self.ctypes.cast(
                self.ctypes.c_char_p(ciphertext),
                self.ctypes.POINTER(self.ctypes.c_char),
            ),
        )
        blob_out = self.DATA_BLOB()
        res = self.crypt32.CryptUnprotectData(
            self.ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0x1,
            self.ctypes.byref(blob_out),
        )
        if not res:
            err = self.ctypes.get_last_error()
            raise AccountManagerError(
                f"CryptUnprotectData failed: error code {err}",
                error_code="DPAPI_UNPROTECT_FAILED",
            )
        try:
            return self.ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self.kernel32.LocalFree(blob_out.pbData)


class FakeCredentialBackend(CredentialBackend):
    """Configurable fake backend for offline tests and non-Windows environments."""

    MAGIC_PREFIX = b"FAKE_DPAPI_ENC:"

    def __init__(
        self,
        active_blob: bytes | None = None,
        store_path: Path | None = None,
    ) -> None:
        self.store_path = store_path
        self._active_blob = active_blob
        if store_path and store_path.exists():
            try:
                self._active_blob = store_path.read_bytes()
            except OSError:
                pass

    def read_active(self) -> bytes | None:
        if self.store_path and self.store_path.exists():
            try:
                return self.store_path.read_bytes()
            except OSError:
                return None
        return self._active_blob

    def write_active(self, blob: bytes) -> None:
        self._active_blob = blob
        if self.store_path:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_bytes(blob)

    def delete_active(self) -> None:
        self._active_blob = None
        if self.store_path and self.store_path.exists():
            try:
                self.store_path.unlink()
            except OSError:
                pass

    def protect(self, plaintext: bytes) -> bytes:
        # Reversible transformation that ensures plaintext is never present on disk
        obfuscated = bytes(b ^ 0x5A for b in plaintext)
        return self.MAGIC_PREFIX + obfuscated

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(self.MAGIC_PREFIX):
            raise AccountManagerError(
                "Invalid fake ciphertext format",
                error_code="DECRYPT_FAILED",
            )
        obfuscated = ciphertext[len(self.MAGIC_PREFIX) :]
        return bytes(b ^ 0x5A for b in obfuscated)


def get_backend(
    backend_type: str | None = None,
    target_name: str = DEFAULT_TARGET,
    state_dir: Path | None = None,
) -> CredentialBackend:
    choice = (
        backend_type
        or os.environ.get("POLYPHONY_CREDENTIAL_BACKEND", "").lower().strip()
    )
    if choice in ("fake", "mock", "test"):
        store = state_dir / ".fake_active.bin" if state_dir else None
        return FakeCredentialBackend(store_path=store)

    if sys.platform == "win32":
        return WindowsCredentialBackend(target_name=target_name)

    raise PlatformNotSupportedError(
        f"Native Windows CredentialBackend is not supported on {sys.platform}. "
        "Set POLYPHONY_CREDENTIAL_BACKEND=fake for offline portable testing."
    )


# -----------------------------------------------------------------------------
# Cross-Process Global Switch Lock
# -----------------------------------------------------------------------------


class GlobalSwitchLock:
    """One cross-process global lock for account switching."""

    def __init__(self, lock_path: Path, timeout: float = 10.0) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self.file_handle: Any = None

    def __enter__(self) -> "GlobalSwitchLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        # a+b creates the lock file atomically when absent. Ensure byte 0 exists
        # because Windows msvcrt locks byte ranges rather than whole files.
        handle = open(self.lock_path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"L")
            handle.flush()
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                self.file_handle = handle
                return
            except (OSError, PermissionError):
                if time.time() - start >= self.timeout:
                    handle.close()
                    raise SwitchLockError(
                        f"Global switch lock on {self.lock_path} timed out after {self.timeout:.1f}s"
                    )
                time.sleep(0.05)

    def release(self) -> None:
        if self.file_handle is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self.file_handle.seek(0)
                    msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                self.file_handle.close()
                self.file_handle = None


# -----------------------------------------------------------------------------
# Account State & Metadata
# -----------------------------------------------------------------------------


@dataclass
class AccountMetadata:
    alias: str
    fingerprint: str
    enabled: bool = True
    created_at: str = ""
    last_used: str | None = None
    status: str = "healthy"  # "healthy", "cooldown", "disabled"
    cooldown_until: str | None = None
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountMetadata":
        return cls(
            alias=data["alias"],
            fingerprint=data["fingerprint"],
            enabled=data.get("enabled", True),
            created_at=data.get("created_at", ""),
            last_used=data.get("last_used"),
            status=data.get("status", "healthy"),
            cooldown_until=data.get("cooldown_until"),
            failure_count=data.get("failure_count", 0),
            last_failure_at=data.get("last_failure_at"),
            last_failure_reason=data.get("last_failure_reason"),
        )


@dataclass
class PoolState:
    version: int = 1
    pool_enabled: bool = False
    current: str | None = None
    accounts: dict[str, AccountMetadata] | None = None

    def __post_init__(self) -> None:
        if self.accounts is None:
            self.accounts = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "pool_enabled": self.pool_enabled,
            "current": self.current,
            "accounts": {
                alias: meta.to_dict() for alias, meta in self.accounts.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PoolState":
        raw_accounts = data.get("accounts", {})
        accounts = {
            alias: AccountMetadata.from_dict(meta)
            for alias, meta in raw_accounts.items()
        }
        return cls(
            version=data.get("version", 1),
            pool_enabled=data.get("pool_enabled", False),
            current=data.get("current"),
            accounts=accounts,
        )


def resolve_state_root(custom_dir: Path | None = None) -> Path:
    """Resolve state root directory with environment overrides."""
    if custom_dir:
        return custom_dir

    env_override = os.environ.get("POLYPHONY_ACCOUNTS_DIR") or os.environ.get(
        "POLYPHONY_ACCOUNTS_ROOT"
    )
    if env_override:
        return Path(env_override).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Polyphony" / "accounts"

    return Path.home() / ".local" / "share" / "Polyphony" / "accounts"


# -----------------------------------------------------------------------------
# Account Manager Core
# -----------------------------------------------------------------------------


class AccountManager:
    """Manages the credential pool, locking, atomic state updates, and rotation."""

    def __init__(
        self,
        state_dir: Path | None = None,
        backend: CredentialBackend | None = None,
        switch_blocker_hook: Callable[[], tuple[bool, str, list[str]]] | None = None,
    ) -> None:
        self.state_dir = resolve_state_root(state_dir)
        self.vault_dir = self.state_dir / "vault"
        self.lock_file = self.state_dir / "switch.lock"
        self.pool_file = self.state_dir / "pool.json"
        self.workers_dir = self.state_dir / "active_workers"
        self.backend = backend or get_backend(state_dir=self.state_dir)
        self.switch_blocker_hook = switch_blocker_hook

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.workers_dir.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> PoolState:
        if not self.pool_file.exists():
            return PoolState()
        try:
            data = json.loads(self.pool_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return PoolState.from_dict(data)
        except (OSError, ValueError):
            pass
        return PoolState()

    def write_state(self, state: PoolState) -> None:
        """Atomically persist pool state metadata to disk."""
        self.ensure_directories()
        data = state.to_dict()
        content = json.dumps(data, indent=2, sort_keys=True) + "\n"

        fd, temp_path = tempfile.mkstemp(
            prefix="pool.", suffix=".tmp", dir=self.state_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.pool_file)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    # --- Vault Storage (Ciphertext Only) ---

    def _vault_path(self, alias: str) -> Path:
        return self.vault_dir / f"{alias}.enc"

    def _save_credential_blob(self, alias: str, plaintext: bytes) -> None:
        self.ensure_directories()
        ciphertext = self.backend.protect(plaintext)
        target = self._vault_path(alias)
        fd, temp_path = tempfile.mkstemp(
            prefix="cred.", suffix=".tmp", dir=self.vault_dir
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _load_credential_blob(self, alias: str) -> bytes:
        path = self._vault_path(alias)
        if not path.exists():
            raise AccountManagerError(
                f"Credential store for alias '{alias}' not found in vault.",
                error_code="CREDENTIAL_NOT_FOUND",
            )
        ciphertext = path.read_bytes()
        return self.backend.unprotect(ciphertext)

    def _delete_credential_blob(self, alias: str) -> None:
        path = self._vault_path(alias)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    # --- Worker Hook Point & Drift Detection ---

    def check_switch_blocked(self, force: bool = False) -> None:
        """Structured SWITCH_BLOCKED hook point for active workers."""
        if force:
            return

        # 1. Direct Python dependency injection hook
        if self.switch_blocker_hook is not None:
            blocked, reason, workers = self.switch_blocker_hook()
            if blocked:
                raise SwitchBlockedError(reason, active_workers=workers)

        # 2. Environment variable triggers
        block_reason = os.environ.get("POLYPHONY_BLOCK_SWITCH")
        if block_reason:
            raise SwitchBlockedError(
                block_reason,
                active_workers=[
                    w.strip()
                    for w in os.environ.get("POLYPHONY_ACTIVE_WORKERS", "").split(",")
                    if w.strip()
                ],
            )

        active_workers_env = os.environ.get("POLYPHONY_ACTIVE_WORKERS")
        if active_workers_env:
            workers = [w.strip() for w in active_workers_env.split(",") if w.strip()]
            if workers:
                raise SwitchBlockedError(
                    f"Active worker processes running: {', '.join(workers)}",
                    active_workers=workers,
                )

        # 3. State directory worker registry
        owner = os.environ.get("POLYPHONY_SWITCH_OWNER", "").strip()
        if self.workers_dir.exists():
            registered = []
            for worker_file in self.workers_dir.iterdir():
                if not worker_file.is_file() or worker_file.stem == owner:
                    continue
                try:
                    worker = json.loads(worker_file.read_text(encoding="utf-8"))
                    pid = int(worker.get("pid") or 0)
                    if pid > 0:
                        os.kill(pid, 0)
                    registered.append(worker_file.stem)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    try:
                        worker_file.unlink()
                    except OSError:
                        registered.append(worker_file.stem)
            if registered:
                raise SwitchBlockedError(
                    f"Active workers registered in state: {', '.join(registered)}",
                    active_workers=registered,
                )

        # Agy may also be running outside Polyphony. A credential change would not
        # affect that process immediately and it could later refresh the old token.
        if sys.platform == "win32" and not isinstance(self.backend, FakeCredentialBackend) \
                and os.environ.get("POLYPHONY_SKIP_AGY_PROCESS_CHECK") != "1":
            try:
                proc = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq agy.exe", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=False,
                )
                pids = re.findall(r'"agy\.exe","([0-9]+)"', proc.stdout, re.IGNORECASE)
                if pids:
                    raise SwitchBlockedError("An Agy process is still running", active_workers=pids)
            except subprocess.TimeoutExpired:
                raise SwitchBlockedError("Timed out while checking for running Agy processes")
            except OSError:
                pass

        # 4. Optional external blocker for host-specific job registries.
        hook_cmd = os.environ.get("POLYPHONY_SWITCH_HOOK")
        if hook_cmd:
            try:
                proc = subprocess.run(
                    hook_cmd, shell=True, text=True, capture_output=True, timeout=5
                )
                if proc.returncode != 0:
                    out = (proc.stdout.strip() or proc.stderr.strip()) or "hook rejected switch"
                    raise SwitchBlockedError(out)
            except subprocess.TimeoutExpired:
                raise SwitchBlockedError("External switch hook timed out")
            except OSError as exc:
                raise SwitchBlockedError(f"Failed to execute switch hook: {exc}")

    def worker_start(self, worker_id: str, account_id: str | None = None) -> dict[str, Any]:
        validate_alias(worker_id)
        self.ensure_directories()
        path = self.workers_dir / f"{worker_id}.json"
        payload = {"pid": os.getppid(), "account_id": account_id, "started_at": utc_iso()}
        path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        return {"status": "ok", "action": "worker-start", "worker_id": worker_id}

    def worker_stop(self, worker_id: str) -> dict[str, Any]:
        validate_alias(worker_id)
        try:
            (self.workers_dir / f"{worker_id}.json").unlink()
        except FileNotFoundError:
            pass
        return {"status": "ok", "action": "worker-stop", "worker_id": worker_id}

    def check_and_backup_drift(self, state: PoolState) -> str | None:
        """Detect out-of-band credential modifications and back them up."""
        live_blob = self.backend.read_active()
        if live_blob is None or len(live_blob) == 0:
            return None

        live_fp = compute_fingerprint(live_blob)

        # If live credential matches current account's recorded fingerprint, no drift
        if (
            state.current
            and state.current in state.accounts
            and state.accounts[state.current].fingerprint == live_fp
        ):
            return None

        # Check if live credential matches ANY existing account in pool
        for alias, meta in state.accounts.items():
            if meta.fingerprint == live_fp:
                # Matches known account; not an unsaved drift
                return None

        # Live credential does not match any known account; auto-backup as _unsaved-<timestamp>
        timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        backup_alias = f"_unsaved-{timestamp}"
        counter = 1
        while backup_alias in state.accounts:
            backup_alias = f"_unsaved-{timestamp}-{counter}"
            counter += 1

        self._save_credential_blob(backup_alias, live_blob)
        state.accounts[backup_alias] = AccountMetadata(
            alias=backup_alias,
            fingerprint=live_fp,
            enabled=False,
            created_at=utc_iso(),
            last_used=utc_iso(),
            status="disabled",
            last_failure_reason="drift_backup",
        )
        self.write_state(state)
        return backup_alias

    def _refresh_cooldowns(self, state: PoolState) -> bool:
        """Clear expired cooldowns; returns True if state changed."""
        now = utc_now()
        changed = False
        for meta in state.accounts.values():
            if meta.status == "cooldown" and meta.cooldown_until:
                cd_time = parse_iso(meta.cooldown_until)
                if cd_time and now >= cd_time:
                    meta.status = "healthy"
                    meta.cooldown_until = None
                    changed = True
        return changed

    # --- Commands ---

    def add(
        self,
        alias: str | None = None,
        credential_bytes: bytes | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        with GlobalSwitchLock(self.lock_file):
            return self._add_locked(alias, credential_bytes, force)

    def _add_locked(
        self,
        alias: str | None = None,
        credential_bytes: bytes | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Add credential to the pool from live store or stdin."""
        state = self.load_state()

        blob = credential_bytes
        if blob is None:
            blob = self.backend.read_active()
            if blob is None or len(blob) == 0:
                raise AccountManagerError(
                    f"No active credential found in target '{DEFAULT_TARGET}' and no input provided.",
                    error_code="NO_CREDENTIAL_FOUND",
                )

        fp = compute_fingerprint(blob)

        if not alias:
            # Deterministic, unique alias derived from fingerprint
            alias = f"acc-{fp[:8]}"

        validate_alias(alias)

        if alias in state.accounts and not force:
            raise AccountManagerError(
                f"Account '{alias}' already exists in pool. Use --force to overwrite.",
                error_code="ACCOUNT_EXISTS",
            )

        self._save_credential_blob(alias, blob)

        now_str = utc_iso()
        meta = AccountMetadata(
            alias=alias,
            fingerprint=fp,
            enabled=True,
            created_at=now_str,
            last_used=now_str if state.current == alias else None,
            status="healthy",
        )
        state.accounts[alias] = meta

        # `add` snapshots the login that is active right now, so that profile is
        # necessarily the current one even when other profiles already exist.
        state.current = alias
        meta.last_used = now_str

        self.write_state(state)
        return {
            "status": "ok",
            "action": "add",
            "alias": alias,
            "fingerprint": fp,
            "current": state.current,
        }

    def list_accounts(self) -> dict[str, Any]:
        with GlobalSwitchLock(self.lock_file):
            return self._list_accounts_locked()

    def _list_accounts_locked(self) -> dict[str, Any]:
        """List all accounts and current pool status."""
        state = self.load_state()
        if self._refresh_cooldowns(state):
            self.write_state(state)

        account_list = []
        for alias in sorted(state.accounts.keys()):
            meta = state.accounts[alias]
            account_list.append(
                {
                    "alias": meta.alias,
                    "current": (state.current == alias),
                    "enabled": meta.enabled,
                    "status": meta.status,
                    "fingerprint": meta.fingerprint,
                    "created_at": meta.created_at,
                    "last_used": meta.last_used,
                    "cooldown_until": meta.cooldown_until,
                    "failure_count": meta.failure_count,
                }
            )

        return {
            "status": "ok",
            "action": "list",
            "pool_enabled": state.pool_enabled,
            "current": state.current,
            "accounts": account_list,
        }

    def current(self) -> dict[str, Any]:
        """Get the current active account and check drift status."""
        state = self.load_state()
        self._refresh_cooldowns(state)

        current_alias = state.current
        if not current_alias or current_alias not in state.accounts:
            return {
                "status": "ok",
                "action": "current",
                "current": None,
                "message": "No active account selected in pool.",
            }

        meta = state.accounts[current_alias]
        live_blob = self.backend.read_active()
        live_matches = False
        if live_blob is not None:
            live_matches = (compute_fingerprint(live_blob) == meta.fingerprint)

        return {
            "status": "ok",
            "action": "current",
            "current": current_alias,
            "fingerprint": meta.fingerprint,
            "status_label": meta.status,
            "enabled": meta.enabled,
            "live_matches": live_matches,
        }

    def switch(self, alias: str, force: bool = False) -> dict[str, Any]:
        """Switch active credential to specified alias under global switch lock."""
        validate_alias(alias)
        with GlobalSwitchLock(self.lock_file):
            self.check_switch_blocked(force=force)

            state = self.load_state()
            if alias not in state.accounts:
                raise AccountManagerError(
                    f"Account '{alias}' does not exist in pool.",
                    error_code="ACCOUNT_NOT_FOUND",
                )
            target = state.accounts[alias]
            if not target.enabled or target.status in {"disabled", "invalid"}:
                raise AccountManagerError(
                    f"Account '{alias}' is disabled or invalid; enable or re-add it before switching.",
                    error_code="ACCOUNT_UNAVAILABLE",
                )

            # Auto-backup drift before overwriting active credential
            drift_backup = self.check_and_backup_drift(state)

            blob = self._load_credential_blob(alias)
            self.backend.write_active(blob)

            prev = state.current
            state.current = alias
            target.last_used = utc_iso()
            self.write_state(state)

            return {
                "status": "ok",
                "action": "switch",
                "previous": prev,
                "current": alias,
                "drift_backup": drift_backup,
            }

    def remove(self, alias: str) -> dict[str, Any]:
        """Remove account from pool."""
        validate_alias(alias)
        with GlobalSwitchLock(self.lock_file):
            state = self.load_state()
            if alias not in state.accounts:
                raise AccountManagerError(
                    f"Account '{alias}' does not exist in pool.",
                    error_code="ACCOUNT_NOT_FOUND",
                )
            if state.current == alias:
                raise AccountManagerError(
                    "Refusing to remove the active account; switch to another saved account first.",
                    error_code="ACTIVE_ACCOUNT_REMOVE_BLOCKED",
                )

            self._delete_credential_blob(alias)
            del state.accounts[alias]
            self.write_state(state)
            return {
                "status": "ok",
                "action": "remove",
                "alias": alias,
            }

    def enable(self, target: str) -> dict[str, Any]:
        with GlobalSwitchLock(self.lock_file):
            return self._enable_locked(target)

    def _enable_locked(self, target: str) -> dict[str, Any]:
        """Enable an account or enable the pool."""
        if target.lower() in ("pool", "--pool"):
            state = self.load_state()
            state.pool_enabled = True
            self.write_state(state)
            return {
                "status": "ok",
                "action": "enable",
                "target": "pool",
                "pool_enabled": True,
            }

        validate_alias(target)
        state = self.load_state()
        if target not in state.accounts:
            raise AccountManagerError(
                f"Account '{target}' does not exist in pool.",
                error_code="ACCOUNT_NOT_FOUND",
            )

        meta = state.accounts[target]
        meta.enabled = True
        if meta.status == "invalid":
            raise AccountManagerError(
                f"Account '{target}' has invalid credentials; log in again and re-add it with --force.",
                error_code="ACCOUNT_INVALID_READD_REQUIRED",
            )
        if meta.status in {"disabled", "cooldown"}:
            meta.status = "healthy"
            meta.cooldown_until = None
        self.write_state(state)

        return {
            "status": "ok",
            "action": "enable",
            "target": target,
            "enabled": True,
        }

    def disable(self, target: str) -> dict[str, Any]:
        with GlobalSwitchLock(self.lock_file):
            return self._disable_locked(target)

    def _disable_locked(self, target: str) -> dict[str, Any]:
        """Disable an account or disable the pool."""
        if target.lower() in ("pool", "--pool"):
            state = self.load_state()
            state.pool_enabled = False
            self.write_state(state)
            return {
                "status": "ok",
                "action": "disable",
                "target": "pool",
                "pool_enabled": False,
            }

        validate_alias(target)
        state = self.load_state()
        if target not in state.accounts:
            raise AccountManagerError(
                f"Account '{target}' does not exist in pool.",
                error_code="ACCOUNT_NOT_FOUND",
            )

        meta = state.accounts[target]
        meta.enabled = False
        meta.status = "disabled"
        self.write_state(state)

        return {
            "status": "ok",
            "action": "disable",
            "target": target,
            "enabled": False,
        }

    def select_candidate(
        self,
        state: PoolState,
        tried: list[str] | None = None,
        exclude_current: bool = True,
    ) -> str:
        """Select next eligible candidate using sticky selection and least-recently-used ordering."""
        self._refresh_cooldowns(state)
        tried_set = set(tried or [])

        # Filter eligible accounts: enabled, healthy (not cooldown), not in tried
        eligible: list[AccountMetadata] = []
        for alias, meta in state.accounts.items():
            if not meta.enabled:
                continue
            if alias in tried_set:
                continue
            if meta.status != "healthy":
                continue
            if exclude_current and alias == state.current:
                continue
            eligible.append(meta)

        if not eligible:
            raise NoEligibleAccountsError()

        # Deterministic sorting: least recently used, lowest failure count, alias tie-breaker
        def sort_key(item: AccountMetadata) -> tuple[int, str, int, str]:
            used_rank = 0 if item.last_used is None else 1
            used_str = item.last_used or ""
            return (used_rank, used_str, item.failure_count, item.alias)

        eligible.sort(key=sort_key)
        return eligible[0].alias

    def rotate(
        self,
        tried: list[str] | None = None,
        reason: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Rotate to next eligible account under lock."""
        with GlobalSwitchLock(self.lock_file):
            self.check_switch_blocked(force=force)

            state = self.load_state()
            if not state.pool_enabled and not force:
                raise PoolDisabledError()

            candidate = self.select_candidate(
                state, tried=tried, exclude_current=True
            )

            drift_backup = self.check_and_backup_drift(state)
            blob = self._load_credential_blob(candidate)
            self.backend.write_active(blob)

            prev = state.current
            state.current = candidate
            state.accounts[candidate].last_used = utc_iso()
            self.write_state(state)

            return {
                "status": "ok",
                "action": "rotate",
                "previous": prev,
                "current": candidate,
                "reason": reason,
                "drift_backup": drift_backup,
            }

    def ensure_active(self, force: bool = False) -> dict[str, Any]:
        """Ensure a healthy account is active; sticky selection keeps current if valid."""
        with GlobalSwitchLock(self.lock_file):
            state = self.load_state()
            self._refresh_cooldowns(state)

            current_alias = state.current
            current_healthy = False
            if current_alias and current_alias in state.accounts:
                curr = state.accounts[current_alias]
                if curr.enabled and curr.status == "healthy":
                    current_healthy = True

            # If current is healthy, sticky selection prefers keeping it
            if current_healthy and current_alias:
                live_blob = self.backend.read_active()
                meta = state.accounts[current_alias]
                live_matches = False
                if live_blob is not None:
                    live_matches = (compute_fingerprint(live_blob) == meta.fingerprint)

                if not live_matches and not state.pool_enabled and not force:
                    return {
                        "status": "ok",
                        "action": "ensure-active",
                        "current": current_alias,
                        "fingerprint": meta.fingerprint,
                        "restored": False,
                        "live_matches": False,
                    }
                if not live_matches:
                    # Drift or empty active target: restore current account
                    self.check_switch_blocked(force=force)
                    drift_backup = self.check_and_backup_drift(state)
                    blob = self._load_credential_blob(current_alias)
                    self.backend.write_active(blob)
                    meta.last_used = utc_iso()
                    self.write_state(state)
                    return {
                        "status": "ok",
                        "action": "ensure-active",
                        "current": current_alias,
                        "fingerprint": meta.fingerprint,
                        "restored": True,
                        "drift_backup": drift_backup,
                    }

                return {
                    "status": "ok",
                    "action": "ensure-active",
                    "current": current_alias,
                    "fingerprint": meta.fingerprint,
                    "restored": False,
                }

            # Current is missing or unhealthy; attempt selection
            if not state.pool_enabled and not force:
                raise AccountManagerError(
                    "Current account is unhealthy/missing and pool rotation is disabled.",
                    error_code="POOL_DISABLED",
                    exit_code=EXIT_POOL_DISABLED,
                )

            self.check_switch_blocked(force=force)
            candidate = self.select_candidate(
                state, tried=[], exclude_current=False
            )
            drift_backup = self.check_and_backup_drift(state)
            blob = self._load_credential_blob(candidate)
            self.backend.write_active(blob)

            state.current = candidate
            state.accounts[candidate].last_used = utc_iso()
            self.write_state(state)

            return {
                "status": "ok",
                "action": "ensure-active",
                "current": candidate,
                "fingerprint": state.accounts[candidate].fingerprint,
                "restored": True,
                "drift_backup": drift_backup,
            }

    def mark_depleted(
        self,
        alias: str | None = None,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Mark an account as depleted / in cooldown."""
        with GlobalSwitchLock(self.lock_file):
            state = self.load_state()
            target = alias or state.current
            if not target or target not in state.accounts:
                raise AccountManagerError(
                    f"Account '{target}' not found to mark depleted.",
                    error_code="ACCOUNT_NOT_FOUND",
                )

            now = utc_now()
            cooldown_until = now + timedelta(seconds=cooldown_seconds)
            meta = state.accounts[target]
            meta.status = "cooldown"
            meta.cooldown_until = utc_iso(cooldown_until)
            meta.failure_count += 1
            meta.last_failure_at = utc_iso(now)
            meta.last_failure_reason = reason or "depleted"

            self.write_state(state)
            return {
                "status": "ok",
                "action": "mark-depleted",
                "alias": target,
                "status_label": "cooldown",
                "cooldown_until": meta.cooldown_until,
                "failure_count": meta.failure_count,
                "reason": meta.last_failure_reason,
            }

    def rotate_after_failure(
        self,
        alias: str | None = None,
        tried: list[str] | None = None,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        reason: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Mark failed account depleted and rotate to next eligible candidate."""
        with GlobalSwitchLock(self.lock_file):
            self.check_switch_blocked(force=force)
            state = self.load_state()

            if not state.pool_enabled and not force:
                raise PoolDisabledError()

            target = alias or state.current
            if target and target in state.accounts:
                now = utc_now()
                meta = state.accounts[target]
                is_auth = (reason or "").lower() in {"auth", "authentication", "auth_invalid"}
                meta.status = "invalid" if is_auth else "cooldown"
                meta.cooldown_until = None if is_auth else utc_iso(now + timedelta(seconds=cooldown_seconds))
                meta.failure_count += 1
                meta.last_failure_at = utc_iso(now)
                meta.last_failure_reason = reason or "failed"
                self.write_state(state)

            tried_list = list(tried or [])
            if target and target not in tried_list:
                tried_list.append(target)

            candidate = self.select_candidate(
                state, tried=tried_list, exclude_current=(target == state.current)
            )
            drift_backup = self.check_and_backup_drift(state)
            blob = self._load_credential_blob(candidate)
            self.backend.write_active(blob)

            prev = state.current
            state.current = candidate
            state.accounts[candidate].last_used = utc_iso()
            self.write_state(state)

            return {
                "status": "ok",
                "action": "rotate-after-failure",
                "depleted": target,
                "previous": prev,
                "current": candidate,
                "drift_backup": drift_backup,
            }

    def doctor(self) -> dict[str, Any]:
        """Run system diagnostics for the account pool."""
        state = self.load_state()
        self._refresh_cooldowns(state)

        live_blob = None
        backend_error = None
        try:
            live_blob = self.backend.read_active()
        except Exception as exc:
            backend_error = str(exc)

        live_fp = compute_fingerprint(live_blob) if live_blob else None
        current_meta = state.accounts.get(state.current) if state.current else None
        live_matches = (
            bool(current_meta and live_fp and current_meta.fingerprint == live_fp)
        )

        switch_blocked = False
        block_reason = None
        try:
            self.check_switch_blocked()
        except SwitchBlockedError as exc:
            switch_blocked = True
            block_reason = exc.message

        accounts_total = len(state.accounts)
        accounts_enabled = sum(1 for a in state.accounts.values() if a.enabled)
        accounts_in_cooldown = sum(
            1 for a in state.accounts.values() if a.status == "cooldown"
        )

        healthy = (
            backend_error is None
            and (accounts_total == 0 or (state.current is not None and live_matches))
        )

        return {
            "status": "ok",
            "action": "doctor",
            "healthy": healthy,
            "platform": sys.platform,
            "backend": type(self.backend).__name__,
            "state_dir": str(self.state_dir),
            "pool_enabled": state.pool_enabled,
            "current": state.current,
            "live_credential_present": live_blob is not None,
            "live_matches_current": live_matches,
            "live_fingerprint": live_fp,
            "accounts_total": accounts_total,
            "accounts_enabled": accounts_enabled,
            "accounts_in_cooldown": accounts_in_cooldown,
            "switch_blocked": switch_blocked,
            "switch_block_reason": block_reason,
            "backend_error": backend_error,
        }


# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------


def _parse_tried(tried_args: list[str] | None) -> list[str]:
    if not tried_args:
        return []
    result = []
    for item in tried_args:
        for part in item.split(","):
            clean = part.strip()
            if clean and clean not in result:
                result.append(clean)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agy-account",
        description="Polyphony Local Account Pool Manager",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as stable JSON",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="State directory override",
    )
    parser.add_argument(
        "--backend",
        type=str,
        default=None,
        choices=["windows", "fake"],
        help="Force credential backend type",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # add [alias]
    p_add = subparsers.add_parser("add", help="Add an account to the pool")
    p_add.add_argument("alias", nargs="?", default=None, help="Account alias")
    p_add.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing alias",
    )

    # list
    subparsers.add_parser("list", help="List all accounts in the pool")

    # current
    subparsers.add_parser("current", help="Show current active account")

    # switch <alias>
    p_switch = subparsers.add_parser("switch", help="Switch active account")
    p_switch.add_argument("alias", help="Target account alias")
    p_switch.add_argument(
        "--force",
        action="store_true",
        help="Bypass worker switch block",
    )

    # remove <alias>
    p_remove = subparsers.add_parser("remove", help="Remove account from pool")
    p_remove.add_argument("alias", help="Account alias to remove")

    # enable <alias> | enable --pool
    p_enable = subparsers.add_parser("enable", help="Enable account or pool")
    p_enable.add_argument("target", nargs="?", default=None, help="Account alias or 'pool'")
    p_enable.add_argument("--pool", action="store_true", help="Enable automatic pool rotation")

    # disable <alias> | disable --pool
    p_disable = subparsers.add_parser("disable", help="Disable account or pool")
    p_disable.add_argument("target", nargs="?", default=None, help="Account alias or 'pool'")
    p_disable.add_argument("--pool", action="store_true", help="Disable automatic pool rotation")

    # rotate
    p_rotate = subparsers.add_parser("rotate", help="Rotate to next eligible account")
    p_rotate.add_argument("--tried", action="append", help="Tried account alias (comma-separated or repeated)")
    p_rotate.add_argument("--reason", default=None, help="Reason for rotation")
    p_rotate.add_argument("--force", action="store_true", help="Bypass pool enable check or worker block")

    # doctor
    subparsers.add_parser("doctor", help="Run diagnostic health checks")

    # Internal commands
    p_ensure = subparsers.add_parser("ensure-active", help="Ensure healthy account is active")
    p_ensure.add_argument("--force", action="store_true", help="Bypass worker check")

    p_depleted = subparsers.add_parser("mark-depleted", help="Mark account as depleted/cooldown")
    p_depleted.add_argument("alias", nargs="?", default=None, help="Account alias (defaults to current)")
    p_depleted.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN_SECONDS, help="Cooldown duration")
    p_depleted.add_argument("--reason", default=None, help="Depletion reason")

    p_rot_fail = subparsers.add_parser("rotate-after-failure", help="Mark failed and rotate")
    p_rot_fail.add_argument("alias", nargs="?", default=None, help="Failed account alias")
    p_rot_fail.add_argument("--tried", action="append", help="Tried account aliases")
    p_rot_fail.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN_SECONDS, help="Cooldown duration")
    p_rot_fail.add_argument("--reason", default=None, help="Failure reason")
    p_rot_fail.add_argument("--force", action="store_true", help="Bypass pool enable check")

    p_worker_start = subparsers.add_parser("worker-start", help=argparse.SUPPRESS)
    p_worker_start.add_argument("worker_id")
    p_worker_start.add_argument("--account", default=None)
    p_worker_stop = subparsers.add_parser("worker-stop", help=argparse.SUPPRESS)
    p_worker_stop.add_argument("worker_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        backend = get_backend(
            backend_type=args.backend,
            state_dir=resolve_state_root(args.state_dir),
        )
        manager = AccountManager(state_dir=args.state_dir, backend=backend)

        result: dict[str, Any] = {}

        if args.command == "add":
            result = manager.add(
                alias=args.alias, force=args.force
            )

        elif args.command == "list":
            result = manager.list_accounts()

        elif args.command == "current":
            result = manager.current()

        elif args.command == "switch":
            result = manager.switch(alias=args.alias, force=args.force)

        elif args.command == "remove":
            result = manager.remove(alias=args.alias)

        elif args.command == "enable":
            target = "pool" if args.pool else args.target
            if not target:
                raise AccountManagerError(
                    "Must provide an account alias or --pool to enable.",
                    error_code="MISSING_TARGET",
                    exit_code=EXIT_USAGE,
                )
            result = manager.enable(target)

        elif args.command == "disable":
            target = "pool" if args.pool else args.target
            if not target:
                raise AccountManagerError(
                    "Must provide an account alias or --pool to disable.",
                    error_code="MISSING_TARGET",
                    exit_code=EXIT_USAGE,
                )
            result = manager.disable(target)

        elif args.command == "rotate":
            tried = _parse_tried(args.tried)
            result = manager.rotate(
                tried=tried, reason=args.reason, force=args.force
            )

        elif args.command == "doctor":
            result = manager.doctor()

        elif args.command == "ensure-active":
            result = manager.ensure_active(force=args.force)

        elif args.command == "mark-depleted":
            result = manager.mark_depleted(
                alias=args.alias,
                cooldown_seconds=args.cooldown_seconds,
                reason=args.reason,
            )

        elif args.command == "rotate-after-failure":
            tried = _parse_tried(args.tried)
            result = manager.rotate_after_failure(
                alias=args.alias,
                tried=tried,
                cooldown_seconds=args.cooldown_seconds,
                reason=args.reason,
                force=args.force,
            )

        elif args.command == "worker-start":
            result = manager.worker_start(args.worker_id, args.account)

        elif args.command == "worker-stop":
            result = manager.worker_stop(args.worker_id)

        if args.json:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
        else:
            # Human readable compact output
            action = result.get("action", args.command)
            if action == "list":
                status_str = "ENABLED" if result.get("pool_enabled") else "DISABLED"
                sys.stdout.write(f"Account Pool [rotation: {status_str}]:\n")
                accounts = result.get("accounts", [])
                if not accounts:
                    sys.stdout.write("  (no accounts in pool)\n")
                for acc in accounts:
                    cur = "*" if acc["current"] else " "
                    en = "enabled" if acc["enabled"] else "disabled"
                    sys.stdout.write(
                        f" {cur} {acc['alias']:<15} [{acc['status']:<8}] ({en}) fp:{acc['fingerprint'][:12]}\n"
                    )
            elif action == "current":
                curr = result.get("current")
                if curr:
                    live = "matches live" if result.get("live_matches") else "LIVE DRIFT DETECTED"
                    sys.stdout.write(f"Current account: {curr} ({live})\n")
                else:
                    sys.stdout.write("No active account selected in pool.\n")
            elif action == "switch":
                drift_note = f" (drift saved: {result['drift_backup']})" if result.get("drift_backup") else ""
                sys.stdout.write(f"Switched to account '{result['current']}'{drift_note}.\n")
            elif action == "rotate":
                drift_note = f" (drift saved: {result['drift_backup']})" if result.get("drift_backup") else ""
                sys.stdout.write(f"Rotated from '{result.get('previous')}' to '{result['current']}'{drift_note}.\n")
            elif action == "doctor":
                h_str = "HEALTHY" if result.get("healthy") else "ATTENTION NEEDED"
                sys.stdout.write(f"Polyphony Account Doctor: {h_str}\n")
                sys.stdout.write(f"  Platform: {result.get('platform')}\n")
                sys.stdout.write(f"  Backend: {result.get('backend')}\n")
                sys.stdout.write(f"  Pool Rotation: {'ENABLED' if result.get('pool_enabled') else 'DISABLED'}\n")
                sys.stdout.write(f"  Current: {result.get('current')}\n")
                sys.stdout.write(f"  Total accounts: {result.get('accounts_total')}\n")
                if result.get("switch_blocked"):
                    sys.stdout.write(f"  Switch Blocked: {result.get('switch_block_reason')}\n")
            else:
                sys.stdout.write(f"{action}: success\n")

        return EXIT_OK

    except AccountManagerError as exc:
        if args.json:
            err_dict = {
                "status": "error",
                "error_code": exc.error_code,
                "message": exc.message,
            }
            if exc.extra:
                err_dict.update(exc.extra)
            sys.stdout.write(json.dumps(err_dict, indent=2) + "\n")
        else:
            sys.stderr.write(f"Error ({exc.error_code}): {exc.message}\n")
        return exc.exit_code

    except Exception as exc:
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {
                        "status": "error",
                        "error_code": "UNEXPECTED_ERROR",
                        "message": str(exc),
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            sys.stderr.write(f"Unexpected error: {exc}\n")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
