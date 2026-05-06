# Examples

Domain-organized debugging playbooks. Each subfolder is a self-contained
cookbook for one technology and ships:

- `rules.yml` — an `ansible_ai_rules` preset (allow/deny lists) calibrated for
  that domain. Drop into `group_vars/<group>.yml` to apply globally, or load
  per-play via `vars_files`.
- `README.md` — assumptions (binaries on PATH, log paths), how to run.
- `*.yml` — runnable playbooks for specific symptoms.

The collection's [conservative defaults](../roles/ai_agent/defaults/main.yml)
(read-only commands, no `write_file`, no network) still apply through the
layered rule merge — domain `rules.yml` files only **add** allow entries
(deny-wins is preserved).

## Index

| Domain | Symptom | Playbook |
|---|---|---|
| **generic** | Disk filling up | [generic/disk-pressure.yml](generic/disk-pressure.yml) |
| **generic** | OOM kill in dmesg | [generic/oom-kill.yml](generic/oom-kill.yml) |
| **generic** | Failed systemd unit | [generic/systemd-failed.yml](generic/systemd-failed.yml) |
| **kafka** | Broker process down / port closed | [kafka/broker-down.yml](kafka/broker-down.yml) |
| **kafka** | Consumer group lagging | [kafka/consumer-lag.yml](kafka/consumer-lag.yml) |
| **kubernetes** | Pod stuck in CrashLoopBackOff | [kubernetes/pod-crashloop.yml](kubernetes/pod-crashloop.yml) |
| **kubernetes** | Node NotReady | [kubernetes/node-not-ready.yml](kubernetes/node-not-ready.yml) |
| **postgres** | Connection refused / pool exhausted | [postgres/connection-issues.yml](postgres/connection-issues.yml) |
| **postgres** | Slow queries / lock contention | [postgres/slow-queries.yml](postgres/slow-queries.yml) |

## Running an example

```bash
# Set provider creds (Claude shown; OpenAI/Bedrock/Ollama also supported).
export ANTHROPIC_API_KEY=sk-...

# Inventory pointing at the target hosts:
ansible-playbook -i inventory.ini examples/kafka/broker-down.yml \
  -e target=kafka_brokers
```

Each playbook accepts an `-e target=<host_pattern>` extra-var (default
`localhost` for safe demo runs). Substitute your actual inventory group.

## Adding a domain

1. `mkdir examples/<domain>` and add `README.md`, `rules.yml`, and one or
   more scenario playbooks.
2. Keep `rules.yml` minimal — only the binaries / paths that are
   domain-specific. Generic read-only commands (`ps`, `ss`, `journalctl`,
   `df`, `cat`, `grep`, …) are already in the collection defaults.
3. Add a row to the index table above.
4. Run `ansible-lint examples/<domain>/` before opening a PR.
