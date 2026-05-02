# Role: ai_agent

Surfaces the conservative default rule set for the
`yalindogusahin.ansible_ai.ai_agent` action plugin as a top-level
inventory variable (`ansible_ai_rules`) so operators can see and override
the allow/deny baseline without reading plugin code.

The action plugin already bakes the same defaults in - including the role
is optional and exists primarily as documentation of the conservative
read-only baseline (no `write_file`, no `network`, no destructive
commands, no `run_python`).

## Usage

```yaml
- hosts: all
  roles:
    - yalindogusahin.ansible_ai.ai_agent
  tasks:
    - yalindogusahin.ansible_ai.ai_agent:
        prompt: "What is consuming the most memory on this host?"
        print_result: true
```

## Defaults

See `defaults/main.yml`. Override at any layer of ansible's variable
precedence (collection defaults < group_vars < host_vars < play vars <
task args). Deny entries always win across layers.

For the full agent docs (parameters, providers, sandbox, examples) see
the [collection README](../../README.md) at the repository root.

## License

MIT - see the repository `LICENSE` file.
