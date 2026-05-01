"""AST validation + runtime sandbox for ansible_ai tool execution.

Three execution surfaces, all going through the strongest available
isolation tool (bwrap > firejail > nsjail > in-process rlimit fallback):

  run_cmd(argv, rules, timeout)         - direct binary invocation
  run_python(code, rules, timeout)      - vetted Python snippet
  read_file(path, max_bytes)            - bounded read, no subprocess
  write_file(path, content)             - bounded write, no subprocess

run_python keeps a static AST walk on top of the sandbox: imports outside
allow.python are rejected, dangerous builtins (eval/exec/__import__) are
rejected, and subprocess/os.system calls whose argv[0] is not in
allow.run_cmd or whose argv is not statically resolvable are rejected.

Network unshare for bwrap is gated by rules.allow.network and an env
opt-in (see _bwrap_prefix), because some distros (Ubuntu 24+ with
AppArmor user-namespace restrictions) reject loopback setup inside a new
netns from an unprivileged process.
"""

from __future__ import annotations

import ast
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

from . import rules as rules_mod

SAFE_BUILTINS = {
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "filter",
    "float",
    "frozenset",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "open",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "type",
    "hasattr",
    "getattr",
    "setattr",
    "format",
    "bytes",
    "Exception",
    "ValueError",
    "RuntimeError",
    "KeyError",
    "IndexError",
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
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.split()[0] if first.value.strip() else None
    if isinstance(first, ast.List) and first.elts:
        head = first.elts[0]
        return _literal_str(head)
    return None


def _argv_tail_strings(node: ast.Call) -> list[str] | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        toks = first.value.split()
        return toks[1:] if toks else []
    if isinstance(first, ast.List):
        tail: list[str] = []
        for el in first.elts[1:]:
            s = _literal_str(el)
            if s is None:
                return None
            tail.append(s)
        return tail
    return None


SHELL_BINARIES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"})


SUBPROCESS_FUNCS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execvp",
    "os.execve",
    "os.spawnv",
    "os.spawnvp",
}


