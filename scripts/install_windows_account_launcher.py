#!/usr/bin/env python3
"""Install the PowerShell/cmd `agy-account` launcher into an existing user PATH entry."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SOURCE = ROOT / "bin" / "agy-account.cmd"
SCRIPT_SOURCE = ROOT / "scripts" / "agy_account.py"


def _norm(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _path_entries() -> set[str]:
    return {_norm(Path(item)) for item in os.environ.get("PATH", "").split(os.pathsep) if item}


def _target_candidates() -> list[Path]:
    override = os.environ.get("POLYPHONY_LAUNCHER_DIR")
    if override:
        return [Path(override).expanduser()]

    path_entries = _path_entries()
    candidates: list[Path] = []

    local_bin = Path.home() / ".local" / "bin"
    if _norm(local_bin) in path_entries:
        candidates.append(local_bin)

    agy = shutil.which("agy.exe") or shutil.which("agy")
    if agy:
        candidates.append(Path(agy).resolve().parent)

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        agy_bin = Path(local_app_data) / "agy" / "bin"
        if _norm(agy_bin) in path_entries:
            candidates.append(agy_bin)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _norm(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _replace_if_changed(source: Path, destination: Path) -> None:
    payload = source.read_bytes()
    try:
        if destination.read_bytes() == payload:
            return
    except FileNotFoundError:
        pass

    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination)
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass


def install() -> Path | None:
    if os.name != "nt" and not os.environ.get("POLYPHONY_LAUNCHER_DIR"):
        return None

    for target in _target_candidates():
        try:
            _replace_if_changed(LAUNCHER_SOURCE, target / "agy-account.cmd")
            _replace_if_changed(SCRIPT_SOURCE, target / "polyphony-agy-account.py")
            return target / "agy-account.cmd"
        except OSError:
            continue
    return None


def main() -> int:
    installed = install()
    if installed is None:
        print("agy-account launcher was not installed: no writable user PATH directory was found", file=sys.stderr)
        return 1
    print(installed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
