"""Provider-neutral tool definitions and target-side dispatch for ansible_ai.

The agent loop talks to the LLM via *tools* (run_cmd, read_file, write_file,
optionally run_python and done). Each provider client serializes this list
into its own wire format. Each target host receives one tool call at a time
through ai_exec and returns a structured result.

Two pieces live here:

  build_tools(rules)   - render the tool list for the LLM, gated by rules.
  exec_tool(name, ..)  - run one tool call on the target inside the sandbox.
"""

from __future__ import annotations

import fnmatch
import os.path
from typing import Any

from . import rules as rules_mod
from . import sandbox as sandbox_mod
from .sandbox import SandboxResult


def _blocked(reason: str) -> SandboxResult:
    return SandboxResult(stdout="", stderr="", exit=126, blocked_by_rule=reason)


# Tool name constants - referenced by orchestrator, ai_exec, prompts.
RUN_CMD = "run_cmd"
READ_FILE = "read_file"
WRITE_FILE = "write_file"
RUN_PYTHON = "run_python"
DONE = "done"

ALL_TOOLS = (RUN_CMD, READ_FILE, WRITE_FILE, RUN_PYTHON, DONE)


def build_tools(rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the tool schema list for the LLM, gated by the rule set.

    A tool is omitted when its allow primitive is empty:
      - run_cmd      : allow.run_cmd
      - read_file    : allow.read_file
      - write_file   : allow.write_file
      - run_python   : allow.python   (opt-in; many fleets disable this)

    `done` is always present so the model can terminate.
    """
    allow = rules.get("allow", {})
    tools: list[dict[str, Any]] = []

    if allow.get("run_cmd"):
        cmds = ", ".join(sorted(allow["run_cmd"]))
        tools.append(
            {
                "name": RUN_CMD,
                "description": (
                    f"Execute one allow-listed command on the target host with a "
                    f"statically-known argv. argv[0] must be one of: {cmds}. "
                    f"Shell forms ('-c <payload>') are rejected. Returns stdout, stderr, exit."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "argv list. argv[0] is the binary name.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why this command advances the investigation.",
                        },
                    },
                    "required": ["argv", "reason"],
                },
            }
        )

    if allow.get("read_file"):
        globs = ", ".join(allow["read_file"])
        tools.append(
            {
                "name": READ_FILE,
                "description": (
                    f"Read a UTF-8 file. The path must match one of: {globs}. "
                    f"Content is truncated to ~16 KB; for larger files use run_cmd "
                    f"with grep/head/tail."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path."},
                        "reason": {"type": "string"},
                    },
                    "required": ["path", "reason"],
                },
            }
        )

    if allow.get("write_file"):
        globs = ", ".join(allow["write_file"])
        tools.append(
            {
                "name": WRITE_FILE,
                "description": (
                    f"Write a UTF-8 file. The path must match one of: {globs}. "
                    f"Use sparingly; this is the only mutating tool."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["path", "content", "reason"],
                },
            }
        )

    if allow.get("python"):
        imports = ", ".join(sorted(allow["python"]))
        tools.append(
            {
                "name": RUN_PYTHON,
                "description": (
                    f"Execute a sandboxed Python 3 snippet. Use only when run_cmd "
                    f"cannot express the analysis (multi-step computation, structured "
                    f"parse, correlation). Allowed imports: {imports}. Subprocess argv[0] "
                    f"must still be in run_cmd. Print results to stdout."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["code", "reason"],
                },
            }
        )

    tools.append(
        {
            "name": DONE,
            "description": (
                "Conclude the investigation. Provide a final summary and stop. "
                "Emit this once you have a diagnosis or after evidence is exhausted."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Human-readable diagnosis.",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["summary", "reason"],
            },
        }
    )

    return tools


# Path patterns that always block reads of credential-bearing files even when
# the operator allows the directory tree they live in. Defense in depth: a
# loose `read_file: ["/etc/**"]` does not need to mean we hand /etc/shadow
# to the LLM verbatim.
ALWAYS_BLOCK_READ = (
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/root/.ssh/**",
    "**/.ssh/id_rsa",
    "**/.ssh/id_ed25519",
    "**/.aws/credentials",
)


READ_FILE_MAX_BYTES = 16 * 1024


def _argv_validate(argv: list[str], rules: dict[str, Any]) -> str | None:
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return "argv must be a non-empty list of strings"
    head = argv[0]
    base = head.rsplit("/", 1)[-1]
    # Match either the basename or the full path - operators may allow-list
    # an absolute path (e.g. /usr/bin/docker) instead of just the binary name.
    if not rules_mod.is_cmd_allowed(rules, base) and not rules_mod.is_cmd_allowed(rules, head):
        return f"command not allowed: {base}"
    if base in sandbox_mod.SHELL_BINARIES:
        for tok in argv[1:]:
            if tok == "-c" or tok.startswith("-c"):
                return f"shell '{base}' with -c is not allowed (defeats run_cmd allowlist)"
    return None


def _resolve_path(path: str, rules: dict[str, Any], mode: str) -> tuple[str | None, str | None]:
    """Validate and canonicalize a path against the rule set.

    Returns (canonical_path, error). On success error is None and canonical_path
    is the lexically-normalized absolute path (no `..` segments, no `//`).
    On failure canonical_path is None and error is the reason string.

    Lexical normalization defeats `/var/log/../../etc/shadow` style traversals
    that fnmatch alone would not catch. Symlink-based escapes are not addressed
    here - the sandbox's read-only mounts are the second line of defense.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return None, "path must be an absolute string"
    normalized = os.path.normpath(path)
    if not normalized.startswith("/"):
        return None, f"path traversal rejected: {path}"
    if mode == "read":
        for pat in ALWAYS_BLOCK_READ:
            if fnmatch.fnmatch(normalized, pat):
                return None, f"read of {normalized} blocked by built-in deny"
    if not rules_mod.is_path_allowed(rules, normalized, mode):
        return None, f"{mode} not allowed: {normalized}"
    return normalized, None


def exec_tool(
    name: str,
    inp: dict[str, Any],
    rules: dict[str, Any],
    timeout: int = 30,
) -> SandboxResult:
    """Dispatch one tool call inside the sandbox and return a structured result.

    `done` is *not* dispatched here - the orchestrator handles termination.
    """
    if name == RUN_CMD:
        argv = inp.get("argv", [])
        reason = _argv_validate(argv, rules)
        if reason:
            return _blocked(reason)
        return sandbox_mod.run_cmd(argv, rules, timeout=timeout)

    if name == READ_FILE:
        canonical, reason = _resolve_path(inp.get("path", ""), rules, "read")
        if reason:
            return _blocked(reason)
        return sandbox_mod.read_file(canonical, max_bytes=READ_FILE_MAX_BYTES)

    if name == WRITE_FILE:
        canonical, reason = _resolve_path(inp.get("path", ""), rules, "write")
        if reason:
            return _blocked(reason)
        content = inp.get("content", "")
        if not isinstance(content, str):
            return _blocked("content must be a string")
        return sandbox_mod.write_file(canonical, content)

    if name == RUN_PYTHON:
        if not rules.get("allow", {}).get("python"):
            return _blocked("run_python disabled (allow.python is empty)")
        code = inp.get("code", "")
        if not isinstance(code, str) or not code.strip():
            return _blocked("code must be a non-empty string")
        try:
            sandbox_mod.validate_ast(code, rules)
        except sandbox_mod.SandboxViolation as e:
            return _blocked(f"{e.reason} ({e.where or 'static'})")
        return sandbox_mod.run_python(code, rules, timeout=timeout)

    return _blocked(f"unknown tool: {name}")
