"""ansible_ai.ai_agent - controller-side ReAct orchestrator action plugin.

Per host:
  1. Merge rule layers (collection defaults < group_vars < host_vars < play < task).
  2. Build host context from facts + groups + filtered hostvars.
  3. Loop: LLM -> action JSON -> validate AST -> ai_exec on target -> observation.
  4. Stop on action=done or budget exhaustion.
"""

from __future__ import annotations

import copy
from typing import Any

from ansible.errors import AnsibleActionFail
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    llm_client as llm_mod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    prompts as prompts_mod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    rules as rules_mod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    sandbox as sandbox_mod,
)

display = Display()


COLLECTION_DEFAULT_RULES: dict[str, Any] = {
    "allow": {
        "run_cmd": [
            "ps",
            "cat",
            "ss",
            "journalctl",
            "df",
            "ls",
            "grep",
            "find",
            "awk",
            "sed",
            "ip",
            "free",
            "uptime",
            "dmesg",
            "netstat",
            "lsof",
            "uname",
            "id",
            "whoami",
            "hostname",
            "stat",
            "wc",
            "head",
            "tail",
            "openssl",
            "curl",
            "dig",
            "nslookup",
            "tracepath",
            "ping",
        ],
        "read_file": ["/var/log/**", "/proc/**", "/etc/**", "/sys/**", "/run/**"],
        "write_file": [],
        "python": [
            "os",
            "os.path",
            "json",
            "re",
            "datetime",
            "collections",
            "pathlib",
            "subprocess",
            "shutil",
            "shlex",
            "io",
            "sys",
            "itertools",
            "functools",
            "typing",
        ],
        "network": False,
    },
    "deny": {
        "run_cmd": [
            "rm",
            "dd",
            "mkfs",
            "mount",
            "umount",
            "systemctl",
            "kill",
            "iptables",
            "shutdown",
            "reboot",
            "chmod",
            "chown",
            "useradd",
            "userdel",
            "passwd",
            "su",
            "sudo",
            "service",
            "init",
        ],
        "write_file": ["**"],
        "python": ["socket", "ctypes", "multiprocessing"],
        "read_file": [],
    },
    "budget": {"max_iterations": 5, "max_tokens": 8000},
}


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset(
        (
            "prompt",
            "rules",
            "max_iterations",
            "max_tokens",
            "provider",
            "model",
            "timeout",
            "aggregate",
            "results",
            "endpoint",
            "api_key",
            "print_result",
        )
    )

    def run(self, tmp=None, task_vars=None):
        task_vars = task_vars or {}
        result = super().run(tmp, task_vars)

        args = self._task.args or {}
        prompt = args.get("prompt")
        if not prompt:
            raise AnsibleActionFail("ai_agent: 'prompt' is required")

        if args.get("aggregate"):
            return self._run_aggregate(args, task_vars, result, prompt)

        layers = self._collect_rule_layers(task_vars, args)
        try:
            rules = rules_mod.merge(layers)
        except rules_mod.RuleError as e:
            raise AnsibleActionFail(f"ai_agent: invalid rules: {e}") from e

        for k in ("max_iterations", "max_tokens"):
            if args.get(k) is not None:
                rules["budget"][k] = int(args[k])

        provider = args.get("provider")
        model = args.get("model")
        endpoint = args.get("endpoint")
        api_key = args.get("api_key")
        timeout = int(args.get("timeout", 30))

        try:
            client = llm_mod.get_client(
                provider=provider,
                model=model,
                endpoint=endpoint,
                api_key=api_key,
            )
        except llm_mod.LLMError as e:
            raise AnsibleActionFail(f"ai_agent: LLM client init failed: {e}") from e

        host_ctx = self._build_host_ctx(task_vars)
        system = prompts_mod.build_system_prompt(prompt, rules, host_ctx)

        transcript: list[dict[str, Any]] = []
        messages: list[dict[str, str]] = [
            {"role": "user", "content": "Begin investigation. Emit your first action."}
        ]
        diagnosis: str | None = None
        iterations = 0
        total_input = 0
        total_output = 0
        max_iter = rules["budget"]["max_iterations"]
        max_tokens = rules["budget"]["max_tokens"]

        for iterations in range(1, max_iter + 1):
            try:
                completion = client.complete(system, messages, max_tokens=1024)
            except llm_mod.LLMError as e:
                transcript.append({"step": iterations, "error": f"llm: {e}"})
                diagnosis = f"LLM error before convergence: {e}"
                break

            total_input += completion.input_tokens
            total_output += completion.output_tokens
            if total_input + total_output > max_tokens:
                transcript.append({"step": iterations, "error": "token budget exceeded"})
                diagnosis = "stopped: token budget exceeded"
                break

            try:
                action = prompts_mod.parse_action(completion.text)
            except ValueError as e:
                transcript.append({"step": iterations, "error": f"parse: {e}", "raw": completion.text[:500]})
                messages.append({"role": "assistant", "content": completion.text})
                messages.append(
                    {
                        "role": "user",
                        "content": f"Your previous output was not valid JSON: {e}. Emit a single JSON object only.",
                    }
                )
                continue

            messages.append({"role": "assistant", "content": completion.text})

            if action["action"] == "done":
                diagnosis = action.get("summary", "(no summary)")
                transcript.append(
                    {
                        "step": iterations,
                        "action": "done",
                        "summary": diagnosis,
                        "reason": action.get("reason", ""),
                    }
                )
                break

            code = action["code"]
            try:
                sandbox_mod.validate_ast(code, rules)
            except sandbox_mod.SandboxViolation as e:
                obs = prompts_mod.render_observation("", "", 126, blocked=e.reason)
                transcript.append(
                    {
                        "step": iterations,
                        "action": "run_python",
                        "code": code,
                        "blocked_by_rule": e.reason,
                    }
                )
                messages.append({"role": "user", "content": f"OBSERVATION:\n{obs}"})
                continue

            module_args = {"code": code, "rules": rules, "timeout": timeout}
            mr = self._execute_module(
                module_name="yalindogusahin.ansible_ai.ai_exec",
                module_args=module_args,
                task_vars=task_vars,
            )

            stdout = mr.get("stdout", "")
            stderr = mr.get("stderr", "")
            exit_code = mr.get("exit", -1)
            blocked = mr.get("blocked_by_rule")

            transcript.append(
                {
                    "step": iterations,
                    "action": "run_python",
                    "code": code,
                    "reason": action.get("reason", ""),
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit": exit_code,
                    "blocked_by_rule": blocked,
                }
            )

            obs = prompts_mod.render_observation(stdout, stderr, exit_code, blocked=blocked)
            messages.append({"role": "user", "content": f"OBSERVATION:\n{obs}"})
        else:
            diagnosis = "stopped: max_iterations reached without 'done'"

        result.update(
            {
                "changed": False,
                "transcript": transcript,
                "diagnosis": diagnosis or "(no diagnosis)",
                "iterations_used": iterations,
                "tokens_used": {"input": total_input, "output": total_output},
                "rules_effective": rules,
            }
        )

        if args.get("print_result"):
            host = task_vars.get("inventory_hostname", "?")
            display.display(f"[ai_agent:{host}] {result['diagnosis']}")

        return result

    def _collect_rule_layers(self, task_vars: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
        layers: list[dict[str, Any]] = [copy.deepcopy(COLLECTION_DEFAULT_RULES)]
        layered = task_vars.get("ansible_ai_rules")
        if isinstance(layered, dict):
            layers.append(layered)
        if isinstance(args.get("rules"), dict):
            layers.append(args["rules"])
        return layers

    def _run_aggregate(
        self,
        args: dict[str, Any],
        task_vars: dict[str, Any],
        result: dict[str, Any],
        prompt: str,
    ) -> dict[str, Any]:
        """Cluster-level summary mode.

        One LLM call. No rules merge, no AST, no sandbox — this path emits
        no code on any target host. Intended to be invoked once per play with
        run_once + delegate_to: localhost, after a per-host ai_agent task has
        registered its results in hostvars.
        """
        results_arg = args.get("results")
        if results_arg is None:
            raise AnsibleActionFail("ai_agent: 'results' is required when aggregate=true")
        if not isinstance(results_arg, dict | list):
            raise AnsibleActionFail("ai_agent: 'results' must be a dict or list")

        provider = args.get("provider")
        model = args.get("model")
        endpoint = args.get("endpoint")
        api_key = args.get("api_key")
        max_tokens = int(args.get("max_tokens", 4096))

        try:
            client = llm_mod.get_client(
                provider=provider,
                model=model,
                endpoint=endpoint,
                api_key=api_key,
            )
        except llm_mod.LLMError as e:
            raise AnsibleActionFail(f"ai_agent: LLM client init failed: {e}") from e

        system = prompts_mod.build_aggregate_prompt(prompt, results_arg)
        messages = [{"role": "user", "content": "Emit your cluster-level summary now."}]

        try:
            completion = client.complete(system, messages, max_tokens=max_tokens)
        except llm_mod.LLMError as e:
            raise AnsibleActionFail(f"ai_agent aggregate: LLM error: {e}") from e

        try:
            action = prompts_mod.parse_action(completion.text)
        except ValueError as e:
            raise AnsibleActionFail(
                f"ai_agent aggregate: malformed JSON from model: {e}; raw={completion.text[:500]!r}"
            ) from e

        if action.get("action") != "done":
            raise AnsibleActionFail(
                f"ai_agent aggregate: expected action='done', got {action.get('action')!r}"
            )

        summary = action.get("summary", "(no summary)")
        result.update(
            {
                "changed": False,
                "diagnosis": summary,
                "tokens_used": {"input": completion.input_tokens, "output": completion.output_tokens},
                "aggregate": True,
                "host_count": (len(results_arg) if isinstance(results_arg, dict | list) else 0),
            }
        )
        if args.get("print_result"):
            host = task_vars.get("inventory_hostname", "?")
            display.display(f"[ai_agent:aggregate@{host}] {summary}")
        return result

    def _build_host_ctx(self, task_vars: dict[str, Any]) -> dict[str, Any]:
        hostname = task_vars.get("inventory_hostname", "<unknown>")
        groups_map = task_vars.get("group_names") or []
        facts = task_vars.get("ansible_facts", {}) or {}
        hostvars = (task_vars.get("hostvars") or {}).get(hostname, {}) or {}
        role = hostvars.get("role") or (groups_map[0] if groups_map else "(none)")
        return {
            "hostname": hostname,
            "groups": list(groups_map),
            "role": role,
            "facts": prompts_mod.filter_facts(facts),
            "hostvars": prompts_mod.filter_hostvars(hostvars),
        }
