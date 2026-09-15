#!/usr/bin/env python3
"""Zero-dependency MCP adapter for the repository's existing Agy wrappers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


PROTOCOL_VERSION = "2024-11-05"
PLUGIN_ROOT = Path(
    os.environ.get("PLUGIN_ROOT")
    or os.environ.get("CLAUDE_PLUGIN_ROOT")
    or Path(__file__).resolve().parents[1]
).resolve()
SCRIPT_DIR = Path(
    os.environ.get("ANTIGRAVITY_SCRIPT_DIR") or PLUGIN_ROOT / "scripts"
).resolve()


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

TOOLS = [
    {
        "name": "delegate",
        "description": "Run an Agy worker. Defaults to flash (High); flash-medium is an explicit option for clearly simple work. Both track the newest Flash family.",
        "inputSchema": _object(
            {
                "prompt": {"type": "string"},
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
            {"question": {"type": "string"}, "directory": DIRECTORY, "tier": {"type": "string", "enum": ["flash-medium", "flash"]}, "timeout": DURATION},
            ["question"],
        ),
    },
    {
        "name": "review",
        "description": "Send a selected Git diff directly to a fresh compact Agy reviewer.",
        "inputSchema": _object(
            {
                "goal": {"type": "string"},
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
                "query": {"type": "string"},
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
                "focus": {"type": "string"},
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
                "prompt": {"type": "string"},
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
                "prompt": {"type": "string"},
                "tier": TIER,
                "yolo": {"type": "boolean"},
            },
            ["prompt"],
        ),
    },
]


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
        stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
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


def _dispatch(name: str, args: dict) -> dict:
    instructions = "\n".join(args[k] for k in ("prompt", "question", "query", "goal", "focus")
                             if isinstance(args.get(k), str))
    if len(instructions.split()) >= 800:
        raise ValueError("Agy instructions must be fewer than 800 words in total. Summarize to 200-500 words; do not bypass with files, stdin, or fragmented prompts.")
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
        return _run_shell("agy-job.sh", argv, _cwd(args), stdin_text)

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
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
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
                "serverInfo": {"name": "polyphony", "version": "0.31.40"},
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
