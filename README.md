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
        print_result: true
```

`print_result: true` writes the diagnosis directly to ansible output. Drop
it (or set false) and `register: r` + `r.diagnosis` if you want the result
in a variable instead.

## `ai_agent` task parameters

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `prompt` | str | — | yes | Natural-language instruction telling the agent what to investigate or do. |
| `provider` | str (`claude`/`anthropic`/`openai`/`bedrock`/`ollama`) | `claude` | no | LLM backend. `anthropic` is an alias for `claude`. |
| `model` | str | provider default | no | Model name. Defaults: `claude-opus-4-7`, `gpt-4o`, `llama3.1`, `anthropic.claude-3-5-sonnet-20241022-v2:0`. |
| `endpoint` | str | env or provider default | no | HTTP base URL override. Useful for self-hosted vLLM, on-prem Ollama, regional Bedrock endpoints. |
| `api_key` | str | env | no | Provider API key. Overrides `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_AUTH_TOKEN`. Prefer env or vault for secrets. |
| `rules` | dict | `{}` | no | Allow/deny rule overrides. Merged with collection defaults and `ansible_ai_rules` from group_vars/host_vars/play vars. Deny wins on conflict. See [Permission model](#permission-model). |
| `max_iterations` | int | 5 | no | Hard cap on LLM round-trips per host. |
| `max_tokens` | int | 8000 | no | Total token budget (input + output) per host across all iterations. When exceeded the loop stops. |
| `timeout` | int | 30 | no | Per-snippet execution timeout in seconds on the target host. |
| `print_result` | bool | false | no | When true, write the final diagnosis line to ansible output so you don't need a separate `register` + `debug` task. |
| `aggregate` | bool | false | no | Switch to cluster-aggregation mode. Skips per-host investigation; does one LLM call that synthesizes registered per-host results into a single cluster-level diagnosis. Pair with `run_once: true` + `delegate_to: localhost`. Requires `results`. |
| `results` | dict or list | — | when `aggregate=true` | Per-host results to aggregate. Either `{hostname: ai_agent_result}` dict or a flat list of result dicts. See [example 6](#examples). |

`ansible-doc yalindogusahin.ansible_ai.ai_agent` renders the same table at the CLI.

## Examples

**1. Single-line diagnosis on every host:**

```yaml
- hosts: all
  tasks:
    - yalindogusahin.ansible_ai.ai_agent:
        prompt: "What is consuming the most memory on this host?"
        print_result: true
```

**2. Investigation with bigger budget on a verbose model:**

```yaml
- hosts: kafka
  tasks:
    - yalindogusahin.ansible_ai.ai_agent:
        prompt: "Connect cannot reach broker. Find why."
        provider: claude
        model: claude-opus-4-7
        max_iterations: 8
        max_tokens: 30000
        print_result: true
```

**3. Local vLLM (OpenAI-compatible) instead of public API:**

```yaml
- yalindogusahin.ansible_ai.ai_agent:
    prompt: "List top 3 processes by CPU"
    provider: openai
    model: Qwen/Qwen3.6-35B-A3B-FP8
    endpoint: "http://10.0.0.10:8000/v1"
    api_key: dummy
    print_result: true
```

**4. Run as root with tight allow list (docker investigation):**

```yaml
- hosts: dev
  become: true
  tasks:
    - yalindogusahin.ansible_ai.ai_agent:
        prompt: "Inspect docker container 'web' and report on its health."
        rules:
          allow:
            run_cmd: [docker, ps, cat, ls, head, tail]
            python: [subprocess, json, os]
        print_result: true
```

**5. Capture the result in a variable instead of printing:**

```yaml
- yalindogusahin.ansible_ai.ai_agent:
    prompt: "Summarize disk usage by mount point"
  register: r

- ansible.builtin.debug:
    var: r.diagnosis
```

**6. Per-host investigation followed by cluster-level summary:**

When you fan out across many hosts, the operator usually wants ONE
cluster-level diagnosis, not one paragraph per host to read. Run the agent
twice in the same play: first per-host, then once with `aggregate: true`.

```yaml
- hosts: kafka
  tasks:
    - name: Per-host investigation
      yalindogusahin.ansible_ai.ai_agent:
        prompt: "Why can connect not reach the broker on this node?"
        max_iterations: 8
      register: agent_result

    - name: Cluster-level summary
      yalindogusahin.ansible_ai.ai_agent:
        aggregate: true
        prompt: >-
          Cluster-level root cause? Which hosts share which symptoms?
          What is the single likeliest fix?
        results: >-
          {{ ansible_play_hosts | map('extract', hostvars, 'agent_result') | list }}
        print_result: true
      run_once: true
      delegate_to: localhost
```

In `aggregate: true` mode the agent does a single LLM call that synthesizes
the previously-registered per-host results — no per-host loop, no AST, no
sandbox. It does not run any code on any target. Pair with
`run_once: true` and `delegate_to: localhost` so the call fires once per
play, not once per host.

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
