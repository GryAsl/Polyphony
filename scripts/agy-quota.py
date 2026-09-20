#!/usr/bin/env python3
"""Read Agy's Gemini 5h/7d quota, cache it, and track user decisions.

The live source is Agy 1.1.28+'s zero-token `/usage` slash command.  The script
deliberately calls the existing delegate wrapper so native Windows still goes
through the vendored ConPTY bridge instead of duplicating its process handling.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


ALERT_THRESHOLDS = (75, 50, 25, 10)
DEFAULT_DEPLETION_THRESHOLD = 2.0
DEFAULT_MAX_AGE = 600
USAGE_RE = re.compile(
    r"^Gemini Models\s+(Weekly|Five Hour) Limit Remaining\s+"
    r"([0-9]+(?:\.[0-9]+)?)%(?:\s+(.*?))?\s*$",
    re.IGNORECASE,
)


def _state_dir() -> Path:
    configured = os.environ.get("AGY_QUOTA_STATE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".antigravity-quota"


def _accounts_root() -> Path:
    configured = os.environ.get("POLYPHONY_ACCOUNTS_DIR") or os.environ.get("POLYPHONY_ACCOUNTS_ROOT")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt" and (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")):
        return Path(os.environ.get("LOCALAPPDATA") or os.environ["APPDATA"]) / "Polyphony" / "accounts"
    return Path.home() / ".local" / "share" / "Polyphony" / "accounts"


def _active_account() -> str | None:
    try:
        state = json.loads((_accounts_root() / "pool.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not state.get("pool_enabled"):
            return None
        alias = state.get("current")
        return str(alias) if alias else None
    except (OSError, ValueError):
        return None


def _state_path(account: str | None = None) -> Path:
    if account:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", account):
            raise ValueError("invalid account alias")
        return _state_dir() / "accounts" / account / "state.json"
    return _state_dir() / "state.json"


def _load_state(account: str | None = None) -> dict[str, Any]:
    try:
        value = json.loads(_state_path(account).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict[str, Any], account: str | None = None) -> None:
    path = _state_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _bash() -> str:
    override = os.environ.get("AGY_GIT_BASH")
    if override:
        return override
    # On Windows, prefer Git Bash before PATH. WSL installs system32\bash.exe,
    # which cannot run this Windows ConPTY wrapper as a Git-Bash script.
    if os.name == "nt":
        roots = (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LocalAppData"),
        )
        suffixes = (
            Path("Git/bin/bash.exe"),
            Path("Git/usr/bin/bash.exe"),
            Path("Programs/Git/bin/bash.exe"),
        )
        for root in filter(None, roots):
            for suffix in suffixes:
                candidate = Path(root) / suffix
                if candidate.is_file():
                    return str(candidate)
    found = shutil.which("bash")
    if found:
        return found
    raise RuntimeError("Git Bash was not found; set AGY_GIT_BASH to bash.exe")


def parse_usage(text: str) -> dict[str, dict[str, Any]]:
    windows: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw_line).strip()
        match = USAGE_RE.match(line)
        if not match:
            continue
        key = "7d" if match.group(1).lower().startswith("weekly") else "5h"
        windows[key] = {
            "remaining": float(match.group(2)),
            "reset_at": match.group(3) or "",
        }
    missing = [key for key in ("5h", "7d") if key not in windows]
    if missing:
        raise ValueError("Agy /usage did not return Gemini " + "/".join(missing) + " quota")
    return windows


def _query_live(raw_file: str | None) -> dict[str, dict[str, Any]]:
    if raw_file:
        return parse_usage(Path(raw_file).read_text(encoding="utf-8"))
    delegate = Path(__file__).with_name("agy-delegate.sh")
    env = os.environ.copy()
    env["AGY_DELEGATE_READ_ONLY"] = "1"
    env["AGY_QUOTA_PROBE"] = "1"
    # Keep the slash command literal when Git Bash eventually launches Windows
    # executables; MSYS path conversion must not turn /usage into C:/.../usage.
    env["MSYS_NO_PATHCONV"] = "1"
    completed = subprocess.run(
        [
            _bash(),
            str(delegate).replace("\\", "/"),
            "--tier", "flash",
            "--mode", "plan",
            "--timeout", os.environ.get("AGY_QUOTA_QUERY_TIMEOUT", "1m"),
            "/usage",
        ],
        cwd=str(delegate.parent.parent),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    # Depending on the Agy build and terminal mode, `/usage` can be rendered on
    # stderr (and a harmless renderer warning can make the process exit non-zero)
    # even though both quota lines are present.  Parse both streams first and
    # accept a complete measurement; only classify it as failed when the data is
    # genuinely missing.  This keeps quota routing from entering a false
    # "depleted/unknown" state because of presentation noise.
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    try:
        return parse_usage(combined)
    except ValueError:
        detail = " ".join(completed.stderr.split())[-400:]
        raise RuntimeError(f"Agy /usage failed with exit {completed.returncode}: {detail}")


def _fresh(state: dict[str, Any], max_age: int) -> bool:
    checked = state.get("checked_epoch")
    windows = state.get("windows")
    return (
        isinstance(checked, (int, float))
        and isinstance(windows, dict)
        and all(key in windows for key in ("5h", "7d"))
        and time.time() - float(checked) <= max_age
    )


def _format_percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return f"{float(value):g}%"


def _env_number(primary: str, secondary: str, default: float, cast):
    raw = os.environ.get(primary, os.environ.get(secondary, str(default)))
    try:
        value = cast(raw)
        return value if value >= 0 else cast(default)
    except (TypeError, ValueError):
        return cast(default)


def _apply_measurement(
    previous: dict[str, Any],
    measured: dict[str, dict[str, Any]],
    depletion_threshold: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    alerts: list[dict[str, Any]] = []
    old_windows = previous.get("windows") if isinstance(previous.get("windows"), dict) else {}
    windows: dict[str, dict[str, Any]] = {}
    for key in ("5h", "7d"):
        remaining = float(measured[key]["remaining"])
        old = old_windows.get(key) if isinstance(old_windows.get(key), dict) else {}
        announced = {
            int(value)
            for value in old.get("announced_thresholds", [])
            if isinstance(value, (int, float)) and int(value) in ALERT_THRESHOLDS
        }
        # A recovering/reset window becomes eligible to announce a threshold again.
        announced = {threshold for threshold in announced if remaining <= threshold}
        newly_crossed = [
            threshold
            for threshold in ALERT_THRESHOLDS
            if remaining <= threshold and threshold not in announced
        ]
        if newly_crossed:
            band = min(newly_crossed)
            announced.update(threshold for threshold in ALERT_THRESHOLDS if remaining <= threshold)
            alerts.append({
                "window": key,
                "threshold": band,
                "remaining": remaining,
                "message": f"Agy Gemini {key} quota has only {band}% remaining.",
            })
        windows[key] = {
            "remaining": remaining,
            "reset_at": str(measured[key].get("reset_at") or ""),
            "announced_thresholds": sorted(announced, reverse=True),
        }

    depleted = any(windows[key]["remaining"] <= depletion_threshold for key in ("5h", "7d"))
    was_depleted = bool(previous.get("depleted"))
    decision = previous.get("decision") if depleted and was_depleted else None
    state = {
        "schema": 1,
        "checked_epoch": time.time(),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "windows": windows,
        "depletion_threshold": depletion_threshold,
        "depleted": depleted,
        "decision": decision if decision in {"sonnet", "wait"} else None,
    }
    return state, alerts


def _payload(state: dict[str, Any], source: str, alerts: list[dict[str, Any]], account: str | None = None) -> dict[str, Any]:
    windows = state.get("windows") if isinstance(state.get("windows"), dict) else {}
    return {
        "status": "DEPLETED" if state.get("depleted") else "AVAILABLE",
        "account_id": account,
        "source": source,
        "depletion_threshold": state.get("depletion_threshold", DEFAULT_DEPLETION_THRESHOLD),
        "gemini": {
            "5h": windows.get("5h", {}),
            "7d": windows.get("7d", {}),
        },
        "decision": state.get("decision"),
        "alerts": alerts,
        "checked_at": state.get("checked_at"),
    }


def _emit(payload: dict[str, Any], json_only: bool, alerts_only: bool) -> None:
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if json_only:
        print(f"AGY_QUOTA {compact}")
        return
    if alerts_only:
        for alert in payload.get("alerts", []):
            print("AGY_QUOTA_ALERT " + json.dumps(alert, ensure_ascii=False, separators=(",", ":")))
        if payload.get("status") == "DEPLETED":
            print(f"AGY_QUOTA_DECISION_REQUIRED {compact}")
        return
    for key in ("5h", "7d"):
        window = payload.get("gemini", {}).get(key, {})
        print(
            f"Agy Gemini {key}: {_format_percent(window.get('remaining'))} remaining"
            f" (resets {window.get('reset_at') or 'unknown'})."
        )
    for alert in payload.get("alerts", []):
        print(alert["message"])
    if payload.get("status") == "DEPLETED":
        print("Agy Gemini quota is depleted; a user decision is required before fallback or waiting.")


def _acquire_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - path.stat().st_mtime > 120:
                path.unlink()
                return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            pass
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Read and track Agy Gemini 5h/7d quota.")
    parser.add_argument("--force", action="store_true", help="Ignore the ten-minute cache.")
    parser.add_argument(
        "--max-age",
        type=int,
        default=_env_number(
            "AGY_QUOTA_MAX_AGE",
            "CLAUDE_PLUGIN_OPTION_QUOTA_CHECK_INTERVAL_SECONDS",
            DEFAULT_MAX_AGE,
            int,
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=_env_number(
            "AGY_QUOTA_THRESHOLD",
            "CLAUDE_PLUGIN_OPTION_QUOTA_DEPLETION_THRESHOLD",
            DEFAULT_DEPLETION_THRESHOLD,
            float,
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit one AGY_QUOTA JSON line.")
    parser.add_argument("--alerts-only", action="store_true")
    parser.add_argument("--state-only", action="store_true", help="Never contact Agy.")
    parser.add_argument("--input", help="Parse saved /usage output instead of calling Agy.")
    parser.add_argument("--decision", choices=("sonnet", "wait", "clear"))
    parser.add_argument("--mark-depleted", action="store_true")
    parser.add_argument("--account", help="Read or update quota state for one saved account alias.")
    parser.add_argument(
        "--active-account",
        action="store_true",
        help="Use the currently active saved account (also the default when one exists).",
    )
    args = parser.parse_args()

    account = _active_account() if args.active_account or not args.account else None
    if args.account:
        account = args.account
    state = _load_state(account)
    if args.decision:
        if args.decision != "clear" and not state.get("depleted"):
            print("agy-quota: no depleted quota state is awaiting a decision", file=sys.stderr)
            return 1
        state["decision"] = None if args.decision == "clear" else args.decision
        _write_state(state, account)
        payload = _payload(state, "state", [], account)
        _emit(payload, args.json, args.alerts_only)
        # Recording an explicit user choice succeeded. A later quota check or
        # delegated Gemini call may still return 10 while the state is depleted.
        return 0

    if args.mark_depleted:
        state.setdefault("schema", 1)
        state.setdefault("checked_epoch", time.time())
        state.setdefault("checked_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        state.setdefault("windows", {})
        state["depletion_threshold"] = args.threshold
        state["depleted"] = True
        state["decision"] = state.get("decision") if state.get("decision") in {"sonnet", "wait"} else None
        _write_state(state, account)
        payload = _payload(state, "failure-signal", [], account)
        _emit(payload, args.json, args.alerts_only)
        return 10

    source = "cache"
    alerts: list[dict[str, Any]] = []
    if not args.state_only and (args.force or args.input or not _fresh(state, args.max_age)):
        lock_path = _state_path(account).with_name("refresh.lock")
        lock_fd = _acquire_lock(lock_path)
        if lock_fd is None:
            deadline = time.time() + 20
            while time.time() < deadline:
                time.sleep(0.25)
                state = _load_state(account)
                if _fresh(state, args.max_age):
                    break
            if not _fresh(state, args.max_age):
                print("agy-quota: another quota refresh did not finish", file=sys.stderr)
                return 2
        else:
            # This is an O_EXCL lockfile protocol, not fcntl/msvcrt advisory locking:
            # the pathname remains the mutex after its descriptor is closed and is
            # removed only in the finally block below.
            os.close(lock_fd)
            try:
                measured = _query_live(args.input)
                state, alerts = _apply_measurement(state, measured, args.threshold)
                _write_state(state, account)
                source = "live"
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                print(f"agy-quota: {exc}", file=sys.stderr)
                return 2
            finally:
                try:
                    lock_path.unlink()
                except OSError:
                    pass

    if not state or not isinstance(state.get("windows"), dict):
        print("agy-quota: no cached quota state is available", file=sys.stderr)
        return 2
    payload = _payload(state, source, alerts, account)
    _emit(payload, args.json, args.alerts_only)
    return 10 if state.get("depleted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
