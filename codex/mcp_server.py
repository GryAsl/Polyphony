#!/usr/bin/env python3
"""Zero-dependency MCP adapter for the repository's existing Agy wrappers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any

RUNTIME = Path(__file__).resolve().parents[1] / "scripts" / "polyphony_runtime.py"
sys.path.insert(0, str(RUNTIME.parent))
from polyphony_runtime import Runtime, RuntimeErrorBase, canonical_workspace  # noqa: E402
from polyphony_routing import get_mode as get_routing_mode, set_mode as set_routing_mode  # noqa: E402
from polyphony_capabilities import registered_mcp_tools  # noqa: E402


PROTOCOL_VERSION = "2024-11-05"
PLUGIN_ROOT = Path(
    os.environ.get("PLUGIN_ROOT")
    or os.environ.get("CLAUDE_PLUGIN_ROOT")
    or Path(__file__).resolve().parents[1]
).resolve()
SCRIPT_DIR = Path(
    os.environ.get("ANTIGRAVITY_SCRIPT_DIR") or PLUGIN_ROOT / "scripts"
).resolve()


def _install_windows_account_launcher() -> None:
    """Best-effort host setup; never let launcher installation block MCP startup."""
    if os.name != "nt":
        return
    try:
        from install_windows_account_launcher import install

        install()
    except Exception:
        pass


def _account_state_path() -> Path:
    configured = os.environ.get("POLYPHONY_ACCOUNTS_DIR") or os.environ.get("POLYPHONY_ACCOUNTS_ROOT")
    if configured:
        return Path(configured).expanduser() / "pool.json"
    if os.name == "nt" and (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")):
        return Path(os.environ.get("LOCALAPPDATA") or os.environ["APPDATA"]) / "Polyphony" / "accounts" / "pool.json"
    return Path.home() / ".local" / "share" / "Polyphony" / "accounts" / "pool.json"


def _active_account_id() -> str | None:
    try:
        state = json.loads(_account_state_path().read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not state.get("pool_enabled"):
            return None
        value = state.get("current")
        return str(value) if value else None
    except (OSError, ValueError):
        return None


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


TIER = {"type": "string", "enum": ["flash-medium", "flash", "pro"]}
DURATION = {"type": "string", "description": "Wrapper duration such as 5m or 300s."}
DIRECTORY = {"type": "string", "description": "Repository/workspace directory."}
AGY_INSTRUCTIONS = {
    "type": "string",
    "maxLength": 8000,
    "description": (
        "Use the shortest sufficient worker contract, normally 200-500 words and never more than 800. "
        "Include objective, paths/scope, non-negotiable constraints, acceptance checks, and the "
        "requested compact receipt. Do not paste code/diffs/logs or split one oversized prompt."
    ),
}

TOOLS = [
    {
        "name": "routing_mode",
        "description": "LOCAL CONTROL PLANE. When the user asks in any language to change Agy strict/soft routing, call this tool immediately. Never delegate a mode change to an Agy worker and never require a restart. The workspace preference is authoritative for the current session and future sessions.",
        "inputSchema": _object(
            {
                "action": {"type": "string", "enum": ["get", "set"]},
                "mode": {"type": "string", "enum": ["strict", "soft"]},
                "directory": DIRECTORY,
            },
            ["action", "directory"],
        ),
    },
    {
        "name": "delegate",
        "description": "Run an Agy worker. Defaults to flash (High); flash-medium is an explicit option for clearly simple work. Both track the newest Flash family.",
        "inputSchema": _object(
            {
                "prompt": AGY_INSTRUCTIONS,
                "directory": DIRECTORY,
                "add_dirs": {"type": "array", "items": {"type": "string"}},
                "tier": TIER,
                "model": {"type": "string"},
                "timeout": DURATION,
                "idle_timeout": {"type": "number", "exclusiveMinimum": 0},
                "yolo": {"type": "boolean"},
                "sandbox": {"type": "boolean"},
                "digest": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["accept-edits", "plan"]},
                "continue": {"type": "boolean"},
                "conversation": {"type": "string"},
            },
            ["prompt"],
        ),
    },
    {
        "name": "scout",
        "description": "Run a compact read-only Gemini Flash scout. The shared delegate defaults to High; Medium is explicitly selectable.",
        "inputSchema": _object(
            {"question": AGY_INSTRUCTIONS, "directory": DIRECTORY, "tier": {"type": "string", "enum": ["flash-medium", "flash"]}, "timeout": DURATION},
            ["question"],
        ),
    },
    {
        "name": "review",
        "description": "Send a selected Git diff directly to a fresh compact Agy reviewer.",
        "inputSchema": _object(
            {
                "goal": AGY_INSTRUCTIONS,
                "directory": DIRECTORY,
                "scope": {
                    "type": "string",
                    "enum": ["worktree", "staged", "last", "range"],
                },
                "range": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "adversarial": {"type": "boolean"},
                "tier": TIER,
                "timeout": DURATION,
            },
            ["goal"],
        ),
    },
    {
        "name": "research",
        "description": "Run a compact, URL-bearing web research pass through agy-delegate.",
        "inputSchema": _object(
            {
                "query": AGY_INSTRUCTIONS,
                "tier": TIER,
                "timeout": DURATION,
                "yolo": {"type": "boolean", "description": "Defaults to true because headless web tools require permission."},
            },
            ["query"],
        ),
    },
    {
        "name": "media",
        "description": "Analyze an image, audio, or video file through agy-media.",
        "inputSchema": _object(
            {
                "file": {"type": "string"},
                "focus": AGY_INSTRUCTIONS,
                "output": {"type": "string"},
                "convert": {"type": "boolean"},
                "tier": TIER,
                "timeout": DURATION,
            },
            ["file"],
        ),
    },
    {
        "name": "job",
        "description": "Start, list, inspect, collect, or cancel an agy-job background task.",
        "inputSchema": _object(
            {
                "action": {"type": "string", "enum": ["start", "list", "status", "result", "cancel", "cancel_all"]},
                "job_id": {"type": "string"},
                "prompt": AGY_INSTRUCTIONS,
                "directory": DIRECTORY,
                "tier": TIER,
                "timeout": DURATION,
                "yolo": {"type": "boolean"},
                "digest": {"type": "boolean"},
            },
            ["action"],
        ),
    },
    {
        "name": "quota",
        "description": "Check both Gemini 5h/7d quotas or record the user's explicit Sonnet/wait choice after depletion.",
        "inputSchema": _object(
            {
                "action": {
                    "type": "string",
                    "enum": ["check", "choose_sonnet", "choose_wait", "clear"],
                },
                "force": {"type": "boolean", "description": "Ignore the ten-minute cache when checking."},
            },
            ["action"],
        ),
    },
    {
        "name": "account",
        "description": "Manage the local, explicitly enabled Agy account pool. Credentials remain protected by the platform backend and are never returned.",
        "inputSchema": _object(
            {
                "action": {"type": "string", "enum": ["add", "list", "current", "switch", "remove", "enable", "disable", "rotate", "doctor"]},
                "alias": {"type": "string"},
                "pool": {"type": "boolean", "description": "For enable/disable, target automatic pool rotation instead of one account."},
            },
            ["action"],
        ),
    },
    {
        "name": "trace",
        "description": "Read or audit Agy execution traces through agy-trace.",
        "inputSchema": _object(
            {
                "action": {"type": "string", "enum": ["show", "audit", "last", "raw", "list"]},
                "target": {"type": "string"},
                "count": {"type": "integer", "minimum": 1},
            },
            ["action"],
        ),
    },
    {
        "name": "doctor",
        "description": "Run the plugin's read-only environment and model health check.",
        "inputSchema": _object({}),
    },
    {
        "name": "migrate",
        "description": "Run the existing Claude-to-Antigravity migration utility. Read-only unless --apply is supplied.",
        "inputSchema": _object(
            {
                "arguments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments accepted by agy-migrate.py, for example [\"--only\", \"skills\"].",
                }
            }
        ),
    },
    {
        "name": "cloud_debug",
        "description": "Fetch and compactly diagnose GCP logs through cloud-debug.",
        "inputSchema": _object(
            {
                "service": {"type": "string"},
                "region": {"type": "string"},
                "since": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1},
                "severity": {"type": "string"},
                "resource_type": {"type": "string"},
                "project": {"type": "string"},
                "tier": TIER,
                "print_command": {"type": "boolean"},
            },
            ["service"],
        ),
    },
    {
        "name": "cost",
        "description": "Run the existing token-volume cost comparison wrapper.",
        "inputSchema": _object(
            {
                "prompt": AGY_INSTRUCTIONS,
                "tier": TIER,
                "yolo": {"type": "boolean"},
            },
            ["prompt"],
        ),
    },
    {
        "name": "agent_task",
        "description": "Claim or inspect a persistent Polyphony subagent task. Reuse is restricted to the creating parent agent and workspace.",
        "inputSchema": _object({
            "action": {"type": "string", "enum": ["claim", "heartbeat", "finish", "resume_fallback", "agents", "handoff"]},
            "parent_agent_id": {"type": "string"}, "agent_id": {"type": "string"},
            "task_id": {"type": "string"}, "lease_token": {"type": "string"},
            "workspace": DIRECTORY, "summary": {"type": "string"},
            "parent_task_id": {"type": "string"}, "fresh_agent": {"type": "boolean"},
            "lease_seconds": {"type": "integer", "minimum": 1},
            "status": {"type": "string", "enum": ["completed", "failed", "cancelled"]},
            "checkpoint": {"type": "string"},
            "account_id": {"type": "string"},
            "from_agent_id": {"type": "string"}, "to_agent_id": {"type": "string"},
            "max_hops": {"type": "integer", "minimum": 0},
        }, ["action"]),
    },
    {
        "name": "persistent_delegate",
        "description": "Run one Agy task through the existing delegate wrapper while safely reusing only the creating parent agent's persistent subagent conversation. Use action=start for long tasks, then status/result/cancel with the returned job_id and the same parent_agent_id/workspace; jobs survive MCP request timeouts and can be collected later.",
        "inputSchema": _object({
            "action": {"type": "string", "enum": ["run", "start", "status", "result", "cancel"]}, "job_id": {"type": "string"},
            "prompt": AGY_INSTRUCTIONS, "parent_agent_id": {"type": "string"}, "workspace": DIRECTORY,
            "agent_id": {"type": "string"}, "parent_task_id": {"type": "string"}, "fresh_agent": {"type": "boolean"},
            "tier": TIER, "model": {"type": "string"}, "timeout": DURATION, "idle_timeout": {"type": "number", "exclusiveMinimum": 0},
            "yolo": {"type": "boolean"}, "sandbox": {"type": "boolean"}, "digest": {"type": "boolean"}, "mode": {"type": "string", "enum": ["accept-edits", "plan"]},
            "lease_seconds": {"type": "integer", "minimum": 1},
        }),
    },
    {
        "name": "agent_message",
        "description": "Send, read, or wait for short SQLite-backed messages between persistent Polyphony agents.",
        "inputSchema": _object({
            "action": {"type": "string", "enum": ["send", "inbox", "wait"]},
            "workspace": DIRECTORY, "from_agent": {"type": "string"}, "to_agent": {"type": "string"},
            "message": {"type": "string"}, "message_type": {"type": "string", "enum": ["message", "request", "response", "handoff"]},
            "task_id": {"type": "string"}, "timeout": {"type": "number", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "unread_only": {"type": "boolean"},
        }, ["action"]),
    },
]

_UNREGISTERED_TOOLS = {tool["name"] for tool in TOOLS} - registered_mcp_tools()
if _UNREGISTERED_TOOLS:
    raise RuntimeError(
        "MCP tools missing from the capability registry: "
        + ", ".join(sorted(_UNREGISTERED_TOOLS))
    )


def _bash() -> str:
    override = os.environ.get("AGY_GIT_BASH")
    if override:
        return override
    found = shutil.which("bash")
    if found:
        return found
    if os.name == "nt":
        roots = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LocalAppData"),
        ]
        suffixes = [
            Path("Git/bin/bash.exe"),
            Path("Git/usr/bin/bash.exe"),
            Path("Programs/Git/bin/bash.exe"),
        ]
        for root in filter(None, roots):
            for suffix in suffixes:
                candidate = Path(root) / suffix
                if candidate.is_file():
                    return str(candidate)
    raise FileNotFoundError("Git Bash was not found; set AGY_GIT_BASH to bash.exe")


def _script(name: str) -> Path:
    path = SCRIPT_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"bundled wrapper is missing: {path}")
    return path


def _cwd(args: dict) -> str:
    value = args.get("directory")
    return str(Path(value).resolve()) if value else os.getcwd()


def _run_shell(
    script: str,
    argv: list[str],
    cwd: str | None = None,
    stdin_text: str | None = None,
) -> dict:
    script_path = str(_script(script)).replace("\\", "/")
    completed = subprocess.run(
        [_bash(), script_path, *argv],
        cwd=cwd or os.getcwd(),
        # subprocess.run creates its own PIPE when input= is supplied. Passing
        # stdin=PIPE as well raises ValueError on every live stdin-backed MCP call.
        stdin=subprocess.DEVNULL if stdin_text is None else None,
        input=stdin_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _run_python(script: str, argv: list[str], cwd: str | None = None) -> dict:
    completed = subprocess.run(
        [sys.executable, str(_script(script)), *argv],
        cwd=cwd or os.getcwd(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _runtime(args: list[str], cwd: str | None = None) -> dict:
    completed = subprocess.run(
        [sys.executable, str(RUNTIME), *args], cwd=cwd or os.getcwd(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    return {"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def _flag(argv: list[str], args: dict, key: str, option: str) -> None:
    value = args.get(key)
    if value is not None and value != "":
        argv.extend([option, str(value)])


def _switch(argv: list[str], args: dict, key: str, option: str) -> None:
    if args.get(key):
        argv.append(option)


def _delegate_args(args: dict, include_prompt: bool = True) -> list[str]:
    argv: list[str] = []
    _flag(argv, args, "tier", "--tier")
    _flag(argv, args, "model", "--model")
    _flag(argv, args, "timeout", "--timeout")
    _flag(argv, args, "idle_timeout", "--idle-timeout")
    dirs = list(args.get("add_dirs") or [])
    if args.get("directory"):
        dirs.insert(0, args["directory"])
    for directory in dirs:
        argv.extend(["--dir", str(directory)])
    _switch(argv, args, "yolo", "--yolo")
    _switch(argv, args, "sandbox", "--sandbox")
    _switch(argv, args, "digest", "--digest")
    _flag(argv, args, "mode", "--mode")
    _switch(argv, args, "continue", "--continue")
    _flag(argv, args, "conversation", "--conversation")
    if include_prompt:
        argv.append(str(args.get("prompt") or ""))
    return argv


def _conversation_from_stderr(stderr: str) -> str | None:
    for line in stderr.splitlines():
        if not line.startswith("AGY_USAGE "):
            continue
        try:
            value = json.loads(line[len("AGY_USAGE "):])
        except json.JSONDecodeError:
            continue
        conversation = value.get("conversation_id")
        if conversation:
            return str(conversation)
    return None


def _persistent_delegate(args: dict) -> dict:
    workspace = canonical_workspace(args.get("workspace") or args.get("directory") or os.getcwd())
    parent = str(args.get("parent_agent_id") or "")
    prompt = str(args.get("prompt") or "")
    if not parent or not prompt:
        raise ValueError("persistent_delegate requires prompt and parent_agent_id")
    runtime = Runtime()
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    try:
        lease_seconds = int(args.get("lease_seconds") or 3600)
        account_id = _active_account_id()
        claim = runtime.claim_task(
            parent, workspace, prompt,
            agent_id=args.get("agent_id"),
            parent_task_id=args.get("parent_task_id"),
            fresh_agent=bool(args.get("fresh_agent")),
            lease_seconds=lease_seconds,
            account_id=account_id,
        )

        def heartbeat_loop() -> None:
            worker_runtime = Runtime(runtime.path)
            try:
                while not heartbeat_stop.wait(max(1.0, min(60.0, lease_seconds / 3))):
                    worker_runtime.heartbeat(claim["task_id"], claim["lease_token"], lease_seconds)
            finally:
                worker_runtime.close()

        heartbeat_thread = threading.Thread(target=heartbeat_loop, name="polyphony-task-heartbeat", daemon=True)
        heartbeat_thread.start()
        boundary = f"--- NEW POLYPHONY TASK {claim['task_id']} ---\nPrevious context is background only. Focus on this objective.\n"
        checkpoint = claim.get("checkpoint")
        if checkpoint:
            boundary += f"Checkpoint from the previous conversation generation:\n{checkpoint}\n"
        delegated = dict(args)
        delegated["directory"] = workspace
        delegated["prompt"] = boundary + "\nObjective:\n" + prompt
        if claim.get("conversation_id"):
            delegated["conversation"] = claim["conversation_id"]
        delegated["digest"] = True if args.get("digest") is None else args.get("digest")
        argv = _delegate_args(delegated, include_prompt=False)
        argv.append("-")
        receipt = _run_shell("agy-delegate.sh", argv, workspace, delegated["prompt"])
        resume_failed = bool(claim.get("conversation_id")) and any(
            marker in receipt["stderr"].lower()
            for marker in ("conversation not found", "invalid conversation", "unknown conversation", "cannot resume")
        )
        if resume_failed and receipt["exit_code"] != 0:
            checkpoint = str(claim.get("checkpoint") or "Previous conversation could not be resumed; treat repository state as authoritative.")
            rotated = runtime.resume_fallback(claim["agent_id"], parent, workspace, checkpoint, account_id=account_id)
            claim["conversation_id"] = rotated["conversation_id"]
            claim["conversation_generation"] = rotated["conversation_generation"]
            claim["checkpoint"] = rotated["checkpoint"]
            delegated.pop("conversation", None)
            fallback_boundary = (
                f"--- NEW POLYPHONY TASK {claim['task_id']} ---\n"
                "Previous context is background only. Focus on this objective.\n"
                f"Checkpoint from the previous conversation generation:\n{checkpoint}\n"
            )
            delegated["prompt"] = fallback_boundary + "\nObjective:\n" + prompt
            argv = _delegate_args(delegated, include_prompt=False)
            argv.append("-")
            receipt = _run_shell("agy-delegate.sh", argv, workspace, delegated["prompt"])
            receipt["resume_fallback"] = True
        conversation = _conversation_from_stderr(receipt["stderr"])
        final_account_id = _active_account_id()
        final_generation = claim["conversation_generation"]
        if receipt["exit_code"] == 0 and receipt["stdout"].strip():
            if conversation:
                stored = runtime.set_conversation(
                    claim["agent_id"], parent, workspace, conversation, account_id=final_account_id
                )
                final_generation = stored["conversation_generation"]
            runtime.finish_task(claim["task_id"], claim["lease_token"], "completed")
        else:
            runtime.finish_task(claim["task_id"], claim["lease_token"], "failed")
        receipt["persistent"] = {"task_id": claim["task_id"], "agent_id": claim["agent_id"], "account_id": final_account_id, "conversation_id": conversation or (claim.get("conversation_id") if final_account_id == account_id else None), "conversation_generation": final_generation}
        return receipt
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        runtime.close()


def _persistent_job_dir() -> Path:
    configured = os.environ.get("POLYPHONY_MCP_JOBS_DIR")
    if configured:
        root = Path(configured).expanduser()
    elif os.name == "nt":
        profile = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()
        root = Path(profile) / "Polyphony" / "mcp-jobs"
    else:
        runtime = Runtime()
        try:
            path = runtime.path
        finally:
            runtime.close()
        root = Path(path).parent / "mcp-jobs" if str(path) != ":memory:" else Path.home() / ".polyphony" / "mcp-jobs"
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    return root


def _write_private(path: Path, value: str, *, atomic: bool = False) -> None:
    target = path.with_suffix(path.suffix + ".tmp") if atomic else path
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if atomic:
        os.replace(target, path)


def _job_owner(job_id: str, args: dict) -> dict:
    owner_path = _persistent_job_file(job_id, "owner")
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("persistent_delegate job not found") from None
    parent = str(args.get("parent_agent_id") or "")
    workspace = canonical_workspace(args.get("workspace") or args.get("directory") or os.getcwd())
    if not parent or owner.get("parent_agent_id") != parent or owner.get("workspace") != workspace:
        raise ValueError("persistent_delegate job ownership mismatch")
    return owner


def _job_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _mark_job_failed(job_id: str, message: str) -> dict:
    receipt = {"exit_code": 1, "stdout": "", "stderr": message}
    try:
        _persistent_job_file(job_id, "request").unlink(missing_ok=True)
    except OSError:
        pass
    _write_private(_persistent_job_file(job_id, "result"), json.dumps(receipt, ensure_ascii=False), atomic=True)
    return receipt


def _enable_windows_kill_on_worker_exit() -> Any:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def _persistent_job_file(job_id: str, suffix: str) -> Path:
    import re
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("invalid persistent_delegate job_id")
    return _persistent_job_dir() / f"{job_id}.{suffix}"


def _persistent_job_worker(job_id: str) -> int:
    request_path = _persistent_job_file(job_id, "request")
    result_path = _persistent_job_file(job_id, "result")
    pid_path = _persistent_job_file(job_id, "pid")
    _write_private(pid_path, str(os.getpid()))
    if _persistent_job_file(job_id, "cancelled").exists() or not request_path.exists():
        return 0
    process_job = None
    try:
        try:
            process_job = _enable_windows_kill_on_worker_exit()
        except OSError:
            # Some hosts already place children in a non-nestable Windows Job.
            # The explicit cancel path still terminates the worker tree.
            process_job = None
        args = json.loads(request_path.read_text(encoding="utf-8"))
        try:
            request_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"could not remove private request file before running: {exc}") from exc
        receipt = _persistent_delegate(args)
    except BaseException as exc:
        receipt = {"exit_code": 1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}
    _write_private(result_path, json.dumps(receipt, ensure_ascii=False), atomic=True)
    if process_job:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle(process_job)
    return 0


def _persistent_job(args: dict) -> dict:
    action = str(args.get("action") or "run")
    if action == "run":
        return _persistent_delegate(args)
    job_id = str(args.get("job_id") or "")
    if action == "start":
        if not args.get("prompt") or not args.get("parent_agent_id"):
            raise ValueError("persistent_delegate start requires prompt and parent_agent_id")
        import uuid
        job_id = uuid.uuid4().hex
        owner_workspace = canonical_workspace(args.get("workspace") or args.get("directory") or os.getcwd())
        _persistent_job_dir()
        request_path = _persistent_job_file(job_id, "request")
        _write_private(_persistent_job_file(job_id, "owner"), json.dumps({"parent_agent_id": str(args["parent_agent_id"]), "workspace": owner_workspace}, ensure_ascii=False))
        _write_private(request_path, json.dumps({key: value for key, value in args.items() if key not in {"action", "job_id"}}, ensure_ascii=False))
        command = [sys.executable, str(Path(__file__).resolve()), "--persistent-delegate-worker", job_id]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(command, cwd=args.get("workspace") or args.get("directory") or os.getcwd(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=(os.name != "nt"))
        except BaseException:
            request_path.unlink(missing_ok=True)
            _persistent_job_file(job_id, "owner").unlink(missing_ok=True)
            raise
        _write_private(_persistent_job_file(job_id, "pid"), str(process.pid))
        return {"exit_code": 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": "running", "pid": process.pid}}
    if not job_id:
        raise ValueError(f"persistent_delegate {action} requires job_id")
    if action not in {"status", "result", "cancel"}:
        raise ValueError("persistent_delegate action must be run, start, status, result, or cancel")
    _job_owner(job_id, args)
    result_path = _persistent_job_file(job_id, "result")
    request_path = _persistent_job_file(job_id, "request")
    cancelled_path = _persistent_job_file(job_id, "cancelled")
    if action in {"status", "result"}:
        if result_path.exists():
            receipt = json.loads(result_path.read_text(encoding="utf-8"))
            status = "completed" if int(receipt.get("exit_code", 1)) == 0 else "failed"
            return {"exit_code": int(receipt.get("exit_code", 1)) if action == "result" else 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": status, "result": receipt if action == "result" else None}}
        if cancelled_path.exists():
            return {"exit_code": 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": "cancelled"}}
        pid_path = _persistent_job_file(job_id, "pid")
        try:
            pid = int(pid_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            pid = 0
        if pid and _job_pid_alive(pid):
            return {"exit_code": 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": "running"}}
        try:
            age = time.time() - _persistent_job_file(job_id, "owner").stat().st_mtime
        except OSError:
            age = 31
        if not pid and request_path.exists() and age < 30:
            return {"exit_code": 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": "running"}}
        failure = _mark_job_failed(job_id, "Persistent delegate worker exited before saving a result.")
        return {"exit_code": failure["exit_code"] if action == "result" else 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": "failed", "result": failure if action == "result" else None}}
    if action == "cancel":
        if result_path.exists():
            receipt = json.loads(result_path.read_text(encoding="utf-8"))
            status = "completed" if int(receipt.get("exit_code", 1)) == 0 else "failed"
            return {"exit_code": 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": status}}
        pid_path = _persistent_job_file(job_id, "pid")
        try:
            pid = int(pid_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            pid = 0
        if cancelled_path.exists():
            return {"exit_code": 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": "cancelled"}}
        if not request_path.exists() and not (pid and _job_pid_alive(pid)):
            raise ValueError("persistent_delegate job not found")
        request_path.unlink(missing_ok=True)
        _write_private(cancelled_path, "cancelled")
        # A final receipt may have won the race while cancellation was being
        # recorded. Preserve it as the authoritative terminal outcome.
        if result_path.exists():
            return _persistent_job({**args, "action": "status"})
        if pid:
            if os.name == "nt":
                if _job_pid_alive(pid):
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                else:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        return {"exit_code": 0, "stdout": "", "stderr": "", "persistent_job": {"job_id": job_id, "status": "cancelled"}}
    raise ValueError("persistent_delegate action must be run, start, status, result, or cancel")


def _dispatch(name: str, args: dict) -> dict:
    instructions = "\n".join(args[k] for k in ("prompt", "question", "query", "goal", "focus")
                             if isinstance(args.get(k), str))
    if len(instructions) > 8000 or len(instructions.split()) > 800:
        raise ValueError(
            "Agy instructions may use at most 800 words and 8,000 characters. Do not retry the "
            "same draft in chunks: rewrite it to the shortest sufficient contract, normally 200-500 words, using only "
            "objective, paths/scope, non-negotiable constraints, acceptance checks, and a compact receipt."
        )
    if name == "routing_mode":
        action = str(args.get("action") or "")
        directory = args.get("directory") or os.getcwd()
        if action == "get":
            return get_routing_mode(directory)
        if action == "set":
            if not args.get("mode"):
                raise ValueError("routing_mode set requires mode")
            return set_routing_mode(str(args["mode"]), directory)
        raise ValueError("routing_mode action must be get or set")

    if name == "delegate":
        argv = _delegate_args(args, include_prompt=False)
        argv.append("-")
        return _run_shell("agy-delegate.sh", argv, _cwd(args), str(args.get("prompt") or ""))

    if name == "scout":
        argv = ["--dir", str(args.get("directory") or os.getcwd())]
        _flag(argv, args, "tier", "--tier")
        _flag(argv, args, "timeout", "--timeout")
        argv.append("-")
        return _run_shell("agy-scout.sh", argv, _cwd(args), str(args.get("question") or ""))

    if name == "review":
        goal = str(args.get("goal") or "")
        argv = ["--dir", str(args.get("directory") or os.getcwd()), "--goal-stdin"]
        scope = args.get("scope", "worktree")
        if scope == "range":
            if not args.get("range"):
                raise ValueError("review scope 'range' requires range")
            argv.extend(["--range", str(args["range"])])
        else:
            argv.append(f"--{scope}")
        for path in args.get("paths") or []:
            argv.extend(["--path", str(path)])
        _switch(argv, args, "adversarial", "--adversarial")
        _flag(argv, args, "tier", "--tier")
        _flag(argv, args, "timeout", "--timeout")
        return _run_shell("agy-review.sh", argv, _cwd(args), goal)

    if name == "research":
        query = str(args.get("query") or "")
        prompt = (
            f"Web-search this topic: {query}\n\n"
            "Return a compact evidence digest. For every factual finding include the exact source URL "
            "and publication date. Separate supported findings, conflicts/gaps, and a one-sentence digest."
        )
        delegated = dict(args)
        delegated["prompt"] = prompt
        delegated["digest"] = True
        delegated["yolo"] = args.get("yolo", True)
        argv = _delegate_args(delegated, include_prompt=False)
        argv.append("-")
        return _run_shell("agy-delegate.sh", argv, os.getcwd(), prompt)

    if name == "media":
        argv = [str(args.get("file") or "")]
        if args.get("focus"):
            argv.append("--focus-stdin")
        _flag(argv, args, "output", "--out")
        _switch(argv, args, "convert", "--convert")
        _flag(argv, args, "tier", "--tier")
        _flag(argv, args, "timeout", "--timeout")
        focus = str(args.get("focus") or "") if args.get("focus") else None
        return _run_shell("agy-media.sh", argv, stdin_text=focus)

    if name == "job":
        action = str(args.get("action") or "")
        argv = [action]
        if action == "start":
            if not args.get("prompt"):
                raise ValueError("job start requires prompt")
            argv.extend(_delegate_args(args, include_prompt=False))
            argv.append("-")
        elif action in {"status", "result", "cancel"}:
            if not args.get("job_id"):
                raise ValueError(f"job {action} requires job_id")
            argv.append(str(args["job_id"]))
        elif action == "cancel_all":
            argv = ["cancel-all"]
        stdin_text = str(args.get("prompt") or "") if action == "start" else None
        receipt = _run_shell("agy-job.sh", argv, _cwd(args), stdin_text)
        if action == "start" and receipt.get("exit_code") == 0:
            for line in str(receipt.get("stdout") or "").splitlines():
                if line.strip():
                    receipt["job_id"] = line.strip().split()[-1]
                    break
        return receipt

    if name == "quota":
        action = str(args.get("action") or "")
        if action == "check":
            argv = ["--json"]
            _switch(argv, args, "force", "--force")
        elif action == "choose_sonnet":
            argv = ["--decision", "sonnet", "--json"]
        elif action == "choose_wait":
            argv = ["--decision", "wait", "--json"]
        elif action == "clear":
            argv = ["--decision", "clear", "--json"]
        else:
            raise ValueError("quota action must be check, choose_sonnet, choose_wait, or clear")
        return _run_python("agy-quota.py", argv)

    if name == "account":
        action = str(args.get("action") or "")
        if action not in {"add", "list", "current", "switch", "remove", "enable", "disable", "rotate", "doctor"}:
            raise ValueError("unsupported account action")
        argv = ["--json", action]
        alias = str(args.get("alias") or "")
        if action in {"switch", "remove"} and not alias:
            raise ValueError(f"account {action} requires alias")
        if action == "add" and alias:
            argv.append(alias)
        elif action in {"switch", "remove"}:
            argv.append(alias)
        elif action in {"enable", "disable"}:
            if args.get("pool"):
                argv.append("--pool")
            elif alias:
                argv.append(alias)
            else:
                raise ValueError(f"account {action} requires alias or pool=true")
        return _run_python("agy_account.py", argv)

    if name == "trace":
        action = str(args.get("action") or "")
        target = args.get("target")
        if action == "show":
            if not target:
                raise ValueError("trace show requires target")
            argv = [str(target)]
        elif action == "last":
            argv = ["--last"]
        elif action == "list":
            argv = ["--list"]
            if args.get("count") is not None:
                argv.append(str(args["count"]))
        else:
            if not target:
                raise ValueError(f"trace {action} requires target")
            argv = [f"--{action}", str(target)]
        return _run_shell("agy-trace.sh", argv)

    if name == "doctor":
        return _run_shell("doctor.sh", [])

    if name == "migrate":
        return _run_python("agy-migrate.py", [str(v) for v in args.get("arguments") or []])

    if name == "cloud_debug":
        argv = ["--service", str(args.get("service") or "")]
        for key, option in (
            ("region", "--region"),
            ("since", "--since"),
            ("limit", "--limit"),
            ("severity", "--severity"),
            ("resource_type", "--resource-type"),
            ("project", "--project"),
            ("tier", "--tier"),
        ):
            _flag(argv, args, key, option)
        _switch(argv, args, "print_command", "--print-command")
        return _run_shell("cloud-debug.sh", argv)

    if name == "cost":
        argv: list[str] = []
        _flag(argv, args, "tier", "--tier")
        _switch(argv, args, "yolo", "--yolo")
        argv.append("-")
        return _run_shell("agy-cost-compare.sh", argv, stdin_text=str(args.get("prompt") or ""))

    if name == "agent_task":
        action = str(args.get("action") or "")
        workspace = str(args.get("workspace") or os.getcwd())
        if action == "claim":
            argv = ["claim", "--parent", str(args.get("parent_agent_id") or ""), "--workspace", workspace, "--summary", str(args.get("summary") or "")]
            for key, option in (("agent_id", "--agent"), ("parent_task_id", "--parent-task"), ("lease_seconds", "--lease")):
                _flag(argv, args, key, option)
            _switch(argv, args, "fresh_agent", "--fresh-agent")
            _flag(argv, args, "account_id", "--account")
        elif action == "heartbeat":
            argv = ["heartbeat", str(args.get("task_id") or ""), str(args.get("lease_token") or "")]
            _flag(argv, args, "lease_seconds", "--lease")
        elif action == "finish":
            argv = ["finish", str(args.get("task_id") or ""), str(args.get("lease_token") or "")]
            _flag(argv, args, "status", "--status")
        elif action == "resume_fallback":
            argv = ["resume-fallback", "--agent", str(args.get("agent_id") or ""), "--parent", str(args.get("parent_agent_id") or ""), "--workspace", workspace, "--checkpoint", str(args.get("checkpoint") or "")]
            _flag(argv, args, "account_id", "--account")
        elif action == "agents":
            argv = ["agents", "--workspace", workspace]
            _flag(argv, args, "parent_agent_id", "--parent")
        elif action == "handoff":
            argv = ["handoff", str(args.get("task_id") or ""), "--from", str(args.get("from_agent_id") or ""), "--to", str(args.get("to_agent_id") or ""), "--token", str(args.get("lease_token") or "")]
            _flag(argv, args, "lease_seconds", "--lease")
            _flag(argv, args, "max_hops", "--max-hops")
        else:
            raise ValueError("agent_task action must be claim, heartbeat, finish, resume_fallback, agents, or handoff")
        return _runtime(argv, workspace)

    if name == "persistent_delegate":
        return _persistent_job(args)

    if name == "agent_message":
        action = str(args.get("action") or "")
        workspace = str(args.get("workspace") or os.getcwd())
        if action == "send":
            argv = ["send", "--workspace", workspace, "--from", str(args.get("from_agent") or ""), "--to", str(args.get("to_agent") or ""), "--message", str(args.get("message") or "")]
            _flag(argv, args, "task_id", "--task"); _flag(argv, args, "message_type", "--type")
        elif action == "inbox":
            argv = ["inbox", "--workspace", workspace, "--to", str(args.get("to_agent") or "")]
            _flag(argv, args, "from_agent", "--from"); _flag(argv, args, "task_id", "--task"); _flag(argv, args, "limit", "--limit")
            if args.get("unread_only") is False: argv.append("--all")
        elif action == "wait":
            argv = ["wait", "--workspace", workspace, "--to", str(args.get("to_agent") or "")]
            _flag(argv, args, "from_agent", "--from"); _flag(argv, args, "task_id", "--task"); _flag(argv, args, "timeout", "--timeout")
        else:
            raise ValueError("agent_message action must be send, inbox, or wait")
        return _runtime(argv, workspace)

    raise KeyError(name)


def _send(obj: dict) -> None:
    payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(payload)
        stream.flush()
    else:
        sys.stdout.write(payload.decode("utf-8"))
        sys.stdout.flush()


def _tool_result(req_id: Any, name: str, args: dict) -> dict:
    try:
        receipt = _dispatch(name, args)
        failed = receipt["exit_code"] != 0
    except (FileNotFoundError, KeyError, TypeError, ValueError, RuntimeErrorBase) as exc:
        receipt = {"exit_code": 1, "stdout": "", "stderr": str(exc)}
        failed = True
    text = json.dumps(receipt, ensure_ascii=False)
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": receipt,
            "isError": failed,
        },
    }


def handle_request(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        # Negotiate only a protocol version this adapter actually implements.
        # Echoing an unknown client version makes otherwise healthy Claude/Codex
        # MCP sessions close immediately during the handshake.
        protocol_version = PROTOCOL_VERSION if requested != PROTOCOL_VERSION else requested
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "polyphony", "version": "0.31.68"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return _tool_result(req_id, str(params.get("name") or ""), params.get("arguments") or {})
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--persistent-delegate-worker":
        return _persistent_job_worker(sys.argv[2])
    _install_windows_account_launcher()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }
        if response is not None:
            _send(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
