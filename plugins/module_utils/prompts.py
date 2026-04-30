"""System prompt construction and JSON action parsing for ansible_ai."""
from __future__ import annotations

import json
import re
from typing import Any


SECRET_KEY_PATTERNS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "credential", "auth", "private", "ssh_key", "cert_key",
)

DEFAULT_FACT_KEYS = (
    "ansible_distribution", "ansible_distribution_version",
    "ansible_kernel", "ansible_os_family", "ansible_architecture",
    "ansible_processor_count", "ansible_memtotal_mb",
    "ansible_default_ipv4", "ansible_hostname", "ansible_fqdn",
    "ansible_service_mgr", "ansible_python_version",
)


SYSTEM_PROMPT_TEMPLATE = """You are a Linux investigation agent operating inside an Ansible task on a remote host.

GOAL: {prompt}

Each turn you must emit ONE JSON object on a single line, nothing else:
  {{"action": "run_python", "code": "<python source>", "reason": "<why this step>"}}
or, when finished:
  {{"action": "done", "summary": "<what you concluded>", "reason": "<why you are done>"}}

Rules:
  * code must be a self-contained Python 3 snippet using only the imports and commands listed under ALLOW below.
  * code must print findings to stdout. The next turn you will see stdout, stderr, exit.
  * Prefer small, targeted snippets. One purpose per snippet.
  * Stop and emit "done" once you have a diagnosis or after evidence is exhausted.

ALLOW (everything else is denied):
  python imports: {allow_python}
  shell commands: {allow_run_cmd}
  read paths:     {allow_read_file}
  write paths:    {allow_write_file}
  network egress: {allow_network}

DENY (always blocked, overrides ALLOW):
  shell commands: {deny_run_cmd}
  python imports: {deny_python}
  write paths:    {deny_write_file}

HOST CONTEXT:
  hostname: {hostname}
  groups:   {groups}
  role:     {role}
  facts:
{facts_block}

Output only the JSON object on a single line. No markdown fences. No commentary."""


def _is_secret_key(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in SECRET_KEY_PATTERNS)


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<...>"
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if _is_secret_key(str(k)) else _redact(v, depth + 1))
            for k, v in value.items()
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
    allow = rules.get("allow", {})
    deny = rules.get("deny", {})
    return SYSTEM_PROMPT_TEMPLATE.format(
        prompt=prompt.strip(),
        allow_python=", ".join(allow.get("python", [])) or "(none)",
        allow_run_cmd=", ".join(allow.get("run_cmd", [])) or "(none)",
        allow_read_file=", ".join(allow.get("read_file", [])) or "(none)",
        allow_write_file=", ".join(allow.get("write_file", [])) or "(none)",
        allow_network="yes" if allow.get("network") else "no",
        deny_run_cmd=", ".join(deny.get("run_cmd", [])) or "(none)",
        deny_python=", ".join(deny.get("python", [])) or "(none)",
        deny_write_file=", ".join(deny.get("write_file", [])) or "(none)",
        hostname=host_ctx.get("hostname", "<unknown>"),
        groups=", ".join(host_ctx.get("groups", [])) or "(none)",
        role=host_ctx.get("role", "(none)"),
        facts_block=_format_facts(host_ctx.get("facts", {})),
    )


def render_observation(stdout: str, stderr: str, exit_code: int, blocked: str | None = None) -> str:
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


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_action(text: str) -> dict[str, Any]:
    """Parse a single-line JSON action object from LLM output.

    Tolerates surrounding whitespace and a single ```json``` fence.
    Raises ValueError on malformed input.
    """
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as e:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            raise ValueError(f"no JSON object found in: {text!r}") from e
        obj = json.loads(m.group(0))

    if not isinstance(obj, dict) or "action" not in obj:
        raise ValueError(f"missing 'action' key: {obj!r}")
    if obj["action"] not in ("run_python", "done"):
        raise ValueError(f"unknown action: {obj['action']!r}")
    if obj["action"] == "run_python" and "code" not in obj:
        raise ValueError("run_python requires 'code'")
    if obj["action"] == "done" and "summary" not in obj:
        raise ValueError("done requires 'summary'")
    return obj
