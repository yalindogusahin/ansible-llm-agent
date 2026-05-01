"""System prompt construction and observation rendering for ansible_ai.

Tool definitions live in tools.build_tools(). The system prompt focuses
on the investigation goal, host context, and operating principles -
allow lists are surfaced through tool descriptions, not duplicated in prose.
"""

from __future__ import annotations

import json
from typing import Any

SECRET_KEY_PATTERNS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "auth",
    "private",
    "ssh_key",
    "cert_key",
)

DEFAULT_FACT_KEYS = (
    "ansible_distribution",
    "ansible_distribution_version",
    "ansible_kernel",
    "ansible_os_family",
    "ansible_architecture",
    "ansible_processor_count",
    "ansible_memtotal_mb",
    "ansible_default_ipv4",
    "ansible_hostname",
    "ansible_fqdn",
    "ansible_service_mgr",
    "ansible_python_version",
)


SYSTEM_PROMPT_TEMPLATE = """You are a Linux investigation agent operating inside an Ansible task on a remote host.

GOAL: {prompt}

How you work:
  * You have a small set of tools (run_cmd, read_file, optionally write_file and run_python, plus done). Each tool's description lists the exact commands or path patterns you may use; anything outside is rejected at the boundary.
  * Prefer small, targeted tool calls. One purpose per call. Inspect output before deciding the next call.
  * When you have a diagnosis, or when evidence is exhausted, call the `done` tool with a concise human-readable summary. Do not stop without calling `done`.
  * Do not request denied operations - the boundary will reject them and waste a turn.

Operator-imposed denies (always blocked, override any allow):
  shell commands: {deny_run_cmd}
  python imports: {deny_python}
  write paths:    {deny_write_file}
  network egress: {network_state}

HOST CONTEXT:
  hostname: {hostname}
  groups:   {groups}
  role:     {role}
  facts:
{facts_block}"""


def _is_secret_key(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in SECRET_KEY_PATTERNS)


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<...>"
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if _is_secret_key(str(k)) else _redact(v, depth + 1)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value]
    return value


def filter_facts(facts: dict[str, Any], keys: tuple[str, ...] = DEFAULT_FACT_KEYS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        if k in facts:
            out[k] = _redact(facts[k])
    return out


def filter_hostvars(hostvars: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in hostvars.items():
        if k.startswith("ansible_"):
            continue
        if _is_secret_key(k):
            out[k] = "<redacted>"
            continue
        out[k] = _redact(v)
    return out


def _format_facts(facts: dict[str, Any]) -> str:
    if not facts:
        return "    (none)"
    lines = []
    for k, v in facts.items():
        rendered = json.dumps(v, default=str)
        if len(rendered) > 200:
            rendered = rendered[:200] + "..."
        lines.append(f"    {k}: {rendered}")
    return "\n".join(lines)


def build_system_prompt(prompt: str, rules: dict[str, Any], host_ctx: dict[str, Any]) -> str:
    deny = rules.get("deny", {})
    allow = rules.get("allow", {})
    return SYSTEM_PROMPT_TEMPLATE.format(
        prompt=prompt.strip(),
        deny_run_cmd=", ".join(deny.get("run_cmd", [])) or "(none)",
        deny_python=", ".join(deny.get("python", [])) or "(none)",
        deny_write_file=", ".join(deny.get("write_file", [])) or "(none)",
        network_state="allowed" if allow.get("network") else "blocked",
        hostname=host_ctx.get("hostname", "<unknown>"),
        groups=", ".join(host_ctx.get("groups", [])) or "(none)",
        role=host_ctx.get("role", "(none)"),
        facts_block=_format_facts(host_ctx.get("facts", {})),
    )


def render_tool_result(stdout: str, stderr: str, exit_code: int, blocked: str | None = None) -> str:
    """Format a tool execution result for feeding back to the model."""
    parts = [f"exit={exit_code}"]
    if blocked:
        parts.append(f"blocked_by_rule={blocked}")
    out = stdout.rstrip()
    err = stderr.rstrip()
    body = "\n".join(parts)
    if out:
        body += f"\nSTDOUT:\n{out[:4000]}"
    if err:
        body += f"\nSTDERR:\n{err[:2000]}"
    return body


AGGREGATE_PROMPT_TEMPLATE = """You are aggregating per-host investigation results from an Ansible play.

GOAL: {prompt}

PER-HOST DIAGNOSES:
{per_host_block}

Synthesize across hosts. Identify common patterns, divergences, and the most
likely cluster-level root cause. Mention which hosts share which symptoms.

When ready, call the `done` tool with a single cluster-level summary."""


def _format_per_host_block(results: Any) -> str:
    if isinstance(results, dict):
        items: list[tuple[str, Any]] = list(results.items())
    elif isinstance(results, list):
        items = [(f"host_{i}", r) for i, r in enumerate(results)]
    else:
        return "(no per-host results provided)"

    blocks: list[str] = []
    for host, r in items:
        if not isinstance(r, dict):
            continue
        diag = r.get("diagnosis", "(no diagnosis)")
        iters = r.get("iterations_used", "?")
        tokens = r.get("tokens_used", {})
        if isinstance(diag, str) and len(diag) > 2000:
            diag = diag[:2000] + "..."
        header = f"--- {host} (iterations={iters}, tokens={tokens}) ---"
        blocks.append(f"{header}\n{diag}")
    return "\n\n".join(blocks) if blocks else "(no per-host results provided)"


def build_aggregate_prompt(prompt: str, results: Any) -> str:
    """Render the cluster-level aggregation system prompt."""
    return AGGREGATE_PROMPT_TEMPLATE.format(
        prompt=prompt.strip(),
        per_host_block=_format_per_host_block(results),
    )
