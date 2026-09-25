#!/usr/bin/env python3
"""Authoritative, dependency-free routing-mode control plane for Polyphony."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any


VALID_MODES = {"strict", "soft"}


def state_dir() -> Path:
    configured = os.environ.get("AGY_ROUTING_STATE_DIR")
    if configured:
        root = Path(configured).expanduser()
    else:
        root = None
        for env_var in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
            value = os.environ.get(env_var)
            if value:
                root = Path(value).expanduser() / "agy-routing"
                break
        if root is None:
            try:
                root = Path.home() / ".claude-agy-routing"
            except Exception:
                root = Path(tempfile.gettempdir()) / "claude-agy-routing"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_workspace_root(directory: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(directory).expanduser() if directory else Path.cwd()
    try:
        resolved = candidate.resolve()
    except Exception:
        resolved = candidate.absolute()
    if resolved.is_file():
        resolved = resolved.parent
    current = resolved
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return resolved
        current = parent


def workspace_state_path(directory: str | os.PathLike[str] | None = None) -> Path:
    workspace = resolve_workspace_root(directory)
    normalized = os.path.normcase(str(workspace))
    safe_id = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:24]
    return state_dir() / f"ws-{safe_id}.json"


def get_mode(directory: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    workspace = resolve_workspace_root(directory)
    path = workspace_state_path(workspace)
    mode = "soft"
    explicit = False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stored = payload.get("mode") if isinstance(payload, dict) else None
        if stored in VALID_MODES:
            mode = stored
            explicit = True
    except (OSError, ValueError, TypeError):
        pass
    return {
        "exit_code": 0,
        "mode": mode,
        "explicit": explicit,
        "workspace": str(workspace),
        "state_path": str(path),
    }


def set_mode(mode: str, directory: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    normalized = str(mode).strip().lower()
    if normalized not in VALID_MODES:
        raise ValueError("mode must be 'strict' or 'soft'")
    workspace = resolve_workspace_root(directory)
    path = workspace_state_path(workspace)
    payload = {
        "mode": normalized,
        "workspace": str(workspace),
        "updated_at": time.time(),
    }
    temporary = path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    receipt = get_mode(workspace)
    if receipt["mode"] != normalized or not receipt["explicit"]:
        raise OSError("routing mode persistence verification failed")
    receipt["changed"] = True
    receipt["message"] = f"Agy routing mode is now {normalized}."
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read or set Polyphony's workspace routing mode.")
    parser.add_argument("action", choices=("get", "set"))
    parser.add_argument("mode", nargs="?", choices=tuple(sorted(VALID_MODES)))
    parser.add_argument("--directory", "--dir", dest="directory")
    args = parser.parse_args(argv)
    if args.action == "set" and not args.mode:
        parser.error("set requires strict or soft")
    result = set_mode(args.mode, args.directory) if args.action == "set" else get_mode(args.directory)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
