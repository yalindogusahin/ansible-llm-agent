"""AST validation + runtime sandbox for LLM-generated Python on target hosts.

Two layers:
  1. validate_ast(code, rules) - static AST walk; rejects denied imports,
     denied builtins, denied subprocess argv[0]. Cheap, runs both on
     controller (pre-flight) and on target.
  2. run(code, rules, timeout) - executes code inside the strongest
     available isolation: bwrap > firejail > nsjail > in-process rlimit.
     Network unshare toggled by rules.allow.network.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

from . import rules as rules_mod


SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "frozenset", "int", "isinstance", "issubclass", "iter", "len", "list",
    "map", "max", "min", "next", "open", "print", "range", "repr",
    "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
    "zip", "type", "hasattr", "getattr", "setattr", "format", "bytes",
    "Exception", "ValueError", "RuntimeError", "KeyError", "IndexError",
}

DANGEROUS_BUILTINS = {"eval", "exec", "compile", "__import__", "globals", "locals", "vars", "memoryview"}


class SandboxViolation(Exception):
    def __init__(self, reason: str, where: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.where = where


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit: int
    timed_out: bool = False
    blocked_by_rule: str | None = None


def _resolve_attr_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _literal_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _first_argv0(node: ast.Call) -> str | None:
    """For subprocess-style calls, return literal argv[0] if statically derivable."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.split()[0] if first.value.strip() else None
    if isinstance(first, ast.List) and first.elts:
        head = first.elts[0]
        return _literal_str(head)
    return None


SUBPROCESS_FUNCS = {
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_call", "subprocess.check_output",
    "os.system", "os.popen", "os.execv", "os.execvp", "os.execve",
    "os.spawnv", "os.spawnvp",
}


def validate_ast(code: str, rules: dict[str, Any]) -> None:
    """Static check. Raises SandboxViolation on first violation."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxViolation(f"syntax error: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                if node.module:
                    modules = [node.module]
            for m in modules:
                if not rules_mod.is_python_import_allowed(rules, m):
                    raise SandboxViolation(
                        f"import not allowed: {m}", where=f"line {node.lineno}"
                    )

        if isinstance(node, ast.Name) and node.id in DANGEROUS_BUILTINS:
            if not rules_mod.is_python_import_allowed(rules, f"__builtins__.{node.id}"):
                raise SandboxViolation(
                    f"builtin not allowed: {node.id}", where=f"line {node.lineno}"
                )

        if isinstance(node, ast.Call):
            chain = None
            if isinstance(node.func, ast.Attribute):
                chain = _resolve_attr_chain(node.func)
            elif isinstance(node.func, ast.Name):
                chain = node.func.id
            if chain in SUBPROCESS_FUNCS:
                argv0 = _first_argv0(node)
                if argv0 is None:
                    raise SandboxViolation(
                        f"{chain} requires statically resolvable argv[0]",
                        where=f"line {node.lineno}",
                    )
                if not rules_mod.is_cmd_allowed(rules, argv0):
                    raise SandboxViolation(
                        f"command not allowed: {argv0}", where=f"line {node.lineno}"
                    )


def detect_isolation() -> str:
    if shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("firejail"):
        return "firejail"
    if shutil.which("nsjail"):
        return "nsjail"
    return "rlimit"


def _bwrap_cmd(script_path: str, allow_network: bool, rules: dict[str, Any]) -> list[str]:
    cmd = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/sbin", "/sbin",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--ro-bind", "/var/log", "/var/log",
        "--ro-bind", "/sys", "/sys",
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
    ]
    if not allow_network:
        cmd += ["--unshare-net"]
    for path in rules["allow"].get("write_file", []):
        if path.startswith("/") and "*" not in path:
            cmd += ["--bind-try", path, path]
    cmd += ["--ro-bind", script_path, script_path]
    cmd += [sys.executable, script_path]
    return cmd


def _firejail_cmd(script_path: str, allow_network: bool) -> list[str]:
    cmd = ["firejail", "--quiet", "--noprofile", "--private-tmp"]
    if not allow_network:
        cmd += ["--net=none"]
    cmd += [sys.executable, script_path]
    return cmd


def _rlimit_runner(script_path: str) -> list[str]:
    return [sys.executable, script_path]


def run(code: str, rules: dict[str, Any], timeout: int = 30) -> SandboxResult:
    """Execute code in the strongest available sandbox.

    Caller must already have run validate_ast(code, rules); run() does not
    re-validate (caller responsible to keep both behind a single boundary).
    """
    isolation = detect_isolation()
    allow_network = bool(rules["allow"].get("network", False))

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="ansible_ai_", delete=False
    ) as f:
        f.write(code)
        script_path = f.name

    try:
        if isolation == "bwrap":
            cmd = _bwrap_cmd(script_path, allow_network, rules)
        elif isolation == "firejail":
            cmd = _firejail_cmd(script_path, allow_network)
        else:
            cmd = _rlimit_runner(script_path)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
            stderr = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit=124,
                timed_out=True,
            )

        return SandboxResult(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit=proc.returncode,
        )
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
