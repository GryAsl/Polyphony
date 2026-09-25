#!/usr/bin/env python3
"""Two-mode Agy routing enforcement and advisory reminder for Claude Code and Codex.

Supports:
- Always use Agy (strict): Gated native substantive tool execution, mandatory Agy delegation.
- Use Agy when appropriate (soft): Non-blocking advisory reminders, once per category per turn.
- Default: Sessions begin in soft mode and never require a routing question.
- Stop enforcement: Requires successful completed Agy work only on substantive strict turns,
  with bounded local exceptions and loop protection.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import time

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
try:
    from polyphony_update import check_for_update
except Exception:  # Update checks are best-effort and must never break routing.
    check_for_update = None


APPROVED_AGY_WRAPPERS = {
    "agy",
    "agy-scout",
    "agy-delegate",
    "agy-review",
    "agy-job",
    "agy-doctor",
    "agy-trace",
    "agy-quota",
    "agy-account",
    "agy-media",
    "agy-migrate",
    "agy-cost-compare",
    "cloud-debug",
    "polyphony-agent",
}

SHELL_TOOL_NAMES = {
    "bash",
    "powershell",
    "exec_command",
    "functions.exec",
    "exec",
}

REJECTED_RESPONSE_STATUSES = {
    "failed",
    "error",
    "running",
    "pending",
    "async",
    "timeout",
    "timed_out",
    "timedout",
    "cancelled",
    "canceled",
}

# Claude Code can return a successful launcher response for a background Bash
# task before the Agy worker has produced any output.  These markers are the
# only signals we treat as "still in flight"; an unmarked empty response stays
# a real failure so quota/permission/worker errors remain fail-closed.
PENDING_RESPONSE_STATUSES = {
    "queued",
    "starting",
    "started",
    "running",
    "pending",
    "in_progress",
    "in-progress",
    "async",
    "asynchronous",
}
TERMINAL_RESPONSE_STATUSES = {
    "complete",
    "completed",
    "done",
    "success",
    "succeeded",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "timeout",
    "timed_out",
    "timed-out",
}
ASYNC_FLAG_KEYS = {
    "run_in_background",
    "runInBackground",
    "background",
    "backgrounded",
    "async",
    "asynchronous",
}
ASYNC_STATUS_KEYS = {"status", "state", "phase"}
ASYNC_TASK_ID_KEYS = {
    "task_id",
    "taskId",
    "background_task_id",
    "backgroundTaskId",
    "job_id",
    "jobId",
}
BACKGROUND_RESULT_TOOLS = {
    "taskoutput",
    "backgroundtaskoutput",
    "backgroundresult",
    "taskresult",
}

MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    ".mp3", ".wav", ".m4a", ".flac", ".mp4", ".mov", ".avi", ".webm",
}

POLICY_FILES = {"claude.md", "agents.md"}

# Keep inline Agy commands below the practical Windows `bash -c`/CreateProcess
# budget.  The character ceiling is authoritative (quotes, paths, and shell
# escaping count too); the word ceiling gives agents a useful prompt-level
# signal before a huge code dump reaches that limit. Authored instructions must
# stay at or below 800 words even when assembled in a task file or delivered via stdin.
# The lower 200-500 word norm is injected before the agent drafts a tool
# call; this hard boundary exists so a high-effort conductor cannot send a huge
# contract and only discover the problem after shell parsing or model launch.
DEFAULT_AGY_PROMPT_MAX_CHARS = 8000
DEFAULT_AGY_PROMPT_MAX_WORDS = 800
AGY_PROMPT_DISCIPLINE = (
    "[Polyphony HARD Agy prompt gate] Before drafting any Agy/Gemini worker request, "
    "use the shortest sufficient contract, normally 200-500 words; it may use at most 800 words "
    "and 8,000 characters. The upper bound is not a target. Include only objective, relevant paths/scope, non-negotiable constraints, "
    "acceptance checks, and the requested compact receipt. Do not paste code, diffs, logs, long "
    "background, or a step-by-step implementation plan: the worker must inspect referenced files. "
    "Never split or incrementally write one oversized prompt to evade the gate; use separate workers "
    "only for genuinely independent outcomes."
)

# Strict is a routing guarantee for substantive work, not a blanket host lock.
# Permit a very small, single-file conductor operation without Agy so the main
# agent can inspect or correct a bounded detail without entering a delegation
# loop. Broad discovery, writes, tests, Git, networking and native agents remain
# gated exactly as before.
STRICT_NATIVE_MAX_OPS = 3
STRICT_NATIVE_MAX_READ_LINES = 200
STRICT_NATIVE_MAX_GREP_RESULTS = 50
STRICT_NATIVE_MAX_EDIT_CHARS = 800

CLAUDE_ONLY_TOOLS = {
    "askuserquestion", "enterplanmode", "exitplanmode", "skill", "toolsearch",
    "todowrite", "taskcreate", "taskget", "tasklist", "taskoutput", "taskstop",
    "taskupdate", "schedulewakeup",
}

# Claude's AskUserQuestion answer is delivered as a tool result rather than a
# normal UserPromptSubmit in the desktop app. Keep this separate from the
# substantive-tool classifier: recording a routing choice is control-plane
# state, not work that must itself be delegated.
MODE_SELECTION_TOOLS = {
    "askuserquestion",
}

REMINDERS = {
    "discovery": (
        "depo/kaynak keşfi veya kod-doküman okuması yapıyorsun",
        "`agy-scout --dir <repo> \"net soru ve kompakt file:line digest isteği\"`",
    ),
    "implementation": (
        "dosya veya kod değişikliği yapıyorsun",
        "varsayılan High `--tier flash` ile, yalnız açıkça basit işler için seçilebilen `--tier flash-medium` ile sınırları ve kabul ölçütleri belirlenmiş bir `agy-delegate` worker'ı",
    ),
    "review": (
        "diff, değişiklik veya kod incelemesi yapıyorsun",
        "taze bir `agy-review` (gerekirse küçük mantıksal path grupları halinde)",
    ),
    "verification": (
        "test/build/lint çalıştırıyor ya da uzun log ve hata çıktısı inceliyorsun",
        "kanıtı dosyada tutan bir Flash `agy-delegate` doğrulayıcısı ve yalnızca kompakt verdict",
    ),
    "git": (
        "Git durumlandırma, commit veya push işi yapıyorsun",
        "ilgili dosyalarla sınırlandırılmış tek bir Flash `agy-delegate` Git worker'ı",
    ),
    "research": (
        "web veya harici kaynak araştırması yapıyorsun",
        "kaynak/alıntı koşulları verilmiş bir Flash `agy-delegate` araştırmacısı",
    ),
    "media": (
        "görsel, ses veya video içeriği inceliyorsun",
        "dosya yolları ve sorular verilmiş multimodal Flash `agy-delegate` worker'ı",
    ),
    "native_agent": (
        "native Claude subagent oluşturuyorsun",
        "iş read-only ise `agy-scout`, yazma işi ise `agy-delegate`, inceleme ise `agy-review`",
    ),
    "terminal": (
        "Claude üzerinden genel bir terminal/otomasyon işi yürütüyorsun",
        "kesin kapsamlı bir Flash `agy-delegate` worker'ı",
    ),
    "external": (
        "harici/MCP aracıyla AGY'ye devredilebilecek bir işlem yürütüyorsun",
        "aynı erişim AGY ortamında varsa Flash `agy-delegate` worker'ı",
    ),
}

DENIAL_INSTRUCTIONS = {
    "discovery": "Strict mode: broad repository, code, and document discovery must use `agy-scout` (or Codex `mcp__antigravity__scout`). This call is outside the bounded single-file native allowance.",
    "implementation": "Strict mode: substantive file and code edits must use `agy-delegate` (or Codex `mcp__antigravity__delegate`) with `--tier flash`. This call is outside the short single-file replacement allowance.",
    "review": "Strict mode: diff and code review must use `agy-review` (or Codex `mcp__antigravity__review`). Native diff/show is denied.",
    "verification": "Strict mode: test/build/lint and log diagnosis must use `agy-delegate` (or Codex `mcp__antigravity__delegate`). Native test/build execution is denied.",
    "git": "Strict mode: Git operations must use `agy-delegate` (or Codex `mcp__antigravity__delegate`). Native Git execution is denied.",
    "research": "Strict mode: web and external research must use `agy-delegate` (or Codex `mcp__antigravity__delegate`). Native search/fetch is denied.",
    "media": "Strict mode: image, audio, and video inspection must use `bin/agy-media` (or Codex `mcp__antigravity__media`). Native media inspection is denied.",
    "native_agent": "Strict mode: native subagents are not permitted. Delegate work to `agy-scout`, `agy-delegate`, or `agy-review`.",
    "terminal": "Strict mode: general terminal automation must use `agy-delegate` (or Codex `mcp__antigravity__delegate`). Native shell execution is denied.",
}

def _polyphony_update_context() -> str:
    if str(os.environ.get("POLYPHONY_UPDATE_CHECK", "on")).strip().lower() in {"0", "false", "no", "off"}:
        return ""
    if check_for_update is None:
        return ""
    try:
        result = check_for_update()
    except Exception:
        return ""
    if not result.get("notify") or not result.get("available"):
        return ""
    current = result.get("current") or "unknown"
    latest = result.get("latest") or "unknown"
    url = result.get("url") or "https://github.com/GryAsl/Polyphony/releases"
    return (
        f"[Polyphony update] A newer Polyphony release is available: installed {current}, latest {latest}. "
        "Ask the user exactly one concise question: update Polyphony now? Do not update without explicit approval. "
        "After approval, use the host's matching command: Claude Code `claude plugin update antigravity@polyphony -y` "
        "then reload/restart; inside Claude's UI use `/plugin marketplace update polyphony` then `/reload-plugins`. "
        "Codex uses `codex plugin marketplace upgrade polyphony` then "
        "`codex plugin add antigravity@polyphony`. Verify the installed version after the command. "
        "Tell the user that a new Claude session/reload or a new Codex task is required for the updated plugin "
        f"to be loaded. Release notes: {url}"
    )


def _load_input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _state_dir() -> Path:
    configured = os.environ.get("AGY_ROUTING_STATE_DIR")
    if configured:
        p = Path(configured).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # Preserve the configured target; writes report failure explicitly.
        return p
    for env_var in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        val = os.environ.get(env_var)
        if val:
            try:
                p = Path(val).expanduser() / "agy-routing"
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                pass
    try:
        p = Path.home() / ".claude-agy-routing"
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        p = Path(tempfile.gettempdir()) / "claude-agy-routing"
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return p


def _session_state_path(session_id: str) -> Path:
    safe_id = hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:24]
    return _state_dir() / f"{safe_id}.json"


def _resolve_workspace_root(data: dict | None = None) -> Path:
    raw = None
    if isinstance(data, dict):
        raw = (
            data.get("cwd")
            or data.get("working_directory")
            or data.get("workingDirectory")
            or data.get("workspace")
            or data.get("project_path")
            or data.get("projectPath")
        )
    p = Path(raw).expanduser() if raw else Path.cwd()
    try:
        resolved = p.resolve()
    except Exception:
        resolved = p.absolute()
    if resolved.is_file():
        resolved = resolved.parent
    cur = resolved
    while True:
        if (cur / ".git").exists():
            return cur
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return resolved


def _workspace_state_path(data: dict | None = None) -> Path:
    ws = _resolve_workspace_root(data)
    norm = os.path.normcase(str(ws))
    safe_id = hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:24]
    return _state_dir() / f"ws-{safe_id}.json"


def _read_persisted_workspace_mode(data: dict | None = None) -> str | None:
    path = _workspace_state_path(data)
    try:
        if path.exists():
            content = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(content, dict):
                mode = content.get("mode")
                if mode in {"strict", "soft"}:
                    return mode
    except Exception:
        pass
    return None


def _write_persisted_workspace_mode(mode: str, data: dict | None = None) -> bool:
    if mode not in {"strict", "soft"}:
        return False
    path = _workspace_state_path(data)
    try:
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        payload = {
            "mode": mode,
            "workspace": str(_resolve_workspace_root(data)),
            "updated_at": time.time(),
        }
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, path)
        return _read_persisted_workspace_mode(data) == mode
    except Exception:
        return False


def _default_state() -> dict:
    return {
        "mode": "soft",
        "question_presented": False,
        "turn_id": "",
        "is_substantive": False,
        "native_helper_used": False,
        "native_small_ops": 0,
        "native_small_path": "",
        "agy_attempted": False,
        "agy_success": False,
        "agy_failed": False,
        "agy_pending": False,
        "agy_task_id": "",
        "last_agy_error": "",
        "denied_categories": [],
        "warned_categories": [],
        "continuation_count": 0,
        "user_mode_selection": False,
        "awaiting_user_input": False,
    }


def _read_state(session_id: str, data: dict | None = None) -> dict:
    path = _session_state_path(session_id)
    state = _default_state()
    loaded = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for k, v in loaded.items():
                    if k in state:
                        state[k] = v
    except Exception:
        pass

    if isinstance(loaded, dict) and loaded.get("mode") in {"strict", "soft"}:
        return state
    persisted_mode = _read_persisted_workspace_mode(data)
    if persisted_mode in {"strict", "soft"}:
        state["mode"] = persisted_mode
        state["question_presented"] = False
    elif state.get("mode") not in {"strict", "soft"}:
        state["mode"] = "soft"
    return state


def _write_state(session_id: str, state: dict) -> bool:
    path = _session_state_path(session_id)
    try:
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except Exception:
        return False


def _delete_state(session_id: str) -> None:
    path = _session_state_path(session_id)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _quota_state_path() -> Path:
    configured = os.environ.get("AGY_QUOTA_STATE_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".antigravity-quota"
    account_root = os.environ.get("POLYPHONY_ACCOUNTS_DIR") or os.environ.get("POLYPHONY_ACCOUNTS_ROOT")
    if account_root:
        pool_path = Path(account_root).expanduser() / "pool.json"
    elif os.name == "nt" and (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")):
        pool_path = Path(os.environ.get("LOCALAPPDATA") or os.environ["APPDATA"]) / "Polyphony" / "accounts" / "pool.json"
    else:
        pool_path = Path.home() / ".local" / "share" / "Polyphony" / "accounts" / "pool.json"
    try:
        pool = json.loads(pool_path.read_text(encoding="utf-8"))
        active = pool.get("current") if isinstance(pool, dict) and pool.get("pool_enabled") else None
        if active and re.fullmatch(r"[A-Za-z0-9_-]+", str(active)):
            return root / "accounts" / str(active) / "state.json"
    except Exception:
        pass
    return root / "state.json"


def _read_quota_state() -> dict:
    try:
        state = json.loads(_quota_state_path().read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


def _quota_context() -> str:
    state = _read_quota_state()
    if not state.get("depleted"):
        return ""
    decision = state.get("decision")
    if decision == "sonnet":
        return (
            "[Agy quota] The user approved the Sonnet fallback. Cancel only active Agy workers "
            "that are no longer progressing (including host-managed background tasks and, when "
            "applicable, `agy-job cancel-all`), then retry the interrupted work. The wrapper will "
            "route Gemini calls to Claude Sonnet 4.6 until both Gemini quota windows recover."
        )
    if decision == "wait":
        return (
            "[Agy quota] The user chose to wait. Do not kill active Agy workers and do not start "
            "Sonnet. Use the host's scheduling/wakeup facility to run `agy-quota --force` (or the "
            "Codex quota check tool with force=true) every 10 minutes, checking both the 5h and 7d "
            "Gemini windows. Stop monitoring and resume Gemini only after both are above 2%."
        )
    return (
        "[Agy quota] Agy Gemini quota is depleted (the 5h or 7d window is at or below 2%). "
        "Before killing workers, waiting, or using another model, ask the user exactly one choice: "
        "Would you like me to (1) kill any active Agy workers that are no longer progressing and "
        "continue with Claude Sonnet 4.6, or (2) keep the workers alive and wait for the 5h/7d "
        "quota to reset while I check both quotas every 10 minutes? Record only the user's explicit "
        "choice with `agy-quota --decision sonnet|wait` (or the Codex quota choice tool)."
    )


def _emit_context(event: str, context: str) -> None:
    if not context:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }))


def _emit_deny(event: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "additionalContext": reason,
        }
    }))


def _emit_stop_block(reason: str) -> None:
    print(json.dumps({
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "decision": "block",
            "reason": reason,
        },
    }))


def _text(tool_input: dict) -> str:
    values = []
    for key in (
        "command", "cmd", "code", "description", "prompt", "query", "pattern", "path",
        "file_path", "url", "goal", "task", "instructions",
    ):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
    return "\n".join(values)


def _path_from(tool_input: dict) -> str:
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _is_policy_or_agy_plumbing(path: str) -> bool:
    normalized = path.replace("/", "\\").lower()
    name = Path(path).name.lower()
    if name in POLICY_FILES:
        return True
    if name.startswith(("agy_task_", "agy_prompt_")):
        return True
    if name.endswith(".output") and "\\tasks\\" in normalized:
        return True
    return any(part in normalized for part in (
        "\\.claude\\hooks\\",
        "\\.claude\\settings.json",
        "\\agy_task_",
        "\\agy_prompt_",
        "\\claude-agy-opportunity\\",
        "\\claude-agy-routing\\",
    ))


def _is_agy_prompt_plumbing(path: str) -> bool:
    """Narrow write exemption for temporary Agy prompt/receipt files only."""
    normalized = path.replace("/", "\\").lower()
    name = normalized.rsplit("\\", 1)[-1]
    return (
        ("\\temp\\claude\\" in normalized and "\\scratchpad\\" in normalized
         and name.endswith((".md", ".txt")))
        or
        name.startswith(("agy_task_", "agy_prompt_"))
        or (name.endswith(".output") and "\\tasks\\" in normalized)
        or "\\claude-agy-opportunity\\" in normalized
        or "\\claude-agy-routing\\" in normalized
    )


def _get_shell_command(tool_input: dict) -> str | None:
    for key in ("command", "cmd"):
        val = tool_input.get(key)
        if isinstance(val, str):
            return val
    return None


def _limit(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


_HEREDOC_BODY = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE)
# A wrapper only counts when it is the EXECUTED command of some pipeline segment
# (start of line, or after ; & | ( or $( ), optionally behind env assignments and a
# path/quote. A mere mention -- `python patch.py scripts/agy-delegate.sh`, a grep for
# the name, or a heredoc body that quotes wrapper source -- is not an Agy prompt.
_AGY_COMMAND_POSITION = re.compile(
    r"(?:^|[;&|(\n]|\$\()\s*(?:\w+=\S*\s+)*[\"']?(?:[^\s;&|()\"']*[/\\])?"
    r"agy(?:[-_](?:delegate|scout|review|job|media|quota|trace|doctor|migrate))?(?:\.sh|\.cmd|\.exe)?(?=[\s\"';&|)]|$)",
    re.IGNORECASE | re.MULTILINE,
)


def _looks_like_agy_invocation(tool_name: str, tool_input: dict) -> bool:
    lowered = tool_name.lower()
    if lowered.startswith("mcp__antigravity__"):
        return True
    if lowered not in SHELL_TOOL_NAMES:
        return False
    command = _HEREDOC_BODY.sub("", _get_shell_command(tool_input) or "")
    return bool(_AGY_COMMAND_POSITION.search(command))


def _prompt_file_text(tool_name: str, tool_input: dict) -> str | None:
    """Count the resulting task file, including earlier chunks, before writing."""
    path = _path_from(tool_input)
    operation = tool_name.lower()
    content = tool_input.get("content")
    if operation in SHELL_TOOL_NAMES:
        prepared = _powershell_prompt_write(_get_shell_command(tool_input) or "")
        if prepared is None:
            return None
        path, operation, content = prepared
    if not path or not _is_agy_prompt_plumbing(path) or not path.lower().endswith((".md", ".txt")):
        return None
    try:
        old = Path(path).read_text(encoding="utf-8-sig") if Path(path).is_file() else ""
    except (OSError, UnicodeError):
        return "word " * 800  # Cannot validate an append/edit; require a fresh compact Write.
    if operation in {"write", "set-content"} and isinstance(content, str):
        return content
    if operation == "add-content" and isinstance(content, str):
        return old + "\n" + content
    if operation == "edit":
        before, after = tool_input.get("old_string"), tool_input.get("new_string")
        if isinstance(before, str) and isinstance(after, str) and before:
            return old.replace(before, after, -1 if tool_input.get("replace_all") else 1)
    return None


def _powershell_prompt_write(command: str) -> tuple[str, str, str] | None:
    # Literal task text only: do not execute PowerShell or interpret expandable strings.
    match = re.fullmatch(
        r"\s*\$(\w+)\s*=\s*'([^'\r\n]+)'\s*[;\r\n]+\s*"
        r"(Set-Content|Add-Content)\s+-(?:LiteralPath|Path)\s+\$(\w+)\s+"
        r"-Encoding\s+utf8\s+-Value\s+@'\r?\n(.*?)\r?\n'@\s*;?\s*",
        command, re.IGNORECASE | re.DOTALL,
    )
    if match and match[1].lower() == match[4].lower() and _is_agy_prompt_plumbing(match[2]):
        return match[2], match[3].lower(), match[5]
    return None


def _small_local_helper(tool_name: str, tool_input: dict) -> bool:
    """Recognize cheap orchestration, without blanket exemptions for python -c."""
    if tool_name.lower() not in SHELL_TOOL_NAMES:
        return False
    command = _get_shell_command(tool_input) or ""
    if _powershell_prompt_write(command):
        return True
    if command.strip() in {"pwd", "Get-Location", "git status --short", "git status --porcelain", "git rev-parse HEAD"}:
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 3 or tokens[0].lower() not in {"python", "python3", "python.exe", "py"} or tokens[1] != "-c":
        return False
    code = tokens[2]
    if len(code) > 1000:
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return False
    # Pure argument/text/arithmetic probes. No file IO, subprocess, eval, imports
    # with effects, loops, or dynamic attribute access.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name != "sys" or alias.asname for alias in node.names):
                return False
        elif isinstance(node, ast.Attribute):
            if not (isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr == "argv"):
                return False
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in {"print", "len", "str", "int", "repr", "abs", "min", "max", "round"} or node.keywords:
                return False
        elif isinstance(node, ast.Name):
            if node.id not in {"sys", "print", "len", "str", "int", "repr", "abs", "min", "max", "round"}:
                return False
        elif not isinstance(node, (ast.Module, ast.Expr, ast.alias, ast.Load, ast.Constant,
                                   ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp,
                                   ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                                   ast.Mod, ast.USub, ast.UAdd, ast.List, ast.Tuple)):
            return False
    # Arguments must be literal shell text; prohibit expansion/control outside quotes.
    return not _has_unquoted_shell_control(command) and "$(" not in command and "`" not in command


def _bounded_native_path(data: dict, path: str) -> Path | None:
    """Resolve a literal path only for small-operation sizing; never create it."""
    if not path or any(char in path for char in "*?[]\r\n"):
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        cwd = data.get("cwd") or data.get("working_directory") or data.get("workingDirectory")
        candidate = Path(cwd).expanduser() / candidate if isinstance(cwd, str) and cwd else Path.cwd() / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _is_sensitive_small_edit(path: str) -> bool:
    normalized = path.replace("/", "\\").lower()
    name = Path(path).name.lower()
    return (
        name in POLICY_FILES
        or name in {"plugin.json", "hooks.json", "settings.json", "settings.local.json", "config.toml", "manifest.json"}
        or name.startswith(".env")
        or name.endswith((".lock", ".pem", ".key", ".pfx", ".p12"))
        or any(part in normalized for part in (
            "\\.git\\", "\\.github\\", "\\hooks\\", "\\auth", "\\security",
            "\\permissions", "\\settings", "\\manifest", "\\secrets",
        ))
    )


def _bounded_native_operation(data: dict, tool_name: str, tool_input: dict, state: dict) -> str | None:
    """Return the single touched path for a tiny strict-mode conductor operation.

    This is deliberately narrow and measurable: at most three operations on one
    file per turn. It lets strict mode tolerate a compact spot-check or one small
    replacement while preserving Agy enforcement for broad/substantive work.
    """
    try:
        used = int(state.get("native_small_ops") or 0)
    except (TypeError, ValueError):
        used = STRICT_NATIVE_MAX_OPS
    if used >= STRICT_NATIVE_MAX_OPS:
        return None

    lowered = tool_name.lower()
    path = _path_from(tool_input)
    if _is_sensitive_small_edit(path):
        return None
    resolved = _bounded_native_path(data, path)
    if resolved is None:
        return None
    normalized = os.path.normcase(str(resolved))
    previous = str(state.get("native_small_path") or "")
    if previous and previous != normalized:
        return None

    if lowered == "read":
        limit = tool_input.get("limit") or tool_input.get("line_limit") or tool_input.get("lineLimit")
        if limit is not None:
            try:
                if not 0 < int(limit) <= STRICT_NATIVE_MAX_READ_LINES:
                    return None
            except (TypeError, ValueError):
                return None
        else:
            try:
                # A small file is bounded even when the host Read tool omits a
                # line limit. Larger/unknown files still go through Agy.
                if not resolved.is_file() or resolved.stat().st_size > 32_768:
                    return None
            except OSError:
                return None
        return normalized

    if lowered == "grep":
        # Native grep is exempt only when scoped to one literal file and a small
        # result cap. Directory scans and Glob remain Agy work.
        if not resolved.is_file():
            return None
        cap = tool_input.get("head_limit") or tool_input.get("headLimit") or tool_input.get("limit")
        try:
            if cap is None or not 0 < int(cap) <= STRICT_NATIVE_MAX_GREP_RESULTS:
                return None
        except (TypeError, ValueError):
            return None
        pattern = tool_input.get("pattern")
        return normalized if isinstance(pattern, str) and 0 < len(pattern) <= 200 else None

    if lowered == "edit":
        if not resolved.is_file() or _is_sensitive_small_edit(path) or tool_input.get("replace_all"):
            return None
        before = tool_input.get("old_string")
        after = tool_input.get("new_string")
        if not isinstance(before, str) or not isinstance(after, str) or not before:
            return None
        if len(before) + len(after) > STRICT_NATIVE_MAX_EDIT_CHARS:
            return None
        return normalized

    return None


def _agy_prompt_budget_violation(tool_name: str, tool_input: dict) -> str | None:
    """Enforce compact instructions across both hosts and task-file preparation.

    The shared delegate additionally validates fully assembled stdin/file content.
    """
    file_text = _prompt_file_text(tool_name, tool_input)
    if file_text is None and not _looks_like_agy_invocation(tool_name, tool_input):
        return None

    lowered = tool_name.lower()
    prompt = ""
    if file_text is not None:
        prompt = file_text
    elif lowered.startswith("mcp__antigravity__"):
        prompt = "\n".join(tool_input[k] for k in ("prompt", "question", "query", "goal", "focus")
                           if isinstance(tool_input.get(k), str))
    else:
        command = _get_shell_command(tool_input) or ""
        # If shell quoting is already malformed, tokenisation is impossible;
        # use the raw command length, which is precisely what `bash -c` limits.
        parsed = parse_single_agy_shell_command(command)
        if parsed is None:
            prompt = command
        else:
            _, tokens = parsed
            if tokens and tokens[-1] != "-":
                prompt = tokens[-1]

    if not prompt:
        return None
    max_chars = _limit("AGY_PROMPT_MAX_CHARS", DEFAULT_AGY_PROMPT_MAX_CHARS)
    max_words = min(_limit("AGY_PROMPT_MAX_WORDS", DEFAULT_AGY_PROMPT_MAX_WORDS), DEFAULT_AGY_PROMPT_MAX_WORDS)
    chars = len(prompt)
    words = len(prompt.split())
    if chars <= max_chars and words <= max_words:
        return None
    return (
        "Agy prompt exceeds the compact instruction budget "
        f"({chars:,} chars/{words:,} words; limits {max_chars:,} chars/{max_words:,} words). "
        "Do not retry the same draft in smaller chunks. Rewrite the complete contract to the "
        "shortest sufficient form, normally 200-500 words and never more than 800 words/8,000 characters. Include only objective, paths/scope, "
        "non-negotiable constraints, acceptance checks, and a compact receipt. Files, stdin, task "
        "files, and multiple writes do not bypass the gate; split only genuinely independent work."
    )


def _has_unquoted_newline(cmd: str) -> bool:
    in_single = False
    in_double = False
    escaped = False
    for ch in cmd:
        if escaped:
            if ch == "\n" or ch == "\r":
                return True
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif (ch == "\n" or ch == "\r") and not in_single and not in_double:
            return True
    return False


def _has_unquoted_shell_control(cmd: str) -> bool:
    """Reject chaining, substitution, and redirection outside quoted prompt text."""
    in_single = False
    in_double = False
    escaped = False
    for index, ch in enumerate(cmd):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if ch in "\r\n;|&<>`":
            return True
        if ch == "$" and index + 1 < len(cmd) and cmd[index + 1] == "(":
            return True
    return False


def parse_single_agy_shell_command(cmd: str) -> tuple[str, list[str]] | None:
    """If cmd is a pure, single command whose sole executable is an approved Polyphony wrapper,
    return (wrapper_stem, tokens).
    If it is compound, chained, uses pipelines/operators, or the executable is not an approved wrapper,
    return None.
    """
    if not cmd or not isinstance(cmd, str):
        return None
    trimmed = cmd.strip()
    if not trimmed:
        return None
    if _has_unquoted_newline(trimmed) or _has_unquoted_shell_control(trimmed):
        return None

    try:
        lexer = shlex.shlex(trimmed, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except Exception:
        return None

    if not tokens:
        return None

    forbidden_tokens = {"&&", "||", ";", "|", "&", "`"}
    prev = ""
    for t in tokens:
        if t in forbidden_tokens:
            return None
        if t == "(" and prev == "$":
            return None
        if "$(" in t or "`" in t:
            return None
        prev = t

    exe = tokens[0]
    stem = Path(exe).stem.lower()
    for ext in (".exe", ".bat", ".cmd", ".sh"):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]

    if stem in APPROVED_AGY_WRAPPERS:
        return stem, tokens

    return None


def _agy_compound_command(cmd: str) -> tuple[str, list[str]] | None:
    """Recognize a safe Agy command with prompt-plumbing prelude.

    Claude commonly prepares a task with `cd`, `cat`/`Get-Content`, and an
    environment assignment before invoking `agy-job`.  The old classifier saw
    the preparation command first and denied the entire operation in strict
    mode.  Permit only those non-mutating heads; arbitrary shell chains (rm,
    git reset, network commands, etc.) still require the normal strict route.
    """
    if not cmd or not re.search(
        r"(?:^|[\s/])agy(?:[-_](?:delegate|scout|review|job|media|quota|trace|doctor|migrate))?\b",
        cmd,
        re.IGNORECASE,
    ):
        return None
    if "\n" in cmd or "\r" in cmd:
        return None
    try:
        lexer = shlex.shlex(cmd.strip(), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except Exception:
        return None
    if not tokens:
        return None

    # Only sequencing operators are accepted for prompt preparation. Pipelines,
    # backgrounding, redirection, and conditional fallbacks can hide unrelated
    # shell work and therefore remain strict-mode denials.
    separators = {"&&", ";"}
    forbidden_controls = {"||", "|", "&", ">", "<"}
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in separators:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    segments = [segment for segment in segments if segment]
    if not segments:
        return None

    allowed_heads = {"cd", "cat", "get-content", "type"}
    agy_wrapper = ""
    agy_tokens: list[str] = []
    compound_work_wrappers = {
        "agy", "agy-delegate", "agy-scout", "agy-review", "agy-job",
        "agy-media", "agy-migrate", "agy-cost-compare", "cloud-debug",
    }
    for segment in segments:
        head = Path(segment[0]).stem.lower()
        if head in APPROVED_AGY_WRAPPERS:
            if head not in compound_work_wrappers:
                return None
            # Shell operators/process substitution must not hide behind an Agy
            # token.  A quoted variable such as "$TASK" is fine; standalone
            # operators and `$(` command substitution are not.
            if any(
                token in {"$", "(", ")", ">", "<", "`"}
                or any(control in token for control in ("`", "\r", "\n"))
                or "$(" in token
                for token in segment[1:]
            ):
                return None
            agy_wrapper, agy_tokens = head, segment
            continue
        if head in allowed_heads:
            if any(token in forbidden_controls or "`" in token for token in segment[1:]):
                return None
            continue
        # POSIX assignment: TASK=$(cat file), TASK=$(Get-Content file)
        if len(segment) == 1 and re.match(
            r"^[A-Za-z_][A-Za-z0-9_]*=\$\((?:cat|get-content|type)\b",
            segment[0],
            re.IGNORECASE,
        ):
            continue
        # PowerShell assignment: $task = Get-Content -Raw file
        if len(segment) >= 3 and re.match(r"^\$?[A-Za-z_][A-Za-z0-9_]*$", segment[0]) and segment[1] == "=" and Path(segment[2]).stem.lower() in {"get-content", "type"}:
            continue
        return None
    return (agy_wrapper, agy_tokens) if agy_wrapper else None


def _classify_shell(command: str) -> str | None:
    if parse_single_agy_shell_command(command) is not None or _agy_compound_command(command) is not None:
        return None

    lowered = command.lower()
    if re.search(r"\bgit\s+(?:diff|show|range-diff|blame)\b", lowered):
        return "review"
    if re.search(r"\bgit\s+(?:add|commit|push|pull|fetch|merge|rebase|tag|checkout|switch|restore|stash|branch|reset)\b", lowered):
        return "git"
    if re.search(r"\b(?:dotnet\s+(?:build|test)|npm\s+(?:test|run)|pnpm\s+(?:test|run)|yarn\s+(?:test|run)|pytest|cargo\s+(?:test|check|build)|go\s+test|mvn\s+test|gradle\s+test|eslint|tsc|ruff|mypy|unity)\b", lowered):
        return "verification"
    if re.search(r"\b(?:rg|grep|findstr|select-string|glob|fd|find|get-content|type|more|less|sed|awk|cat|dir|ls)\b", lowered):
        return "discovery"
    if re.search(r"\b(?:curl|wget|invoke-webrequest|invoke-restmethod)\b|https?://", lowered):
        return "research"
    return "terminal"


def _classify_mcp(tool_name: str, payload: str) -> str:
    lowered = f"{tool_name} {payload}".lower()
    if any(word in lowered for word in ("write", "edit", "patch", "create", "delete", "move", "rename")):
        return "implementation"
    if any(word in lowered for word in ("review", "diff", "pull_request", "commit", "push")):
        return "review" if any(word in lowered for word in ("review", "diff")) else "git"
    if any(word in lowered for word in ("test", "build", "lint", "compile", "log")):
        return "verification"
    if any(word in lowered for word in ("browser", "web", "search", "fetch", "github")):
        return "research"
    if any(word in lowered for word in ("read", "file", "repo", "code", "filesystem")):
        return "discovery"
    return "external"


def _classify(tool_name: str, tool_input: dict) -> str | None:
    lowered_name = tool_name.lower()
    path = _path_from(tool_input)

    if lowered_name in SHELL_TOOL_NAMES:
        cmd = _get_shell_command(tool_input) or _text(tool_input)
        return _classify_shell(cmd)
    if lowered_name in {"grep", "glob"}:
        return "discovery"
    if lowered_name == "read":
        if _is_policy_or_agy_plumbing(path):
            return None
        suffix = Path(path).suffix.lower()
        if suffix in MEDIA_EXTENSIONS:
            return "media"
        if suffix in {".log", ".out", ".trace"}:
            return "verification"
        if suffix in {".diff", ".patch"}:
            return "review"
        return "discovery"
    if lowered_name in {"edit", "write", "notebookedit", "notebook_edit", "apply_patch"}:
        if _is_agy_prompt_plumbing(path):
            return None
        return "implementation"
    if lowered_name in {"agent", "task", "spawn_agent"}:
        return "native_agent"
    if lowered_name in {"webfetch", "websearch", "web_fetch", "web_search", "web__run"}:
        return "research"
    if lowered_name in {"view_image", "imagegen", "image_gen__imagegen"}:
        return "media"
    if lowered_name in {"read_mcp_resource", "list_mcp_resources", "list_mcp_resource_templates"}:
        return "discovery"
    if lowered_name.startswith("mcp__"):
        if lowered_name.startswith("mcp__antigravity__"):
            return None
        return "external"
    if lowered_name in CLAUDE_ONLY_TOOLS:
        return None
    return "external"


def is_work_producing_agy_call(tool_name: str, tool_input: dict) -> bool:
    lowered_name = tool_name.lower()

    if lowered_name.startswith("mcp__antigravity__"):
        if lowered_name in {
            "mcp__antigravity__delegate",
            "mcp__antigravity__scout",
            "mcp__antigravity__review",
            "mcp__antigravity__media",
            "mcp__antigravity__migrate",
            "mcp__antigravity__cloud_debug",
            "mcp__antigravity__cost_compare",
            "mcp__antigravity__job_result",
            "mcp__antigravity__persistent_delegate",
        }:
            return True
        if lowered_name == "mcp__antigravity__job":
            return tool_input.get("action") in {"start", "result"}
        return False

    if lowered_name in SHELL_TOOL_NAMES:
        cmd = _get_shell_command(tool_input)
        if not cmd:
            return False
        parsed = parse_single_agy_shell_command(cmd)
        if parsed is None:
            parsed = _agy_compound_command(cmd)
        if parsed is None:
            return False
        wrapper, tokens = parsed
        if wrapper in {
            "agy-delegate", "agy-scout", "agy-review", "agy-media",
            "agy-migrate", "agy-cost-compare", "cloud-debug",
        }:
            return True
        if wrapper == "agy-job":
            return len(tokens) > 1 and tokens[1].lower() in {"start", "result"}
        if wrapper == "agy":
            return len(tokens) > 1 and tokens[1].lower() in {
                "delegate", "scout", "review", "media", "migrate", "cost-compare",
            }
        if Path(tokens[0]).stem.lower() == "research" and "commands" in tokens[0].lower():
            return True

    return False


def is_any_agy_call(tool_name: str, tool_input: dict) -> bool:
    lowered_name = tool_name.lower()
    if lowered_name.startswith("mcp__antigravity__"):
        return True
    if lowered_name in SHELL_TOOL_NAMES:
        cmd = _get_shell_command(tool_input)
        if cmd and (parse_single_agy_shell_command(cmd) is not None or _agy_compound_command(cmd) is not None):
            return True
    return False


def is_control_plane_exempt(tool_name: str, tool_input: dict) -> bool:
    lowered_name = tool_name.lower()

    if _small_local_helper(tool_name, tool_input):
        return True

    if lowered_name in {"askuserquestion", "request_user_input", "request_user_input_async", "ask_user_question", "functions.request_user_input", "functions.request_user_input_async"}:
        return True
    if lowered_name.startswith("mcp__codex_app__"):
        return lowered_name.removeprefix("mcp__codex_app__") in {
            "list_threads", "read_thread", "wait_threads", "list_projects",
            "navigate_to_codex_page", "open_in_codex", "create_thread", "fork_thread",
            "send_message_to_thread", "set_thread_title", "set_thread_archived",
            "get_usage_limits", "read_thread_terminal",
        }
    if lowered_name in SHELL_TOOL_NAMES and _host_control_shell(_get_shell_command(tool_input) or ""):
        return True
    if lowered_name in CLAUDE_ONLY_TOOLS:
        return True

    path = _path_from(tool_input)
    if path:
        if lowered_name == "read" and _is_policy_or_agy_plumbing(path):
            return True
        if lowered_name in {"edit", "write", "notebookedit", "notebook_edit", "apply_patch"} \
                and _is_agy_prompt_plumbing(path):
            return True

    # Quota MCP tools
    if lowered_name in {"mcp__antigravity__quota", "mcp__antigravity__account"} or (
        lowered_name.startswith("mcp__antigravity__") and lowered_name.endswith(("__quota", "__account"))
    ):
        return True

    # Persistent-agent registry and message-bus operations are control-plane
    # bookkeeping. They must remain available in strict mode; only the actual
    # persistent_delegate call is work-producing and is gated above.
    if lowered_name in {"mcp__antigravity__agent_task", "mcp__antigravity__agent_message"}:
        return True

    # Quota shell commands
    if lowered_name in SHELL_TOOL_NAMES:
        cmd = _get_shell_command(tool_input)
        if cmd:
            parsed = parse_single_agy_shell_command(cmd)
            if parsed is not None and parsed[0] in {"agy-quota", "agy-account"}:
                return True

    # Doctor / trace / job management MCP tools
    if lowered_name in {
        "mcp__antigravity__doctor", "mcp__antigravity__trace",
        "mcp__antigravity__job_list", "mcp__antigravity__job_status",
        "mcp__antigravity__job_cancel", "mcp__antigravity__job_cancel_all",
    }:
        return True
    if lowered_name == "mcp__antigravity__job" and tool_input.get("action") in {"list", "status", "cancel", "cancel_all"}:
        return True

    # Doctor / trace / job management shell commands
    if lowered_name in SHELL_TOOL_NAMES:
        cmd = _get_shell_command(tool_input)
        if cmd:
            parsed = parse_single_agy_shell_command(cmd)
            if parsed is not None:
                wrapper, tokens = parsed
                if wrapper in {"agy-doctor", "agy-trace"}:
                    return True
                if wrapper == "polyphony-agent":
                    return True
                if wrapper == "agy-job" and len(tokens) > 1 and tokens[1].lower() in {"list", "status", "cancel", "cancel-all"}:
                    return True

    return False


def _host_control_shell(command: str) -> bool:
    """Host administration only; never exempt arbitrary update/test scripts."""
    if _has_unquoted_shell_control(command) or "$" in command or "`" in command:
        return False
    try:
        # This exact allowlist needs paths literally, not POSIX backslash escapes.
        tokens = shlex.split(command, posix=False)
        tokens = [token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'} else token for token in tokens]
    except ValueError:
        return False
    if tokens in (["claude", "--version"], ["codex", "--version"]):
        return True
    if len(tokens) >= 3 and tokens[:2] in (["claude", "plugin"], ["codex", "plugin"]):
        if tokens[2:] == ["list"]:
            return True
        if tokens == ["claude", "plugin", "update", "antigravity@polyphony", "-y"]:
            return True
        if tokens == ["codex", "plugin", "marketplace", "upgrade", "polyphony"]:
            return True
        return len(tokens) == 4 and tokens[2] in {"add", "install", "update"} and tokens[3] == "antigravity@polyphony"
    if len(tokens) == 2 and Path(tokens[0]).name.lower() in {"python", "python3", "python.exe", "python3.exe"}:
        try:
            return Path(tokens[1]).resolve() == Path(__file__).resolve().parents[1] / "scripts" / "polyphony_update.py"
        except OSError:
            return False
    if len(tokens) == 3 and tokens[:2] == ["py", "-3"]:
        try:
            return Path(tokens[2]).resolve() == Path(__file__).resolve().parents[1] / "scripts" / "polyphony_update.py"
        except OSError:
            return False
    return False


def _check_status(val: any) -> str | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return f"invalid boolean status {val}"
    if isinstance(val, int):
        if val != 0 and not (200 <= val < 300):
            return f"status code {val}"
        return None
    if isinstance(val, str):
        lowered = val.strip().lower()
        if lowered in REJECTED_RESPONSE_STATUSES or any(
            lowered.startswith(s) for s in ("fail", "err", "run", "pend", "async", "time", "cancel")
        ):
            return f"status {val}"
    return None


def _check_exit_code(obj: dict) -> str | None:
    for key in ("exit_code", "exitCode", "returncode", "code"):
        if key in obj:
            code = obj[key]
            if isinstance(code, bool):
                return f"invalid boolean exit code {code}"
            if isinstance(code, int) and code != 0:
                return f"exit code {code}"
            if isinstance(code, str):
                try:
                    parsed = int(code.strip())
                except ValueError:
                    continue
                if parsed != 0:
                    return f"exit code {code}"
    return None


def _check_error_flag(obj: dict) -> str | None:
    if obj.get("isError") is True or obj.get("is_error") is True:
        return str(obj.get("error") or obj.get("stderr") or "isError flag set")
    err = obj.get("error")
    if err is not None and err is not False and err != "" and err != {}:
        return str(err)
    return None


def _response_dict(response: any) -> dict | None:
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        stripped = response.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except Exception:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _nested_response_dicts(value: any):
    """Yield a response and the host's common task/result envelopes."""
    if not isinstance(value, dict):
        return
    yield value
    for key in ("result", "job", "task", "background_task", "backgroundTask", "structuredContent", "structured_content"):
        nested = value.get(key)
        if isinstance(nested, dict):
            yield from _nested_response_dicts(nested)


