#!/usr/bin/python
"""ansible_ai.ai_agent - documentation sidecar for the action plugin.

The actual agent logic lives in plugins/action/ai_agent.py; this file exists
so `ansible-doc yalindogusahin.ansible_ai.ai_agent` can render parameters,
examples, and return values.
"""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: ai_agent
short_description: LLM-driven ReAct investigation agent for Ansible
author:
  - Yalın Şahin (@yalindogusahin)
description:
  - Takes a natural-language prompt and runs a tool-use loop against a target
    host. Each iteration the LLM picks one tool call (run_cmd, read_file,
    write_file, or run_python), the call runs sandboxed on the target via
    the ai_exec module, and the structured result feeds back into the next
    iteration. The loop stops when the LLM calls the `done` tool or when
    iteration/token budget is exhausted.
  - Tool calls are gated by an allow/deny rule set, layered through ansible's
    variable precedence (collection defaults < group_vars < host_vars <
    play vars < task args). Deny entries always win on conflict. run_python
    is opt-in - it is offered to the model only when allow.python is non-empty.
  - Provider-agnostic. Supports Anthropic Claude, OpenAI Chat Completions,
    AWS Bedrock, and Ollama (and any OpenAI-compatible endpoint via
    `provider=openai` plus `endpoint`). Each provider is driven through its
    native tool-use API; no JSON-action parsing.
options:
  prompt:
    description: Natural-language instruction telling the agent what to investigate or do.
    type: str
    required: true
  rules:
    description:
      - Allow/deny rule overrides for this task. Merged with collection defaults
        and any `ansible_ai_rules` from group_vars/host_vars/play vars. Deny wins.
      - "Shape: {allow: {run_cmd: [str], read_file: [str], write_file: [str], python: [str], network: bool}, deny: {same}, budget: {max_iterations: int, max_tokens: int}}."
    type: dict
    required: false
  max_iterations:
    description: Hard cap on LLM round-trips per host. Overrides budget.max_iterations from rules.
    type: int
    required: false
    default: 5
  max_tokens:
    description: Total token budget (input + output) per host across all iterations. When exceeded the loop stops.
    type: int
    required: false
    default: 8000
  provider:
    description: LLM provider. Aliases - 'anthropic' resolves to 'claude'.
    type: str
    required: false
    choices: [claude, anthropic, openai, bedrock, ollama]
    default: claude
  model:
    description: Model name override. Provider-specific. Defaults are claude-opus-4-7, gpt-4o, llama3.1, anthropic.claude-3-5-sonnet-20241022-v2:0.
    type: str
    required: false
  endpoint:
    description: HTTP base URL override for the chosen provider. Useful for self-hosted vLLM, on-prem Ollama, regional Bedrock endpoints, etc. Overrides ANTHROPIC_BASE_URL / OPENAI_BASE_URL / OLLAMA_URL env.
    type: str
    required: false
  api_key:
    description: Provider API key. Overrides env (ANTHROPIC_API_KEY, OPENAI_API_KEY, ANTHROPIC_AUTH_TOKEN). Prefer env or vault for secrets. Marked no_log in the action plugin's argspec so it never appears in ansible logs.
    type: str
    required: false
  timeout:
    description: Per-snippet execution timeout (seconds) on the target host.
    type: int
    required: false
    default: 30
  print_result:
    description: When true, write the final diagnosis line to the ansible runner's display so you don't need a separate `register` + `debug` task.
    type: bool
    required: false
    default: false
  stream:
    description:
      - When true, emit one compact line per orchestrator step to the ansible
        runner's display (tool name + key params + exit code). Useful on long
        investigations where the operator wants to see live progress instead
        of waiting for the final diagnosis. Also enabled by env ANSIBLE_AI_STREAM=1.
    type: bool
    required: false
    default: false
  aggregate:
    description:
      - When true, switch to cluster-aggregation mode. Skips per-host investigation
        entirely; instead the agent does a single LLM call that synthesizes
        previously-registered per-host results into one cluster-level diagnosis.
      - "Pair with `run_once: true` and `delegate_to: localhost` so the call fires once per play, not per host."
      - Requires `results`.
    type: bool
    required: false
    default: false
  results:
    description:
      - Per-host results to aggregate. Required when `aggregate=true`.
      - Either a dict mapping hostname to the registered ai_agent result, or a
        flat list of those result dicts (host names will be synthesized).
      - "Typical pattern - {{ ansible_play_hosts | map('extract', hostvars, 'agent_result') | list }}."
    type: raw
    required: false
  save_transcript:
    description:
      - "If set, write a JSON artifact of the run (prompt, rules, transcript, diagnosis, tokens, model/provider) to this path."
      - "The string `{host}` is substituted with `inventory_hostname` so multi-host plays don't collide."
      - "Failures are non-fatal - the warning is logged but the task still succeeds."
    type: str
    required: false
notes:
  - Runs as an action plugin on the controller. The actual code execution
    happens inside the ai_exec module on the target host.
  - "When `become: true` is set on the play or task, the LLM-generated snippet runs as root on the target. Combine with tight rules."
  - Sandbox tool detection is runtime - presence of bwrap on PATH is not
    enough. Each tool is probed before use; AppArmor-blocked bwrap falls
    through to firejail / nsjail / rlimit.
