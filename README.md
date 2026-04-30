# ansible_ai

LLM-driven cross-host investigation agent for Ansible.

`ai_agent` is an action plugin that takes a natural-language prompt, asks an
LLM to generate small Python inspection snippets, ships each snippet to the
target host through `ai_exec`, observes stdout/stderr/exit, and iterates
until a diagnosis is reached or budget is exhausted. Per-host, in parallel,
inside ansible's normal task runtime.

It is not a config-management module. It is for the situation where you do
not know the root cause yet and need to look at several heterogeneous nodes
at once.

## Why

Declarative ansible breaks down when the question is "why is X failing on
some subset of hosts and we do not know which probes to run." This
collection lets you say:

```yaml
- yalindogusahin.ansible_ai.ai_agent:
    prompt: "connect cannot reach broker. find why."
```

and have the model fan out across `hosts: kafka` deciding what to inspect
on each node based on its facts, group, and role.

## Permission model

Generated code is constrained by an allow/deny rule set, layered like any
other ansible variable:

```
collection defaults < group_vars < host_vars < play vars < task args
```

Deny entries always win across layers.

```yaml
ansible_ai_rules:
  allow:
    run_cmd: [ps, ss, journalctl, cat]
    read_file: ["/var/log/**", "/proc/**"]
    write_file: []
    python: [os, json, subprocess]
    network: false
  deny:
    run_cmd: [rm, systemctl, kill]
    write_file: ["**"]
  budget:
    max_iterations: 5
    max_tokens: 8000
```

Enforcement runs in three layers:

1. The rules are rendered into the LLM system prompt so the model is told
   what it can and cannot do.
2. Before any snippet runs on the target, an AST walk rejects denied
   imports, denied builtins (`eval`, `exec`, `__import__`, ...), and
   `subprocess`/`os.system` calls whose argv\[0\] is not in the allow list
   or whose argv is not statically resolvable.
3. Execution itself is wrapped in the strongest available isolation:
   `bwrap` -> `firejail` -> `nsjail` -> in-process rlimit fallback. Each tool
   is probed at runtime: presence on PATH is not enough, so a bwrap blocked
   by AppArmor (e.g. Ubuntu 24's `kernel.apparmor_restrict_unprivileged_userns=1`)
   automatically falls through to the next tool. Run
   `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0` to enable
   bwrap on those hosts.

The model can never invoke a denied command, even if the prompt is
adversarial — the AST layer rejects it at the boundary regardless of what
the LLM emits.

## Providers

Pluggable. Choose via env or task arg:

| Provider | Env required | Default model |
|---|---|---|
| `claude` (default) | `ANTHROPIC_API_KEY` | `claude-opus-4-7` |
| `openai` | `OPENAI_API_KEY` | `gpt-4o` |
| `ollama` | `OLLAMA_URL` (default `http://127.0.0.1:11434`) | `llama3.1` |
| `bedrock` | AWS creds + `boto3` | `anthropic.claude-3-5-sonnet-20241022-v2:0` |

```bash
export ANSIBLE_AI_PROVIDER=ollama
ansible-playbook -i hosts site.yml -e provider=ollama
```

## Layout

```
ansible_ai/
  galaxy.yml
  meta/runtime.yml
  plugins/
    action/ai_agent.py            # controller orchestrator
    modules/ai_exec.py            # target sandboxed runner
    module_utils/
      llm_client.py
      rules.py
      sandbox.py
      prompts.py
  roles/ai_agent/defaults/main.yml
  tests/
    unit/
    integration/
```

## Install

```bash
ansible-galaxy collection build .
ansible-galaxy collection install yalindogusahin-ansible_ai-*.tar.gz
```

## Quick start

```yaml
- hosts: kafka
  roles: [yalindogusahin.ansible_ai.ai_agent]
  tasks:
    - yalindogusahin.ansible_ai.ai_agent:
        prompt: "what is wrong with this node, focus on networking and disk"
        max_iterations: 5
      register: r
    - debug: msg="{{ inventory_hostname }}: {{ r.diagnosis }}"
```

## Returned shape

```yaml
agent_result:
  changed: false
  diagnosis: "..."
  iterations_used: 3
  tokens_used: { input: 4123, output: 712 }
  rules_effective: { allow: {...}, deny: {...}, budget: {...} }
  transcript:
    - step: 1
      action: run_python
      code: "..."
      reason: "..."
      stdout: "..."
      stderr: ""
      exit: 0
      blocked_by_rule: null
    - step: 2
      action: done
      summary: "..."
      reason: "..."
```

## Development

```bash
python -m pytest tests/unit/
ansible-playbook -i tests/integration/inventory.ini tests/integration/playbook_debug.yml
```

## Status

Pre-release. Expect breaking changes to the rule schema and action output
shape until a `1.0.0` tag is cut.

## License

MIT.
