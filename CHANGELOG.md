# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-01

### Added

- `aggregate: true` task arg on `ai_agent` switches the action plugin into cluster-aggregation mode. One LLM call synthesizes previously-registered per-host results into a single cluster-level diagnosis. No per-host loop, no AST, no sandbox — does not execute code on any target. Pair with `run_once: true` and `delegate_to: localhost`.
- `results` task arg supplies the per-host inputs to aggregate mode. Accepts `{hostname: ai_agent_result}` dict or flat list of result dicts. Required when `aggregate=true`.
- New helper `prompts.build_aggregate_prompt(prompt, results)` renders the cluster-aggregation system prompt. New return-value fields in aggregate mode: `aggregate: true`, `host_count: int`.

## [0.1.4] - 2026-05-01

### Security

- AST validator now rejects `bash -c <payload>` (and other POSIX shells: `sh`, `zsh`, `dash`, `ksh`, `ash`, `fish`, `csh`, `tcsh`) even when the shell binary is on the `run_cmd` allow list. A shell with `-c` defeats the `run_cmd` allowlist by passing arbitrary commands as a single string argument; the new check rejects the call at static-analysis time. Also rejects shell calls whose argv tail is not statically resolvable (e.g. `subprocess.run(['bash', *args])`).

### Added

- `tests/integration/playbook_localhost_smoke.yml` — harmless localhost-only smoke playbook that runs the full `ai_agent` loop with `connection: local`, no SSH or inventory required. Useful for manual end-to-end validation without standing up the kafka lab.

## [0.1.3] - 2026-05-01

### Changed

- `galaxy.yml` `repository:` URL updated to `https://github.com/yalindogusahin/ansible-ai-agent` to match the renamed GitHub repo.

## [0.1.2] - 2026-04-30

### Added

- `print_result: true` task arg on `ai_agent` — prints the diagnosis directly to ansible output, no `register` + `debug` boilerplate needed.

## [0.1.1] - 2026-04-30

### Fixed

- Add required `authors` field to `galaxy.yml` (Galaxy publish was rejected on 0.1.0 with "'authors' is required").

## [0.1.0] - 2026-04-30

### Added

- Initial release.
- `ai_agent` action plugin: ReAct loop over LLM-generated Python on target hosts.
- `ai_exec` module: AST-validated, sandboxed Python execution with bwrap/firejail/nsjail/rlimit fallback.
- Layered allow/deny rule system (collection defaults < group_vars < host_vars < play < task), deny-wins.
- Provider-agnostic LLM client: Claude (Anthropic), OpenAI, Bedrock, Ollama.
- `endpoint` and `api_key` task args override env vars.
- Conservative read-only default rules in `roles/ai_agent/defaults/main.yml`.
- 54 unit tests covering rules merge, sandbox AST, prompt rendering, LLM client wiring.
- Integration smoke playbook for 2-node kafka lab.
