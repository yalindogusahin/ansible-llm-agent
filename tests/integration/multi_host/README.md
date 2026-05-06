# Multi-host integration test

End-to-end fan-out test against the scripted-LLM stub. Three localhost
aliases (`host1`/`host2`/`host3`) all back to `127.0.0.1` with
`connection: local`, so the test runs on a single CI runner without
needing real SSH targets.

## What it covers

- **Per-host rule precedence (Ansible default).** `group_vars/cluster.yml`
  defines a baseline `ansible_ai_rules`; `host_vars/host2.yml` redefines
  it to a different value. Per Ansible's default `hash_behaviour=replace`,
  host_vars overwrites group_vars wholesale per host — it does NOT
  deep-merge. The action plugin sees the resolved-per-host value and
  layers it under collection defaults + the task `rules:` arg. If you
  want true deep-merge behaviour across group_vars/host_vars, set
  `hash_behaviour=merge` in `ansible.cfg`, or explicitly merge into a
  single dict and pass via the task-level `rules:` arg. The test
  asserts `host2` sees only its own redefinition (`['uname','hostname']`)
  and `host1`/`host3` see only the group-level entry (`['uname']`).
- **`{host}` substitution in `save_transcript`.** The path
  `/tmp/ansible-ai-multi-host-transcripts/{host}.json` should be expanded
  to `inventory_hostname` per host. A second play stat's all three files
  to prove the substitution and per-host write actually happened.
- **`aggregate: true` mode.** A `run_once` + `delegate_to: localhost`
  play feeds the per-host registered results back into the agent for
  cluster-level synthesis, asserting `host_count == 3`.

## Running locally

```bash
# In one terminal
python tests/integration/stub_llm.py 8765

# In another
ANSIBLE_COLLECTIONS_PATH=$PWD/.. ANTHROPIC_API_KEY=stub \
  ansible-playbook \
    -i tests/integration/multi_host/inventory.ini \
    tests/integration/multi_host/playbook.yml \
    -e endpoint=http://127.0.0.1:8765 \
    -e api_key=stub \
    -e model=stub-claude \
    -e transcript_dir=/tmp/ansible-ai-multi-host-transcripts
```

## Stub behavior

The stub is **stateless**: it inspects each request body and replies with
either a `tool_use run_cmd uname -r` (when no `tool_result` block is
present yet) or a `tool_use done` (when the conversation has already
fed a tool_result back). Threaded HTTP server so multiple hosts running
concurrently don't block each other.
