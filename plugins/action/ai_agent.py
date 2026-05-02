"""ansible_ai.ai_agent - controller-side ReAct orchestrator action plugin.

Per host:
  1. Merge rule layers (collection defaults < group_vars < host_vars < play < task).
  2. Build host context from facts + groups + filtered hostvars.
  3. Hand off to module_utils.orchestrator.run_agent.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from ansible.errors import AnsibleActionFail
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    llm_client as llm_mod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    orchestrator as orch_mod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    prompts as prompts_mod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    rules as rules_mod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    tools as tools_mod,
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
        # Shell-first: run_python is opt-in. Operator can populate this in
        # group_vars / host_vars / play / task to enable the run_python tool
        # for compute-heavy investigations (multi-step parse, correlation).
        "python": [],
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
    "budget": dict(rules_mod.DEFAULT_BUDGET),
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
            "stream",
            "save_transcript",
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

        def exec_callable(
            tool: str,
            tool_input: dict[str, Any],
            eff_rules: dict[str, Any],
            to: int,
        ) -> dict[str, Any]:
            return self._execute_module(
                module_name="yalindogusahin.ansible_ai.ai_exec",
                module_args={
                    "tool": tool,
                    "input": tool_input,
                    "rules": eff_rules,
                    "timeout": to,
                },
                task_vars=task_vars,
            )

        stream_enabled = bool(args.get("stream")) or os.environ.get("ANSIBLE_AI_STREAM") == "1"
        on_step = self._build_on_step(task_vars) if stream_enabled else None

        out = orch_mod.run_agent(
            prompt=prompt,
            rules=rules,
            host_ctx=host_ctx,
            llm_client=client,
            exec_callable=exec_callable,
            timeout=timeout,
            on_step=on_step,
        )

        result.update(
            {
                "changed": False,
                "transcript": out["transcript"],
                "diagnosis": out["diagnosis"],
                "iterations_used": out["iterations_used"],
                "tokens_used": out["tokens_used"],
                "rules_effective": rules,
            }
        )

        save_path = args.get("save_transcript")
        if save_path:
            self._write_transcript(
                path=str(save_path),
                task_vars=task_vars,
                prompt=prompt,
                host_ctx=host_ctx,
                rules=rules,
                run_out=out,
                provider_name=getattr(client, "name", None),
                model=getattr(client, "model", None),
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
        """Cluster-level summary mode. One LLM call, no targets touched."""
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

        try:
            agg = orch_mod.run_aggregate(prompt, results_arg, client, max_tokens=max_tokens)
        except llm_mod.LLMError as e:
            raise AnsibleActionFail(f"ai_agent aggregate: LLM error: {e}") from e
        except ValueError as e:
            raise AnsibleActionFail(f"ai_agent aggregate: {e}") from e

        result.update(
            {
                "changed": False,
                "diagnosis": agg["diagnosis"],
                "tokens_used": agg["tokens_used"],
                "aggregate": True,
                "host_count": agg["host_count"],
            }
        )
        if args.get("print_result"):
            host = task_vars.get("inventory_hostname", "?")
            display.display(f"[ai_agent:aggregate@{host}] {agg['diagnosis']}")
        return result

    def _build_on_step(self, task_vars: dict[str, Any]):
        """Return an on_step callback that prints one line per orchestrator step.

        Format intentionally compact: ansible-playbook output already has plenty
        of structure; we want a single grep-able line per step.
        """
        host = task_vars.get("inventory_hostname", "?")

        def on_step(entry: dict[str, Any]) -> None:
            step = entry.get("step", "?")
            if "error" in entry:
                display.display(f"[ai_agent:{host} step={step}] error: {entry['error']}")
                return
            action = entry.get("action", "?")
            if action == tools_mod.DONE:
                summary = entry.get("summary", "")
                display.display(f"[ai_agent:{host} step={step}] done: {summary[:200]}")
                return
            if action == "text_only":
                display.display(f"[ai_agent:{host} step={step}] text: {entry.get('text', '')[:200]}")
                return
            inp = entry.get("input", {}) or {}
            head = ""
            if action == tools_mod.RUN_CMD:
                argv = inp.get("argv", [])
                head = "argv=" + " ".join(argv[:6]) + (" ..." if len(argv) > 6 else "")
            elif action == tools_mod.READ_FILE:
                head = f"path={inp.get('path', '')}"
            elif action == tools_mod.WRITE_FILE:
                content = inp.get("content", "")
                head = f"path={inp.get('path', '')} bytes={len(content)}"
            elif action == tools_mod.RUN_PYTHON:
                head = f"code={len(inp.get('code', ''))}b"
            else:
                head = ""
            tail = f"exit={entry.get('exit', '?')}"
            blocked = entry.get("blocked_by_rule")
            if blocked:
                tail += f" blocked={blocked[:80]}"
            display.display(f"[ai_agent:{host} step={step}] {action} {head} -> {tail}")

        return on_step

    def _write_transcript(
        self,
        path: str,
        task_vars: dict[str, Any],
        prompt: str,
        host_ctx: dict[str, Any],
        rules: dict[str, Any],
        run_out: dict[str, Any],
        provider_name: str | None,
        model: str | None,
    ) -> None:
        """Write a JSON artifact of the run for offline replay/debug.

        `path` may contain `{host}` to disambiguate per-host files when running
        a fan-out play. Failures are non-fatal - we warn and continue, since
        losing the artifact must not fail an otherwise-successful run.
        """
        host = task_vars.get("inventory_hostname", "localhost")
        rendered = path.replace("{host}", str(host))
        artifact = {
            "prompt": prompt,
            "host": host,
            "host_ctx": host_ctx,
            "rules": rules,
            "transcript": run_out.get("transcript", []),
            "diagnosis": run_out.get("diagnosis", ""),
            "iterations_used": run_out.get("iterations_used", 0),
            "tokens_used": run_out.get("tokens_used", {}),
            "provider": provider_name,
            "model": model,
        }
        try:
            parent = os.path.dirname(os.path.abspath(rendered))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(rendered, "w", encoding="utf-8") as f:
                json.dump(artifact, f, indent=2, default=str)
        except OSError as e:
            display.warning(f"ai_agent: save_transcript to {rendered} failed: {e}")

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
