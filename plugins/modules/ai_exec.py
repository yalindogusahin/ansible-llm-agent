#!/usr/bin/python
"""ansible_ai.ai_exec - sandboxed Python execution module."""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: ai_exec
short_description: Run sandboxed LLM-generated Python on a target host
description:
  - Validates Python source against an allow/deny rule set, then runs it
    inside the strongest available isolation (bwrap, firejail, nsjail, or
    in-process rlimit fallback).
  - Intended to be invoked by the ai_agent action plugin, but can be called
    directly for testing.
options:
  code:
    description: Python source to execute.
    type: str
    required: true
  rules:
    description: Rule dict (allow/deny/budget). Must follow ansible_ai schema.
    type: dict
    required: true
  timeout:
    description: Seconds before the snippet is killed.
    type: int
    default: 30
"""

EXAMPLES = r"""
- name: Inspect listening sockets
  ysahin.ansible_ai.ai_exec:
    code: |
      import subprocess
      print(subprocess.run(["ss", "-tnlp"], capture_output=True, text=True).stdout)
    rules:
      allow:
        run_cmd: [ss]
        python: [subprocess]
      deny: {}
      budget:
        max_iterations: 1
        max_tokens: 1
"""

RETURN = r"""
stdout:
  description: Captured stdout from the snippet.
  returned: always
  type: str
stderr:
  description: Captured stderr from the snippet.
  returned: always
  type: str
exit:
  description: Exit code of the sandboxed process. 124 means timeout.
  returned: always
  type: int
blocked_by_rule:
  description: Reason string when the snippet was rejected before execution.
  returned: when blocked
  type: str
timed_out:
  description: True if the snippet was killed by the timeout.
  returned: always
  type: bool
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.ysahin.ansible_ai.plugins.module_utils import rules as rules_mod
from ansible_collections.ysahin.ansible_ai.plugins.module_utils import sandbox as sandbox_mod


def main() -> None:
    module = AnsibleModule(
        argument_spec=dict(
            code=dict(type="str", required=True, no_log=False),
            rules=dict(type="dict", required=True),
            timeout=dict(type="int", default=30),
        ),
        supports_check_mode=True,
    )

    code = module.params["code"]
    rules = module.params["rules"]
    timeout = module.params["timeout"]

    try:
        rules_mod.validate(rules)
    except rules_mod.RuleError as e:
        module.fail_json(msg=f"invalid rules: {e}")

    try:
        sandbox_mod.validate_ast(code, rules)
    except sandbox_mod.SandboxViolation as e:
        module.exit_json(
            changed=False,
            stdout="",
            stderr="",
            exit=126,
            blocked_by_rule=f"{e.reason} ({e.where or 'static'})",
            timed_out=False,
        )

    if module.check_mode:
        module.exit_json(changed=False, stdout="", stderr="", exit=0, timed_out=False, blocked_by_rule=None)

    result = sandbox_mod.run(code, rules, timeout=timeout)

    changed = False
    write_allow = rules.get("allow", {}).get("write_file", [])
    if write_allow and result.exit == 0:
        changed = True

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
