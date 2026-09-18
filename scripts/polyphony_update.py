#!/usr/bin/env python3
"""Best-effort, throttled Polyphony release checks.

This module deliberately only checks and reports. It never updates a plugin or
executes a command; the host agent must obtain explicit user approval first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


REPOSITORY = "GryAsl/Polyphony"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RAW_MANIFEST_URL = f"https://raw.githubusercontent.com/{REPOSITORY}/master/.claude-plugin/plugin.json"
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_FAILURE_RETRY_SECONDS = 15 * 60
DEFAULT_TIMEOUT_SECONDS = 4
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")


def parse_version(value: Any) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _plugin_root(plugin_root: str | Path | None) -> Path:
    if plugin_root:
        return Path(plugin_root).expanduser()
    for name in ("POLYPHONY_PLUGIN_ROOT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return Path(__file__).resolve().parents[1]


def installed_version(plugin_root: str | Path | None = None) -> str | None:
    root = _plugin_root(plugin_root)
    for relative in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        try:
            manifest = json.loads((root / relative).read_text(encoding="utf-8"))
            version = str(manifest.get("version", "")).strip()
            if parse_version(version):
                return version
        except (OSError, ValueError, TypeError):
            continue
    return None


def state_path() -> Path:
    configured = os.environ.get("POLYPHONY_UPDATE_STATE_FILE")
    if configured:
        return Path(configured).expanduser()
    configured_dir = os.environ.get("POLYPHONY_UPDATE_STATE_DIR")
    if configured_dir:
        return Path(configured_dir).expanduser() / "update-check.json"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Polyphony" / "update-check.json"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "polyphony" / "update-check.json"


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(value: dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def _number_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


def _fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Polyphony-update-check"})
    with urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("GitHub returned a non-object response")
    return value


def _latest_release(timeout: float) -> tuple[str, str]:
    try:
        payload = _fetch_json(LATEST_RELEASE_URL, timeout)
        version = str(payload.get("tag_name", "")).strip()
        if parse_version(version):
            return version.lstrip("v"), f"https://github.com/{REPOSITORY}/releases/tag/{version}"
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        pass

    payload = _fetch_json(RAW_MANIFEST_URL, timeout)
    version = str(payload.get("version", "")).strip()
    if not parse_version(version):
        raise ValueError("GitHub manifest has no valid Polyphony version")
    return version.lstrip("v"), f"https://github.com/{REPOSITORY}/releases"


def check_for_update(plugin_root: str | Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Return a compact status dict and persist a check at most once per interval."""

    current_time = time.time() if now is None else float(now)
    current = installed_version(plugin_root)
    state = _read_state()
    interval = _number_env("POLYPHONY_UPDATE_CHECK_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
    failure_interval = _number_env("POLYPHONY_UPDATE_FAILURE_RETRY_SECONDS", DEFAULT_FAILURE_RETRY_SECONDS)
    last_checked = float(state.get("last_checked_at", 0) or 0)
    retry_interval = failure_interval if state.get("error") else interval
    if current and current == state.get("installed_version") and current_time - last_checked < retry_interval:
        latest = str(state.get("latest_version", ""))
        available = bool(parse_version(current) and parse_version(latest) and parse_version(latest) > parse_version(current))
        return {"checked": False, "available": available, "current": current, "latest": latest, "url": state.get("latest_url", "")}

    if not current:
        return {"checked": False, "available": False, "current": None, "latest": None, "url": ""}

    try:
        latest, url = _latest_release(_number_env("POLYPHONY_UPDATE_CHECK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except Exception as exc:
        state.update({"last_checked_at": current_time, "installed_version": current, "error": str(exc)[:200]})
        _write_state(state)
        return {"checked": True, "available": False, "current": current, "latest": None, "url": "", "error": str(exc)[:200]}

    latest_tuple = parse_version(latest)
    current_tuple = parse_version(current)
    available = bool(latest_tuple and current_tuple and latest_tuple > current_tuple)
    previous_notification = state.get("notified_version")
    notify = available and previous_notification != latest
    state.update({
        "last_checked_at": current_time,
        "installed_version": current,
        "latest_version": latest,
        "latest_url": url,
        "error": "",
        "notified_version": latest if notify else previous_notification,
    })
    if not available:
        state["notified_version"] = ""
    _write_state(state)
    return {"checked": True, "available": available, "notify": notify, "current": current, "latest": latest, "url": url}


if __name__ == "__main__":
    print(json.dumps(check_for_update(), ensure_ascii=False))
