# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`yalindogusahin.ansible_ai` — an Ansible **collection** (not a standalone Python package) that ships an LLM-driven cross-host investigation agent. Two plugins:

- `ai_agent` — controller-side **action plugin** (ReAct orchestrator).
- `ai_exec` — target-side **module** (sandboxed Python runner).

Per host, in parallel inside ansible's task runtime: prompt → LLM emits a Python snippet → AST validation → sandboxed execution on the target → observation fed back → loop until `done` or budget exhausted.

## Common commands

```bash
# Unit tests (no ansible install needed — conftest synthesizes the collection namespace)
python -m pytest tests/unit/ -v
python -m pytest tests/unit/test_sandbox.py::test_name -v   # single test

# Lint (matches CI pre-commit job)
pre-commit run --all-files
ruff check . && ruff format --check .

# Build the collection tarball
ansible-galaxy collection build .

# Integration smoke (real inventory + ANTHROPIC_API_KEY etc. required)
ansible-playbook -i tests/integration/inventory.ini tests/integration/playbook_debug.yml
```

CI matrix: Python 3.10 / 3.11 / 3.12. The `pre-commit` CI job runs ruff + yamllint + ansible-lint + pytest.

## Architecture

### Layered pipeline (controller → target)

`plugins/action/ai_agent.py` runs on the controller per host. Its `run()` loop:

1. **Rule merge** (`module_utils/rules.py::merge`) — layers in precedence order: collection defaults (hardcoded in `action/ai_agent.py::COLLECTION_DEFAULT_RULES`) < `ansible_ai_rules` from group/host/play vars < task `rules:` arg. **Deny always wins** across layers (subtracted from allow at end of merge).
2. **Host context** (`prompts.filter_facts` / `filter_hostvars`) — restricts LLM-visible facts to a known whitelist (`DEFAULT_FACT_KEYS`) and redacts any key matching `SECRET_KEY_PATTERNS`.
3. **System prompt** (`prompts.build_system_prompt`) — bakes the merged allow/deny lists into the prompt so the model is *told* its boundaries.
4. **LLM call** (`module_utils/llm_client.py`) — provider chosen by task arg → `ANSIBLE_AI_PROVIDER` env → `claude` default. Providers: `claude` (alias `anthropic`), `openai`, `ollama`, `bedrock` (lazy-imports `boto3`). All non-bedrock backends use stdlib `urllib`; **no SDK dependencies**.
5. **Action parse** (`prompts.parse_action`) — strict JSON; tolerates one ```json fence. Two action types: `run_python` (with `code`) or `done` (with `summary`).
6. **AST pre-flight** (`module_utils/sandbox.py::validate_ast`) — runs on the controller *before* shipping code. Rejects denied imports, dangerous builtins (`eval` / `exec` / `__import__` / ...), and any `subprocess.*` / `os.system` call whose `argv[0]` is not statically resolvable to an allowed command. Statically unresolvable argv is itself a violation.
7. **Target execution** — `_execute_module("yalindogusahin.ansible_ai.ai_exec", {code, rules, timeout})` ships the snippet through the standard ansible module pipeline. `ai_exec` re-runs `validate_ast` on the target (defense in depth), then `sandbox.run`.
8. **Observation** — stdout/stderr/exit/blocked rendered into the next user turn.
9. **Budget enforcement** — iteration counter and cumulative `input + output` token counter; the `for…else` clause sets the "max_iterations reached" diagnosis when the loop exhausts without break.

### Sandbox isolation

`sandbox.detect_isolation()` probes (not just `which`) in this order: **bwrap → firejail → nsjail → in-process rlimit fallback**. The probe is essential: bwrap on Ubuntu 24+ with `kernel.apparmor_restrict_unprivileged_userns=1` is on PATH but unusable. The probe runs `bwrap --bind / / -- true` and falls through on failure.

bwrap config does **not** use `--unshare-net` by default — same AppArmor restrictions break loopback setup inside a new netns and that breaks even read-only `import subprocess`. Network egress is gated at the **rule layer** (argv[0] denylist) instead. Set `ANSIBLE_AI_BWRAP_UNSHARE_NET=1` to force netns isolation on hosts that support it.

The three enforcement layers (system prompt, AST, runtime sandbox) are intentionally redundant — even an adversarial prompt cannot bypass the AST check, since the AST runs on validated code paths regardless of model output.

### Test bootstrap

`tests/conftest.py` synthesizes the `ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils` namespace package pointing at the in-repo `plugins/module_utils/` so unit tests run without `ansible-galaxy collection install`. **All in-repo imports across `plugins/` use the `ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils.X` FQN** — this is mandatory for the collection to load when installed; do not switch to relative imports inside `plugins/`.

`tests/integration/playbook_debug.yml` is excluded from `ansible-lint` (see `.ansible-lint`) because the collection it references is the one being linted (chicken-and-egg with FQCN resolution). `ansible-playbook --syntax-check` covers it in CI instead.

## Conventions

- **Imports inside `plugins/`**: always `ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils.X` — never relative. Tests rely on this and so does the collection at install time.
- **Python**: `from __future__ import annotations` at the top of every module (we use `X | Y` annotations on py3.10).
- **Ruff**: `line-length = 110`, `target-version = py310`. `tests/integration/` excluded. `plugins/modules/*.py` may have `E402` (top-level `DOCUMENTATION` strings before imports — an ansible convention).
- **Modules** (`plugins/modules/*.py`) follow ansible's `DOCUMENTATION` / `EXAMPLES` / `RETURN` triple-string convention — keep them in sync with the action-plugin's `_VALID_ARGS` and the README parameters table.
- **No new SDK dependencies** for LLM providers — keep stdlib `urllib` for HTTP. `boto3` is lazy-imported inside `BedrockClient.complete` only.
- **Rule schema is pre-1.0** — README warns it may change. Any schema change has to update: `rules.validate`, `EMPTY_RULES`, `COLLECTION_DEFAULT_RULES` in `action/ai_agent.py`, `roles/ai_agent/defaults/main.yml`, and the README `Permission model` block.
- **Commit messages**: single-line, no trailers. Format `type: short description`.