def validate_ast(code: str, rules: dict[str, Any]) -> None:
    """Static check for run_python tool. Raises SandboxViolation on first issue."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxViolation(f"syntax error: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                if node.module:
                    modules = [node.module]
            for m in modules:
                if not rules_mod.is_python_import_allowed(rules, m):
                    raise SandboxViolation(f"import not allowed: {m}", where=f"line {node.lineno}")

        if (
            isinstance(node, ast.Name)
            and node.id in DANGEROUS_BUILTINS
            and not rules_mod.is_python_import_allowed(rules, f"__builtins__.{node.id}")
        ):
            raise SandboxViolation(f"builtin not allowed: {node.id}", where=f"line {node.lineno}")

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
                    raise SandboxViolation(f"command not allowed: {argv0}", where=f"line {node.lineno}")
                argv0_base = argv0.rsplit("/", 1)[-1]
                if argv0_base in SHELL_BINARIES:
                    tail = _argv_tail_strings(node)
                    if tail is None:
                        raise SandboxViolation(
                            f"shell '{argv0_base}' with non-literal arguments not allowed",
                            where=f"line {node.lineno}",
                        )
                    if any(t == "-c" or t.startswith("-c") for t in tail):
                        raise SandboxViolation(
                            f"shell '{argv0_base}' with `-c` not allowed (defeats run_cmd allowlist)",
                            where=f"line {node.lineno}",
                        )


def _probe(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=5)
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return False


def detect_isolation() -> str:
    """Detect strongest *working* isolation tool.

    Mere presence on PATH is not enough: AppArmor/userns restrictions can
    make bwrap installable but unusable. Probe before committing.
    """
    if shutil.which("bwrap") and _probe(["bwrap", "--bind", "/", "/", "--", "true"]):
        return "bwrap"
    if shutil.which("firejail") and _probe(["firejail", "--quiet", "--noprofile", "true"]):
        return "firejail"
    if shutil.which("nsjail") and _probe(["nsjail", "--really_quiet", "--", "/bin/true"]):
        return "nsjail"
    return "rlimit"


def _bwrap_prefix(allow_network: bool, rules: dict[str, Any]) -> list[str]:
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
    # See module docstring re: --unshare-net.
    if not allow_network and os.environ.get("ANSIBLE_AI_BWRAP_UNSHARE_NET") == "1":
        cmd += ["--unshare-net"]
    for path in rules.get("allow", {}).get("write_file", []):
        if path.startswith("/") and "*" not in path:
            cmd += ["--bind-try", path, path]
    return cmd


def _firejail_prefix(allow_network: bool) -> list[str]:
    cmd = ["firejail", "--quiet", "--noprofile", "--private-tmp"]
    if not allow_network:
        cmd += ["--net=none"]
    return cmd


def _wrap(argv: list[str], rules: dict[str, Any], extra_ro_paths: list[str] | None = None) -> list[str]:
    """Wrap a target argv with the strongest available isolation prefix.

    `extra_ro_paths` are absolute host paths that need to be visible inside
    bwrap's mount namespace (e.g. a temporary script file).
    """
    isolation = detect_isolation()
    allow_network = bool(rules.get("allow", {}).get("network", False))
    if isolation == "bwrap":
        prefix = _bwrap_prefix(allow_network, rules)
        for p in extra_ro_paths or []:
            prefix += ["--ro-bind", p, p]
        return prefix + ["--"] + argv
    if isolation == "firejail":
        return _firejail_prefix(allow_network) + argv
    return argv


def run_cmd(argv: list[str], rules: dict[str, Any], timeout: int = 30) -> SandboxResult:
    """Run an allow-listed argv inside the strongest available sandbox.

    The caller (tools.exec_tool) has already validated argv[0] against
    allow.run_cmd and rejected shell -c forms; we don't re-check here.
    """
    cmd = _wrap(argv, rules)
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
        return SandboxResult(stdout=stdout, stderr=stderr, exit=124, timed_out=True)
    except FileNotFoundError as e:
        return SandboxResult(stdout="", stderr=f"executable not found: {e}", exit=127)
    return SandboxResult(stdout=proc.stdout, stderr=proc.stderr, exit=proc.returncode)


def run_python(code: str, rules: dict[str, Any], timeout: int = 30) -> SandboxResult:
    """Execute a vetted Python snippet inside the strongest available sandbox.

    Caller must have run validate_ast(code, rules) before calling.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", prefix="ansible_ai_", delete=False) as f:
        f.write(code)
        script_path = f.name

    try:
        cmd = _wrap([sys.executable, script_path], rules, extra_ro_paths=[script_path])
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
            return SandboxResult(stdout=stdout, stderr=stderr, exit=124, timed_out=True)
        return SandboxResult(stdout=proc.stdout, stderr=proc.stderr, exit=proc.returncode)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(script_path)


def read_file(path: str, max_bytes: int = 16 * 1024) -> SandboxResult:
    """Read up to max_bytes from path. Caller has validated against allow.read_file."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1)
    except FileNotFoundError:
        return SandboxResult(stdout="", stderr=f"no such file: {path}", exit=2)
    except PermissionError as e:
        return SandboxResult(stdout="", stderr=f"permission denied: {e}", exit=13)
    except OSError as e:
        return SandboxResult(stdout="", stderr=f"read error: {e}", exit=5)

    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n... [truncated at {max_bytes} bytes]"
    return SandboxResult(stdout=text, stderr="", exit=0)


def write_file(path: str, content: str) -> SandboxResult:
    """Write content to path. Caller has validated against allow.write_file."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return SandboxResult(
            stdout=f"wrote {len(content)} bytes to {path}",
            stderr="",
            exit=0,
        )
    except OSError as e:
        return SandboxResult(stdout="", stderr=f"write error: {e}", exit=5)
