#!/usr/bin/env python3
"""Declarative capability classifier shared by Polyphony routing surfaces."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "tool-capabilities.json"
VALID_CAPABILITIES = frozenset({"control-plane", "read-only", "work-producing"})
INTROSPECTION_ARGUMENTS = frozenset({"-h", "--help", "help", "-v", "--version", "version"})


class CapabilityRegistryError(RuntimeError):
    """The bundled capability registry is malformed."""


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CapabilityRegistryError(f"cannot load {REGISTRY_PATH}: {exc}") from exc
    if data.get("schema_version") != 1:
        raise CapabilityRegistryError("unsupported capability registry schema")
    for surface in ("shell", "mcp"):
        entries = data.get(surface)
        if not isinstance(entries, dict):
            raise CapabilityRegistryError(f"{surface} capability map is missing")
        for name, rule in entries.items():
            if not isinstance(name, str) or not isinstance(rule, dict):
                raise CapabilityRegistryError(f"invalid {surface} capability entry")
            values = [rule.get("capability"), rule.get("default")]
            actions = rule.get("actions", {})
            if not isinstance(actions, dict):
                raise CapabilityRegistryError(f"invalid action map for {surface}:{name}")
            values.extend(actions.values())
            if any(value is not None and value not in VALID_CAPABILITIES for value in values):
                raise CapabilityRegistryError(f"invalid capability for {surface}:{name}")
    return data


def normalize_executable(value: str) -> str:
    """Return a platform-neutral command stem for paths from Bash or Windows."""
    normalized = str(value).replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".sh", ".py"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def registered_shell_commands() -> frozenset[str]:
    return frozenset(load_registry()["shell"])


def registered_mcp_tools() -> frozenset[str]:
    return frozenset(load_registry()["mcp"])


def _rule_capability(rule: Mapping[str, Any], action: str | None) -> str | None:
    if action:
        actions = rule.get("actions", {})
        raw_action = action.lower()
        resolved = actions.get(raw_action)
        if resolved is None:
            resolved = actions.get(raw_action.replace("_", "-"))
        if resolved is None:
            resolved = actions.get(raw_action.replace("-", "_"))
        if resolved:
            return str(resolved)
        if actions:
            return None
    value = rule.get("capability", rule.get("default"))
    return str(value) if value in VALID_CAPABILITIES else None


def shell_capability(tokens: Sequence[str]) -> str | None:
    """Classify one registered shell invocation; unknown commands/actions fail closed."""
    if not tokens:
        return None
    name = normalize_executable(tokens[0])
    rule = load_registry()["shell"].get(name)
    if rule is None:
        return None
    arguments = [str(token).lower() for token in tokens[1:]]
    # Only a standalone help/version request is read-only. A help flag embedded
    # in a longer work command must not turn that whole invocation into an
    # exemption from strict routing.
    if (len(arguments) == 1 and arguments[0] in INTROSPECTION_ARGUMENTS) or (
        len(arguments) == 2
        and arguments[0] in rule.get("actions", {})
        and arguments[1] in INTROSPECTION_ARGUMENTS
    ):
        return "read-only"
    action = arguments[0] if arguments else None
    return _rule_capability(rule, action)


def normalize_mcp_tool(tool_name: str) -> str:
    lowered = str(tool_name).lower()
    for prefix in ("mcp__antigravity__", "mcp__polyphony__"):
        if lowered.startswith(prefix):
            return lowered[len(prefix):]
    return lowered


def mcp_capability(tool_name: str, arguments: Mapping[str, Any] | None = None) -> str | None:
    """Classify a registered Polyphony MCP invocation."""
    name = normalize_mcp_tool(tool_name)
    rule = load_registry()["mcp"].get(name)
    if rule is None:
        return None
    action = str((arguments or {}).get("action") or "").lower() or None
    return _rule_capability(rule, action)
