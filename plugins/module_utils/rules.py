"""Layered rule merge and validation for ansible_ai.

Rules express what an LLM-generated snippet may do on a target. Layered
across collection defaults, group_vars, host_vars, play vars, and task args
following ansible's normal precedence. Deny always wins on conflict.
"""

from __future__ import annotations

import copy
import fnmatch
from typing import Any

PRIMITIVE_KEYS = ("run_cmd", "read_file", "write_file", "python")
ALLOW_KEYS = PRIMITIVE_KEYS + ("network",)
DENY_KEYS = PRIMITIVE_KEYS

DEFAULT_BUDGET = {"max_iterations": 5, "max_tokens": 8000}

EMPTY_RULES: dict[str, Any] = {
    "allow": {k: [] for k in PRIMITIVE_KEYS} | {"network": False},
    "deny": {k: [] for k in DENY_KEYS},
    "budget": dict(DEFAULT_BUDGET),
}


class RuleError(ValueError):
    """Raised when a rules dict fails schema or sanity checks."""


def validate(rules: dict[str, Any]) -> None:
    if not isinstance(rules, dict):
        raise RuleError("rules must be a dict")

    allow = rules.get("allow", {})
    deny = rules.get("deny", {})
    budget = rules.get("budget", {})

    if not isinstance(allow, dict):
        raise RuleError("rules.allow must be a dict")
    if not isinstance(deny, dict):
        raise RuleError("rules.deny must be a dict")
    if not isinstance(budget, dict):
        raise RuleError("rules.budget must be a dict")

    for k, v in allow.items():
        if k == "network":
            if not isinstance(v, bool):
                raise RuleError("rules.allow.network must be bool")
            continue
        if k not in PRIMITIVE_KEYS:
            raise RuleError(f"unknown allow key: {k}")
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise RuleError(f"rules.allow.{k} must be list[str]")

    for k, v in deny.items():
        if k not in DENY_KEYS:
            raise RuleError(f"unknown deny key: {k}")
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise RuleError(f"rules.deny.{k} must be list[str]")

    for cmd_key in ("run_cmd",):
        for entry in allow.get(cmd_key, []) + deny.get(cmd_key, []):
            if any(c in entry for c in "*?["):
                raise RuleError(f"glob not allowed in {cmd_key}: {entry!r}")

    for path_key in ("read_file", "write_file"):
        for entry in allow.get(path_key, []) + deny.get(path_key, []):
            if not entry.startswith("/") and not entry.startswith("**"):
                raise RuleError(f"{path_key} entry must be absolute or **: {entry!r}")

    if "max_iterations" in budget and not (
        isinstance(budget["max_iterations"], int) and budget["max_iterations"] > 0
    ):
        raise RuleError("budget.max_iterations must be positive int")
    if "max_tokens" in budget and not (isinstance(budget["max_tokens"], int) and budget["max_tokens"] > 0):
        raise RuleError("budget.max_tokens must be positive int")


def merge(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge rule layers in precedence order (lowest first to highest last).

    Allow lists union across layers. Deny lists union across layers.
    A deny entry in any layer overrides an allow entry for the same primitive.
    Budget values from later layers replace earlier ones.
    """
    out = copy.deepcopy(EMPTY_RULES)

    for layer in layers:
        if not layer:
            continue
        validate(layer)
        for k in PRIMITIVE_KEYS:
            for v in layer.get("allow", {}).get(k, []):
                if v not in out["allow"][k]:
                    out["allow"][k].append(v)
        if "network" in layer.get("allow", {}):
            out["allow"]["network"] = bool(layer["allow"]["network"])
        for k in DENY_KEYS:
            for v in layer.get("deny", {}).get(k, []):
                if v not in out["deny"][k]:
                    out["deny"][k].append(v)
        for k in ("max_iterations", "max_tokens"):
            if k in layer.get("budget", {}):
                out["budget"][k] = layer["budget"][k]

    for k in DENY_KEYS:
        out["allow"][k] = [v for v in out["allow"][k] if v not in out["deny"][k]]

    return out


def is_cmd_allowed(rules: dict[str, Any], cmd: str) -> bool:
    if cmd in rules["deny"]["run_cmd"]:
        return False
    return cmd in rules["allow"]["run_cmd"]


def is_path_allowed(rules: dict[str, Any], path: str, mode: str) -> bool:
    """mode is 'read' or 'write'."""
    key = "read_file" if mode == "read" else "write_file"
    for pat in rules["deny"].get(key, []):
        if fnmatch.fnmatch(path, pat) or pat == "**":
            return False
    return any(fnmatch.fnmatch(path, pat) or pat == "**" for pat in rules["allow"].get(key, []))


def is_python_import_allowed(rules: dict[str, Any], module: str) -> bool:
    if module in rules["deny"]["python"]:
        return False
    if any(module.startswith(d + ".") for d in rules["deny"]["python"]):
        return False
    if module in rules["allow"]["python"]:
        return True
    return any(module.startswith(a + ".") for a in rules["allow"]["python"])