seealso:
  - module: yalindogusahin.ansible_ai.ai_exec
"""

EXAMPLES = r"""
- name: Quick diagnosis, single line of output
  yalindogusahin.ansible_ai.ai_agent:
    prompt: "What is consuming the most memory on this host?"
    print_result: true

- name: Investigation with explicit budget and provider
  hosts: kafka
  tasks:
    - yalindogusahin.ansible_ai.ai_agent:
        prompt: "Connect cannot reach broker. Find why."
        provider: claude
        model: claude-opus-4-7
        max_iterations: 8
        max_tokens: 30000
        print_result: true

- name: Local vLLM (OpenAI-compatible) with custom endpoint
  yalindogusahin.ansible_ai.ai_agent:
    prompt: "List top 3 processes by CPU"
    provider: openai
    model: Qwen/Qwen3.6-35B-A3B-FP8
    endpoint: "http://10.0.0.10:8000/v1"
    api_key: dummy
    print_result: true

- name: Run as root with tight allow list
  hosts: all
  become: true
  tasks:
    - yalindogusahin.ansible_ai.ai_agent:
        prompt: "Inspect docker container 'web' and report on its health."
        rules:
          allow:
            run_cmd: [docker, ps, cat, ls, head, tail]
            python: [subprocess, json, os]
        print_result: true

- name: Capture the result instead of printing
  yalindogusahin.ansible_ai.ai_agent:
    prompt: "Summarize disk usage by mount point"
  register: r

- ansible.builtin.debug:
    var: r.diagnosis

- name: Per-host investigation followed by cluster-level aggregate
  hosts: kafka
  tasks:
    - yalindogusahin.ansible_ai.ai_agent:
        prompt: "Why can connect not reach the broker on this node?"
        max_iterations: 8
      register: agent_result

    - yalindogusahin.ansible_ai.ai_agent:
        aggregate: true
        prompt: >-
          Cluster-level root cause? Which hosts share which symptoms?
          What is the single likeliest fix?
        results: >-
          {{ ansible_play_hosts | map('extract', hostvars, 'agent_result') | list }}
        print_result: true
      run_once: true
      delegate_to: localhost
      register: cluster
"""

RETURN = r"""
changed:
  description: Always false - the agent does not by itself change state. Snippets it runs may, depending on rules.
  returned: always
  type: bool
diagnosis:
  description: Final human-readable result. Either the LLM's "done" summary, or a budget-exceeded message.
  returned: always
  type: str
iterations_used:
  description: Number of LLM round-trips actually executed.
  returned: always
  type: int
tokens_used:
  description: "Token accounting per host. Keys - input, output, cache_read, cache_write (cache_read/cache_write are 0 on providers that do not report prompt-cache usage)."
  returned: always
  type: dict
rules_effective:
  description: The fully merged rule set used for this run (after layered merge + deny-wins resolution).
  returned: always
  type: dict
transcript:
  description: Per-iteration log. Each entry has step, action (tool name or "done"), input (the tool's input dict), reason, stdout, stderr, exit, blocked_by_rule. Not present in aggregate mode.
  returned: when not aggregate
  type: list
  elements: dict
aggregate:
  description: True when this result came from `aggregate=true` mode (cluster-level summary), false otherwise.
  returned: when aggregate
  type: bool
host_count:
  description: Number of per-host results that were aggregated into the summary.
  returned: when aggregate
  type: int
"""


def main():
    """Documentation sidecar; the action plugin handles execution.

    Real invocation is intercepted by plugins/action/ai_agent.py on the controller
    before this module is ever shipped to a target. We still initialize
    AnsibleModule so ansible-test sanity (validate-modules) sees an argspec
    consistent with DOCUMENTATION, then fail cleanly if reached.
    """
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "prompt": {"type": "str", "required": True},
            "rules": {"type": "dict"},
            "max_iterations": {"type": "int", "default": 5},
            # no_log=False is explicit: validate-modules' heuristic flags any
            # argument containing 'token' as potentially secret; this one is a
            # numeric budget cap, not a credential.
            "max_tokens": {"type": "int", "default": 8000, "no_log": False},
            "provider": {
                "type": "str",
                "choices": ["claude", "anthropic", "openai", "bedrock", "ollama"],
                "default": "claude",
            },
            "model": {"type": "str"},
            "endpoint": {"type": "str"},
            "api_key": {"type": "str", "no_log": True},
            "timeout": {"type": "int", "default": 30},
            "print_result": {"type": "bool", "default": False},
            "stream": {"type": "bool", "default": False},
            "aggregate": {"type": "bool", "default": False},
            "results": {"type": "raw"},
            "save_transcript": {"type": "str"},
        },
        supports_check_mode=False,
    )
    module.fail_json(
        msg="ai_agent must be invoked through its action plugin on the controller; "
        "bare module execution on a target is not supported."
    )


if __name__ == "__main__":
    main()