def _normalize_async_status(value: any) -> str:
    return str(value).strip().lower().replace(" ", "_") if value is not None else ""


def _is_truthy_flag(value: any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


def _background_task_id(*values: any) -> str:
    for value in values:
        for obj in _nested_response_dicts(value) or ():
            for key in ASYNC_TASK_ID_KEYS:
                task_id = obj.get(key)
                if task_id is not None and str(task_id).strip():
                    return str(task_id).strip()
    return ""


def _agy_job_start_command(tool_input: any) -> bool:
    if not isinstance(tool_input, dict):
        return False
    cmd = _get_shell_command(tool_input)
    if not cmd:
        return False
    parsed = parse_single_agy_shell_command(cmd)
    if parsed is None:
        parsed = _agy_compound_command(cmd)
    if parsed is None:
        return False
    wrapper, tokens = parsed
    return wrapper == "agy-job" and len(tokens) > 1 and tokens[1].lower() == "start"


def _agy_job_started_id(response: any) -> str:
    sources = _nested_response_dicts(_response_dict(response) or response)
    for obj in sources or ():
        text = str(obj.get("stdout") or obj.get("output") or "")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped.split()[-1]
        for key in ("job_id", "jobId"):
            value = obj.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    if isinstance(response, str):
        for line in response.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped.split()[-1]
    return ""


def _mcp_job_start(data: dict, tool_input: dict) -> bool:
    tool_name = str(data.get("tool_name") or data.get("toolName") or data.get("name") or "").lower()
    return tool_name == "mcp__antigravity__job" and tool_input.get("action") == "start"


def _mcp_job_start_succeeded(data: dict, tool_input: dict, response: any) -> bool:
    """Recognize a real MCP job launch without turning launch failures into pending work."""
    if not _mcp_job_start(data, tool_input) or response is None:
        return False
    response_obj = _response_dict(response) or response
    for obj in _nested_response_dicts(response_obj) or ():
        if _check_error_flag(obj) or _check_exit_code(obj):
            return False
    return bool(_background_task_id(data, response) or _agy_job_started_id(response))


def _background_result_is_pending(data: dict, tool_input: dict, response: any) -> bool:
    """Return true only for explicit host signals that work is still running.

    A blank stdout by itself is deliberately not enough.  The wrapper uses an
    empty final response to signal a real failure, so guessing that every blank
    result is asynchronous would weaken strict routing and hide failures.
    """
    response_obj = _response_dict(response)
    if _agy_job_start_command(tool_input):
        return True
    if _mcp_job_start_succeeded(data, tool_input, response):
        return True
    # `run_in_background` belongs to the launcher invocation. Its shell
    # command can legitimately report exit 0/completed while the spawned Agy
    # worker is still running, so this signal takes precedence over that
    # generic command status. The later TaskOutput call does not carry this
    # launcher flag and is evaluated normally below.
    if isinstance(tool_input, dict) and any(
        _is_truthy_flag(tool_input.get(key)) for key in ASYNC_FLAG_KEYS
    ):
        return True

    sources = (data, response_obj)
    statuses: set[str] = set()
    has_async_flag = False
    for source in sources:
        if not isinstance(source, dict):
            continue
        statuses.update({
            _normalize_async_status(source.get(key))
            for key in ASYNC_STATUS_KEYS
            if source.get(key) is not None
        })
        has_async_flag = has_async_flag or any(
            _is_truthy_flag(source.get(key)) for key in ASYNC_FLAG_KEYS
        )

    if statuses & PENDING_RESPONSE_STATUSES:
        return True

    # A background flag in the tool input/response is a definitive signal
    # that the visible result is only the launcher acknowledgement. A terminal
    # status overrides it because the final TaskOutput may retain the original
    # flag in its envelope.
    return has_async_flag and not statuses & TERMINAL_RESPONSE_STATUSES


def _is_background_result_tool(tool_name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", tool_name.lower())
    return normalized in BACKGROUND_RESULT_TOOLS


def is_agy_response_successful(response: any) -> tuple[bool, str]:
    if response is None:
        return False, "missing response"

    if isinstance(response, str):
        s = response.strip()
        if not s:
            return False, "empty output"
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    response = parsed
            except Exception:
                pass
        if isinstance(response, str):
            if re.match(r"^usage:\s+\S+", s, re.IGNORECASE):
                return False, "usage-only output"
            if re.match(
                r"^(?:traceback \(most recent call last\):|error\s*:|failed\b|"
                r"timed out\b|command exited with (?:code|status)\s+-?\d+\b|"
                r"agy-(?:delegate|scout|review)\s*:.*(?:failed|returned empty))",
                s,
                re.IGNORECASE,
            ):
                return False, "diagnostic-only failure output"
            if all(
                line.lstrip().startswith(("AGY_USAGE ", "AGY_SIGNAL "))
                for line in s.splitlines() if line.strip()
            ):
                return False, "metadata-only output"
            return True, ""

    if not isinstance(response, dict):
        return False, "invalid response type"

    err = _check_error_flag(response)
    if err:
        return False, err

    err = _check_exit_code(response)
    if err:
        return False, err

    err = _check_status(response.get("status")) or _check_status(response.get("state"))
    if err:
        return False, err

    text_parts = []
    for key in ("stdout", "output", "text"):
        val = response.get(key)
        if isinstance(val, str) and val.strip():
            text_parts.append(val.strip())

    content = response.get("content")
    if isinstance(content, str) and content.strip():
        text_parts.append(content.strip())
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("isError") is True or item.get("type") == "error":
                    return False, "error in content block"
                if item.get("text"):
                    text_parts.append(str(item["text"]).strip())
            elif isinstance(item, str) and item.strip():
                text_parts.append(item.strip())

    structured = response.get("structuredContent") or response.get("structured_content")
    if isinstance(structured, dict):
        err = _check_error_flag(structured)
        if err:
            return False, f"structuredContent {err}"
        err = _check_exit_code(structured)
        if err:
            return False, f"structuredContent {err}"
        err = _check_status(structured.get("status")) or _check_status(structured.get("state"))
        if err:
            return False, f"structuredContent {err}"
        for key in ("stdout", "output", "text", "content"):
            val = structured.get(key)
            if isinstance(val, str) and val.strip():
                text_parts.append(val.strip())

    result = response.get("result")
    if isinstance(result, dict):
        err = _check_error_flag(result)
        if err:
            return False, f"result {err}"
        err = _check_exit_code(result)
        if err:
            return False, f"result {err}"
        err = _check_status(result.get("status")) or _check_status(result.get("state"))
        if err:
            return False, f"result {err}"
        for key in ("stdout", "output", "text", "content"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                text_parts.append(val.strip())

    job = response.get("job")
    if isinstance(job, dict):
        err = _check_error_flag(job)
        if err:
            return False, f"job {err}"
        err = _check_status(job.get("status")) or _check_status(job.get("state"))
        if err:
            return False, f"job {err}"

    combined_text = "\n".join(text_parts).strip()
    if not combined_text:
        stderr = response.get("stderr") or (structured.get("stderr") if isinstance(structured, dict) else None)
        if stderr and str(stderr).strip():
            return False, f"stderr only: {str(stderr).strip()}"
        if "usage" in response or (isinstance(structured, dict) and "usage" in structured):
            return False, "usage-only payload without output"
        return False, "empty output"

    if re.match(r"^usage:\s+\S+", combined_text, re.IGNORECASE):
        return False, "usage-only output"
    if re.match(r"^(?:total_tokens|input_tokens|output_tokens|tokens used)\b", combined_text, re.IGNORECASE):
        return False, "usage-only payload"

    return True, ""


def parse_mode_switch_intent(text: str) -> tuple[str | None, bool]:
    if not text or not isinstance(text, str):
        return None, False

    quoted_match = re.search(
        r"""["'`][^"'`]*\b(?:switch|set|change)\s+(?:(?:agy|routing)\s+)*(?:mode\s+to\s+(?:strict|soft)|to\s+(?:strict|soft)\s+mode|to\s+(?:strict|soft)|strict|soft)\b[^"'`]*["'`]""",
        text,
        re.IGNORECASE,
    )
    if quoted_match:
        return None, False

    cleaned = text.strip().lower()
    # Explicit Turkish imperatives, not quoted examples, questions or negations.
    turkish = text.strip().translate(str.maketrans("ıİşŞğĞüÜöÖçÇ", "iIsSgGuUoOcC")).lower()
    if not any(q in cleaned for q in ('"', "'", "`", "?")):
        match = re.match(
            r"^(?:lutfen\s+)?(?:(?:agy|polyphony)\s+)?(?:modunu\s+)?"
            r"(strict|soft)\s+(?:mod(?:a|una|u|unu)?\s+)?(?:gec|yap|ayarla|degistir)(?=$|[\s.,;])",
            turkish,
        )
        if match:
            tail = turkish[match.end():].strip(" .,!;")
            return match.group(1), not tail
    if "?" in cleaned or re.match(r"^(?:please\s+)?(?:do not|don't|dont|never)\b", cleaned):
        return None, False
    norm = re.sub(r"^[^\w\d(]+|[^\w\d)]+$", "", cleaned)
    if (
        (norm.startswith("(") and norm.endswith(")"))
        or (norm.startswith('"') and norm.endswith('"'))
        or (norm.startswith("'") and norm.endswith("'"))
    ):
        norm = norm[1:-1].strip()

    # Pure switch
    if re.fullmatch(
        r"(?:please\s+)?(?:switch|set|change)\s+(?:(?:agy|routing)\s+)*(?:mode\s+to\s+|to\s+|to\s+mode\s+)?(strict|soft)(?:\s+mode)?",
        norm,
    ):
        m = re.search(r"\b(strict|soft)\b", norm)
        return (m.group(1).lower() if m else None), True

    # Combined imperative switch + substantive work: "switch to strict and inspect files".
    # Anchor it so questions or negated prose cannot silently change session policy.
    m_switch = re.match(
        r"^(?:please\s+)?(?:switch|set|change)\s+(?:(?:agy|routing)\s+)*(?:mode\s+to\s+|to\s+|to\s+mode\s+)?(strict|soft)(?:\s+mode)?\b",
        cleaned,
    )
    if m_switch:
        return m_switch.group(1).lower(), False

    return None, False


def parse_mode_selection_intent(text: str) -> tuple[str | None, bool]:
    if not text or not isinstance(text, str):
        return None, False

    switch_mode, is_sole_switch = parse_mode_switch_intent(text)
    if switch_mode is not None:
        return switch_mode, is_sole_switch

    cleaned = text.strip().lower()
    if "?" in cleaned or re.match(r"^(?:please\s+)?(?:do not|don't|dont|never)\b", cleaned):
        return None, False
    has_strict_word = bool(re.search(r"\b(?:strict|always|7/24|24/7)\b", cleaned))
    has_soft_word = bool(re.search(r"\b(?:soft|appropriate)\b", cleaned))
    if has_strict_word and has_soft_word:
        return None, False

    norm = re.sub(r"^[^\w\d(]+|[^\w\d)]+$", "", cleaned)
    if (
        (norm.startswith("(") and norm.endswith(")"))
        or (norm.startswith('"') and norm.endswith('"'))
        or (norm.startswith("'") and norm.endswith("'"))
    ):
        norm = norm[1:-1].strip()

    # Pure selection
    if norm in {
        "1", "strict", "always", "7/24", "24/7",
        "always use agy", "always use agy (strict)", "always use agy strict",
        "her zaman agy (strict)",
    }:
        return "strict", True
    if re.fullmatch(
        r"(?:please\s+)?(?:choose\s+|select\s+|use\s+)?(?:always\s+use\s+agy\s*(?:\(strict\))?|strict|always|7/24|24/7)(?:\s+mode)?",
        norm,
    ):
        return "strict", True

    if norm in {
        "2", "soft", "when appropriate", "appropriate",
        "use agy when appropriate", "use agy when appropriate (soft)", "use agy when appropriate soft",
        "uygun olduğunda agy (soft)",
    }:
        return "soft", True
    if re.fullmatch(
        r"(?:please\s+)?(?:choose\s+|select\s+|use\s+)?(?:use\s+agy\s+when\s+appropriate\s*(?:\(soft\))?|soft|when\s+appropriate|appropriate)(?:\s+mode)?",
        norm,
    ):
        return "soft", True

    # Combined selection with substantive work (e.g. "1 and inspect files", "Always use Agy (strict) and check files")
    if re.match(r"^(?:1\b|(?:please\s+)?(?:choose\s+|select\s+|use\s+)?always\s+use\s+agy\b)", cleaned):
        return "strict", False
    if re.match(r"^(?:2\b|(?:please\s+)?(?:choose\s+|select\s+|use\s+)?use\s+agy\s+when\s+appropriate\b)", cleaned):
        return "soft", False

    return None, False


def parse_mode_selection(text: str) -> str | None:
    mode, is_sole = parse_mode_selection_intent(text)
    return mode if is_sole else None


def parse_mode_switch(text: str) -> str | None:
    mode, _ = parse_mode_switch_intent(text)
    return mode


def _is_control_plane_prompt(prompt: str) -> bool:
    if not prompt or not isinstance(prompt, str):
        return False
    lowered = prompt.strip().lower()
    if lowered in {"hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "yes", "no"}:
        return True
    if any(k in lowered for k in ("agy-quota", "quota check", "choose sonnet", "choose wait", "quota clear")):
        return True
    if any(k in lowered for k in ("agy-doctor", "agy-trace", "agy-job list", "agy-job status", "agy-job cancel", "agy-job cancel-all", "cancel-all")):
        return True
    switch_mode, is_sole_switch = parse_mode_switch_intent(prompt)
    if switch_mode is not None and is_sole_switch:
        return True
    sel_mode, is_sole_sel = parse_mode_selection_intent(prompt)
    if sel_mode is not None and is_sole_sel:
        return True
    return False


def _presents_mode_choices(message: str) -> bool:
    if not message or not isinstance(message, str):
        return False
    lowered = message.lower()
    has_strict = "always use agy (strict)" in lowered or ("always use agy" in lowered and "strict" in lowered)
    has_soft = "use agy when appropriate (soft)" in lowered or ("use agy when appropriate" in lowered and "soft" in lowered)
    return has_strict and has_soft


def _extract_last_message(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("last_assistant_message", "last_message", "message", "response", "content", "text"):
        val = data.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            text = val.get("text") or val.get("content") or val.get("message")
            if isinstance(text, str):
                return text
        if isinstance(val, list):
            parts = []
            for b in val:
                if isinstance(b, dict) and b.get("text"):
                    parts.append(str(b["text"]))
                elif isinstance(b, str):
                    parts.append(b)
            if parts:
                return "\n".join(parts)

    messages = data.get("messages")
    if isinstance(messages, list) and messages:
        for msg in reversed(messages):
            if isinstance(msg, dict):
                role = msg.get("role")
                if role in ("assistant", None):
                    c = msg.get("content") or msg.get("text") or msg.get("message")
                    if isinstance(c, str):
                        return c
                    if isinstance(c, list):
                        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("text")]
                        return "\n".join(parts)
            elif isinstance(msg, str):
                return msg

    return ""


def _iter_mode_answer_text(value: object, depth: int = 0):
    """Yield likely AskUserQuestion answer values without ingesting its prompt/options."""
    if depth > 8:
        return
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return
        # Claude may wrap a structured tool result in a JSON text block.
        if stripped[:1] in {"{", "["}:
            try:
                decoded = json.loads(stripped)
            except Exception:
                decoded = None
            if decoded is not None:
                yield from _iter_mode_answer_text(decoded, depth + 1)
                return
        yield value
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_mode_answer_text(item, depth + 1)
        return
    if not isinstance(value, dict):
        return

    preferred = {
        "answer", "answers", "choice", "choices", "selected", "selected_option",
        "selectedoption", "selection", "value", "values", "response", "result",
        "content", "text",
    }
    found_preferred = False
    for key, item in value.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalized in preferred or normalized.startswith("answer"):
            found_preferred = True
            yield from _iter_mode_answer_text(item, depth + 1)
    if found_preferred:
        return

    # Some Claude payloads put the answer under an arbitrary question id. Do
    # not recurse through prompt/options metadata, which would otherwise make
    # the available choices look like a user selection.
    ignored = {"question", "questions", "options", "option", "header", "description", "label"}
    for key, item in value.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalized in ignored:
            continue
        yield from _iter_mode_answer_text(item, depth + 1)


def _extract_mode_from_answer(data: dict) -> str | None:
    """Extract a strict/soft choice from Claude's AskUserQuestion result."""
    response = data.get("tool_response")
    if response is None:
        response = data.get("toolResponse")
    if response is None:
        response = data.get("result")
    if response is None:
        response = data.get("response")
    payloads = [response, data.get("answers"), data.get("answer")]
    seen: set[str] = set()
    for payload in payloads:
        for candidate in _iter_mode_answer_text(payload):
            key = candidate.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            mode, is_sole = parse_mode_selection_intent(key)
            if mode is not None and is_sole:
                return mode
    return None


def handle_session_start(data: dict, session_id: str) -> None:
    matcher = str(data.get("matcher") or "").lower()
    source = str(data.get("source") or "").lower()
    trigger = str(data.get("trigger") or "").lower()
    compact_flag = bool(data.get("compact") or data.get("is_compact"))
    if "compact" in {matcher, source, trigger} or compact_flag:
        return

    state = _read_state(session_id, data=data)
    if "clear" in {matcher, source, trigger}:
        current_mode = state.get("mode")
        state = _default_state()
        state["mode"] = current_mode
        if current_mode in {"strict", "soft"}:
            state["question_presented"] = False

    state["turn_id"] = ""
    state["is_substantive"] = False
    state["native_helper_used"] = False
    state["native_small_ops"] = 0
    state["native_small_path"] = ""
    state["agy_attempted"] = False
    state["agy_success"] = False
    state["agy_failed"] = False
    state["agy_pending"] = False
    state["agy_task_id"] = ""
    state["last_agy_error"] = ""
    state["denied_categories"] = []
    state["warned_categories"] = []
    state["continuation_count"] = 0
    state["user_mode_selection"] = False
    state["awaiting_user_input"] = False
    state["question_presented"] = False

    update_context = _polyphony_update_context()
    # Preserve the old lightweight setup warning without a second hook process
    # or launching a headless Windows CLI. Full diagnostics remain agy-doctor.
    agy_path = os.environ.get("AGY_PATH", "")
    native_agy = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
    setup_notes = []
    if not (shutil.which("agy") or (agy_path and Path(agy_path).is_file()) or native_agy.is_file()):
        setup_notes.append("[Polyphony setup] Antigravity CLI is not available. Install/authenticate agy before delegating; use agy-doctor for diagnostics.")
    if os.name == "nt" and importlib.util.find_spec("winpty") is None:
        setup_notes.append("[Polyphony setup] This Python needs pywinpty for native Windows ConPTY delegation.")
    update_context = "\n\n".join(filter(None, (*setup_notes, update_context)))
    if os.environ.get("CLAUDE_PLUGIN_OPTION_CODING_POLICY", "on").lower() not in {"off", "false", "0", "no", "disabled"}:
        try:
            policy = json.loads(Path(__file__).with_name("policy-context.json").read_text(encoding="utf-8"))
            shared_policy = f"{AGY_PROMPT_DISCIPLINE}\n\n{policy['hookSpecificOutput']['additionalContext']}"
            update_context = "\n\n".join(filter(None, (shared_policy, update_context)))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    if state.get("mode") not in {"strict", "soft"}:
        state["mode"] = "soft"
    _write_state(session_id, state)
    if state["mode"] == "strict":
        mode_context = (
            "[Agy routing] Strict routing is active. Substantive work must be delegated to Antigravity; "
            "bounded single-file checks, tiny corrections, local orchestration, and host-only capabilities remain available natively."
        )
    else:
        mode_context = (
            "[Agy routing] Soft routing is active. Substantive work may be completed natively or delegated. "
            "Delegation reminders are advisory."
        )
    context = f"{mode_context}\n\n{update_context}" if update_context else mode_context
    _emit_context("SessionStart", context)


def handle_session_end(session_id: str, data: dict | None = None) -> None:
    state = _read_state(session_id, data=data)
    mode = state.get("mode")
    if mode in {"strict", "soft"}:
        preserved = _default_state()
        preserved["mode"] = mode
        _write_state(session_id, preserved)


def handle_user_prompt_submit(data: dict, state: dict, session_id: str, turn_id: str) -> None:
    prompt = data.get("prompt") or _text(data)

    state["turn_id"] = turn_id
    state["is_substantive"] = False
    state["native_helper_used"] = False
    state["native_small_ops"] = 0
    state["native_small_path"] = ""
    state["agy_attempted"] = False
    state["agy_success"] = False
    state["agy_failed"] = False
    state["agy_pending"] = False
    state["agy_task_id"] = ""
    state["last_agy_error"] = ""
    state["denied_categories"] = []
    state["warned_categories"] = []
    state["continuation_count"] = 0
    state["user_mode_selection"] = False
    state["awaiting_user_input"] = False

    context_to_emit = ""

    mode_target, is_sole = parse_mode_selection_intent(prompt)
    if re.match(r"^[12](?:\b|$)", str(prompt).strip()) and not state.get("question_presented"):
        mode_target = None
    if mode_target in {"strict", "soft"}:
        state["mode"] = mode_target
        state["user_mode_selection"] = is_sole
        persisted = _write_persisted_workspace_mode(mode_target, data)
        if mode_target == "strict":
            context_to_emit = (
                "[Agy routing] Switched Agy routing mode to strict. "
                "Substantive Agy-capable work must be delegated; bounded single-file checks, tiny corrections, local orchestration, and host-only capabilities remain available natively."
            )
        else:
            context_to_emit = (
                "[Agy routing] Switched Agy routing mode to soft. "
                "Substantive work may be completed natively or delegated. Delegation reminders are advisory."
            )
        if not persisted:
            context_to_emit = "[Agy routing] Could not persist the requested mode. Do not claim it was saved; check routing-state directory permissions."

    saved = _write_state(session_id, state)
    if mode_target is not None and not saved:
        context_to_emit = "[Agy routing] Could not save the requested session mode. Do not claim it changed; repair routing-state directory permissions."

    contexts = [value for value in (context_to_emit, _quota_context(), _polyphony_update_context()) if value]
    if (state.get("mode") == "strict"
            and os.environ.get("CLAUDE_PLUGIN_OPTION_CODING_POLICY", "on").lower().strip()
            not in {"off", "false", "0", "no", "disabled"}):
        # Strict mode is the path where every substantive turn may create an Agy
        # request. Reinject immediately before the conductor drafts that request;
        # SessionStart/compact injection remains the all-mode baseline.
        contexts.insert(0, AGY_PROMPT_DISCIPLINE)
    if (state.get("mode") == "soft" and not context_to_emit
            and os.environ.get("CLAUDE_PLUGIN_OPTION_DELEGATION_NUDGE", "on").lower().strip() not in {"off", "false", "0", "no", "disabled"}
            and not any(token in str(prompt).lower() for token in ("antigravity", "agy-delegate", "agy-job"))
            and re.search(r"all files|every file|across the codebase|entire codebase|whole repo|migrat|generate tests|test coverage|exhaustive test|scaffold|boilerplate|deep research|web search|一括|全ファイル|すべてのファイル|網羅|移行|大量|横断|リポジトリ全体", str(prompt), re.I)):
        contexts.append(f"{AGY_PROMPT_DISCIPLINE} This soft-mode delegation reminder is advisory.")
    if contexts:
        _emit_context("UserPromptSubmit", "\n\n".join(contexts))


def handle_pre_tool_use(data: dict, state: dict, session_id: str) -> None:
    tool_name = str(data.get("tool_name") or data.get("toolName") or data.get("name") or "")
    tool_input = data.get("tool_input")
    if tool_input is None:
        tool_input = data.get("toolInput", data.get("arguments", {}))
    if not isinstance(tool_input, dict):
        return

    budget_error = _agy_prompt_budget_violation(tool_name, tool_input)
    if budget_error:
        # Prompt optimization is mandatory in both strict and soft routing modes.
        _emit_deny("PreToolUse", budget_error)
        return

    if is_control_plane_exempt(tool_name, tool_input):
        if re.sub(r"[^a-z0-9]", "", tool_name.lower().split(".")[-1]) in {"askuserquestion", "requestuserinput", "requestuserinputasync"}:
            state["awaiting_user_input"] = True
            state["question_presented"] = _presents_mode_choices(json.dumps(tool_input, ensure_ascii=False))
            _write_state(session_id, state)
        if _small_local_helper(tool_name, tool_input):
            state["native_helper_used"] = True
            _write_state(session_id, state)
        return

    if is_any_agy_call(tool_name, tool_input):
        if is_work_producing_agy_call(tool_name, tool_input):
            state["is_substantive"] = True
            state["awaiting_user_input"] = False
            _write_state(session_id, state)
        if tool_name.lower() in SHELL_TOOL_NAMES:
            command = _get_shell_command(tool_input) or ""
            if parse_single_agy_shell_command(command) is None and _agy_compound_command(command) is not None:
                _emit_context(
                    "PreToolUse",
                    "Agy command accepted in strict mode. For lower shell-quoting risk and a smaller command, prefer a direct wrapper call or pass the task via stdin (`agy-delegate ... -`); the current command's `cd`/prompt-file preparation is allowed because the actual worker is Agy.",
                )
        return

    category = _classify(tool_name, tool_input)
    if category is None:
        return

    mode = state.get("mode")
    effective_strict = mode == "strict"
    quota_context = _quota_context()

    if category == "external":
        # Host-only capabilities without a proven Agy equivalent stay native.
        state["native_helper_used"] = True
        _write_state(session_id, state)
        warned = set(state.get("warned_categories") or [])
        if "external" not in warned:
            warned.add("external")
            state["warned_categories"] = sorted(warned)
            _write_state(session_id, state)
            activity, route = REMINDERS["external"]
            context = (
                f"AGY fırsat uyarısı: Şu an {activity}. Bunu doğrudan Claude context'inde "
                f"sürdürmek yerine {route} kullanmak genellikle daha az Claude tokenı harcar. "
                "Bu araç çağrısı engellenmedi. Yalnızca küçük bir conductor kontrolüyse, kullanıcı "
                "etkileşimi gerekiyorsa, AGY aynı erişime sahip değilse veya AGY kanıtında somut bir "
                "sorun varsa doğrudan devam et."
            )
            if quota_context:
                context = f"{context}\n\n{quota_context}"
            _emit_context("PreToolUse", context)
        elif quota_context:
            _emit_context("PreToolUse", quota_context)
        return

    if effective_strict:
        small_path = _bounded_native_operation(data, tool_name, tool_input, state)
        if small_path is not None:
            state["native_helper_used"] = True
            state["native_small_ops"] = int(state.get("native_small_ops") or 0) + 1
            state["native_small_path"] = small_path
            _write_state(session_id, state)
            return
        state["is_substantive"] = True
        state["awaiting_user_input"] = False
        denied = set(state.get("denied_categories") or [])
        denied.add(category)
        state["denied_categories"] = sorted(denied)
        _write_state(session_id, state)

        instruction = DENIAL_INSTRUCTIONS.get(
                category,
                "Strict mode: this substantive operation must be routed through an Agy worker. Native execution is denied.",
        )
        if quota_context:
            instruction = f"{instruction}\n\n{quota_context}"
        _emit_deny("PreToolUse", instruction)
        return

    # Soft mode advisory
    state["is_substantive"] = True
    warned = set(state.get("warned_categories") or [])
    if category in warned:
        if quota_context:
            _emit_context("PreToolUse", quota_context)
        return
    warned.add(category)
    state["warned_categories"] = sorted(warned)
    _write_state(session_id, state)

    activity, route = REMINDERS[category]
    context = (
        f"AGY fırsat uyarısı: Şu an {activity}. Bunu doğrudan Claude context'inde "
        f"sürdürmek yerine {route} kullanmak genellikle daha az Claude tokenı harcar. "
        "Bu araç çağrısı engellenmedi. Yalnızca küçük bir conductor kontrolüyse, kullanıcı "
        "etkileşimi gerekiyorsa, AGY aynı erişime sahip değilse veya AGY kanıtında somut bir "
        "sorun varsa doğrudan devam et."
    )
    if quota_context:
        context = f"{context}\n\n{quota_context}"
    _emit_context("PreToolUse", context)


def handle_post_tool_use(event: str, data: dict, state: dict, session_id: str) -> None:
    tool_name = str(data.get("tool_name") or data.get("toolName") or data.get("name") or "")
    tool_input = data.get("tool_input")
    if tool_input is None:
        tool_input = data.get("toolInput", data.get("arguments", {}))
    if not isinstance(tool_input, dict):
        tool_input = {}

    # AskUserQuestion answers may still be used for an explicit mode change in
    # Claude Code desktop. Persist that answer before evaluating substantive Agy
    # work so the very next tool sees the choice; this path is optional now that
    # sessions default to soft without a mandatory question.
    normalized_tool = re.sub(r"[^a-z0-9]", "", tool_name.lower())
    if event == "PostToolUse" and normalized_tool in MODE_SELECTION_TOOLS:
        selected_mode = _extract_mode_from_answer(data)
        if selected_mode is not None:
            state["mode"] = selected_mode
            state["question_presented"] = False
            state["user_mode_selection"] = True
            state["is_substantive"] = False
            state["denied_categories"] = []
            state["warned_categories"] = []
            state["continuation_count"] = 0
            persisted = _write_persisted_workspace_mode(selected_mode, data)
            saved = _write_state(session_id, state)
            if not persisted or not saved:
                _emit_context("PostToolUse", "[Agy routing] Mode persistence failed. Do not claim the selection was saved; check routing-state directory permissions.")
                return
            label = "Always use Agy (strict)" if selected_mode == "strict" else "Use Agy when appropriate (soft)"
            _emit_context(
                "PostToolUse",
                f"[Agy routing] Recorded your selection: {label}. Continue the original request using this mode.",
            )
            return

    response = data.get("tool_response") if data.get("tool_response") is not None else data.get("toolResponse", data.get("result", data.get("response")))

    # Claude's TaskOutput (and equivalent host tools) are not themselves Agy
    # calls, but they complete a previously recorded background delegation.
    # Consume their terminal result so a successful worker can release the
    # strict Stop gate and a terminal empty/error result remains a failure.
    if state.get("agy_pending") and _is_background_result_tool(tool_name):
        if event == "PostToolUseFailure":
            state["agy_pending"] = False
            state["agy_failed"] = True
            state["agy_success"] = False
            state["last_agy_error"] = str(data.get("error") or "background result collection failed")
        elif _background_result_is_pending(data, tool_input, response):
            state["agy_task_id"] = _background_task_id(data, tool_input, response) or state.get("agy_task_id", "")
        else:
            success, err = is_agy_response_successful(response)
            state["agy_pending"] = False
            state["agy_task_id"] = _background_task_id(data, tool_input, response) or state.get("agy_task_id", "")
            if success:
                state["agy_success"] = True
                state["agy_failed"] = False
                state["last_agy_error"] = ""
            else:
                state["agy_success"] = False
                state["agy_failed"] = True
                state["last_agy_error"] = err
        _write_state(session_id, state)
        return

    if not is_work_producing_agy_call(tool_name, tool_input):
        return

    state["agy_attempted"] = True
    state["is_substantive"] = True

    if event == "PostToolUseFailure":
        state["agy_pending"] = False
        state["agy_failed"] = True
        state["agy_success"] = False
        state["last_agy_error"] = str(data.get("error") or "tool execution failed")
    else:
        if _background_result_is_pending(data, tool_input, response):
            state["agy_pending"] = True
            state["agy_task_id"] = (
                _background_task_id(data, tool_input, response)
                or _agy_job_started_id(response)
                or state.get("agy_task_id", "")
            )
            state["agy_success"] = False
            state["agy_failed"] = False
            state["last_agy_error"] = ""
        else:
            success, err = is_agy_response_successful(response)
            state["agy_pending"] = False
            state["agy_task_id"] = _background_task_id(data, tool_input, response) or state.get("agy_task_id", "")
            if success:
                state["agy_success"] = True
                state["agy_failed"] = False
                state["last_agy_error"] = ""
            else:
                state["agy_success"] = False
                state["agy_failed"] = True
                state["last_agy_error"] = err

    _write_state(session_id, state)


def handle_stop(data: dict, state: dict, session_id: str) -> None:
    stop_active = bool(data.get("stop_hook_active") or data.get("stopHookActive"))
    continuation_count = state.get("continuation_count", 0)

    if stop_active:
        return

    mode = state.get("mode")

    if mode != "strict":
        return

    # Strict mode
    # An asynchronous launcher acknowledgement is not a failure and must not
    # be mistaken for a completed delegation. Keep the turn open until the
    # host collects a terminal TaskOutput/background result. This check is
    # intentionally before the generic continuation cap: allowing a stop here
    # would reintroduce the false-positive race this state represents.
    if state.get("agy_pending"):
        state["continuation_count"] = continuation_count + 1
        _write_state(session_id, state)
        task_id = state.get("agy_task_id")
        task_hint = f" (task id: {task_id})" if task_id else ""
        _emit_stop_block(
            "An Agy background worker is still running and has not produced a final result"
            f"{task_hint}. Do not end the turn or report an empty-output failure yet; "
            "collect/wait for the host background task with TaskOutput (or the equivalent "
            "job result operation), then continue once its terminal result is available."
        )
        return

    if continuation_count >= 3:
        return

    if state.get("agy_failed"):
        state["continuation_count"] = continuation_count + 1
        _write_state(session_id, state)
        err = state.get("last_agy_error") or "Agy worker error"
        _emit_stop_block(
            f"Agy delegation failed ({err}). In strict mode, surface the failure or quota choice to the user rather than silently pretending success or continuing natively."
        )
        return

    if state.get("agy_success") or not state.get("is_substantive"):
        return
    if state.get("awaiting_user_input"):
        return
    if state.get("native_helper_used") and not state.get("agy_attempted") and not state.get("denied_categories"):
        return

    state["continuation_count"] = continuation_count + 1
    _write_state(session_id, state)
    _emit_stop_block(
        "Strict Agy routing is active. Substantive work must be delegated to Antigravity (agy-scout, agy-delegate, agy-review, or Polyphony MCP tools) and complete successfully before stopping."
    )


def main():
    data = _load_input()
    event = data.get("hook_event_name") or data.get("hookEventName") or data.get("event") or ""
    session_id = str(data.get("session_id") or data.get("sessionId") or data.get("thread_id") or data.get("threadId") or "default-session")
    turn_id = str(data.get("turn_id") or data.get("turnId") or data.get("prompt_id") or data.get("promptId") or "")

    if event == "SessionStart":
        handle_session_start(data, session_id)
        return

    if event == "SessionEnd":
        handle_session_end(session_id, data=data)
        return

    state = _read_state(session_id, data=data)

    if turn_id and turn_id != state.get("turn_id"):
        state["turn_id"] = turn_id
        state["is_substantive"] = False
        state["native_helper_used"] = False
        state["native_small_ops"] = 0
        state["native_small_path"] = ""
        state["agy_attempted"] = False
        state["agy_success"] = False
        state["agy_failed"] = False
        state["agy_pending"] = False
        state["agy_task_id"] = ""
        state["last_agy_error"] = ""
        state["denied_categories"] = []
        state["warned_categories"] = []
        state["continuation_count"] = 0
        state["user_mode_selection"] = False
        state["awaiting_user_input"] = False
        _write_state(session_id, state)

    if event == "UserPromptSubmit":
        handle_user_prompt_submit(data, state, session_id, turn_id)
        return

    if event == "PreToolUse":
        handle_pre_tool_use(data, state, session_id)
        return

    if event in {"PostToolUse", "PostToolUseFailure"}:
        handle_post_tool_use(event, data, state, session_id)
        return

    if event == "Stop":
        handle_stop(data, state, session_id)
        return


if __name__ == "__main__":
    main()
