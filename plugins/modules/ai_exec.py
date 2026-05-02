#!/usr/bin/python
"""ansible_ai.ai_exec - sandboxed tool dispatcher on the target host."""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: ai_exec
short_description: Run one LLM-issued tool call inside the ansible_ai sandbox
author:
  - Yalın Şahin (@yalindogusahin)
description:
  - Receives a single tool call (run_cmd, read_file, write_file, or run_python)
    from the ai_agent action plugin, validates it against the merged rule set,
    and executes it inside the strongest available isolation tool (bwrap,
    firejail, nsjail, or rlimit fallback).
  - Tool boundaries are enforced both in the system prompt sent to the LLM
    and at this module's entry point - a model that emits a denied call has
    its call rejected here regardless of what was promised.
  - Intended to be invoked by ai_agent. Direct invocation is supported for
    testing and ad-hoc target-side checks.
options:
  tool:
    description: Tool name. One of run_cmd, read_file, write_file, run_python.
    type: str
    required: true
    choices: [run_cmd, read_file, write_file, run_python]
  input:
    description: Tool-specific input dict. Shape depends on `tool`.
    type: dict
    required: true
  rules:
    description: Rule dict (allow/deny/budget). Must follow ansible_ai schema.
    type: dict
    required: true
  timeout:
    description: Seconds before the tool execution is killed (run_cmd / run_python only).
    type: int
    default: 30
"""

EXAMPLES = r"""
- name: Run a single allow-listed command
  yalindogusahin.ansible_ai.ai_exec:
    tool: run_cmd
    input:
      argv: [ss, -tnlp]
      reason: list listening tcp sockets
    rules:
      allow:
        run_cmd: [ss]
      deny: {}
      budget:
        max_iterations: 1
        max_tokens: 1

- name: Read a config file under an allow-listed glob
  yalindogusahin.ansible_ai.ai_exec:
    tool: read_file
    input:
      path: /etc/resolv.conf
      reason: inspect resolver config
    rules:
      allow:
        read_file: ["/etc/**"]
      deny: {}
      budget:
        max_iterations: 1
        max_tokens: 1
"""

RETURN = r"""
stdout:
  description: Tool output (file contents for read_file, command stdout for run_cmd, snippet stdout for run_python).
  returned: always
  type: str
stderr:
  description: Tool error stream.
  returned: always
  type: str
exit:
  description: Exit code. 124 means timeout, 126 means blocked by rule, 127 means executable not found.
  returned: always
  type: int
blocked_by_rule:
  description: Reason string when the tool call was rejected before or during execution.
  returned: when blocked
  type: str
timed_out:
  description: True if the tool was killed by the timeout.
  returned: always
  type: bool
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import rules as rules_mod
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import tools as tools_mod


def main() -> None:
    module = AnsibleModule(
        argument_spec=dict(
            tool=dict(
                type="str",
                required=True,
                choices=[tools_mod.RUN_CMD, tools_mod.READ_FILE, tools_mod.WRITE_FILE, tools_mod.RUN_PYTHON],
            ),
            input=dict(type="dict", required=True),
            rules=dict(type="dict", required=True),
            timeout=dict(type="int", default=30),
        ),
        supports_check_mode=True,
    )

    tool = module.params["tool"]
    inp = module.params["input"] or {}
    rules = module.params["rules"]
    timeout = module.params["timeout"]

    try:
        rules_mod.validate(rules)
    except rules_mod.RuleError as e:
        module.fail_json(msg=f"invalid rules: {e}")

    if module.check_mode:
        module.exit_json(
            changed=False,
            stdout="",
            stderr="",
            exit=0,
            timed_out=False,
            blocked_by_rule=None,
        )

    result = tools_mod.exec_tool(tool, inp, rules, timeout=timeout)

    changed = tool == tools_mod.WRITE_FILE and result.exit == 0

    module.exit_json(
        changed=changed,
        stdout=result.stdout,
        stderr=result.stderr,
        exit=result.exit,
        timed_out=result.timed_out,
        blocked_by_rule=result.blocked_by_rule,
    )


if __name__ == "__main__":
    main()
